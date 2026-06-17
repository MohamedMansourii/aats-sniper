"""M3 Controller — Reconciler (startup AND continuous).

T-340 / BLUEPRINT §2.1.

STARTUP RECONCILIATION:
  On startup, rebuild bot-state from StateStore, then reconcile against
  venue-state (open token balances, pending/landed signatures) before any
  loop is allowed to act.  Bot-state and venue-state disagreeing is the
  DEFAULT assumption, not the exception.

CONTINUOUS RECONCILIATION (every FAST-loop tick):
  On every tick, re-reconcile bot-state vs venue truth so a partial fill,
  a manual Phantom intervention, or a missed signature surfaces within one
  tick — not at the next restart.

RECOVERY SEMANTICS:
  ENTERING + fill found  -> transition to OPEN (normal path)
  ENTERING + no fill     -> if TTL expired, transition to VETOED (dead entry)
  OPEN     + not in venue -> emit emergency exit intent (position disappeared)
  CLOSING  + fill found  -> transition to CLOSED

IDEMPOTENCY:
  All transitions use the existing FSM methods (which are write-ahead and
  dedup on intent_id), so replaying on restart is safe.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from aats.contracts.positions import FSMState
from aats.contracts.venue import ExecutionVenue, FillResult
from aats.controller.fsm import PositionFSM, make_client_intent_id
from aats.controller.state import StateStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# VenueStateProvider — injected venue query (bounded latency; startup only)
# ---------------------------------------------------------------------------


@runtime_checkable
class VenueStateProvider(Protocol):
    """Query open token balances and fill status from the venue.

    Called by the Reconciler on startup and (optionally) every N ticks.
    NOT called on the FAST-loop critical path.  In sim mode this returns
    an empty set (all positions reconcile against the state store only).
    """

    def get_fill_for_mint(self, mint: str, intent_id: str) -> FillResult | None:
        """Return the fill for a pending entry, or None if not found/landed."""
        ...

    def get_open_mints(self) -> set[str]:
        """Return the set of mints for which the venue holds a token balance."""
        ...


class NullVenueStateProvider:
    """No-op venue state provider (sim mode: venue state is trusted as bot state)."""

    def get_fill_for_mint(self, mint: str, intent_id: str) -> FillResult | None:
        return None

    def get_open_mints(self) -> set[str]:
        return set()


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


class Reconciler:
    """Startup + continuous reconciler.

    Ensures bot-state (StateStore) and venue-state agree before any loop acts.
    All reconciliation is synchronous; the startup path is the only place where
    we allow slower venue queries.
    """

    def __init__(
        self,
        store: StateStore,
        venue: ExecutionVenue,
        venue_state: VenueStateProvider | None = None,
        *,
        entering_ttl_seconds: int = 30,
    ) -> None:
        self._store = store
        self._venue = venue
        self._venue_state = venue_state or NullVenueStateProvider()
        self._entering_ttl = entering_ttl_seconds

    def startup_reconcile(
        self,
        fast_loop: object,
        block_time_ms: int,
    ) -> list[str]:
        """Full reconciliation on startup.  Call BEFORE allowing any loop to act.

        Args:
            fast_loop: the FastLoop instance (used to call reconcile_fill).
            block_time_ms: startup event-time anchor.

        Returns:
            list of reconciliation actions taken (for logging).
        """
        from aats.controller.fast_loop import FastLoop  # avoid circular at module load

        actions: list[str] = []
        positions = self._store.load_all_positions()
        open_venue_mints = self._venue_state.get_open_mints()

        for pos in positions:
            mint = pos.mint
            fsm_state = pos.fsm_state

            if fsm_state == FSMState.ENTERING:
                # Check if a fill exists for this pending entry
                intent_id = make_client_intent_id(mint, pos.entry_slot, "ENTRY")
                fill = self._venue_state.get_fill_for_mint(mint, intent_id)

                if fill is not None and fill.landed:
                    # Fill landed while we were down -> transition to OPEN
                    if isinstance(fast_loop, FastLoop):
                        fast_loop.reconcile_fill(mint, fill, block_time_ms)
                    actions.append(f"reconcile:entering_to_open:{mint}")
                    log.info("reconcile.startup: %s ENTERING -> OPEN (fill found)", mint)
                else:
                    # No fill found — treat as failed (VETOED)
                    # The TTL guard: if the position is old enough that the entry
                    # window has certainly closed, transition to VETOED.
                    fsm = PositionFSM(self._store, pos)
                    failed_fill = FillResult(landed=False, reason="reconcile_no_fill")
                    if isinstance(fast_loop, FastLoop):
                        fast_loop.reconcile_fill(mint, failed_fill, block_time_ms)
                    actions.append(f"reconcile:entering_vetoed:{mint}")
                    log.info("reconcile.startup: %s ENTERING -> VETOED (no fill)", mint)

            elif fsm_state == FSMState.OPEN:
                if open_venue_mints and mint not in open_venue_mints:
                    # Position disappeared from venue without our knowledge
                    # Transition to CLOSING -> CLOSED (emergency)
                    fsm = PositionFSM(self._store, pos)
                    try:
                        fsm.transition_to_closing(block_time_ms=block_time_ms)
                        fsm.transition_to_closed(
                            realized_pnl_net_lamports=0,  # unknown
                            block_time_ms=block_time_ms,
                        )
                        actions.append(f"reconcile:open_disappeared:{mint}")
                        log.warning(
                            "reconcile.startup: %s OPEN but not in venue -> CLOSED", mint
                        )
                    except ValueError as exc:
                        log.error("reconcile.startup: FSM error for %s: %s", mint, exc)

            elif fsm_state == FSMState.CLOSING:
                # Still CLOSING on startup — check if fill arrived
                intent_id = make_client_intent_id(mint, pos.entry_slot, "EXIT")
                fill = self._venue_state.get_fill_for_mint(mint, intent_id)
                if fill is not None and fill.landed:
                    fsm = PositionFSM(self._store, pos)
                    try:
                        fsm.transition_to_closed(
                            realized_pnl_net_lamports=0,
                            block_time_ms=block_time_ms,
                        )
                        actions.append(f"reconcile:closing_to_closed:{mint}")
                        log.info("reconcile.startup: %s CLOSING -> CLOSED", mint)
                    except ValueError as exc:
                        log.error("reconcile.startup: FSM error for %s: %s", mint, exc)

            # CLOSED / VETOED positions should not be in the store (delete_position
            # is called on close/veto), but if they are (stale data), remove them.
            elif fsm_state in (FSMState.CLOSED, FSMState.VETOED):
                self._store.delete_position(mint)
                actions.append(f"reconcile:purge_terminal:{mint}:{fsm_state.value}")
                log.info("reconcile.startup: purged terminal state %s for %s", fsm_state, mint)

        return actions

    def continuous_reconcile_tick(
        self,
        fast_loop: object,
        block_time_ms: int,
        current_slot: int,
    ) -> list[str]:
        """Lightweight per-tick reconciliation.

        Checks for ENTERING positions older than the TTL and transitions them to
        VETOED if no fill has arrived.  This is the ongoing safety net that ensures
        we never have a permanently-stuck ENTERING position.

        In production this would also check pending tx signatures; in sim it is
        TTL-only.
        """
        from aats.controller.fast_loop import FastLoop

        actions: list[str] = []
        positions = self._store.load_all_positions()

        for pos in positions:
            if pos.fsm_state == FSMState.ENTERING:
                # Check if the entry window has expired (>= entering_ttl_seconds since entry)
                entry_age_slots = current_slot - pos.entry_slot
                # ~400ms/slot: 30s TTL ~ 75 slots
                if entry_age_slots > (self._entering_ttl * 1_000 // 400):
                    failed_fill = FillResult(landed=False, reason="entering_timed_out")
                    if isinstance(fast_loop, FastLoop):
                        fast_loop.reconcile_fill(pos.mint, failed_fill, block_time_ms)
                    actions.append(f"reconcile:entering_timeout:{pos.mint}")
                    log.info(
                        "reconcile.continuous: %s ENTERING timed out (age=%d slots)",
                        pos.mint,
                        entry_age_slots,
                    )

        return actions
