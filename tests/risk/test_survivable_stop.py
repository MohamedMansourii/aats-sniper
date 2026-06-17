"""T-321 — survivable-stop core proofs: rule, thresholds, enforcer, tighten-only.

Proves the deterministic rule and the in-process FAST-loop enforcer (Layer 2):
  - the FIXED trigger is derived correctly from the entry fill price;
  - the breach test is a pure point-in-time comparison;
  - a breach fires an ExitIntent (full flatten, de-risk) through the venue seam;
  - the stop is TIGHTEN-ONLY — a widen attempt RAISES (no rule increases risk);
  - money is integer/Decimal — a float price is REJECTED at every boundary;
  - the enforcer holds a DeRiskIntentFactory and is structurally unable to add
    risk (no entry() method).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aats.contracts.intents import DeRiskIntentFactory, ExitIntent
from aats.contracts.venue import FillResult
from aats.risk.secondary_enforcer import MarkTick, SecondaryStopEnforcer
from aats.risk.survivable_stop import (
    StopState,
    StopThresholds,
    derive_stop_trigger_price,
)

MINT = "So1111111111111111111111111111111111111111"


# ---------------------------------------------------------------------------
# A spy ExecutionVenue that records exit() calls (no network).
# ---------------------------------------------------------------------------
class _ExitSpyVenue:
    name = "exit-spy"

    def __init__(self) -> None:
        self.exits: list[tuple[object, object]] = []

    def exit(self, intent, position) -> FillResult:  # noqa: ANN001 - test stub
        self.exits.append((intent, position))
        return FillResult(landed=True, reason="sim_exit")

    # The enforcer only calls .exit(); the rest of the ABC is unused here.
    def __getattr__(self, name):  # pragma: no cover - defensive
        raise AttributeError(name)


# ---------------------------------------------------------------------------
# Rule + thresholds
# ---------------------------------------------------------------------------
class TestStopRuleThresholds:
    def test_default_trigger_is_40pct_below_entry(self):
        # entry 100 lamports/token, -40% => trigger 60.
        stop = StopState.at_entry(mint=MINT, entry_fill_price=100)
        assert stop.trigger_price == Decimal("60")
        assert stop.thresholds.hard_stop_drawdown_bps == 4000

    def test_derive_trigger_exact_decimal(self):
        th = StopThresholds(hard_stop_drawdown_bps=2500)  # -25%
        trig = derive_stop_trigger_price(Decimal("0.0008"), th)
        assert trig == Decimal("0.0008") * (Decimal("7500") / Decimal("10000"))

    def test_breach_is_point_in_time_pure(self):
        stop = StopState.at_entry(mint=MINT, entry_fill_price=100)
        assert stop.is_breached(60) is True  # at trigger -> breach
        assert stop.is_breached("59.999") is True  # below trigger -> breach
        assert stop.is_breached(61) is False  # above trigger -> safe
        # Pure: calling it twice never changes state.
        assert stop.is_breached(61) is False

    def test_float_price_rejected_everywhere(self):
        with pytest.raises(TypeError):
            StopState.at_entry(mint=MINT, entry_fill_price=100.0)
        stop = StopState.at_entry(mint=MINT, entry_fill_price=100)
        with pytest.raises(TypeError):
            stop.is_breached(60.0)
        with pytest.raises(TypeError):
            derive_stop_trigger_price(100.0, StopThresholds())
        with pytest.raises(TypeError):
            StopThresholds(hard_stop_drawdown_bps=4000.0)

    def test_trigger_above_entry_rejected(self):
        # A trigger above entry would be a take-profit, not a hard stop.
        with pytest.raises(ValueError):
            StopState(
                mint=MINT,
                entry_fill_price=Decimal("100"),
                trigger_price=Decimal("110"),
                thresholds=StopThresholds(),
            )


# ---------------------------------------------------------------------------
# TIGHTEN-ONLY — the non-waivable "no rule increases risk" proof
# ---------------------------------------------------------------------------
class TestTightenOnly:
    def test_tighten_raises_trigger(self):
        stop = StopState.at_entry(mint=MINT, entry_fill_price=100)  # trigger 60
        tightened = stop.tighten(80)
        assert tightened.trigger_price == Decimal("80")
        # original is immutable / unchanged
        assert stop.trigger_price == Decimal("60")

    def test_widen_attempt_raises(self):
        stop = StopState.at_entry(mint=MINT, entry_fill_price=100)  # trigger 60
        with pytest.raises(ValueError, match="only be TIGHTENED"):
            stop.tighten(50)  # lower trigger == wider stop == more room to lose

    def test_tighten_above_entry_rejected(self):
        stop = StopState.at_entry(mint=MINT, entry_fill_price=100)
        with pytest.raises(ValueError, match="cannot tighten above entry"):
            stop.tighten(120)

    def test_idempotent_tighten_returns_self(self):
        stop = StopState.at_entry(mint=MINT, entry_fill_price=100)
        same = stop.tighten(stop.trigger_price)
        assert same.trigger_price == stop.trigger_price


# ---------------------------------------------------------------------------
# Layer 2 — in-process FAST-loop enforcer
# ---------------------------------------------------------------------------
class TestSecondaryEnforcer:
    def _enforcer(self) -> tuple[SecondaryStopEnforcer, _ExitSpyVenue]:
        venue = _ExitSpyVenue()
        enf = SecondaryStopEnforcer(venue)  # type: ignore[arg-type]
        stop = StopState.at_entry(mint=MINT, entry_fill_price=100)
        enf.register(stop, position=object())
        return enf, venue

    def test_no_fire_above_trigger(self):
        enf, venue = self._enforcer()
        tick = MarkTick(mint=MINT, mark_price=Decimal("70"), block_time_ms=1_700_000_000_000)
        assert enf.decide(tick) is False
        assert enf.on_tick(tick) is None
        assert venue.exits == []

    def test_fire_on_breach_emits_full_exit(self):
        enf, venue = self._enforcer()
        tick = MarkTick(mint=MINT, mark_price=Decimal("59"), block_time_ms=1_700_000_000_000)
        assert enf.decide(tick) is True
        fill = enf.on_tick(tick)
        assert fill is not None and fill.landed
        assert len(venue.exits) == 1
        intent, _pos = venue.exits[0]
        assert isinstance(intent, ExitIntent)
        assert intent.fraction_bps == 10_000  # full flatten on a hard-stop breach
        assert intent.mint == MINT
        # Deterministic, event-time-keyed id (idempotent replay).
        assert intent.client_intent_id == f"stop_exit:{MINT}:1700000000000"

    def test_stop_does_not_refire_after_breach(self):
        enf, venue = self._enforcer()
        t1 = MarkTick(mint=MINT, mark_price=Decimal("59"), block_time_ms=1_700_000_000_000)
        enf.on_tick(t1)
        t2 = MarkTick(mint=MINT, mark_price=Decimal("10"), block_time_ms=1_700_000_001_000)
        assert enf.on_tick(t2) is None  # already handed off, deregistered
        assert len(venue.exits) == 1

    def test_unregistered_mint_never_fires(self):
        enf, venue = self._enforcer()
        tick = MarkTick(mint="other", mark_price=Decimal("0"), block_time_ms=1_700_000_000_000)
        assert enf.decide(tick) is False
        assert enf.on_tick(tick) is None
        assert venue.exits == []

    def test_enforcer_factory_has_no_entry(self):
        enf, _venue = self._enforcer()
        # Structural: the enforcer's factory cannot construct an EntryIntent.
        assert not hasattr(DeRiskIntentFactory, "entry")
        assert not hasattr(enf._factory, "entry")

    def test_enforcer_tighten_only(self):
        enf, _venue = self._enforcer()
        tightened = enf.tighten(MINT, 80)
        assert tightened.trigger_price == Decimal("80")
        with pytest.raises(ValueError, match="only be TIGHTENED"):
            enf.tighten(MINT, 50)

    def test_marktick_rejects_float(self):
        with pytest.raises(TypeError):
            MarkTick(mint=MINT, mark_price=59.0, block_time_ms=1)  # type: ignore[arg-type]
