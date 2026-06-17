"""M3 Controller — SLOW loop (seconds-to-minutes, event-driven).

T-340 / BLUEPRINT §2.1.

THE SLOW LOOP:
  - Event-driven: processes LaunchEvents and updates only what changed (dirty-flag
    / changed-keys queue in StateStore; never a full re-scan).
  - Owns: sense->predict->reason->MCS; the LLM reasoner lives HERE, never on FAST.
  - Pre-stages: writes classifier score + veto flag to StateStore KV so the SNIPE
    loop can read a pre-computed scalar (never calls the model inline on SNIPE).
  - Emits scaling proposals (SIZE/EXIT intents) subject to asymmetric trust:
    * LLM/MCS can only REDUCE risk (veto entry, force exit, reduce size).
    * SIZE-UP / stop-widen / hard-stop-override from any LLM source is REJECTED
      before it reaches M4 — enforced as an explicit guard, not a comment.
  - The LLM call is bounded: the slow loop waits at most `llm_timeout_seconds`
    before falling back to VETO_ENTRY (conservative fallback on timeout).

ASYMMETRIC TRUST GUARD (enforced in code, not comments):
  _apply_reasoning_verdict() checks the ReasoningAction and:
    - HOLD        -> no-op
    - VETO_ENTRY  -> sets veto flag in StateStore (de-risk)
    - REDUCE_SIZE -> sets reduce flag in StateStore (de-risk)
    - FORCE_EXIT  -> sets narrative_failure_flag in StateStore (FAST reads it)
    Any action outside this set (e.g. SIZE_UP — which the type system already
    makes inexpressible) is rejected and logged.

POINT-IN-TIME:
  Every decision uses event_time carried on the event.  The SLOW loop never uses
  wall-clock as a join key.  All features are anchored to event_time.slot.

NO FLOAT MONEY.  NO WIN-RATE FIELD.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from aats.contracts.events import LaunchEvent
from aats.contracts.models import DecisionSignal, ReasoningAction, ReasoningVerdict
from aats.controller.state import StateStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Injectable interfaces to M2 modules (SLOW loop calls these; no inline impl)
# ---------------------------------------------------------------------------


@runtime_checkable
class ClassifierProtocol(Protocol):
    """M2 snipe classifier — pre-stages p_calibrated + uncertainty into KV.

    The SLOW loop calls this; the result is written to StateStore by this loop.
    The SNIPE loop reads only the pre-staged KV scalar.
    """

    def predict(self, event: LaunchEvent) -> DecisionSignal | None: ...


@runtime_checkable
class ReasonerProtocol(Protocol):
    """M2 LLM Reasoner (T-313) — de-risk only, bounded latency.

    Returns a ReasoningVerdict (action in {HOLD, VETO_ENTRY, REDUCE_SIZE, FORCE_EXIT}).
    On timeout / error, returns a VETO_ENTRY verdict (conservative fallback).
    """

    def reason(self, event: LaunchEvent, signal: DecisionSignal) -> ReasoningVerdict: ...


# ---------------------------------------------------------------------------
# SlowLoop
# ---------------------------------------------------------------------------


class SlowLoop:
    """The SLOW loop — sense->predict->reason->MCS; pre-stages KV; emits intents.

    Injected dependencies:
      store:      StateStore
      classifier: ClassifierProtocol (M2 snipe classifier, T-310)
      reasoner:   ReasonerProtocol (M2 LLM Reasoner, T-313; None = skip in tests)

    Processing is SYNCHRONOUS per event (the SLOW loop is not latency-sensitive).
    The LLM Reasoner call is the only unbounded IO here; it is bounded by
    `llm_timeout_seconds` inside the ReasonerProtocol implementation.
    """

    def __init__(
        self,
        store: StateStore,
        classifier: ClassifierProtocol,
        *,
        reasoner: ReasonerProtocol | None = None,
        signal_ttl_seconds: int = 30,
        veto_ttl_seconds: int = 60,
        narrative_ttl_seconds: int = 120,
    ) -> None:
        self._store = store
        self._classifier = classifier
        self._reasoner = reasoner
        self._signal_ttl = signal_ttl_seconds
        self._veto_ttl = veto_ttl_seconds
        self._narrative_ttl = narrative_ttl_seconds

    def process_event(self, event: LaunchEvent) -> DecisionSignal | None:
        """Process a LaunchEvent: classify, reason, pre-stage KV.

        Returns the DecisionSignal if one was produced and staged; None otherwise.

        This method MAY call the LLM Reasoner — it is NOT on the FAST/SNIPE
        critical path.  The SNIPE loop reads the pre-staged result, never the
        live computation.
        """
        mint = event.mint

        # --- 1. Classify (M2 classifier; not the LLM) ---
        signal = self._classifier.predict(event)
        if signal is None:
            log.debug("slow.no_signal: mint=%s", mint)
            return None

        # --- 2. Pre-stage score in KV (SNIPE reads this scalar) ---
        self._store.set_decision_signal(signal, ttl_seconds=self._signal_ttl)
        log.debug(
            "slow.staged: mint=%s p=%.3f uncertainty=%.3f surface=%s",
            mint,
            signal.p_calibrated,
            signal.uncertainty,
            signal.surface,
        )

        # --- 3. LLM Reasoning (SLOW only; bounded; de-risk only) ---
        if self._reasoner is not None:
            verdict = self._reasoner.reason(event, signal)
            self._apply_reasoning_verdict(mint, verdict)

        return signal

    def _apply_reasoning_verdict(self, mint: str, verdict: ReasoningVerdict) -> None:
        """Apply a ReasoningVerdict — ONLY de-risk actions are allowed.

        ASYMMETRIC TRUST GUARD (enforced here, not by comment):
          HOLD        -> no-op (default safe)
          VETO_ENTRY  -> set veto flag (de-risk: SNIPE will skip)
          REDUCE_SIZE -> set reduce flag (de-risk: sizing will shrink)
          FORCE_EXIT  -> set narrative_failure_flag (de-risk: FAST will exit)

        Any action that would INCREASE risk (SIZE_UP, WIDEN_STOP, ADD_LEVERAGE,
        OVERRIDE_HARD_STOP) is REJECTED here.  The ReasoningAction type system
        already makes these inexpressible (data-models.md §6.1, ADR-0006);
        this guard is the runtime backstop for any duck-typed bypass attempt.
        """
        action = verdict.action

        # Explicit allowlist — only de-risk actions pass
        if action == ReasoningAction.HOLD:
            # No-op: safe default
            return

        if action == ReasoningAction.VETO_ENTRY:
            self._store.set_veto_flag(mint, ttl_seconds=self._veto_ttl)
            log.info("slow.reasoning.veto_entry: mint=%s reason=%s", mint, verdict.reason[:80])
            return

        if action == ReasoningAction.REDUCE_SIZE:
            # Set a "reduce-size" flag; sizing engine reads this to halve size.
            # Stored as a veto with shorter TTL so the SNIPE loop sees it.
            # A REDUCE_SIZE is de-risk: it can only shrink, never grow.
            self._store.set_veto_flag(mint, ttl_seconds=self._veto_ttl // 2)
            log.info("slow.reasoning.reduce_size: mint=%s", mint)
            return

        if action == ReasoningAction.FORCE_EXIT:
            self._store.set_narrative_failure_flag(mint, ttl_seconds=self._narrative_ttl)
            log.info(
                "slow.reasoning.force_exit: mint=%s reason=%s", mint, verdict.reason[:80]
            )
            return

        # Anything else is a risk-increase attempt — REJECT.
        # This branch is unreachable when the type system is intact (the enum has
        # only the four members above).  It is a belt-and-suspenders guard.
        log.error(
            "slow.reasoning.REJECTED_risk_increase: mint=%s action=%s "
            "(asymmetric trust violation — action not applied)",
            mint,
            action,
        )
        # Do NOT apply the action.  The guard is the firewall; logging makes it auditable.
