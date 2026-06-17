"""T-321 — Survivable stop: the RULE + thresholds (Layer-defining half of the seam).

ADR-0008 — three independent layers, the stop holds even if the bot process dies:

  Layer 1  VENUE-NATIVE resting protection (off-box keeper / trigger order) —
           survives a busy OR dead process for the on-chain price breach.
  Layer 2  IN-PROCESS FAST-loop enforcer (steady-state primary, <=50ms p99) —
           deterministic, NO LLM / NO RPC await on the decision path.
  Layer 3  DEAD-MAN'S SWITCH (separate failure domain) — on heartbeat loss
           > T_DMS it auto-flattens every open position.

SEAM OWNERSHIP (AATS-ROSTER §4):
  - `risk-guardrails-engineer` (THIS FILE):  the stop RULE + thresholds, the
    deterministic enforcer decision, the venue-native PLACEMENT request, and the
    DMS arm/flatten decision.  We INVOKE `ExecutionVenue` — we never author
    transaction-building or RPC code.
  - `solana-execution-engineer`:  implements the venue-native resting order and
    the actual build/sign/land behind the `ExecutionVenue` ABC.
  - `agent-orchestration-engineer`:  OPERATES the enforcer loop + DMS heartbeat
    wiring (T-342).

NON-WAIVABLE RULES encoded here (not as comments — as types/asserts):
  * Money is integer lamports.  NEVER float.  A float stop price is rejected at
    construction.
  * The stop price is FIXED at entry.  Signals may only TIGHTEN (raise a SELL
    stop's trigger price) — never widen/lower it.  `tighten()` rejects any
    proposed price below the current stop.  A widen attempt raises.
  * The LLM is NEVER on this FAST decision path and may only DE-RISK.  The
    tighten capability is the only thing a de-risk signal can do to a stop, and
    even that is one-directional.  There is no API on this module to widen, move
    away, disarm, or override a stop from any automated/LLM caller.
  * Point-in-time: the breach test compares a point-in-time mark price (carried
    on the tick with its event-time block_time_ms) against the fixed trigger.
    No wall-clock, no lookahead.

POINT-IN-TIME (C-5):  every breach decision is a pure function of (fixed trigger,
point-in-time mark price).  The same function runs live and in backtest.

This module performs NO real-capital action itself; the flatten/exit handoff is
the `ExecutionVenue` seam (T-327) which is behind the DRY_RUN gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# A SELL-side hard stop: we are LONG the token, so the stop fires when the mark
# price falls TO OR BELOW the trigger.  "Tighten" therefore means RAISE the
# trigger (closer to mark) — never lower it.
#
# Prices are SOL-lamports per token base unit, carried as Decimal (money-derived
# exact quantity, data-models.md §0).  We never use float for a price.


def _to_decimal_price(value: object, field: str) -> Decimal:
    """Coerce a price input to Decimal, REJECTING float (data-models.md §0).

    int and str (and Decimal) are accepted; a raw float is refused so a lossy
    binary price can never enter the stop math.
    """
    if isinstance(value, float):
        raise TypeError(
            f"{field} must be Decimal/int/str (SOL-lamports per token base unit), "
            f"got float {value!r}.  Float money is forbidden (data-models.md §0)."
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        return Decimal(value)
    raise TypeError(f"{field} must be Decimal/int/str, got {type(value).__name__}.")


@dataclass(frozen=True)
class StopThresholds:
    """The thresholds that define the hard stop, FIXED at entry.

    `hard_stop_drawdown_bps` mirrors the sim's `hard_stop` (e.g. 0.60 => -40% =>
    4000 bps) but in integer basis points.  The trigger PRICE is derived from the
    entry fill price at construction and then FROZEN — preset changes never
    retro-modify an open position (AC-033, mirrored from Position.exit_config).
    """

    # -40% by default (sim FAST/SECURE hard_stop=0.60 => 4000 bps below entry).
    hard_stop_drawdown_bps: int = 4000

    def __post_init__(self) -> None:
        if isinstance(self.hard_stop_drawdown_bps, float):
            raise TypeError(
                "hard_stop_drawdown_bps must be int (bps), not float (data-models.md §0)."
            )
        if not (0 < self.hard_stop_drawdown_bps <= 10_000):
            raise ValueError(
                "hard_stop_drawdown_bps must be in (0, 10000] bps; "
                f"got {self.hard_stop_drawdown_bps}."
            )


def derive_stop_trigger_price(
    entry_fill_price: Decimal | int | str,
    thresholds: StopThresholds,
) -> Decimal:
    """Compute the FIXED hard-stop trigger price from the entry fill price.

    trigger = entry_fill_price * (1 - drawdown_bps/10000), exact in Decimal.
    For a -40% stop on a 100-lamport fill this is 60 lamports/token.
    """
    entry = _to_decimal_price(entry_fill_price, "entry_fill_price")
    if entry <= 0:
        raise ValueError(f"entry_fill_price must be > 0, got {entry}.")
    factor = (Decimal(10_000) - Decimal(thresholds.hard_stop_drawdown_bps)) / Decimal(10_000)
    return entry * factor


@dataclass(frozen=True)
class StopState:
    """The live, FIXED-at-entry hard-stop for one open position.

    Immutable.  Tightening produces a NEW StopState with a higher trigger; the
    original is never mutated.  There is no constructor path that lowers the
    trigger — `tighten()` raises on any non-tightening proposal.

    `trigger_price` is Decimal (SOL-lamports per token base unit).  It is the
    only price the breach test consults; a slow tick or a dead process does not
    change it (that is the whole point — the trigger lives independently of our
    liveness on Layer 1, and re-derives identically on Layer 2).
    """

    mint: str
    entry_fill_price: Decimal
    trigger_price: Decimal
    thresholds: StopThresholds

    def __post_init__(self) -> None:
        # Reject float-bearing construction (defense in depth; the factories use
        # Decimal already).
        for name in ("entry_fill_price", "trigger_price"):
            val = getattr(self, name)
            if isinstance(val, float):
                raise TypeError(f"StopState.{name} must be Decimal, not float (data-models.md §0).")
        if self.trigger_price <= 0:
            raise ValueError("StopState.trigger_price must be > 0.")
        if self.trigger_price > self.entry_fill_price:
            # The stop must sit at or below entry for a long.  A trigger ABOVE
            # entry would be a take-profit, not a stop — refuse it here so a
            # mislabeled construction can never masquerade as a hard stop.
            raise ValueError(
                f"StopState.trigger_price ({self.trigger_price}) must be "
                f"<= entry_fill_price ({self.entry_fill_price}) for a long hard stop."
            )

    @classmethod
    def at_entry(
        cls,
        *,
        mint: str,
        entry_fill_price: Decimal | int | str,
        thresholds: StopThresholds | None = None,
    ) -> StopState:
        """Construct the FIXED stop at entry time from the fill price."""
        th = thresholds or StopThresholds()
        entry = _to_decimal_price(entry_fill_price, "entry_fill_price")
        trigger = derive_stop_trigger_price(entry, th)
        return cls(
            mint=mint,
            entry_fill_price=entry,
            trigger_price=trigger,
            thresholds=th,
        )

    def is_breached(self, mark_price: Decimal | int | str) -> bool:
        """Pure, point-in-time breach test.

        We are LONG: the stop is breached when the point-in-time mark price has
        fallen TO OR BELOW the fixed trigger.  This is the ONLY decision the
        enforcer makes — deterministic, no IO, no LLM, no wall-clock.
        """
        mark = _to_decimal_price(mark_price, "mark_price")
        return mark <= self.trigger_price

    def tighten(self, proposed_trigger: Decimal | int | str) -> StopState:
        """Return a NEW StopState with a TIGHTER (higher) trigger — or raise.

        TIGHTEN-ONLY (non-waivable): a SELL stop tightens by RAISING its trigger
        toward the mark.  A proposed trigger that is LOWER than the current
        trigger would WIDEN the stop (more room to lose) and is REFUSED with a
        raise — there is no code path that lowers a stop.  This is the single
        capability a de-risk signal (incl. the LLM via the de-risk factory) may
        exert on a stop, and it is one-directional by construction.

        A tightened trigger must also remain at/below entry (it always will,
        since it is between the current trigger and entry).
        """
        proposed = _to_decimal_price(proposed_trigger, "proposed_trigger")
        if proposed < self.trigger_price:
            raise ValueError(
                f"tighten() refused: proposed trigger {proposed} is LOWER than the "
                f"current trigger {self.trigger_price}.  A stop may only be TIGHTENED "
                "(raised), never widened/lowered (non-waivable: no rule increases risk)."
            )
        if proposed == self.trigger_price:
            # No-op tighten is allowed (idempotent) but returns self unchanged.
            return self
        if proposed > self.entry_fill_price:
            # Tightening above entry would turn the hard stop into a take-profit.
            # Clamp the *meaning*: refuse — TP is a different rule (T-325).
            raise ValueError(
                f"tighten() refused: proposed trigger {proposed} exceeds entry "
                f"{self.entry_fill_price}; a hard stop cannot tighten above entry "
                "(that is a take-profit, owned by the exit engine, not the stop)."
            )
        return StopState(
            mint=self.mint,
            entry_fill_price=self.entry_fill_price,
            trigger_price=proposed,
            thresholds=self.thresholds,
        )
