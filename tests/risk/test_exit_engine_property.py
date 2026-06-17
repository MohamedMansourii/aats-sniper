"""T-325 PROPERTY / FUZZ + FAULT INJECTION — the ExitEngine invariants hold under
any input, any path, any preset.

No external fuzzer (hypothesis not vendored); seeded `random` for determinism,
matching the established T-320/T-323/T-324 property-test style.  We fuzz the price
path, the preset, the narrative flag, and the tick order to EXTREMES and assert
the non-waivable invariants that make this engine safe:

  E1  remaining_bps is MONOTONE NON-INCREASING across any walk (de-risk only).
  E2  the trailing floor NEVER widens — peak_r is monotone non-decreasing.
  E8  the PROFIT-LOCK RATCHET floor (locked_floor_r) NEVER widens — it is monotone
      non-decreasing across any path; a ratchet breach always full-exits.
  E3  every emitted intent is de-risk (ExitIntent/ReduceIntent) with
      1 <= fraction_bps <= 10000 — NEVER an EntryIntent, never an increase.
  E4  a hard-stop breach ALWAYS full-exits the remainder (no silent skip).
  E5  a fired position stays flat (idempotent — a closed position holds).
  E6  defensive exits (stop/trailing/timeout/narrative) ALWAYS route SECURE.
  E7  the decision is a PURE function of (config, state, mark) — re-evaluating
      the same (state, mark) yields the same reason + intent.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from aats.contracts.intents import ExitIntent, ReduceIntent
from aats.risk.exit_engine import (
    AUTO_STRAT_PRESETS,
    ExitEngine,
    ExitReason,
    ExitState,
    MevExitMode,
    preset,
)

ENTRY = Decimal("100")
BT = 1_700_000_000_000

_DEFENSIVE = {
    ExitReason.HARD_STOP,
    ExitReason.RATCHET_STOP,
    ExitReason.TRAILING_STOP,
    ExitReason.TIMEOUT,
    ExitReason.NARRATIVE_FAILURE,
}


def _random_path(rng: random.Random, n: int) -> list[Decimal]:
    """A wild price path in multiples of entry — spikes, crashes, chop."""
    out: list[Decimal] = []
    for _ in range(n):
        mult = rng.choice(
            [
                rng.uniform(0.001, 0.6),  # crash / rug territory
                rng.uniform(0.6, 1.5),  # chop near entry
                rng.uniform(1.5, 12.0),  # runner spikes
            ]
        )
        out.append(Decimal(str(mult)).quantize(Decimal("0.000001")))
    return out


def test_property_invariants_hold_under_fuzz():
    rng = random.Random(325)
    labels = list(AUTO_STRAT_PRESETS)

    for _trial in range(3000):
        label = rng.choice(labels)
        eng = ExitEngine(config=preset(label))
        st = ExitState.at_entry(mint="M", entry_fill_price=ENTRY, config=eng.config)
        path = _random_path(rng, rng.randint(1, 40))
        narrative_at = rng.choice([None, None, None, rng.randint(0, len(path) - 1)])

        prev_remaining = st.remaining_bps
        prev_peak = st.peak_r
        prev_floor = st.locked_floor_r
        flat = False

        for step, mult in enumerate(path):
            mark = ENTRY * mult
            flag = narrative_at is not None and step == narrative_at
            d = eng.on_tick(
                st, mark_price=mark, block_time_ms=BT + step, narrative_failure_flag=flag
            )

            # E1: remaining never grows.
            assert d.next_state.remaining_bps <= prev_remaining
            prev_remaining = d.next_state.remaining_bps

            # E2: peak never recedes (trailing floor never widens).
            assert d.next_state.peak_r >= prev_peak
            prev_peak = d.next_state.peak_r

            # E8: the profit-lock ratchet floor only ever rises (never widens).
            assert d.next_state.locked_floor_r >= prev_floor
            prev_floor = d.next_state.locked_floor_r

            # E8: a ratchet-stop breach full-exits the remainder (no silent skip).
            if d.reason is ExitReason.RATCHET_STOP:
                assert isinstance(d.intent, ExitIntent)
                assert d.next_state.remaining_bps == 0

            # E3: every emitted intent is a bounded de-risk reduction.
            if d.intent is not None:
                assert isinstance(d.intent, ExitIntent | ReduceIntent)
                assert 1 <= d.intent.fraction_bps <= 10_000

            # E4: a hard-stop breach (with no higher rule) full-exits.
            if d.reason is ExitReason.HARD_STOP:
                assert isinstance(d.intent, ExitIntent)
                assert d.next_state.remaining_bps == 0

            # E5: once flat, stays flat and holds.
            if flat:
                assert d.intent is None
                assert d.next_state.remaining_bps == 0
            if d.next_state.remaining_bps == 0:
                flat = True

            # E6: defensive exits route SECURE.
            if d.reason in _DEFENSIVE and d.intent is not None:
                assert d.exit_mode is MevExitMode.SECURE
                assert d.intent.exit_mode == "secure"

            # E7: pure — re-evaluating the SAME (state, mark) reproduces it.
            d2 = eng.on_tick(
                st, mark_price=mark, block_time_ms=BT + step, narrative_failure_flag=flag
            )
            assert d2.reason == d.reason
            assert d2.intent == d.intent

            st = d.next_state


def test_fault_injection_extreme_marks_never_crash_or_widen():
    """FAULT INJECTION: feed degenerate marks (tiny, huge, exactly-trigger) and
    assert the engine never raises on a valid Decimal and never widens risk."""
    eng = preset("secure_balanced")
    engine = ExitEngine(config=eng)
    extremes = [
        Decimal("0.000000001"),  # near-zero (deep rug)
        Decimal("1"),  # below entry
        Decimal("60"),  # exactly the hard-stop trigger
        Decimal("60.00000001"),  # a hair above the trigger (must NOT fire)
        Decimal("100"),  # entry
        Decimal("1000000"),  # 10000x spike
    ]
    st = ExitState.at_entry(mint="M", entry_fill_price=ENTRY, config=eng)
    for i, mark in enumerate(extremes):
        d = engine.on_tick(st, mark_price=mark, block_time_ms=BT + i)
        assert d.next_state.remaining_bps <= st.remaining_bps
        st = d.next_state
        if st.remaining_bps == 0:
            break


def test_fault_injection_hair_above_trigger_does_not_fire():
    """The hard stop fires at-or-below the trigger; one ulp above must HOLD (no
    spurious exit, no off-by-one that would dump a healthy position)."""
    engine = ExitEngine(config=preset("secure_balanced"))
    st = ExitState.at_entry(mint="M", entry_fill_price=ENTRY, config=engine.config)
    d = engine.on_tick(st, mark_price=Decimal("60.00000001"), block_time_ms=BT)
    assert d.reason is ExitReason.NONE  # holds — above the trigger


def test_fault_injection_negative_block_time_rejected():
    engine = ExitEngine(config=preset("secure_balanced"))
    st = ExitState.at_entry(mint="M", entry_fill_price=ENTRY, config=engine.config)
    with pytest.raises(ValueError):
        engine.on_tick(st, mark_price=Decimal("100"), block_time_ms=-1)


def test_fault_injection_float_anywhere_rejected():
    """A float mark / config value is rejected at the boundary — float money can
    never enter the exit math (data-models.md §0)."""
    engine = ExitEngine(config=preset("secure_balanced"))
    st = ExitState.at_entry(mint="M", entry_fill_price=ENTRY, config=engine.config)
    with pytest.raises(TypeError):
        engine.on_tick(st, mark_price=0.5, block_time_ms=BT)
    with pytest.raises(TypeError):
        ExitState.at_entry(mint="M", entry_fill_price=100.0, config=engine.config)


def test_property_cumulative_scale_out_never_exceeds_position():
    """Across any path, the SUM of basis sold (relative to original) never exceeds
    100% — the engine cannot sell more than the position holds."""
    rng = random.Random(99)
    engine = ExitEngine(config=preset("secure_runner"))
    for _ in range(500):
        st = ExitState.at_entry(mint="M", entry_fill_price=ENTRY, config=engine.config)
        path = _random_path(rng, rng.randint(1, 40))
        sold_original_bps = 0
        for step, mult in enumerate(path):
            before = st.remaining_bps
            d = engine.on_tick(st, mark_price=ENTRY * mult, block_time_ms=BT + step)
            after = d.next_state.remaining_bps
            sold_original_bps += before - after  # basis (of original) sold this tick
            st = d.next_state
            if after == 0:
                break
        assert sold_original_bps <= 10_000
