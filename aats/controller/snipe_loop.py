"""M3 Controller — SNIPE loop (event-triggered, latency-budget-aware).

T-340 / BLUEPRINT §2.1.

THE SNIPE LOOP:
  - Event-triggered (not polling): processes one LaunchEvent at a time.
  - Reads ONLY pre-staged KV scalars (score, veto flag) — never calls the LLM,
    the slow model, or any unbounded network path.
  - Runs the pre-trade safety gate (M4), cost gate (via EntryIntent validation),
    and emits an EntryIntent to the venue.
  - Before building the tx, performs an ATOMIC FSM claim (SETNX on per-mint lock).
    A duplicate event for the same mint returns fsm_state_conflict and is dropped.
  - The client_intent_id is DETERMINISTIC (mint + slot + kind) so a crash-replay
    produces the same id and M4 deduplicates.

LATENCY BUDGET:
  If the pre-staged score is missing or stale (data_staleness_ms > budget), the
  SNIPE loop SKIPS — it never blocks waiting for the SLOW loop to produce one.
  Missing score = no trade, logged.  (BLUEPRINT §2.1 back-pressure rule.)

DRY-RUN FIRST:
  The venue is injected.  In tests/paper mode it is SimulationVenue.
  Real capital is only possible when DRY_RUN_ENABLED=false + CEO auth.

HARD RULES (non-waivable):
  - No await on LLM / slow model / unbounded RPC (BLUEPRINT §2.1).
  - Write-ahead: ENTERING claim persisted before tx submitted.
  - Atomic claim: exactly one ENTERING per mint (ADR-0007, AC-012).
  - No float money.  No win-rate field.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Protocol, runtime_checkable

from aats.contracts.events import LaunchEvent
from aats.contracts.intents import CostStack, EntryIntent
from aats.contracts.risk import RiskConfig
from aats.contracts.venue import ExecutionVenue, FillResult
from aats.controller.candidate_store import CandidateQueue
from aats.controller.fsm import PositionFSM, make_client_intent_id
from aats.controller.state import StateStore

log = logging.getLogger(__name__)

# Constants
LAMPORTS_PER_SOL = 1_000_000_000


# ---------------------------------------------------------------------------
# Interfaces to the M4 pre-trade gates (called by SNIPE; implemented in M4)
# ---------------------------------------------------------------------------


@runtime_checkable
class PreTradeGate(Protocol):
    """Sub-10ms safety gate (T-323).  Returns True if the entry is safe to proceed."""

    def evaluate(self, event: LaunchEvent) -> bool: ...


@runtime_checkable
class SizingEngine(Protocol):
    """Fractional-Kelly sizing (T-324).  Returns sol_in_lamports (int)."""

    def compute_size_lamports(
        self,
        p_calibrated: float,
        uncertainty: float,
        current_aggregate_lamports: int,
        config: RiskConfig,
    ) -> int: ...


@runtime_checkable
class CostModelProtocol(Protocol):
    """Cost gate (T-324).  Returns a CostStack or None if edge <= cost."""

    def compute(
        self,
        event: LaunchEvent,
        sol_in_lamports: int,
        tip_lamports: int,
    ) -> CostStack | None: ...


# ---------------------------------------------------------------------------
# SnipeDecision — the structured result of one snipe evaluation
# ---------------------------------------------------------------------------


class SnipeSkipReason:
    NO_SIGNAL = "no_signal"
    STALE_SIGNAL = "stale_signal"
    VETO_ACTIVE = "veto_active"
    IN_COOLDOWN = "in_cooldown"
    FSM_CONFLICT = "fsm_state_conflict"
    SAFETY_GATE_FAIL = "safety_gate_fail"
    BREAKER_TRIPPED = "breaker_tripped"
    COST_GATE_FAIL = "cost_gate_fail"
    BELOW_THRESHOLD = "below_snipe_threshold"
    DUPLICATE_INTENT = "duplicate_intent"
    BUDGET_BLOWN = "latency_budget_blown"
    FILL_FAILED = "fill_not_landed"


# ---------------------------------------------------------------------------
# SnipeLoop
# ---------------------------------------------------------------------------


class SnipeLoop:
    """The SNIPE loop — event-triggered entry decision with latency budget enforcement.

    Injected dependencies (all behind interfaces, all swappable for tests):
      store:      StateStore (InMemoryStateStore or RedisStateStore)
      venue:      ExecutionVenue (SimulationVenue in paper/test mode)
      gate:       PreTradeGate (M4 SafetyGate; None = skip gate in tests)
      sizing:     SizingEngine (M4 KellySizer; None = use min_sol_lamports)
      cost_model: CostModelProtocol (M4 CostModel; None = use a dummy cost stack)
      config:     RiskConfig

    The FAST loop takes ownership of the position after a successful fill by
    loading it from the StateStore and calling transition_to_open().
    """

    def __init__(
        self,
        store: StateStore,
        venue: ExecutionVenue,
        config: RiskConfig,
        *,
        gate: PreTradeGate | None = None,
        sizing: SizingEngine | None = None,
        cost_model: CostModelProtocol | None = None,
        # Tip lamports: in prod this comes from the live tip cache (T-330).
        # In tests/sim, use a fixed value.
        default_tip_lamports: int = 200_000,
        default_cu_price_microlamports: int = 10_000,
        # Latency budget: skip the entry if we detect we're over budget.
        # In tests, set to None (no enforcement).
        latency_budget_ms: int | None = 150,
        # Cooldown after exit (seconds)
        cooldown_seconds: int = 30,
        # Minimum SOL to enter (fallback when no sizing engine)
        min_sol_lamports: int = 10_000_000,  # 0.01 SOL
        wallet_id: str = "default",
        # E3: Optional candidate queue for GET /api/candidates (read-only operator view).
        # If None, candidate tracking is disabled (backward-compatible default).
        candidate_queue: CandidateQueue | None = None,
    ) -> None:
        self._store = store
        self._venue = venue
        self._config = config
        self._gate = gate
        self._sizing = sizing
        self._cost_model = cost_model
        self._default_tip = default_tip_lamports
        self._default_cu = default_cu_price_microlamports
        self._latency_budget_ms = latency_budget_ms
        self._cooldown_seconds = cooldown_seconds
        self._min_sol = min_sol_lamports
        self._wallet_id = wallet_id
        self._candidate_queue = candidate_queue

    def _record_candidate(
        self,
        event: LaunchEvent,
        *,
        wall_ms: int,
        status: str,
        model_p: float | None,
        reason: str,
        gate_passed: bool,
        gate_reasons: list[str] | None = None,
    ) -> None:
        """Record a candidate evaluation to the optional candidate queue (E3).

        This is a non-blocking write to an in-process deque — safe on the SNIPE
        path (no await, no Redis, no LLM call).  If the queue is not wired,
        this is a no-op.
        """
        if self._candidate_queue is None:
            return
        self._candidate_queue.record(
            mint=event.mint,
            evaluated_at_slot=event.event_time.slot,
            evaluated_at_wall_ms=wall_ms,
            status=status,
            model_p=model_p,
            reason=reason,
            gate_passed=gate_passed,
            gate_reasons=gate_reasons or [],
        )

    def process_event(self, event: LaunchEvent) -> FillResult | str:
        """Process a single LaunchEvent.

        Returns:
          FillResult if we attempted an entry (check .landed for fill status).
          str (SnipeSkipReason) if the entry was skipped before venue submission.

        This method is SYNCHRONOUS.  It may block on the venue (SimulationVenue
        returns immediately; real venue has a bounded RPC timeout).
        It NEVER blocks on the LLM, the slow model, or the SLOW loop.
        """
        start_wall_ms = time.monotonic_ns() // 1_000_000
        mint = event.mint

        # --- 1. Pre-checks: breaker, cooldown, per-mint concurrency ---
        breaker = self._store.load_breaker_state()
        if breaker is not None and breaker.state == "TRIPPED":
            log.info("snipe.skip: mint=%s reason=%s", mint, SnipeSkipReason.BREAKER_TRIPPED)
            self._record_candidate(
                event, wall_ms=start_wall_ms, status="skipped", model_p=None,
                reason=SnipeSkipReason.BREAKER_TRIPPED, gate_passed=False,
                gate_reasons=[SnipeSkipReason.BREAKER_TRIPPED],
            )
            return SnipeSkipReason.BREAKER_TRIPPED

        if self._store.in_cooldown(mint):
            log.info("snipe.skip: mint=%s reason=%s", mint, SnipeSkipReason.IN_COOLDOWN)
            self._record_candidate(
                event, wall_ms=start_wall_ms, status="skipped", model_p=None,
                reason=SnipeSkipReason.IN_COOLDOWN, gate_passed=False,
                gate_reasons=[SnipeSkipReason.IN_COOLDOWN],
            )
            return SnipeSkipReason.IN_COOLDOWN

        # --- 2. Read pre-staged score (the ONLY thing we read from SLOW) ---
        signal = self._store.get_decision_signal(mint)
        if signal is None:
            log.info("snipe.skip: mint=%s reason=%s", mint, SnipeSkipReason.NO_SIGNAL)
            self._record_candidate(
                event, wall_ms=start_wall_ms, status="monitoring", model_p=None,
                reason=SnipeSkipReason.NO_SIGNAL, gate_passed=False,
                gate_reasons=[SnipeSkipReason.NO_SIGNAL],
            )
            return SnipeSkipReason.NO_SIGNAL

        if signal.p_calibrated < self._config.snipe_threshold:
            log.info(
                "snipe.skip: mint=%s reason=%s p=%.3f threshold=%.3f",
                mint,
                SnipeSkipReason.BELOW_THRESHOLD,
                signal.p_calibrated,
                self._config.snipe_threshold,
            )
            self._record_candidate(
                event, wall_ms=start_wall_ms, status="skipped",
                model_p=signal.p_calibrated,
                reason=SnipeSkipReason.BELOW_THRESHOLD, gate_passed=False,
                gate_reasons=[SnipeSkipReason.BELOW_THRESHOLD],
            )
            return SnipeSkipReason.BELOW_THRESHOLD

        if event.data_staleness_ms > 5_000:  # 5s staleness budget
            log.warning(
                "snipe.skip: mint=%s reason=%s staleness_ms=%d",
                mint,
                SnipeSkipReason.STALE_SIGNAL,
                event.data_staleness_ms,
            )
            self._record_candidate(
                event, wall_ms=start_wall_ms, status="skipped",
                model_p=signal.p_calibrated,
                reason=SnipeSkipReason.STALE_SIGNAL, gate_passed=False,
                gate_reasons=[SnipeSkipReason.STALE_SIGNAL],
            )
            return SnipeSkipReason.STALE_SIGNAL

        # --- 3. Check LLM veto flag (written by SLOW, read here; no LLM call) ---
        if self._store.get_veto_flag(mint):
            log.info("snipe.skip: mint=%s reason=%s", mint, SnipeSkipReason.VETO_ACTIVE)
            self._record_candidate(
                event, wall_ms=start_wall_ms, status="skipped",
                model_p=signal.p_calibrated,
                reason=SnipeSkipReason.VETO_ACTIVE, gate_passed=False,
                gate_reasons=[SnipeSkipReason.VETO_ACTIVE],
            )
            return SnipeSkipReason.VETO_ACTIVE

        # --- 4. Pre-trade safety gate (M4 sub-10ms gate; 0-RPC) ---
        if self._gate is not None and not self._gate.evaluate(event):
            log.info("snipe.skip: mint=%s reason=%s", mint, SnipeSkipReason.SAFETY_GATE_FAIL)
            self._record_candidate(
                event, wall_ms=start_wall_ms, status="skipped",
                model_p=signal.p_calibrated,
                reason=SnipeSkipReason.SAFETY_GATE_FAIL, gate_passed=False,
                gate_reasons=[SnipeSkipReason.SAFETY_GATE_FAIL],
            )
            return SnipeSkipReason.SAFETY_GATE_FAIL

        # --- 5. Sizing ---
        current_agg = self._store.get_aggregate_open_lamports()
        if self._sizing is not None:
            sol_in = self._sizing.compute_size_lamports(
                signal.p_calibrated,
                signal.uncertainty,
                current_agg,
                self._config,
            )
        else:
            # Fallback: use minimum size, capped by config
            remaining_headroom = max(
                0, self._config.max_aggregate_lamports - current_agg
            )
            sol_in = min(
                self._min_sol,
                self._config.per_trade_cap_lamports,
                remaining_headroom,
            )
        if sol_in <= 0:
            log.info("snipe.skip: mint=%s reason=size_zero", mint)
            self._record_candidate(
                event, wall_ms=start_wall_ms, status="skipped",
                model_p=signal.p_calibrated,
                reason="size_zero", gate_passed=True,
                gate_reasons=["size_zero"],
            )
            return "size_zero"

        # --- 6. Atomic FSM claim (SETNX = single write, no double-entry) ---
        intent_id = make_client_intent_id(mint, event.event_time.slot, "ENTRY")

        # Intent dedup check (idempotent replay — same id = no-op)
        if not self._store.mark_intent_seen(intent_id):
            log.info(
                "snipe.skip: mint=%s reason=%s intent_id=%s",
                mint,
                SnipeSkipReason.DUPLICATE_INTENT,
                intent_id,
            )
            self._record_candidate(
                event, wall_ms=start_wall_ms, status="skipped",
                model_p=signal.p_calibrated,
                reason=SnipeSkipReason.DUPLICATE_INTENT, gate_passed=True,
                gate_reasons=[SnipeSkipReason.DUPLICATE_INTENT],
            )
            return SnipeSkipReason.DUPLICATE_INTENT

        claimed = self._store.claim_entering(mint, intent_id, ttl_seconds=30)
        if not claimed:
            log.info("snipe.skip: mint=%s reason=%s", mint, SnipeSkipReason.FSM_CONFLICT)
            self._record_candidate(
                event, wall_ms=start_wall_ms, status="skipped",
                model_p=signal.p_calibrated,
                reason=SnipeSkipReason.FSM_CONFLICT, gate_passed=True,
                gate_reasons=[SnipeSkipReason.FSM_CONFLICT],
            )
            return SnipeSkipReason.FSM_CONFLICT

        # --- 7. Build cost stack and validate edge > cost ---
        tip_lamports = self._default_tip

        if self._cost_model is not None:
            cost_stack = self._cost_model.compute(event, sol_in, tip_lamports)
            if cost_stack is None:
                self._store.release_entering(mint)
                log.info("snipe.skip: mint=%s reason=%s", mint, SnipeSkipReason.COST_GATE_FAIL)
                self._record_candidate(
                    event, wall_ms=start_wall_ms, status="skipped",
                    model_p=signal.p_calibrated,
                    reason=SnipeSkipReason.COST_GATE_FAIL, gate_passed=True,
                    gate_reasons=[SnipeSkipReason.COST_GATE_FAIL],
                )
                return SnipeSkipReason.COST_GATE_FAIL
        else:
            # Fallback cost stack for tests/sim: a simple stack that passes the gate
            cost_stack = self._make_default_cost_stack(sol_in, tip_lamports)

        # --- 8. Build EntryIntent (cost gate enforced by constructor: edge > cost) ---
        target_slot = event.event_time.slot + 6  # no block-0 (>= slot+5, AC-017)

        try:
            intent = EntryIntent(
                client_intent_id=intent_id,
                mint=mint,
                event_time=event.event_time,
                sol_in_lamports=sol_in,
                slippage_bps=self._config.max_slippage_bps,
                tip_lamports=tip_lamports,
                cu_price_microlamports=self._default_cu,
                target_slot=target_slot,
                cost_stack=cost_stack,
                venue=self._venue.name,
                wallet_id=self._wallet_id,
            )
        except ValueError as exc:
            self._store.release_entering(mint)
            log.info("snipe.skip: mint=%s reason=intent_validation_fail: %s", mint, exc)
            self._record_candidate(
                event, wall_ms=start_wall_ms, status="skipped",
                model_p=signal.p_calibrated,
                reason=f"intent_validation_fail:{exc}", gate_passed=True,
                gate_reasons=["intent_validation_fail"],
            )
            return f"intent_validation_fail:{exc}"

        # --- 9. Write-ahead: persist ENTERING record BEFORE submitting tx ---
        PositionFSM.create_entering(
            self._store,
            mint=mint,
            token=mint,  # in sim, token == mint; production gets ATA address
            surface=signal.surface,
            entry_slot=event.event_time.slot,
            sol_in_lamports=sol_in,
            tokens_out_base=0,  # updated on fill
            entry_slippage_bps=0,  # updated on fill
            exit_config_label="default",
            exit_mode="secure",
            tp_total=3,
            hard_stop_price_lamports=max(
                1,  # floor of 1 so StopState never receives 0
                int(
                    Decimal(str(event.sol_reserve_lamports))
                    / Decimal(str(max(1, event.token_reserve_base)))
                    # R-1 FIX: store 60% of entry price (hard_stop_r=0.60 = -40% stop).
                    # Original code used Decimal("6") which gave 6× entry price —
                    # i.e. a stop ABOVE entry, causing immediate halt on any position.
                    # Correct factor is Decimal("6") / Decimal("10") = 0.6.
                    # We intentionally keep the two-literal form to make it auditable:
                    # "6 tenths" is the -40% stop geometry (entry * 0.60).
                    * Decimal("6") / Decimal("10")
                ),
            ),
            cost_breakdown=cost_stack,
            client_intent_id=intent_id,
        )

        # --- 10. Latency budget check (abort if we're already over budget) ---
        if self._latency_budget_ms is not None:
            elapsed_ms = (time.monotonic_ns() // 1_000_000) - start_wall_ms
            if elapsed_ms > self._latency_budget_ms:
                # Budget blown — abort (de-risk: better to skip than enter late)
                self._store.release_entering(mint)
                self._store.delete_position(mint)
                log.warning(
                    "snipe.skip: mint=%s reason=%s elapsed_ms=%d budget_ms=%d",
                    mint,
                    SnipeSkipReason.BUDGET_BLOWN,
                    elapsed_ms,
                    self._latency_budget_ms,
                )
                self._record_candidate(
                    event, wall_ms=start_wall_ms, status="skipped",
                    model_p=signal.p_calibrated,
                    reason=SnipeSkipReason.BUDGET_BLOWN, gate_passed=True,
                    gate_reasons=[SnipeSkipReason.BUDGET_BLOWN],
                )
                return SnipeSkipReason.BUDGET_BLOWN

        # --- 11. Submit to venue ---
        log.info("snipe.execute: mint=%s intent_id=%s sol_in=%d", mint, intent_id, sol_in)
        fill = self._venue.execute(intent, event)

        if not fill.landed:
            # Fill failed — release the claim; the FAST loop may see a stale ENTERING
            # and transition to VETOED on the next tick (reconcile will handle it).
            # We do NOT transition here to keep the single-writer contract.
            log.info(
                "snipe.fill_failed: mint=%s reason=%s intent_id=%s",
                mint,
                fill.reason,
                intent_id,
            )
            self._record_candidate(
                event, wall_ms=start_wall_ms, status="skipped",
                model_p=signal.p_calibrated,
                reason=SnipeSkipReason.FILL_FAILED, gate_passed=True,
                gate_reasons=["fill_not_landed"],
            )
        else:
            # Successfully sniped — record as sniped status.
            self._record_candidate(
                event, wall_ms=start_wall_ms, status="sniped",
                model_p=signal.p_calibrated,
                reason="entered", gate_passed=True,
                gate_reasons=["passed"],
            )

        return fill

    def _make_default_cost_stack(self, sol_in: int, tip_lamports: int) -> CostStack:
        """Fallback cost stack for tests/sim: edge=300bps > cost=150bps."""
        adverse_sel = self._config.pre_calibration_haircut_bps  # >= 150
        jito_tip_bps = max(1, (tip_lamports * 10_000) // max(1, sol_in))
        # total: 50 (amm) + 50 (slip) + adverse_sel + jito + 10 (priority)
        total = 50 + 50 + adverse_sel + jito_tip_bps + 10
        edge = total + 100  # edge > total by construction
        return CostStack(
            expected_edge_bps=edge,
            jito_tip_bps=jito_tip_bps,
            priority_fee_bps=10,
            entry_slippage_bps=50,
            amm_fee_bps=50,
            exit_slippage_bps=50,
            adverse_selection_bps=adverse_sel,
            total_cost_bps=total,
        )
