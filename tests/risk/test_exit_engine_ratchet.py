"""AUDIT-ratchet — the trailing PROFIT-LOCK RATCHET is a RATCHET, not a fixed -20%.

The directive: confirm the stepped profit-lock ratchet shape — breakeven at 2x,
lock 2x at 3x, with rungs below — and prove it RATCHETS UP at discrete rungs and
NEVER widens.  These tests are mutation-meaningful: each asserts an exact locked
floor at an exact rung, so flipping a `>=`/`<=`, lowering a `lock_r`, or removing
a rung makes a test fail.

Distinct from the proportional `trail_drawdown_bps` trail (tested elsewhere): the
ratchet locks a FIXED multiple of entry the instant the PEAK crosses a rung, and
holds that floor through any subsequent give-back.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aats.contracts.intents import ExitIntent
from aats.risk.exit_engine import (
    AUTO_STRAT_PRESETS,
    ExitConfig,
    ExitEngine,
    ExitReason,
    ExitState,
    MevExitMode,
    RatchetRung,
    TpRung,
    preset,
)

ENTRY = Decimal("100")
BT = 1_700_000_000_000


def _engine(label: str = "secure_balanced") -> ExitEngine:
    return ExitEngine(config=preset(label))


def _state(engine: ExitEngine) -> ExitState:
    return ExitState.at_entry(mint="M", entry_fill_price=ENTRY, config=engine.config)


def _tick(engine, state, mult, *, bt=BT):
    return engine.on_tick(state, mark_price=ENTRY * Decimal(str(mult)), block_time_ms=bt)


# --------------------------------------------------------------------------- #
# The canonical ratchet shape lives in the preset.
# --------------------------------------------------------------------------- #


def test_secure_balanced_ratchet_has_breakeven_at_2x_and_lock_2x_at_3x():
    """The audit's canonical shape is encoded in the default preset's ladder."""
    cfg = preset("secure_balanced")
    rungs = {r.at_r: r.lock_r for r in cfg.ratchet_ladder}
    # breakeven at 2x (the trail-arm) — lock entry once you double.
    assert rungs[Decimal("2.0")] == Decimal("1.0")
    # lock 2x at 3x — once you triple, two-bag profit is guaranteed.
    assert rungs[Decimal("3.0")] == Decimal("2.0")
    # a rung BELOW breakeven exists (rungs below).
    assert any(r.at_r < Decimal("2.0") for r in cfg.ratchet_ladder)
    # and a higher rung above 3x keeps ratcheting.
    assert any(r.at_r > Decimal("3.0") for r in cfg.ratchet_ladder)


# --------------------------------------------------------------------------- #
# The floor ratchets UP at the discrete rungs (not a fixed band).
# --------------------------------------------------------------------------- #


def test_floor_ratchets_to_breakeven_when_peak_hits_2x():
    eng = _engine()
    st = _state(eng)
    # Below 2x: the 1.5x rung engaged (lock 0.9x) but not breakeven yet.
    st = _tick(eng, st, "1.5").next_state
    assert st.locked_floor_r == Decimal("0.9")
    # Cross 2x: the floor ratchets to breakeven (1.0x of entry).
    st = _tick(eng, st, "2.0").next_state
    assert st.locked_floor_r == Decimal("1.0")


def test_floor_ratchets_to_2x_when_peak_hits_3x():
    eng = _engine()
    st = _state(eng)
    st = _tick(eng, st, "2.0").next_state
    st = _tick(eng, st, "3.0").next_state
    # Once the peak triples, the locked floor is 2.0x of entry (two-bag locked).
    assert st.locked_floor_r == Decimal("2.0")


def test_single_jump_past_several_rungs_locks_the_highest_floor():
    """A jump straight to 7x engages every rung up to 6x and locks the 6x floor."""
    eng = _engine()
    st = _state(eng)
    d = _tick(eng, st, "7.0")
    # 6x rung locks 4.0x — the highest engaged floor.
    assert d.next_state.locked_floor_r == Decimal("4.0")


# --------------------------------------------------------------------------- #
# The ratchet fires a full de-risk exit when price falls back to the locked floor.
# --------------------------------------------------------------------------- #


def test_ratchet_fires_full_exit_at_locked_breakeven_after_2x():
    """After the peak doubled (floor = breakeven 1.0x), a fall to entry force-exits
    via the RATCHET (you double, you do not give it all back)."""
    eng = _engine()
    st = _state(eng)
    st = _tick(eng, st, "2.0").next_state  # floor -> 1.0x
    assert st.locked_floor_r == Decimal("1.0")
    d = _tick(eng, st, "1.0")  # back to entry — at the locked floor
    assert d.reason is ExitReason.RATCHET_STOP
    assert isinstance(d.intent, ExitIntent)
    assert d.next_state.remaining_bps == 0
    assert d.exit_mode is MevExitMode.SECURE  # defensive exit is private


def test_ratchet_fires_full_exit_at_locked_2x_after_3x():
    """After the peak tripled (floor = 2.0x), a fall to 2x force-exits the locked
    two-bag profit — BEFORE the -40% hard stop and proven by an exact rung."""
    eng = _engine()
    st = _state(eng)
    st = _tick(eng, st, "3.0").next_state  # floor -> 2.0x
    assert st.locked_floor_r == Decimal("2.0")
    d = _tick(eng, st, "2.0")  # down to the locked 2x floor
    assert d.reason is ExitReason.RATCHET_STOP
    assert d.next_state.remaining_bps == 0


def test_ratchet_holds_just_above_the_locked_floor():
    """A hair ABOVE the locked floor must HOLD — no spurious dump of a healthy
    position (mutation guard on the <= comparison)."""
    eng = _engine()
    st = _state(eng)
    st = _tick(eng, st, "3.0").next_state  # floor -> 2.0x
    d = _tick(eng, st, "2.0000001")  # a hair above the 2x floor
    assert d.reason is not ExitReason.RATCHET_STOP


# --------------------------------------------------------------------------- #
# NEVER WIDENS — the locked floor only rises; a give-back never lowers it.
# --------------------------------------------------------------------------- #


def test_floor_never_widens_after_a_giveback():
    """Climb to 7x (floor 4.0x via the 6x rung), give back to 4.5x WITHOUT
    re-firing: the floor must stay 4.0x — it does not fall back to a lower rung."""
    eng = _engine()
    st = _state(eng)
    st = _tick(eng, st, "7.0").next_state
    assert st.locked_floor_r == Decimal("4.0")  # 6x rung lock (highest engaged)
    # Give back to 4.5x — still above the 4.0x floor, no exit, floor unchanged.
    d = _tick(eng, st, "4.5")
    assert d.reason is not ExitReason.RATCHET_STOP
    assert d.next_state.locked_floor_r == Decimal("4.0")  # never widened down


def test_floor_is_monotone_across_a_chop_path():
    eng = _engine()
    st = _state(eng)
    prev = st.locked_floor_r
    for mult in ["1.6", "2.2", "1.9", "3.3", "2.6", "5.5", "4.0"]:
        d = _tick(eng, st, mult)
        st = d.next_state
        assert st.locked_floor_r >= prev  # monotone non-decreasing
        prev = st.locked_floor_r
        if st.remaining_bps == 0:
            break


def test_replace_refuses_to_lower_the_floor():
    """Defense in depth: a state transition may NEVER lower locked_floor_r."""
    eng = _engine()
    st = _tick(eng, _state(eng), "3.0").next_state
    assert st.locked_floor_r == Decimal("2.0")
    with pytest.raises(ValueError, match="locked_floor_r may only RISE"):
        st._replace(locked_floor_r=Decimal("1.0"))


# --------------------------------------------------------------------------- #
# It is NOT a fixed -20% band — the locked floor is a fixed multiple of ENTRY,
# independent of the proportional trail, and rises in discrete steps.
# --------------------------------------------------------------------------- #


def test_ratchet_is_not_a_fixed_percentage_of_peak():
    """At a 3x peak a fixed -20%-of-peak trail would protect 2.4x; the ratchet
    locks a DIFFERENT, fixed-of-entry 2.0x.  Proving the two are distinct floors
    (the ratchet is not merely a renamed proportional trail)."""
    eng = _engine()
    st = _tick(eng, _state(eng), "3.0").next_state
    fixed_20pct_of_peak = st.peak_r * Decimal("0.80")  # 2.4x
    assert st.locked_floor_r == Decimal("2.0")
    assert st.locked_floor_r != fixed_20pct_of_peak


# --------------------------------------------------------------------------- #
# RatchetRung / ExitConfig validation — the ratchet cannot be built to widen.
# --------------------------------------------------------------------------- #


def test_ratchet_rung_rejects_lock_at_or_above_arm():
    with pytest.raises(ValueError, match="must be < at_r"):
        RatchetRung(at_r=Decimal("3.0"), lock_r=Decimal("3.0"))
    with pytest.raises(ValueError, match="must be < at_r"):
        RatchetRung(at_r=Decimal("3.0"), lock_r=Decimal("4.0"))


def test_ratchet_rung_rejects_arm_at_or_below_entry():
    with pytest.raises(ValueError, match="must be > 1"):
        RatchetRung(at_r=Decimal("1.0"), lock_r=Decimal("0.5"))


def test_ratchet_rung_rejects_float():
    with pytest.raises(TypeError):
        RatchetRung(at_r=2.0, lock_r=Decimal("1.0"))  # type: ignore[arg-type]


def test_config_rejects_non_ascending_ratchet_at_r():
    with pytest.raises(ValueError, match="ascending in at_r"):
        ExitConfig(
            label="bad",
            tp_ladder=(TpRung(Decimal("1.5"), 3000),),
            hard_stop_r=Decimal("0.60"),
            trail_arm_r=Decimal("2.0"),
            trail_drawdown_bps=3000,
            max_hold_steps=24,
            ratchet_ladder=(
                RatchetRung(at_r=Decimal("3.0"), lock_r=Decimal("1.0")),
                RatchetRung(at_r=Decimal("2.0"), lock_r=Decimal("1.5")),
            ),
        )


def test_config_rejects_non_ascending_ratchet_lock_r():
    """A higher rung that locks a LOWER floor is a widen-in-disguise — rejected."""
    with pytest.raises(ValueError, match="ascending in lock_r"):
        ExitConfig(
            label="bad",
            tp_ladder=(TpRung(Decimal("1.5"), 3000),),
            hard_stop_r=Decimal("0.60"),
            trail_arm_r=Decimal("2.0"),
            trail_drawdown_bps=3000,
            max_hold_steps=24,
            ratchet_ladder=(
                RatchetRung(at_r=Decimal("2.0"), lock_r=Decimal("1.5")),
                RatchetRung(at_r=Decimal("3.0"), lock_r=Decimal("1.0")),  # lower lock!
            ),
        )


# --------------------------------------------------------------------------- #
# Every preset's ratchet (where present) is ascending and de-risk-shaped.
# --------------------------------------------------------------------------- #


def test_all_preset_ratchets_are_monotone_and_below_their_arm():
    for cfg in AUTO_STRAT_PRESETS.values():
        prev_at = Decimal(0)
        prev_lock = Decimal(0)
        for rr in cfg.ratchet_ladder:
            assert rr.at_r > prev_at
            assert rr.lock_r > prev_lock
            assert rr.lock_r < rr.at_r  # cannot force an instant exit
            prev_at, prev_lock = rr.at_r, rr.lock_r


def test_disabled_ratchet_falls_back_to_proportional_trail_only():
    """A config with NO ratchet rungs never engages the ratchet branch; the floor
    stays 0 and the proportional trail alone governs the runner."""
    cfg = ExitConfig(
        label="no_ratchet",
        tp_ladder=(TpRung(Decimal("1.5"), 3000),),
        hard_stop_r=Decimal("0.60"),
        trail_arm_r=Decimal("2.0"),
        trail_drawdown_bps=3000,
        max_hold_steps=24,
        ratchet_ladder=(),
    )
    eng = ExitEngine(config=cfg)
    st = ExitState.at_entry(mint="M", entry_fill_price=ENTRY, config=cfg)
    st = eng.on_tick(st, mark_price=ENTRY * Decimal("3.0"), block_time_ms=BT).next_state
    assert st.locked_floor_r == Decimal(0)  # never engaged
    # A fall to 1.0x with no ratchet does NOT fire a ratchet stop (the proportional
    # trail at 30% of peak-3x = 2.1x fires the TRAILING stop instead).
    d = eng.on_tick(st, mark_price=ENTRY * Decimal("1.0"), block_time_ms=BT)
    assert d.reason is not ExitReason.RATCHET_STOP
