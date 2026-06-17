"""E12 — Time-stop / stale-narrative EXIT (ExitEngine).

PROVES the enhancement's contract (de-risk only, configurable N, event-time):

  * A position FLAT (mark inside the flat band of entry) for >= N event-time hours
    AND whose SLOW-loop narrative score has COOLED to/below the threshold is
    force-exited (full remainder, SECURE routing).
  * BOTH conditions are required — flat-but-still-hot HOLDS; cooled-but-moving HOLDS.
  * N is CONFIGURABLE (time_stop_hours); the branch is DISABLED (None) by default so
    existing presets are unchanged.
  * The clock is EVENT-TIME (block_time_ms deltas) — no wall-clock / compute-time
    leak.  A mark that LEAVES the flat band resets the flat clock (only quiet runs
    accumulate flat time).
  * DE-RISK ONLY: the sole outcome is a full exit; remaining_bps only ever shrinks.
    The narrative score is a pre-computed SLOW-loop input — never an LLM call here,
    never a buy trigger, never sizes up or extends a hold.
  * Priority: higher rules (narrative_failure, hard stop, trailing, timeout) still
    win when they co-fire.
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
    TpRung,
    preset,
)

ENTRY = Decimal("100")
HOUR_MS = 3_600_000
BT0 = 1_700_000_000_000


def _ts_config(
    *,
    hours: int | None = 6,
    band_bps: int = 1_500,
    cooled_at: Decimal = Decimal("0.2"),
    max_hold_steps: int = 100_000,  # high so the step-timeout never pre-empts the time-stop
) -> ExitConfig:
    return ExitConfig(
        label="ts_test",
        tp_ladder=(TpRung(trigger_r=Decimal("1.5"), fraction_bps=3000),),
        hard_stop_r=Decimal("0.60"),
        trail_arm_r=Decimal("2.0"),
        trail_drawdown_bps=3000,
        max_hold_steps=max_hold_steps,
        time_stop_hours=hours,
        time_stop_flat_band_bps=band_bps,
        time_stop_narrative_cooled_at=cooled_at,
        default_exit_mode=MevExitMode.SECURE,
    )


def _engine(**kw) -> ExitEngine:
    return ExitEngine(config=_ts_config(**kw))


def _state(eng: ExitEngine) -> ExitState:
    return ExitState.at_entry(mint="M", entry_fill_price=ENTRY, config=eng.config)


# --------------------------------------------------------------------------- #
# Core: flat N hours AND cooled -> EXIT.
# --------------------------------------------------------------------------- #


def test_flat_and_cooled_triggers_time_stop_exit():
    """A position flat at entry for >= N hours with a cooled narrative force-exits."""
    eng = _engine(hours=6)
    st = _state(eng)
    # Tick 1 anchors the flat clock at BT0 (mark == entry, in-band).
    d = eng.on_tick(
        st,
        mark_price=ENTRY,
        block_time_ms=BT0,
        narrative_score=Decimal("0.1"),
    )
    assert d.reason is ExitReason.NONE  # 0 hours elapsed -> hold
    st = d.next_state
    # Tick 2 at +6h, still flat (mark == entry), narrative still cold -> EXIT.
    d2 = eng.on_tick(
        st,
        mark_price=ENTRY,
        block_time_ms=BT0 + 6 * HOUR_MS,
        narrative_score=Decimal("0.1"),
    )
    assert d2.reason is ExitReason.STALE_NARRATIVE_TIME_STOP
    assert isinstance(d2.intent, ExitIntent)
    assert d2.fraction_bps == 10_000  # full remainder
    assert d2.next_state.remaining_bps == 0
    # Defensive exit always routes SECURE — never sandwiched on the way out.
    assert d2.exit_mode is MevExitMode.SECURE
    assert d2.intent.exit_mode == "secure"


def test_just_under_N_hours_holds():
    """One ms short of N event-time hours must NOT fire (off-by-one safety)."""
    eng = _engine(hours=6)
    st = _state(eng)
    st = eng.on_tick(
        st, mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("0")
    ).next_state
    d = eng.on_tick(
        st,
        mark_price=ENTRY,
        block_time_ms=BT0 + 6 * HOUR_MS - 1,
        narrative_score=Decimal("0"),
    )
    assert d.reason is ExitReason.NONE  # one ms short -> hold


def test_configurable_N_hours():
    """N is configurable: a 2h horizon fires at +2h where a 6h horizon would not."""
    st_t = BT0 + 2 * HOUR_MS
    # 2h horizon: fires.
    eng2 = _engine(hours=2)
    s2 = _state(eng2)
    s2 = eng2.on_tick(
        s2, mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("0")
    ).next_state
    d2 = eng2.on_tick(s2, mark_price=ENTRY, block_time_ms=st_t, narrative_score=Decimal("0"))
    assert d2.reason is ExitReason.STALE_NARRATIVE_TIME_STOP
    # 6h horizon: same elapsed time holds.
    eng6 = _engine(hours=6)
    s6 = _state(eng6)
    s6 = eng6.on_tick(
        s6, mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("0")
    ).next_state
    d6 = eng6.on_tick(s6, mark_price=ENTRY, block_time_ms=st_t, narrative_score=Decimal("0"))
    assert d6.reason is ExitReason.NONE


# --------------------------------------------------------------------------- #
# BOTH conditions required.
# --------------------------------------------------------------------------- #


def test_flat_but_narrative_still_hot_holds():
    """Flat for N hours but the story is STILL HOT (score above threshold) -> HOLD.
    The time-stop never dumps a live narrative."""
    eng = _engine(hours=6, cooled_at=Decimal("0.2"))
    st = _state(eng)
    st = eng.on_tick(
        st, mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("0.9")
    ).next_state
    d = eng.on_tick(
        st,
        mark_price=ENTRY,
        block_time_ms=BT0 + 12 * HOUR_MS,
        narrative_score=Decimal("0.9"),  # still hot
    )
    assert d.reason is ExitReason.NONE


def test_cooled_but_price_moving_resets_flat_clock():
    """A cooled narrative but a MOVING price (mark leaves the flat band) resets the
    flat clock — the time-stop does not fire on a position that is still trading."""
    eng = _engine(hours=6, band_bps=1_500)  # +/-15% band
    st = _state(eng)
    st = eng.on_tick(
        st, mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("0")
    ).next_state
    # +3h: a real move to 1.30x (outside +/-15%) resets last_active to this time.
    st = eng.on_tick(
        st,
        mark_price=ENTRY * Decimal("1.30"),
        block_time_ms=BT0 + 3 * HOUR_MS,
        narrative_score=Decimal("0"),
    ).next_state
    # +7h overall is only +4h since the move -> not yet N hours flat -> HOLD.
    d = eng.on_tick(
        st,
        mark_price=ENTRY,
        block_time_ms=BT0 + 7 * HOUR_MS,
        narrative_score=Decimal("0"),
    )
    assert d.reason is ExitReason.NONE
    # But +6h *after the move* (BT0 + 9h) with renewed flatness -> EXIT.
    d2 = eng.on_tick(
        d.next_state,
        mark_price=ENTRY,
        block_time_ms=BT0 + 9 * HOUR_MS,
        narrative_score=Decimal("0"),
    )
    assert d2.reason is ExitReason.STALE_NARRATIVE_TIME_STOP


def test_no_narrative_read_never_forces_exit():
    """A missing SLOW-loop narrative read (None) NEVER forces a time-stop — fail-safe
    (absence of a cool signal is not a cool signal)."""
    eng = _engine(hours=6)
    st = _state(eng)
    st = eng.on_tick(st, mark_price=ENTRY, block_time_ms=BT0, narrative_score=None).next_state
    d = eng.on_tick(
        st,
        mark_price=ENTRY,
        block_time_ms=BT0 + 100 * HOUR_MS,
        narrative_score=None,
    )
    assert d.reason is ExitReason.NONE


# --------------------------------------------------------------------------- #
# Disabled by default / opt-in.
# --------------------------------------------------------------------------- #


def test_time_stop_disabled_by_default_on_existing_presets():
    """Existing presets do NOT enable the time-stop — even flat-forever + ice-cold
    narrative does not fire on secure_balanced (the A/B preset is unchanged)."""
    eng = ExitEngine(config=preset("secure_balanced"))
    assert eng.config.time_stop_hours is None
    st = ExitState.at_entry(mint="M", entry_fill_price=ENTRY, config=eng.config)
    st = eng.on_tick(
        st, mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("0")
    ).next_state
    d = eng.on_tick(
        st,
        mark_price=ENTRY,
        block_time_ms=BT0 + 1_000 * HOUR_MS,
        narrative_score=Decimal("0"),
    )
    assert d.reason is not ExitReason.STALE_NARRATIVE_TIME_STOP


def test_timestop_preset_is_registered_and_enabled():
    """The opt-in preset exists, enables the time-stop, and is de-risk shaped."""
    cfg = AUTO_STRAT_PRESETS["secure_balanced_timestop"]
    assert cfg.time_stop_hours == 6
    assert cfg.time_stop_narrative_cooled_at == Decimal("0.2")
    assert cfg.default_exit_mode is MevExitMode.SECURE
    # Same TP geometry as secure_balanced (A/B unchanged on hot paths).
    assert cfg.tp_ladder == AUTO_STRAT_PRESETS["secure_balanced"].tp_ladder


# --------------------------------------------------------------------------- #
# Priority — higher rules win when they co-fire.
# --------------------------------------------------------------------------- #


def test_hard_stop_beats_time_stop():
    """If the mark also breaches the hard stop, HARD_STOP is reported (higher)."""
    eng = _engine(hours=6)
    st = _state(eng)
    st = eng.on_tick(
        st, mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("0")
    ).next_state
    # +6h flat+cooled would time-stop, but mark also breaches the -40% hard stop.
    d = eng.on_tick(
        st,
        mark_price=Decimal("50"),
        block_time_ms=BT0 + 6 * HOUR_MS,
        narrative_score=Decimal("0"),
    )
    assert d.reason is ExitReason.HARD_STOP


def test_narrative_failure_beats_time_stop():
    """The catastrophic narrative_failure flag (highest) wins over the time-stop."""
    eng = _engine(hours=6)
    st = _state(eng)
    st = eng.on_tick(
        st, mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("0")
    ).next_state
    d = eng.on_tick(
        st,
        mark_price=ENTRY,
        block_time_ms=BT0 + 6 * HOUR_MS,
        narrative_failure_flag=True,
        narrative_score=Decimal("0"),
    )
    assert d.reason is ExitReason.NARRATIVE_FAILURE


def test_step_timeout_beats_time_stop_when_both_due():
    """The unconditional max-hold step timeout is a hard cap above the time-stop."""
    eng = _engine(hours=6, max_hold_steps=1)
    st = _state(eng)
    # Tick 1 anchors AND reaches steps_held=1 == max_hold_steps -> TIMEOUT first.
    d = eng.on_tick(st, mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("0"))
    assert d.reason is ExitReason.TIMEOUT


# --------------------------------------------------------------------------- #
# De-risk only / type guards.
# --------------------------------------------------------------------------- #


def test_time_stop_is_de_risk_only_remaining_shrinks():
    eng = _engine(hours=6)
    st = _state(eng)
    st = eng.on_tick(
        st, mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("0")
    ).next_state
    before = st.remaining_bps
    d = eng.on_tick(
        st, mark_price=ENTRY, block_time_ms=BT0 + 6 * HOUR_MS, narrative_score=Decimal("0")
    )
    assert d.next_state.remaining_bps <= before
    assert d.next_state.remaining_bps == 0  # full exit


def test_float_narrative_score_rejected():
    eng = _engine(hours=6)
    with pytest.raises(TypeError):
        eng.on_tick(_state(eng), mark_price=ENTRY, block_time_ms=BT0, narrative_score=0.5)  # type: ignore[arg-type]


def test_bool_narrative_score_rejected():
    eng = _engine(hours=6)
    with pytest.raises(TypeError):
        eng.on_tick(_state(eng), mark_price=ENTRY, block_time_ms=BT0, narrative_score=True)  # type: ignore[arg-type]


def test_out_of_range_narrative_score_rejected():
    eng = _engine(hours=6)
    with pytest.raises(ValueError):
        eng.on_tick(
            _state(eng), mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("1.5")
        )


def test_config_rejects_non_positive_time_stop_hours():
    with pytest.raises(ValueError):
        _ts_config(hours=0)
    with pytest.raises(ValueError):
        _ts_config(hours=-1)


def test_config_rejects_float_time_stop_hours():
    with pytest.raises(TypeError):
        _ts_config(hours=6.0)  # type: ignore[arg-type]


def test_config_rejects_out_of_range_cooled_threshold():
    with pytest.raises(ValueError):
        _ts_config(cooled_at=Decimal("1.5"))


def test_config_rejects_bad_flat_band():
    with pytest.raises(ValueError):
        _ts_config(band_bps=0)
    with pytest.raises(ValueError):
        _ts_config(band_bps=20_000)


# --------------------------------------------------------------------------- #
# Point-in-time / event-time correctness.
# --------------------------------------------------------------------------- #


def test_clock_is_event_time_not_wall_clock():
    """The flat horizon is measured purely from block_time_ms deltas: identical
    decisions regardless of any wall-clock; an out-of-order (earlier) tick never
    spuriously fires the time-stop."""
    eng = _engine(hours=6)
    st = _state(eng)
    st = eng.on_tick(
        st, mark_price=ENTRY, block_time_ms=BT0 + 10 * HOUR_MS, narrative_score=Decimal("0")
    ).next_state
    # An EARLIER event-time tick (negative span) must not fire.
    d = eng.on_tick(st, mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("0"))
    assert d.reason is ExitReason.NONE


def test_pure_deterministic_time_stop():
    """Re-evaluating the same (state, mark, time, score) reproduces the decision."""
    eng = _engine(hours=6)
    st = _state(eng)
    st = eng.on_tick(
        st, mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("0")
    ).next_state
    a = eng.on_tick(
        st, mark_price=ENTRY, block_time_ms=BT0 + 6 * HOUR_MS, narrative_score=Decimal("0")
    )
    b = eng.on_tick(
        st, mark_price=ENTRY, block_time_ms=BT0 + 6 * HOUR_MS, narrative_score=Decimal("0")
    )
    assert a.reason == b.reason == ExitReason.STALE_NARRATIVE_TIME_STOP
    assert a.intent == b.intent  # identical client_intent_id -> idempotent replay


def test_flat_clock_anchors_on_first_tick():
    """First tick anchors entry/last-active; the flat horizon is measured FROM it,
    not from an arbitrary zero."""
    eng = _engine(hours=6)
    st = _state(eng)
    assert st.entry_block_time_ms == -1
    d = eng.on_tick(st, mark_price=ENTRY, block_time_ms=BT0, narrative_score=Decimal("0"))
    assert d.next_state.entry_block_time_ms == BT0
    assert d.next_state.last_active_block_time_ms == BT0
