"""M3 Controller — FAST loop (deterministic <100ms tick).

T-340 / BLUEPRINT §2.1.

THE FAST LOOP IS SACRED (BLUEPRINT §2.1):
  - Tick budget: <= 100ms.
  - Hard-stop trigger -> exit(): <= 50ms p99.
  - NEVER awaits an LLM, the slow model, or any unbounded RPC.
  - Everything it reads is LOCAL (StateStore) or PRE-COMPUTED (KV pre-staged flags).
  - If a value is stale past its TTL, it treats absence as risk-off (never "assume fine").

OWNERSHIP:
  - Single-writer for FSM: only the FAST loop writes ENTERING->OPEN,
    ENTERING->VETOED, OPEN->CLOSING, CLOSING->CLOSED.
  - Owns stop-loss / take-profit triggering via ExitEngine (M4, T-325).
  - Owns fill reconciliation: on every tick, reconciles bot-state vs venue-truth
    so a partial fill, missed signature, or manual intervention surfaces within
    one tick.
  - Owns the dead-man's-switch heartbeat: emits event-time heartbeat each tick.
  - Owns survivable-stop Layer 2 enforcer: calls venue.exit() within <=50ms of
    a breach (FR-033, AC-026).

FAST LOOP CRITICAL PATH — NO LLM PROOF:
  The tick() method contains zero calls to any LLM client, zero imports of the
  reasoning module, and zero awaits on any coroutine that could block on a model.
  The narrative_failure_flag is a pre-computed boolean READ from the StateStore
  (written by the SLOW loop); the FAST loop never calls the LLM to compute it.
  The ExitEngine.on_tick() call is a PURE function (no IO, no network, no LLM).

MONEY: integer lamports or Decimal.  Never float.
TIME:  event_time.block_time_ms (on-chain).  Never wall-clock for decisions.
"""

from __future__ import annotations

import contextlib
import logging
import time
from decimal import Decimal
from typing import Protocol, runtime_checkable

from aats.contracts.intents import ExitIntent, ReduceIntent
from aats.contracts.positions import FSMState, Position
from aats.contracts.venue import ExecutionVenue, FillResult
from aats.controller.enforcer_wiring import FastLoopEnforcerWiring
from aats.controller.fsm import PositionFSM, make_client_intent_id
from aats.controller.state import StateStore
from aats.risk.circuit_breaker import (
    CircuitBreaker,
    PnLEvent,
)
from aats.risk.exit_engine import ExitConfig, ExitEngine, ExitState

log = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000


# ---------------------------------------------------------------------------
# MarkPriceProvider — injected price feed (no unbounded RPC on the hot path)
# ---------------------------------------------------------------------------


@runtime_checkable
class MarkPriceProvider(Protocol):
    """Returns the current mark price for a mint as a Decimal lamports/token.

    In production: reads from a pre-subscribed price stream (not a live RPC call).
    In tests/sim: returns a deterministic price.
    """

    def get_mark_price(self, mint: str, event_slot: int) -> Decimal | None: ...


# ---------------------------------------------------------------------------
# FastLoopFlattenHandler — bridges the CircuitBreaker's FlattenHandler to
# the venue.  All exits are de-risk only (no EntryIntent constructed here).
# ---------------------------------------------------------------------------


class FastLoopFlattenHandler:
    """Bridge the CircuitBreaker's flatten call to venue exits.

    The CircuitBreaker calls emergency_flatten_all() on a trip.  We iterate
    all open positions and emit ExitIntent -> venue.exit() for each.
    The FAST loop owns this bridge; M4 venue owns the build/sign/land.
    """

    def __init__(
        self,
        store: StateStore,
        venue: ExecutionVenue,
        fsm_registry: dict[str, PositionFSM],
    ) -> None:
        self._store = store
        self._venue = venue
        self._registry = fsm_registry

    def _flatten_position(self, pos: Position, reason: str) -> None:
        """Flatten a single position record.

        Handles OPEN, CLOSING, and ENTERING states.
        ENTERING positions are transitioned to VETOED (the entry is abandoned).
        OPEN/CLOSING positions receive a full ExitIntent -> venue.exit().
        """
        mint = pos.mint

        if pos.fsm_state == FSMState.ENTERING:
            # An in-flight entry must be abandoned to prevent a live position
            # appearing after the kill.  Transition to VETOED.  If the venue
            # later lands the entry anyway (race), reconcile_fill will surface it.
            log.warning(
                "fast.flatten_entering: mint=%s reason=%s "
                "(entry abandoned; vetoed to prevent post-kill position)",
                mint,
                reason,
            )
            fsm = self._registry.get(mint)
            if fsm is None:
                # Create a transient FSM to run the transition through.
                fsm = PositionFSM(self._store, pos)
            with contextlib.suppress(ValueError):
                fsm.transition_to_vetoed(
                    reason=f"abandoned_on_{reason}", block_time_ms=1
                )
            # Remove from registry so the FAST loop no longer tracks it.
            self._registry.pop(mint, None)
            return

        if pos.fsm_state not in (FSMState.OPEN, FSMState.CLOSING):
            return  # already closed / vetoed

        intent_id = make_client_intent_id(
            pos.mint, pos.entry_slot, "EMERGENCY_EXIT"
        )
        exit_intent = ExitIntent(
            client_intent_id=intent_id,
            mint=pos.mint,
            fraction_bps=10_000,  # full exit
            exit_mode="secure",
            reason=f"emergency_flatten:{reason}",
        )
        fill = self._venue.exit(exit_intent, pos)
        log.warning(
            "fast.emergency_flatten: mint=%s landed=%s reason=%s",
            pos.mint,
            fill.landed,
            reason,
        )
        # Transition via FSM if registered
        fsm = self._registry.get(pos.mint)
        if fsm is not None:
            if pos.fsm_state == FSMState.OPEN:
                with contextlib.suppress(ValueError):
                    fsm.transition_to_closing(block_time_ms=1)
            if fill.landed:
                with contextlib.suppress(ValueError):
                    fsm.transition_to_closed(
                        realized_pnl_net_lamports=0,
                        block_time_ms=1,
                    )

    def emergency_flatten_all(self, reason: str) -> None:
        """Idempotent: flatten every open/entering position.  Called on breaker trip or kill."""
        positions = self._store.load_all_positions()
        for pos in positions:
            self._flatten_position(pos, reason)

    def flatten_one(self, mint: str, reason: str) -> bool:
        """Flatten a single position by mint; leave all others unchanged.

        api-contracts.md §5 POST /api/flatten/{mint} (AC-044):
          Only the named position is exited; all other open positions remain.

        Returns True if the position was found and acted on, False if not found.
        """
        pos = self._store.load_position(mint)
        if pos is None:
            return False
        self._flatten_position(pos, reason)
        return True


# ---------------------------------------------------------------------------
# FastLoop
# ---------------------------------------------------------------------------


class FastLoop:
    """The FAST loop — deterministic <100ms tick.

    Injected dependencies:
      store:    StateStore (InMemoryStateStore or RedisStateStore)
      venue:    ExecutionVenue (SimulationVenue in paper/test)
      breaker:  CircuitBreaker (M4 T-320)
      price_feed: MarkPriceProvider (pre-subscribed; NOT a live RPC on critical path)
      exit_config: ExitConfig (the FROZEN-at-entry TP/trailing/stop config from T-325)

    The FAST loop maintains a registry of PositionFSM objects (one per open mint)
    and a parallel registry of ExitState objects (the per-position lifecycle state).
    On each tick it evaluates every open position against the ExitEngine and
    emits exit intents as needed.  The SNIPE loop adds new positions; the FAST
    loop removes them on close.

    CRITICAL PATH PROOF — no LLM / no slow-model await:
      tick() calls only:
        - self._store.* (synchronous local/Redis reads)
        - self._price_feed.get_mark_price() (pre-subscribed; bounded)
        - self._breaker.entries_allowed() / record_pnl() (in-process math)
        - ExitEngine.on_tick() (PURE function — no IO, no LLM, no network)
        - self._venue.exit() (venue: sim is immediate; real has bounded timeout)
      Zero calls to any LLM client, reasoning module, or SLOW-loop function.
      Zero 'await' expressions anywhere in this class.
    """

    def __init__(
        self,
        store: StateStore,
        venue: ExecutionVenue,
        breaker: CircuitBreaker,
        price_feed: MarkPriceProvider,
        exit_config: ExitConfig,
        *,
        enforcer_wiring: FastLoopEnforcerWiring | None = None,
        tick_budget_ms: int = 100,
        hard_stop_budget_ms: int = 50,
        cooldown_seconds: int = 30,
    ) -> None:
        """
        Args:
            store:           StateStore (InMemoryStateStore or RedisStateStore).
            venue:           ExecutionVenue (SimulationVenue in paper/test).
            breaker:         CircuitBreaker (M4 T-320).
            price_feed:      MarkPriceProvider (pre-subscribed; NOT a live RPC).
            exit_config:     ExitConfig (FROZEN-at-entry TP/trailing/stop config).
            enforcer_wiring: Optional FastLoopEnforcerWiring (T-342).  When
                             provided, this is the SINGLE source for:
                               - Layer-2 survivable-stop enforcement (via
                                 SecondaryStopEnforcer on every tick), AND
                               - DMS heartbeat emission into the HeartbeatWriter
                                 that the aats-dms watchdog reads cross-process.
                             When None (legacy / partial integration), the loop
                             falls back to StateStore.emit_heartbeat() for
                             monitoring-side heartbeat only (no DMS cross-process
                             path) and handles stop triggering via _force_exit().
                             Production MUST always inject enforcer_wiring so
                             Layer 2 + Layer 3 are live.
        """
        self._store = store
        self._venue = venue
        self._breaker = breaker
        self._price_feed = price_feed
        self._exit_config = exit_config
        self._engine = ExitEngine(config=exit_config)
        self._enforcer_wiring = enforcer_wiring
        self._tick_budget_ms = tick_budget_ms
        self._hard_stop_budget_ms = hard_stop_budget_ms
        self._cooldown_seconds = cooldown_seconds

        # Per-mint FSM registry (FAST loop is the single writer for OPEN/CLOSING/CLOSED)
        self._fsm_registry: dict[str, PositionFSM] = {}

        # Per-mint ExitState registry (the stateful lifecycle for each position)
        self._exit_states: dict[str, ExitState] = {}

        # Flatten handler (bridges emergency flatten -> venue exits)
        self._flatten_handler = FastLoopFlattenHandler(
            store, venue, self._fsm_registry
        )

    @property
    def flatten_handler(self) -> FastLoopFlattenHandler:
        return self._flatten_handler

    def tick(self, block_time_ms: int, current_slot: int) -> list[str]:
        """Run one FAST-loop tick.

        Args:
            block_time_ms: on-chain block_time (event-time) — AUTHORITATIVE clock.
            current_slot:  current on-chain slot.

        Returns:
            list of actions taken (for logging / ops feed).

        CRITICAL PATH PROOF:
          This method has NO 'await'.  It has NO call to any LLM, reasoning,
          or slow-model module.  The narrative_failure_flag is a boolean read
          from StateStore (pre-computed by SLOW loop).  ExitEngine.on_tick()
          is a pure function with zero IO.
        """
        tick_start_wall_ms = time.monotonic_ns() // 1_000_000
        actions: list[str] = []

        # --- Layer-2 enforcement + DMS heartbeat (the single source when wired) ---
        # When enforcer_wiring is present it is the SINGLE path for:
        #   (a) Layer-2 survivable-stop enforcement (SecondaryStopEnforcer), and
        #   (b) DMS heartbeat emission into the HeartbeatWriter the aats-dms
        #       watchdog reads cross-process (BLUEPRINT §9, FR-033, AC-027).
        # We also call _store.emit_heartbeat() for the monitoring/dashboard side
        # (StateStore key `heartbeat:fast_loop`), which is a separate concern.
        # These are NOT duplicates: the HeartbeatWriter is the cross-process DMS
        # boundary; StateStore.emit_heartbeat is the in-process observability path.
        self._store.emit_heartbeat(block_time_ms)

        # Collect (mint, mark, block_time_ms) for the enforcer tick.
        # We build this list below during position evaluation and call the wiring
        # once per tick (after price lookup) to avoid a double loop.
        _enforcer_ticks: list[tuple[str, Decimal, int]] = []

        # --- Synchronize registry with store (crash-recovery) ---
        self._sync_registry(block_time_ms)

        # --- Evaluate each open position ---
        for mint in list(self._fsm_registry.keys()):
            fsm = self._fsm_registry.get(mint)
            if fsm is None:
                continue
            pos = fsm.position

            # Only act on OPEN positions (ENTERING waits for fill reconcile)
            if pos.fsm_state != FSMState.OPEN:
                continue

            # Get mark price from PRE-SUBSCRIBED feed (no live RPC call)
            mark = self._price_feed.get_mark_price(mint, current_slot)
            if mark is None:
                # Stale / unavailable price: treat as risk-off (BLUEPRINT §2.1).
                # On the enforcer-wiring path: deregister from Layer-2 first so
                # the wiring does not also fire on this mint this tick.
                log.warning(
                    "fast.mark_unavailable: mint=%s -> force_exit_risk_off", mint
                )
                if self._enforcer_wiring is not None:
                    self._enforcer_wiring.deregister(mint)
                self._force_exit(fsm, "mark_price_unavailable", block_time_ms)
                actions.append(f"exit:mark_unavailable:{mint}")
                continue

            # Collect price for the Layer-2 enforcer tick (called once after loop)
            if self._enforcer_wiring is not None:
                _enforcer_ticks.append((mint, mark, block_time_ms))

            # --- Narrative failure flag check (PRE-COMPUTED by SLOW; zero LLM call) ---
            # This is the ONLY LLM-derived input and it can ONLY force an exit (de-risk).
            narrative_failure = self._store.get_narrative_failure_flag(mint)

            # --- Insider/dev-sell dump flag check (E14b; PRE-SET off-hot-path by E14a) ---
            # A bare bool read — zero computation, no RPC, no LLM — exactly like the
            # narrative flag above.  It can ONLY force an exit (de-risk): a creator/
            # top-holder distribution past threshold is a rug-in-progress.
            insider_dump = self._store.get_insider_dump_flag(mint)

            # --- Delayed-honeypot / tax-flip sellability flag (E17; PRE-SET off-hot-path) ---
            # A bare bool read — zero computation, no RPC, no sell-sim — exactly like
            # the two flags above.  It can ONLY force an exit (de-risk): the SLOW-loop
            # sell-sim re-probe found the token may have flipped into a honeypot after
            # entry, so we exit SECURE while an exit is still possible.
            sellability_degraded = self._store.get_sellability_degraded_flag(mint)

            # --- ExitEngine evaluation (PURE function — no IO, no LLM, no network) ---
            exit_state = self._exit_states.get(mint)
            if exit_state is None:
                # Should not happen after reconcile_fill, but defensive
                continue

            decision = self._engine.on_tick(
                exit_state,
                mark_price=mark,
                block_time_ms=block_time_ms,
                narrative_failure_flag=narrative_failure,
                insider_dump_flag=insider_dump,
                sellability_degraded_flag=sellability_degraded,
            )

            # Update the exit state for the next tick (pure fold)
            self._exit_states[mint] = decision.next_state

            # Update unrealized PnL (integer math)
            if exit_state.entry_fill_price > 0 and pos.tokens_out_base > 0:
                unrealized = int(
                    (mark - exit_state.entry_fill_price)
                    * Decimal(str(pos.tokens_out_base))
                )
                old_unrealized = pos.unrealized_pnl_net_lamports
                fsm.update_unrealized_pnl(unrealized)

                # Emit PnL delta to breaker (event-time, integer lamports)
                pnl_event = PnLEvent(
                    mint=mint,
                    block_time_ms=block_time_ms,
                    delta_lamports=unrealized - old_unrealized,
                    kind="unrealized",
                )
                new_breaker_state = self._breaker.record_pnl(pnl_event)
                # T-402-F1: project the authoritative BreakerState into the StateStore
                # so /api/state + /api/metrics.breaker_tripped reflect reality.
                # The CircuitBreaker's own BreakerStore is the write-ahead source of
                # truth; the StateStore projection is the observability/read path.
                # De-risk only: projection cannot increase exposure.
                self._store.save_breaker_state(new_breaker_state)

            # --- Act on decision ---
            if decision.is_exit() and decision.intent is not None:
                if decision.is_full_exit():
                    log.info(
                        "fast.exit_full: mint=%s reason=%s mark=%s",
                        mint, decision.reason.value, mark,
                    )
                    self._do_full_exit(fsm, decision.intent, block_time_ms)
                    actions.append(f"exit:full:{mint}:{decision.reason.value}")
                else:
                    # Partial scale-out (TP rung)
                    log.info(
                        "fast.scale_out: mint=%s reason=%s fraction_bps=%d",
                        mint, decision.reason.value, decision.fraction_bps,
                    )
                    self._do_scale_out(fsm, decision.intent, block_time_ms)
                    fsm.record_tp_hit()
                    actions.append(f"exit:scale_out:{mint}:{decision.reason.value}")

        # --- Layer-2 + DMS heartbeat via FastLoopEnforcerWiring (B1 integration) ---
        # This is the SINGLE Layer-2 enforcement call and the SINGLE DMS heartbeat
        # emission for this tick.  It runs AFTER the ExitEngine loop so that a
        # position already closed by the ExitEngine this tick is not also passed
        # to the enforcer (we deregistered it in _do_full_exit above).
        # The enforcer's breach decision is a pure Decimal comparison (no IO,
        # no LLM, no network) — safe on the FAST-loop critical path.
        if self._enforcer_wiring is not None:
            # Snapshot armed mints BEFORE the tick so we can detect which fired.
            _armed_before = self._enforcer_wiring.armed_mints()
            # on_tick() emits heartbeat AND runs Layer-2 breach checks.
            # Pass _enforcer_ticks (already filtered to OPEN mints with valid marks).
            enforcer_fills = self._enforcer_wiring.on_tick(_enforcer_ticks)
            # Mints that were armed before but are not armed after have fired.
            _armed_after = self._enforcer_wiring.armed_mints()
            _fired_mints = _armed_before - _armed_after
            # Transition FSM to CLOSING/CLOSED for each stop fired by the enforcer.
            # Note: the SecondaryStopEnforcer already called venue.exit() inside
            # on_tick(); we only need to do the FSM bookkeeping here.
            for breached_mint in _fired_mints:
                fsm = self._fsm_registry.get(breached_mint)
                if fsm is not None:
                    try:
                        fsm.transition_to_closing(block_time_ms=block_time_ms)
                        fsm.transition_to_closed(
                            realized_pnl_net_lamports=0,
                            block_time_ms=block_time_ms,
                        )
                    except ValueError as exc:
                        log.error(
                            "fast.enforcer_stop.fsm_error: mint=%s err=%s",
                            breached_mint,
                            exc,
                        )
                    self._fsm_registry.pop(breached_mint, None)
                    self._exit_states.pop(breached_mint, None)
                    self._store.set_cooldown(breached_mint, self._cooldown_seconds)
                    actions.append(f"exit:layer2_stop:{breached_mint}")
            # Log fill outcomes for observability
            for fill in enforcer_fills:
                log.warning(
                    "fast.layer2_stop_fired: fill.landed=%s",
                    fill.landed,
                )

        # --- Budget check ---
        elapsed_ms = (time.monotonic_ns() // 1_000_000) - tick_start_wall_ms
        if elapsed_ms > self._tick_budget_ms:
            log.warning(
                "fast.tick_budget_breach: elapsed_ms=%d budget_ms=%d open_positions=%d",
                elapsed_ms,
                self._tick_budget_ms,
                len(self._fsm_registry),
            )
            # Emit metric but CONTINUE (BLUEPRINT §2.1: do not queue work that compounds)
            actions.append(f"budget_breach:{elapsed_ms}ms")

        return actions

    # ------------------------------------------------------------------
    # Fill reconciliation — called by FAST on each tick or on fill event
    # ------------------------------------------------------------------

    def reconcile_fill(self, mint: str, fill: FillResult, block_time_ms: int) -> bool:
        """Transition ENTERING->OPEN on a confirmed fill.

        Called by the fill consumer (from the fills stream) or by the Reconciler
        on startup.  Returns True if the transition happened, False if the mint
        was not in ENTERING state (idempotent).
        """
        pos = self._store.load_position(mint)
        if pos is None or pos.fsm_state != FSMState.ENTERING:
            return False

        if not fill.landed:
            # Fill failed — transition to VETOED
            fsm = self._fsm_registry.get(mint)
            if fsm is None:
                fsm = PositionFSM(self._store, pos)
                self._fsm_registry[mint] = fsm
            try:
                fsm.transition_to_vetoed(reason=fill.reason, block_time_ms=block_time_ms)
            except ValueError as exc:
                log.error("fast.reconcile_fill.veto_error: mint=%s err=%s", mint, exc)
            self._fsm_registry.pop(mint, None)
            self._exit_states.pop(mint, None)
            self._store.set_cooldown(mint, self._cooldown_seconds)
            return True

        # Fill landed — transition to OPEN
        fsm = self._fsm_registry.get(mint)
        if fsm is None:
            fsm = PositionFSM(self._store, pos)
        try:
            fsm.transition_to_open(fill, block_time_ms=block_time_ms)
        except ValueError as exc:
            log.error("fast.reconcile_fill.open_error: mint=%s err=%s", mint, exc)
            return False
        self._fsm_registry[mint] = fsm

        # Create ExitState for this position (the stateful lifecycle tracker)
        entry_price = (
            Decimal(fill.effective_price_decimal)
            if fill.effective_price_decimal != "0"
            else Decimal("1")  # fallback for sim (price=0 in dry-run)
        )
        self._exit_states[mint] = ExitState.at_entry(
            mint=mint,
            entry_fill_price=entry_price,
            config=self._exit_config,
        )

        # Arm Layer-2 stop enforcer for this position (T-342 B1 integration).
        # wiring.arm() registers the fixed stop trigger in SecondaryStopEnforcer
        # so on_tick() will enforce it on every subsequent FAST-loop tick.
        if self._enforcer_wiring is not None:
            self._enforcer_wiring.arm(
                mint=mint,
                entry_fill_price=entry_price,
                position=fsm.position,
            )
            log.info(
                "fast.reconcile_fill.enforcer_armed: mint=%s entry_price=%s",
                mint,
                entry_price,
            )

        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sync_registry(self, block_time_ms: int) -> None:
        """Sync the in-memory FSM registry with the store (crash-recovery path).

        Adds any OPEN positions in the store that are not in the registry.
        Removes registry entries for positions that no longer exist in the store.
        """
        store_positions = {p.mint: p for p in self._store.load_all_positions()}

        # Add missing OPEN positions (e.g. process restarted mid-run)
        for mint, pos in store_positions.items():
            if mint not in self._fsm_registry:
                self._fsm_registry[mint] = PositionFSM(self._store, pos)
                if pos.fsm_state == FSMState.OPEN and mint not in self._exit_states:
                    # Re-derive entry price from position data
                    entry_price = self._estimate_entry_price(pos)
                    self._exit_states[mint] = ExitState.at_entry(
                        mint=mint,
                        entry_fill_price=entry_price,
                        config=self._exit_config,
                    )

        # Remove stale registry entries
        stale = [m for m in self._fsm_registry if m not in store_positions]
        for mint in stale:
            self._fsm_registry.pop(mint, None)
            self._exit_states.pop(mint, None)

    def _force_exit(
        self, fsm: PositionFSM, reason: str, block_time_ms: int
    ) -> None:
        """Force a full exit on an OPEN position (risk-off path).

        Used for non-breach risk-off events (e.g. mark price unavailable).
        When enforcer_wiring is present, deregister from Layer-2 first so the
        enforcer does not also fire on the same mint this tick.
        """
        intent_id = make_client_intent_id(fsm.mint, fsm.position.entry_slot, "FORCE_EXIT")
        exit_intent = ExitIntent(
            client_intent_id=intent_id,
            mint=fsm.mint,
            fraction_bps=10_000,
            exit_mode="secure",
            reason=reason,
        )
        self._venue.exit(exit_intent, fsm.position)
        try:
            fsm.transition_to_closing(block_time_ms=block_time_ms)
            fsm.transition_to_closed(
                realized_pnl_net_lamports=0,
                block_time_ms=block_time_ms,
            )
        except ValueError as exc:
            log.error("fast.force_exit.fsm_error: mint=%s err=%s", fsm.mint, exc)
        self._fsm_registry.pop(fsm.mint, None)
        self._exit_states.pop(fsm.mint, None)
        if self._enforcer_wiring is not None:
            self._enforcer_wiring.deregister(fsm.mint)
        self._store.set_cooldown(fsm.mint, self._cooldown_seconds)

    def _do_full_exit(
        self,
        fsm: PositionFSM,
        intent: ExitIntent | ReduceIntent,
        block_time_ms: int,
    ) -> None:
        """Execute a full exit intent and transition FSM to CLOSED."""
        self._venue.exit(intent, fsm.position)
        try:
            fsm.transition_to_closing(block_time_ms=block_time_ms)
            fsm.transition_to_closed(
                realized_pnl_net_lamports=0,
                block_time_ms=block_time_ms,
            )
        except ValueError as exc:
            log.error("fast.do_full_exit.fsm_error: mint=%s err=%s", fsm.mint, exc)
        self._fsm_registry.pop(fsm.mint, None)
        self._exit_states.pop(fsm.mint, None)
        if self._enforcer_wiring is not None:
            self._enforcer_wiring.deregister(fsm.mint)
        self._store.set_cooldown(fsm.mint, self._cooldown_seconds)

    def _do_scale_out(
        self,
        fsm: PositionFSM,
        intent: object,
        block_time_ms: int,
    ) -> None:
        """Execute a partial scale-out; position remains OPEN."""
        self._venue.exit(intent, fsm.position)  # type: ignore[arg-type]
        # Position remains OPEN; FSM stays OPEN; ExitState updated in tick()

    def _estimate_entry_price(self, pos: Position) -> Decimal:
        """Estimate entry fill price from position data."""
        if pos.tokens_out_base > 0:
            return Decimal(str(pos.sol_in_lamports)) / Decimal(str(pos.tokens_out_base))
        return Decimal("1")  # fallback > 0 required by StopState

    # ------------------------------------------------------------------
    # Public accessors for reconciler / control plane
    # ------------------------------------------------------------------

    def get_open_positions(self) -> list[Position]:
        return [fsm.position for fsm in self._fsm_registry.values()]

    def get_fsm(self, mint: str) -> PositionFSM | None:
        return self._fsm_registry.get(mint)
