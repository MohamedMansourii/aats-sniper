"""The de-risk-only Reasoner (T-313 + E7) — the M2 LLM juror with one vote.

WHAT THIS IS
============
The Reasoner judges AGREEMENT / CONFLICT between the calibrated quant probability
(DecisionSignal, from T-310) and the Market Conviction Score (MCSScore, from T-306) and
emits a STRUCTURED, SCHEMA-ENFORCED, DE-RISK-ONLY verdict over the bus:

    ReasoningVerdict(action ∈ {HOLD, VETO_ENTRY, REDUCE_SIZE, FORCE_EXIT}, ...)

THE PIPELINE (every stage can ONLY add de-risk)
===============================================
  1. router.route(...)          → a SCHEMA-VALIDATED raw LLM verdict (or conservative
                                   fallback on timeout/error/malformed output).
  2. clamp_to_derisk_action(...) → the asymmetric-trust clamp narrows the raw signal to a
                                   de-risk-only action; a risk-increase request is dropped.
  3. evaluate_matrix(...)        → the conflict/agreement matrix imposes a de-risk FLOOR.
  4. combine_clamp_and_matrix    → the STRONGER (more de-risk) of clamp & floor is applied.
  5. ReasoningVerdict(...)       → the bus-facing contract; its enum has NO risk-increase
                                   member, so the output is de-risk-only BY TYPE.

THE LLM IS NEVER ON THE FAST / SNIPE PATH
=========================================
This is SLOW-loop adjudication.  `adjudicate()` is the full path; `fast_veto()` is the
degraded sub-200ms local-only de-risk path that meets the slow-loop budget.  Neither is
called by the SNIPE loop — SNIPE reads a pre-staged score from KV.

NARRATIVE_FAILURE → M4 CATASTROPHIC EXIT
========================================
When the verdict's narrative_failure fires on an OPEN position, the Reasoner constructs a
FORCE_EXIT via the DeRiskIntentFactory — an `ExitIntent` (full exit, secure mode).  That
ExitIntent is the ONLY thing that reaches execution, and the factory has NO entry method,
so the catastrophic-exit path cannot size up or widen a stop.

E7 — NEWS NARRATIVE FAILURE HOOK
=================================
`adjudicate_with_news(news_signal=...)` is the E7 extension that accepts a `NewsSignal`
from the MCS pipeline.  When `news_signal.credible_negative_event=True` AND a position is
open, the narrative_failure flag is forced to True BEFORE the LLM verdict is applied.
This means the FORCE_EXIT path fires independently of what the LLM said — a credible
breaking-news collapse overrides an optimistic LLM verdict.

HARD RULES for the news hook (E7):
  - news_signal.credible_negative_event=True → narrative_failure=True (FORCE_EXIT),
    but ONLY when position_is_open=True (no open position = VETO_ENTRY at most).
  - The news signal can NEVER raise risk (no size-up, no stop-widen).
  - When news_signal is None or credible_negative_event=False, behaviour is
    IDENTICAL to adjudicate() — the news hook is additive, not a replacement.
  - The news_signal.headline_digest is treated as QUOTED UNTRUSTED DATA in the
    audit reason string; it is NEVER executed as instructions.

DE-RISK-ONLY BY TYPE
====================
The Reasoner holds a `DeRiskIntentFactory` (aats.contracts.intents).  It has no reference
to EntryIntent's constructor.  A prompt-injection that makes the LLM say "Strong Buy" or
"SIZE_UP" is structurally incapable of producing an EntryIntent — the factory cannot make
one, and the ReasoningAction enum cannot express one.  This is the enforcement boundary.

DETERMINISM FOR CONTROL FLOW
============================
temperature=0 on the decision call (router default).  Every verdict logs the backend id,
a prompt hash, and the routing tier for reproducibility / auditability.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from aats.contracts.events import EventTime
from aats.contracts.intents import (
    DeRiskIntentFactory,
    ExitIntent,
    Intent,
    ReduceIntent,
    VetoIntent,
)
from aats.contracts.models import (
    DecisionSignal,
    MCSScore,
    ReasoningAction,
    ReasoningVerdict,
)
from aats.sentiment.models import NewsSignal  # E7 — news narrative failure hook
from aats.reasoning.clamp import clamp_to_derisk_action
from aats.reasoning.matrix import combine_clamp_and_matrix, evaluate_matrix
from aats.reasoning.prompt import (
    SYSTEM_PROMPT,
    PromptInputs,
    assert_point_in_time,
    build_reasoner_prompt,
)
from aats.reasoning.router import (
    LLMRouter,
    RouteTier,
    SignalBucketKey,
)
from aats.reasoning.schema import (
    bucket_quant,
    bucket_sentiment,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReasonerOutput:
    """The Reasoner's full output: the bus verdict + optional de-risk Intent + audit.

    `verdict` is the bus-facing ReasoningVerdict (schema-enforced, de-risk-only).
    `intent` is the de-risk Intent to dispatch to execution when the action requires one
    (VETO_ENTRY → VetoIntent, REDUCE_SIZE → ReduceIntent, FORCE_EXIT → ExitIntent); it is
    None for HOLD (no-op).  The Intent is ALWAYS a de-risk variant — there is no code path
    here that can construct an EntryIntent.
    `route_tier` / `backend_id` / `prompt_hash` are the audit trail (reproducibility).
    """

    verdict: ReasoningVerdict
    intent: Intent | None
    route_tier: RouteTier
    backend_id: str
    prompt_hash: str
    elapsed_ms: float
    cache_hit: bool


class DeRiskReasoner:
    """The de-risk-only Reasoner.  Holds ONLY a de-risk Intent factory (ADR-0006).

    It cannot construct an EntryIntent — there is no method on DeRiskIntentFactory that
    returns one, and ReasoningAction has no risk-increase member.  The asymmetric trust is
    enforced by TYPE; the clamp and matrix are the behavioural backstop.
    """

    def __init__(
        self,
        router: LLMRouter | None = None,
        *,
        intent_factory: DeRiskIntentFactory | None = None,
    ) -> None:
        # Default router uses the offline mock backend — zero network.
        self._router = router or LLMRouter()
        # The ONLY factory the reasoning path holds — de-risk variants only.
        self._factory = intent_factory or DeRiskIntentFactory()

    # --- public API --------------------------------------------------------

    def adjudicate(
        self,
        *,
        decision_signal: DecisionSignal,
        mcs: MCSScore,
        total_cost_bps: int,
        expected_edge_bps: int,
        position_is_open: bool,
    ) -> ReasonerOutput:
        """Full SLOW-loop adjudication → a schema-enforced de-risk-only verdict.

        The pipeline (router → clamp → matrix → combine) can only ever add de-risk.  The
        output ReasoningVerdict has a de-risk-only action by type; the optional Intent is a
        de-risk variant by construction.
        """
        inputs = PromptInputs(
            decision_signal=decision_signal,
            mcs=mcs,
            total_cost_bps=total_cost_bps,
            expected_edge_bps=expected_edge_bps,
            position_is_open=position_is_open,
        )
        # Point-in-time guard: refuse mixed event-times (a leak).
        assert_point_in_time(inputs)

        key = self._bucket_key(inputs)
        user_prompt = build_reasoner_prompt(inputs)
        prompt_hash = _prompt_hash(SYSTEM_PROMPT, user_prompt)

        routed = self._router.route(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            key=key,
        )
        return self._finalize(
            inputs=inputs,
            raw=routed.raw,
            route_tier=routed.tier,
            backend_id=routed.backend_id,
            prompt_hash=prompt_hash,
            elapsed_ms=routed.elapsed_ms,
            cache_hit=routed.cache_hit,
        )

    def adjudicate_with_news(
        self,
        *,
        decision_signal: DecisionSignal,
        mcs: MCSScore,
        news_signal: NewsSignal,
        total_cost_bps: int,
        expected_edge_bps: int,
        position_is_open: bool,
    ) -> ReasonerOutput:
        """E7 — Full SLOW-loop adjudication with news narrative failure hook.

        Identical to `adjudicate()` PLUS: when the NewsSignal reports a credible
        negative event (news_signal.credible_negative_event=True) and a position is
        open, the narrative_failure flag is forced to True before the LLM verdict is
        applied.  The result is FORCE_EXIT via the de-risk-only factory — the same
        M4 catastrophic exit as a standard narrative_failure.

        HARD RULES (E7, non-waivable):
          - This method can ONLY force FORCE_EXIT when position_is_open=True.
          - When position_is_open=False, a credible_negative_event becomes VETO_ENTRY
            (block the entry) — NEVER raises risk.
          - When news_signal.credible_negative_event=False, the method is IDENTICAL
            to adjudicate() — the news path is additive, not a replacement.
          - The news headline_digest is treated as QUOTED UNTRUSTED DATA in the
            audit reason string — it is NEVER executed as instructions.
          - This method CANNOT size up, widen a stop, add leverage, or override a
            hard stop.  It may ONLY force-exit or veto-entry.

        Args:
            decision_signal:    The calibrated quant probability.
            mcs:                The Market Conviction Score (post-news adjustment).
            news_signal:        The NewsSignal from the MCS pipeline (E7).
            total_cost_bps:     Total round-trip cost in basis points.
            expected_edge_bps:  Expected edge before costs in basis points.
            position_is_open:   True iff a position is currently open for this mint.

        Returns:
            ReasonerOutput — schema-valid, de-risk-only verdict + optional Intent.
        """
        inputs = PromptInputs(
            decision_signal=decision_signal,
            mcs=mcs,
            total_cost_bps=total_cost_bps,
            expected_edge_bps=expected_edge_bps,
            position_is_open=position_is_open,
        )
        assert_point_in_time(inputs)

        key = self._bucket_key(inputs)
        user_prompt = build_reasoner_prompt(inputs)
        prompt_hash = _prompt_hash(SYSTEM_PROMPT, user_prompt)

        routed = self._router.route(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            key=key,
        )

        # E7 news narrative failure hook — FORCE_EXIT gate.
        # credible_negative_event=True AND position_is_open → force narrative_failure.
        # credible_negative_event=True AND !position_is_open → force veto=True.
        # This override is applied BEFORE the clamp/matrix pipeline.
        # The raw LLM verdict is still routed (for the audit trail) but the
        # news-forced flag takes precedence via _finalize_with_news_override().
        return self._finalize_with_news_override(
            inputs=inputs,
            raw=routed.raw,
            route_tier=routed.tier,
            backend_id=routed.backend_id,
            prompt_hash=prompt_hash,
            elapsed_ms=routed.elapsed_ms,
            cache_hit=routed.cache_hit,
            news_signal=news_signal,
        )

    def fast_veto(
        self,
        *,
        decision_signal: DecisionSignal,
        mcs: MCSScore,
        total_cost_bps: int,
        expected_edge_bps: int,
        position_is_open: bool,
    ) -> ReasonerOutput:
        """The sub-200ms LOCAL-ONLY de-risk fast path.

        Uses the router's fast-path (cache → local with a hard budget → conservative
        fallback).  NEVER escalates, NEVER blocks, NEVER silently passes the trade.  On
        timeout/error it returns the conservative de-risk verdict.
        """
        inputs = PromptInputs(
            decision_signal=decision_signal,
            mcs=mcs,
            total_cost_bps=total_cost_bps,
            expected_edge_bps=expected_edge_bps,
            position_is_open=position_is_open,
        )
        assert_point_in_time(inputs)

        key = self._bucket_key(inputs)
        user_prompt = build_reasoner_prompt(inputs)
        prompt_hash = _prompt_hash(SYSTEM_PROMPT, user_prompt)

        routed = self._router.fast_path_veto(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            key=key,
        )
        return self._finalize(
            inputs=inputs,
            raw=routed.raw,
            route_tier=routed.tier,
            backend_id=routed.backend_id,
            prompt_hash=prompt_hash,
            elapsed_ms=routed.elapsed_ms,
            cache_hit=routed.cache_hit,
        )

    # --- internals ---------------------------------------------------------

    def _finalize_with_news_override(
        self,
        *,
        inputs: PromptInputs,
        raw,
        route_tier: RouteTier,
        backend_id: str,
        prompt_hash: str,
        elapsed_ms: float,
        cache_hit: bool,
        news_signal: NewsSignal,
    ) -> ReasonerOutput:
        """E7 — Apply the news narrative-failure override THEN run the standard pipeline.

        When news_signal.credible_negative_event=True:
          - position_is_open=True  → force raw.narrative_failure=True → FORCE_EXIT
          - position_is_open=False → force raw.veto=True → VETO_ENTRY at minimum

        The news override is applied by constructing a replacement LLMRawVerdict
        with the overridden flags.  The LLM's raw verdict is still captured in the
        audit trail under action_received_raw.

        HARD RULE: the override can ONLY set more-conservative flags (narrative_failure
        or veto from False to True).  It NEVER sets them from True to False.
        The news signal is DE-RISK ONLY — it can ONLY force more de-risk, never less.
        """
        from aats.reasoning.schema import LLMRawVerdict  # avoid circular at module level

        if news_signal.credible_negative_event:
            # Build a more-conservative version of the raw verdict.
            # HARD RULE: override only increases de-risk flags (True direction only).
            override_narrative_failure = raw.narrative_failure or inputs.position_is_open
            override_veto = raw.veto or True  # credible negative → always veto if pre-entry

            # Sanitise the headline_digest for the audit rationale.
            # It is QUOTED UNTRUSTED DATA — we cap it and mark it clearly.
            _MAX_NEWS_RATIONALE = 300
            sanitised_digest = news_signal.headline_digest[:_MAX_NEWS_RATIONALE]
            news_audit = (
                f"[E7-NEWS-OVERRIDE] credible_negative_event=True "
                f"mcs_delta={news_signal.mcs_delta:.3f} "
                f"red_flags={news_signal.red_flags} "
                f"[QUOTED-UNTRUSTED-HEADLINE-DIGEST] {sanitised_digest}"
            )

            override_rationale = (
                f"[news_override] {news_audit} "
                f"| [llm_original_rationale-QUOTED-UNTRUSTED] {raw.rationale}"
            )
            # Cap to the same bound as the normal rationale validator
            if len(override_rationale) > 2000:
                override_rationale = override_rationale[:2000]

            raw = LLMRawVerdict(
                decision_signal=raw.decision_signal,
                confidence=raw.confidence,
                veto=override_veto,
                narrative_failure=override_narrative_failure,
                rationale=override_rationale,
            )

        return self._finalize(
            inputs=inputs,
            raw=raw,
            route_tier=route_tier,
            backend_id=backend_id,
            prompt_hash=prompt_hash,
            elapsed_ms=elapsed_ms,
            cache_hit=cache_hit,
        )

    def _bucket_key(self, inputs: PromptInputs) -> SignalBucketKey:
        sig = inputs.decision_signal
        mcs = inputs.mcs
        quant = bucket_quant(sig.p_calibrated, sig.uncertainty)
        sentiment = bucket_sentiment(
            conviction=mcs.conviction,
            synchronicity=mcs.synchronicity,
            account_age_median_days=mcs.account_age_median_days,
            coordinated_shill_flag=mcs.coordinated_shill_flag,
        )
        net_edge = inputs.expected_edge_bps - inputs.total_cost_bps
        net_sign = (net_edge > 0) - (net_edge < 0)
        return SignalBucketKey(
            quant=quant,
            sentiment=sentiment,
            position_is_open=inputs.position_is_open,
            net_edge_sign=net_sign,
        )

    def _finalize(
        self,
        *,
        inputs: PromptInputs,
        raw,
        route_tier: RouteTier,
        backend_id: str,
        prompt_hash: str,
        elapsed_ms: float,
        cache_hit: bool,
    ) -> ReasonerOutput:
        """Clamp → matrix → combine → build the bus verdict + de-risk Intent.

        `raw` is a schema-validated LLMRawVerdict (the router guarantees this).
        """
        sig = inputs.decision_signal
        mcs = inputs.mcs

        quant = bucket_quant(sig.p_calibrated, sig.uncertainty)
        sentiment = bucket_sentiment(
            conviction=mcs.conviction,
            synchronicity=mcs.synchronicity,
            account_age_median_days=mcs.account_age_median_days,
            coordinated_shill_flag=mcs.coordinated_shill_flag,
        )

        # 1) The asymmetric-trust clamp — narrows the raw signal to a de-risk action.
        clamp = clamp_to_derisk_action(
            quant=quant,
            signal=raw.decision_signal,
            veto=raw.veto,
            narrative_failure=raw.narrative_failure,
            # The audit "raw action" string is the human-vocabulary signal the LLM emitted.
            action_received_raw=str(raw.decision_signal),
            position_is_open=inputs.position_is_open,
        )

        # 2) The conflict/agreement matrix — imposes a de-risk floor.
        matrix = evaluate_matrix(
            quant=quant,
            signal=raw.decision_signal,
            veto=raw.veto,
            narrative_failure=raw.narrative_failure,
            sentiment=sentiment,
            position_is_open=inputs.position_is_open,
        )

        # 3) Combine: the STRONGER (more de-risk) of clamp and matrix floor.
        applied = combine_clamp_and_matrix(clamp.action, matrix.floor_action)

        # 4) The bus-facing verdict — de-risk-only by TYPE.
        # narrative_failure is surfaced on the verdict iff the applied action is the
        # catastrophic FORCE_EXIT we wired it to (M4).  An LLM-asserted narrative_failure
        # that the matrix/clamp resolved to a milder action is recorded in the rationale
        # but does not over-claim a catastrophic exit.
        applied_narrative_failure = applied is ReasoningAction.FORCE_EXIT and (
            raw.narrative_failure or matrix.floor_action is ReasoningAction.FORCE_EXIT
        )

        verdict = ReasoningVerdict(
            mint=sig.mint,
            event_time=sig.event_time,
            action=applied,
            reason=self._audit_reason(
                raw=raw,
                quant=quant,
                sentiment=sentiment,
                matrix_cell=matrix.cell,
                clamped=clamp.risk_increase_clamped,
                narrative_failure=applied_narrative_failure,
                route_tier=route_tier,
                backend_id=backend_id,
                prompt_hash=prompt_hash,
            ),
            confidence=raw.confidence,
            action_received_raw=clamp.action_received_raw,
            risk_increase_clamped=clamp.risk_increase_clamped,
        )

        # 5) The de-risk Intent (None for HOLD) — built via the de-risk-only factory.
        intent = self._build_intent(
            action=applied,
            mint=sig.mint,
            event_time=sig.event_time,
            reason=verdict.reason,
        )

        return ReasonerOutput(
            verdict=verdict,
            intent=intent,
            route_tier=route_tier,
            backend_id=backend_id,
            prompt_hash=prompt_hash,
            elapsed_ms=elapsed_ms,
            cache_hit=cache_hit,
        )

    def _build_intent(
        self,
        *,
        action: ReasoningAction,
        mint: str,
        event_time: EventTime,
        reason: str,
    ) -> Intent | None:
        """Map a de-risk action to a de-risk Intent via the factory (no EntryIntent path).

        HOLD       → None (no-op; the reasoning path adds no risk and removes none).
        VETO_ENTRY → VetoIntent (cancel a pending entry).
        REDUCE_SIZE→ ReduceIntent (partial exit).
        FORCE_EXIT → ExitIntent (full exit, secure mode) — the M4 catastrophic exit.
        """
        client_intent_id = _deterministic_intent_id(mint, event_time, action)

        if action is ReasoningAction.HOLD:
            return None
        if action is ReasoningAction.VETO_ENTRY:
            veto: VetoIntent = self._factory.veto(
                client_intent_id=client_intent_id, mint=mint, reason=reason
            )
            return veto
        if action is ReasoningAction.REDUCE_SIZE:
            # A conservative partial reduction (50%).  De-risk only; never increases size.
            reduce: ReduceIntent = self._factory.reduce(
                client_intent_id=client_intent_id,
                mint=mint,
                fraction_bps=5_000,
                reason=reason,
            )
            return reduce
        # FORCE_EXIT — the catastrophic exit wired to narrative_failure (M4).
        exit_intent: ExitIntent = self._factory.exit(
            client_intent_id=client_intent_id,
            mint=mint,
            fraction_bps=10_000,  # full exit
            exit_mode="secure",
            reason=reason,
        )
        return exit_intent

    @staticmethod
    def _audit_reason(
        *,
        raw,
        quant,
        sentiment,
        matrix_cell: str,
        clamped: bool,
        narrative_failure: bool,
        route_tier: RouteTier,
        backend_id: str,
        prompt_hash: str,
    ) -> str:
        """Build the audit-log reason string (NEVER used for control flow).

        The raw LLM rationale is included clearly marked as QUOTED UNTRUSTED TEXT.  It is
        data, not a command.  The control-relevant facts (buckets, matrix cell, clamp,
        routing) are recorded for reproducibility.
        """
        return (
            f"quant={quant} sentiment={sentiment} cell={matrix_cell} "
            f"raw_signal={raw.decision_signal} raw_veto={raw.veto} "
            f"raw_narrative_failure={raw.narrative_failure} "
            f"applied_narrative_failure={narrative_failure} "
            f"risk_increase_clamped={clamped} "
            f"route={route_tier} backend={backend_id} prompt_hash={prompt_hash[:12]} "
            f"| [QUOTED-UNTRUSTED-RATIONALE] {raw.rationale}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prompt_hash(system_prompt: str, user_prompt: str) -> str:
    """Deterministic prompt hash for the audit trail (reproducibility)."""
    h = hashlib.sha256()
    h.update(system_prompt.encode())
    h.update(b"\x00")
    h.update(user_prompt.encode())
    return h.hexdigest()


def _deterministic_intent_id(mint: str, event_time: EventTime, action: ReasoningAction) -> str:
    """Deterministic client_intent_id (mint + slot + action) for idempotent replay.

    Same decision instant + same action → same id, so a replayed verdict does not double
    the de-risk Intent (FR-024 idempotency).  event_time.slot is the join anchor;
    wall_clock_ms is NEVER used here (it is monitoring-only).
    """
    raw = f"reason:{mint}:{event_time.slot}:{event_time.block_time_ms}:{action}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
