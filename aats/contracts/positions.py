"""Position + FSMState — the per-position finite-state machine contract.

Source of truth: data-models.md §7.

FSM legal transitions (single-writer enforced by atomic Lua CAS, BLUEPRINT §2.2):
  IDLE → ENTERING   (SNIPE, atomic SETNX on per-mint lock)
  ENTERING → OPEN   (FAST, on fill)
  ENTERING → VETOED (FAST, on veto)
  OPEN → CLOSING    (FAST)
  CLOSING → CLOSED  (FAST)

A second IDLE→ENTERING on a mint already ENTERING/OPEN is rejected
fsm_state_conflict (AC-012).  Illegal transitions raise, never silently no-op.

Money fields (data-models.md §0):
  sol_in_lamports            → int (lamports)
  tokens_out_base            → int (base units)
  hard_stop_price_lamports   → int (lamports)
  realized_pnl_net_lamports  → int (lamports) or None
  unrealized_pnl_net_lamports → int (lamports)
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, model_validator

from aats.contracts.intents import CostStack


class FSMState(StrEnum):
    """Per-position finite-state machine states.

    SINGLE-WRITER:
      SNIPE writes IDLE→ENTERING (atomic CAS; holds the per-mint lock).
      FAST writes all other transitions.
    Illegal transitions raise; they NEVER silently no-op (data-models.md §7).
    """

    IDLE = "IDLE"
    ENTERING = "ENTERING"  # claimed by SNIPE (atomic CAS); write-ahead before submit
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    VETOED = "VETOED"


# Legal FSM transitions — the complete set; anything else is illegal.
FSM_LEGAL_TRANSITIONS: frozenset[tuple[FSMState, FSMState]] = frozenset(
    {
        (FSMState.IDLE, FSMState.ENTERING),
        (FSMState.ENTERING, FSMState.OPEN),
        (FSMState.ENTERING, FSMState.VETOED),
        (FSMState.OPEN, FSMState.CLOSING),
        (FSMState.CLOSING, FSMState.CLOSED),
    }
)


def validate_fsm_transition(current: FSMState, next_state: FSMState) -> None:
    """Assert that a state transition is legal.

    Raises ValueError with reason 'fsm_illegal_transition' if the transition is
    not in FSM_LEGAL_TRANSITIONS.  Illegal transitions MUST raise — they never
    silently no-op (data-models.md §7, AC-012).

    Args:
        current: The current FSM state.
        next_state: The proposed next FSM state.

    Raises:
        ValueError: If the transition is illegal.
    """
    if (current, next_state) not in FSM_LEGAL_TRANSITIONS:
        raise ValueError(
            f"fsm_illegal_transition: {current.value} → {next_state.value} is not a "
            f"legal FSM transition.  Legal transitions: "
            + ", ".join(
                f"{a.value}→{b.value}" for a, b in sorted(FSM_LEGAL_TRANSITIONS)
            )
        )


class Position(BaseModel):
    """A live or closed position record.

    Source: data-models.md §7.
    Single-writer: SNIPE writes ENTERING; FAST writes all other FSM states (FR-024).
    Persisted to Redis KV (while open) + Parquet positions_closed/ (on close).

    completeness_status = CENSORED is how un-snapshotted / un-labeled outcomes
    survive in the dataset (C-6, AC-006) — never dropped, so survivorship bias
    cannot creep in.

    Money fields are int (lamports/base-units) — NEVER float (data-models.md §0).
    """

    mint: str
    token: str
    surface: str  # EH-001 | EH-003 | ... (which hypothesis fired)
    fsm_state: FSMState
    entry_slot: int
    # SOL in: integer lamports (NOT float)
    sol_in_lamports: int
    # Tokens received: integer base units (NOT float)
    tokens_out_base: int
    entry_slippage_bps: int
    # frozen at entry — preset changes never retro-modify (AC-033)
    exit_config_label: str
    exit_mode: Literal["secure", "fast"]
    tp_hit: int
    tp_total: int
    trailing_armed: bool
    # Price-based stop: integer lamports (NOT float)
    hard_stop_price_lamports: int
    # PnL: integer lamports (NOT float), None while open
    realized_pnl_net_lamports: int | None
    # Unrealized PnL: integer lamports (NOT float)
    unrealized_pnl_net_lamports: int
    cost_breakdown: CostStack
    status: Literal["open", "closed"]
    # C-6: never drop unlabeled/un-snapshotted outcomes
    completeness_status: Literal["complete", "CENSORED"]

    # NO win_rate field (HONESTY CLAUSE, AC-037).

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _reject_float_money(cls, data: object) -> object:
        """Reject float values for integer money fields (data-models.md §0)."""
        if not isinstance(data, dict):
            return data
        _INT_MONEY_FIELDS = (
            "sol_in_lamports",
            "tokens_out_base",
            "hard_stop_price_lamports",
            "realized_pnl_net_lamports",
            "unrealized_pnl_net_lamports",
        )
        for field in _INT_MONEY_FIELDS:
            val = data.get(field)
            if val is not None and isinstance(val, float):
                raise ValueError(
                    f"Position field '{field}' must be int (lamports/base-units), "
                    f"got float {val!r}.  Use integer base units (data-models.md §0)."
                )
        return data

    @model_validator(mode="after")
    def _closed_invariant(self) -> Position:
        """A closed position must have a realized_pnl_net_lamports."""
        if self.status == "closed" and self.realized_pnl_net_lamports is None:
            raise ValueError(
                "A closed Position must have realized_pnl_net_lamports set "
                "(cannot be None when status='closed')."
            )
        return self
