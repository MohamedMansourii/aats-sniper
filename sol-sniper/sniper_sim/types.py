"""Typed contracts shared across the loops and the venue.

These are the message shapes that cross loop boundaries. In production they'd be
pydantic models validated on the bus; here plain dataclasses keep it stdlib-only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

LAMPORTS_PER_SOL = 1_000_000_000


class Decision(str, Enum):
    SNIPE = "SNIPE"
    SKIP = "SKIP"          # model said no / not worth it
    VETO = "VETO"          # gate or LLM killed it (de-risk only)


@dataclass(frozen=True)
class LaunchEvent:
    """A detected liquidity event.

    The first block of fields is what the bot can actually observe at decision
    time. The `truth_*` block is GROUND TRUTH known only to the simulator and is
    used to score outcomes — the bot's model/gate never get to read it directly.
    """
    mint: str
    slot: int
    sol_reserve: float          # initial pool SOL
    token_reserve: float        # initial pool tokens
    competitors: int            # other bots racing this same launch

    # ---- ground truth (simulator-only; never fed to model/gate) ----
    truth_is_rug: bool
    truth_max_multiple: float   # best *sellable* price multiple within horizon
    truth_rug_detectable: bool  # whether a good gate could have caught the rug


@dataclass(frozen=True)
class SwapIntent:
    """What the snipe loop hands to the venue."""
    mint: str
    sol_in: float
    slippage_bps: int
    tip_lamports: int
    cu_price_microlamports: int
    target_slot: int


@dataclass
class FillResult:
    """What the venue returns. `landed=False` means you never got a position."""
    landed: bool
    reason: str = ""
    land_slot: int | None = None
    slot_delay: int | None = None        # slots behind the LP-add event
    buyers_ahead: int = 0                 # co-buyers who moved price before you
    tokens_out: float = 0.0
    effective_price: float = 0.0          # SOL per token actually paid
    entry_slippage: float = 0.0           # fraction vs untouched spot
    tip_lamports: int = 0
    priority_lamports: int = 0
