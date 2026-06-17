"""T-321 PROPERTY / FUZZ proof — invariants over randomized stops + price paths.

No external fuzzing dependency (hypothesis is not vendored; matches the breaker
property tests).  Seeded `random` makes the run deterministic and reproducible.

Invariants (must hold for ALL inputs):
  S1  The breach decision is EXACT and monotone in the mark price: it fires iff
      mark <= trigger.  No float, no rounding drift (Decimal throughout).
  S2  TIGHTEN-ONLY: tighten() either raises the trigger or raises an error; it can
      NEVER lower the trigger (no rule increases risk).
  S3  A tightened stop is at least as protective: its trigger is >= the original,
      so any price that breached the original also breaches the tightened one.
  S4  Money discipline: every trigger price is a Decimal, never a float.
  S5  Off-box keeper / in-process enforcer agree on the SAME fixed trigger and
      therefore make the SAME breach decision for any mark.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from aats.contracts.venue import FillResult
from aats.risk.secondary_enforcer import MarkTick, SecondaryStopEnforcer
from aats.risk.survivable_stop import StopState, StopThresholds
from aats.risk.venue_native_stop import (
    SimulationRestingStopVenue,
    resting_order_from_stop,
)

MINT = "So1111111111111111111111111111111111111111"


def _rand_entry(rng: random.Random) -> Decimal:
    # Wide range of plausible SOL-lamports-per-token prices (integers + fractions).
    scale = rng.choice([1, 100, 10_000, 1_000_000])
    num = rng.randint(1, 10_000)
    return Decimal(num) / Decimal(scale) if rng.random() < 0.5 else Decimal(num * scale)


class _Rec:
    name = "rec"

    def __init__(self) -> None:
        self.exits = 0

    def exit(self, intent, position) -> FillResult:  # noqa: ANN001
        self.exits += 1
        return FillResult(landed=True, reason="sim_exit")


def test_S1_breach_is_exact_and_monotone():
    rng = random.Random(20260616)
    for _ in range(5000):
        entry = _rand_entry(rng)
        bps = rng.randint(1, 9999)
        stop = StopState.at_entry(
            mint=MINT, entry_fill_price=entry, thresholds=StopThresholds(hard_stop_drawdown_bps=bps)
        )
        trig = stop.trigger_price
        # mark strictly below trigger -> breach; strictly above -> safe; equal -> breach
        below = trig - (trig / Decimal(1000))
        above = trig + (trig / Decimal(1000))
        assert stop.is_breached(below) is True
        assert stop.is_breached(trig) is True
        assert stop.is_breached(above) is False


def test_S2_S3_tighten_only_and_more_protective():
    rng = random.Random(7)
    for _ in range(5000):
        entry = _rand_entry(rng)
        stop = StopState.at_entry(mint=MINT, entry_fill_price=entry)  # -40%
        trig = stop.trigger_price
        # propose a random new trigger; tighten() must accept iff trig <= proposed <= entry
        proposed = trig + (entry - trig) * Decimal(rng.random())  # in [trig, entry]
        # quantize to avoid silly precision; still a Decimal
        if proposed < trig:
            proposed = trig
        if proposed > entry:
            proposed = entry
        tightened = stop.tighten(proposed)
        assert tightened.trigger_price >= stop.trigger_price  # S2: never lower
        # S3: any mark breaching original also breaches tightened
        m = stop.trigger_price - Decimal("0.0001") * stop.trigger_price
        assert stop.is_breached(m) and tightened.is_breached(m)
        # A strictly-lower proposal always raises.
        lower = trig - (trig / Decimal(2))
        if lower > 0:
            with pytest.raises(ValueError):
                stop.tighten(lower)


def test_S4_no_float_trigger_ever():
    rng = random.Random(99)
    for _ in range(2000):
        stop = StopState.at_entry(mint=MINT, entry_fill_price=_rand_entry(rng))
        assert isinstance(stop.trigger_price, Decimal)
        assert not isinstance(stop.trigger_price, float)


def test_S5_offbox_and_inprocess_agree():
    rng = random.Random(123456)
    for _ in range(3000):
        entry = _rand_entry(rng)
        stop = StopState.at_entry(mint=MINT, entry_fill_price=entry)
        venue = SimulationRestingStopVenue()
        placed = venue.place_resting_stop(resting_order_from_stop(stop))
        enforcer = SecondaryStopEnforcer(_Rec())  # type: ignore[arg-type]
        enforcer.register(stop, object())
        # Random mark; both layers must agree on breach.
        mark = stop.trigger_price * Decimal(rng.choice(["0.5", "0.999", "1.0", "1.001", "2.0"]))
        in_process = enforcer.decide(MarkTick(mint=MINT, mark_price=mark, block_time_ms=1))
        off_box = mark <= placed.trigger_price
        assert in_process == off_box
