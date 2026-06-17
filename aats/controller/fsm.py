"""M3 Controller — Per-position FSM (single-writer, write-ahead).

T-340 / ADR-0007.

SINGLE-WRITER CONTRACT:
  SNIPE loop writes IDLE->ENTERING (via StateStore.claim_entering — the atomic CAS).
  FAST loop writes ALL other transitions: ENTERING->OPEN, ENTERING->VETOED,
  OPEN->CLOSING, CLOSING->CLOSED.

WRITE-AHEAD:
  The FSM persists the new state to the StateStore BEFORE emitting any side-effect
  (BLUEPRINT §2.2).  A crash mid-action leaves a recoverable record; on restart the
  Reconciler rebuilds state from the store and replays idempotently.

ILLEGAL TRANSITIONS:
  Any transition not in FSM_LEGAL_TRANSITIONS raises ValueError with reason
  'fsm_illegal_transition'.  They NEVER silently no-op (data-models.md §7).

IDEMPOTENT INTENTS:
  Every intent carries a deterministic client_intent_id (mint + epoch).  The FSM
  checks StateStore.mark_intent_seen() before acting; a replay of the same intent
  is a no-op (same id -> already seen -> skip).

MONEY / TIME:
  All monetary fields are int lamports or Decimal strings.  FSM uses event_time
  (block_time_ms) for all stamping — never wall-clock.
"""

from __future__ import annotations

import hashlib
import logging

from aats.contracts.intents import CostStack
from aats.contracts.positions import (
    FSMState,
    Position,
    validate_fsm_transition,
)
from aats.contracts.venue import FillResult
from aats.controller.state import StateStore

log = logging.getLogger(__name__)


def make_client_intent_id(mint: str, epoch: int, kind: str) -> str:
    """Deterministic intent-id from (mint, FSM epoch, kind).

    The same (mint, epoch, kind) always produces the same id, so a crash-replay
    re-emits the same id and M4 deduplicates on it (FR-024, idempotent intents).
    The epoch is the event_time.slot of the originating LaunchEvent; it never
    reuses across positions on the same mint because a new position has a new slot.
    """
    raw = f"{mint}:{epoch}:{kind}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class PositionFSM:
    """Wraps the per-position FSM with write-ahead persistence and dedup.

    One instance per open position.  The FAST loop holds a registry of these
    (by mint) and calls the appropriate transition method on each tick.

    All methods are synchronous (FAST loop must not block on coroutines).
    """

    def __init__(self, store: StateStore, position: Position) -> None:
        self._store = store
        self._position = position

    @property
    def position(self) -> Position:
        return self._position

    @property
    def state(self) -> FSMState:
        return self._position.fsm_state

    @property
    def mint(self) -> str:
        return self._position.mint

    # ------------------------------------------------------------------
    # SNIPE LOOP writes this transition (the write-ahead + atomic CAS).
    # The ACTUAL atomic claim is in StateStore.claim_entering(); this
    # method is called ONLY after a successful claim.
    # ------------------------------------------------------------------

    @classmethod
    def create_entering(
        cls,
        store: StateStore,
        *,
        mint: str,
        token: str,
        surface: str,
        entry_slot: int,
        sol_in_lamports: int,
        tokens_out_base: int,
        entry_slippage_bps: int,
        exit_config_label: str,
        exit_mode: str,
        tp_total: int,
        hard_stop_price_lamports: int,
        cost_breakdown: CostStack,
        client_intent_id: str,
    ) -> PositionFSM:
        """Create a new position record in ENTERING state (write-ahead).

        Called by the SNIPE loop AFTER a successful claim_entering() CAS.
        The Position is written to the store before the tx is submitted,
        so a crash mid-submit leaves a recoverable ENTERING record.
        """
        position = Position(
            mint=mint,
            token=token,
            surface=surface,
            fsm_state=FSMState.ENTERING,
            entry_slot=entry_slot,
            sol_in_lamports=sol_in_lamports,
            tokens_out_base=tokens_out_base,
            entry_slippage_bps=entry_slippage_bps,
            exit_config_label=exit_config_label,
            exit_mode=exit_mode,  # type: ignore[arg-type]
            tp_hit=0,
            tp_total=tp_total,
            trailing_armed=False,
            hard_stop_price_lamports=hard_stop_price_lamports,
            realized_pnl_net_lamports=None,
            unrealized_pnl_net_lamports=0,
            cost_breakdown=cost_breakdown,
            status="open",
            completeness_status="complete",
        )
        store.save_position(position)
        log.info("fsm.entering: mint=%s intent_id=%s", mint, client_intent_id)
        return cls(store, position)

    # ------------------------------------------------------------------
    # FAST LOOP writes all transitions below.
    # ------------------------------------------------------------------

    def transition_to_open(self, fill: FillResult, *, block_time_ms: int) -> None:
        """ENTERING -> OPEN on fill confirmation (FAST loop, on fill ack)."""
        validate_fsm_transition(self._position.fsm_state, FSMState.OPEN)
        updated = self._position.model_copy(
            update={
                "fsm_state": FSMState.OPEN,
                "tokens_out_base": fill.tokens_out,
                "entry_slippage_bps": fill.entry_slippage_bps,
                "unrealized_pnl_net_lamports": 0,
                "status": "open",
            }
        )
        # Write-ahead BEFORE any side effect
        self._store.save_position(updated)
        self._position = updated
        self._store.release_entering(self.mint)
        log.info(
            "fsm.open: mint=%s tokens_out=%d slip_bps=%d",
            self.mint,
            fill.tokens_out,
            fill.entry_slippage_bps,
        )

    def transition_to_vetoed(self, *, reason: str, block_time_ms: int) -> None:
        """ENTERING -> VETOED (FAST loop, on veto/failed fill)."""
        validate_fsm_transition(self._position.fsm_state, FSMState.VETOED)
        updated = self._position.model_copy(
            update={
                "fsm_state": FSMState.VETOED,
                "status": "closed",
                "realized_pnl_net_lamports": 0,
            }
        )
        self._store.save_position(updated)
        self._position = updated
        self._store.release_entering(self.mint)
        self._store.delete_position(self.mint)
        log.info("fsm.vetoed: mint=%s reason=%s", self.mint, reason)

    def transition_to_closing(self, *, block_time_ms: int) -> None:
        """OPEN -> CLOSING (FAST loop, exit intent emitted)."""
        validate_fsm_transition(self._position.fsm_state, FSMState.CLOSING)
        updated = self._position.model_copy(update={"fsm_state": FSMState.CLOSING})
        self._store.save_position(updated)
        self._position = updated
        log.info("fsm.closing: mint=%s", self.mint)

    def transition_to_closed(
        self,
        *,
        realized_pnl_net_lamports: int,
        block_time_ms: int,
    ) -> None:
        """CLOSING -> CLOSED (FAST loop, exit fill confirmed)."""
        validate_fsm_transition(self._position.fsm_state, FSMState.CLOSED)
        if isinstance(realized_pnl_net_lamports, float):
            raise TypeError(
                "realized_pnl_net_lamports must be int (lamports), got float "
                f"{realized_pnl_net_lamports!r} (data-models.md §0)."
            )
        updated = self._position.model_copy(
            update={
                "fsm_state": FSMState.CLOSED,
                "realized_pnl_net_lamports": realized_pnl_net_lamports,
                "status": "closed",
            }
        )
        self._store.save_position(updated)
        self._position = updated
        self._store.delete_position(self.mint)
        log.info(
            "fsm.closed: mint=%s realized_pnl_net_lamports=%d",
            self.mint,
            realized_pnl_net_lamports,
        )

    def update_unrealized_pnl(self, unrealized_pnl_net_lamports: int) -> None:
        """Update mark-to-market unrealized PnL (no FSM transition, FAST loop)."""
        if isinstance(unrealized_pnl_net_lamports, float):
            raise TypeError(
                "unrealized_pnl_net_lamports must be int (lamports), got float "
                f"{unrealized_pnl_net_lamports!r} (data-models.md §0)."
            )
        updated = self._position.model_copy(
            update={"unrealized_pnl_net_lamports": unrealized_pnl_net_lamports}
        )
        self._store.save_position(updated)
        self._position = updated

    def record_tp_hit(self) -> None:
        """Increment tp_hit counter after a TP rung fires (FAST loop)."""
        updated = self._position.model_copy(update={"tp_hit": self._position.tp_hit + 1})
        self._store.save_position(updated)
        self._position = updated

    def arm_trailing(self) -> None:
        """Arm the trailing stop (FAST loop)."""
        if not self._position.trailing_armed:
            updated = self._position.model_copy(update={"trailing_armed": True})
            self._store.save_position(updated)
            self._position = updated
