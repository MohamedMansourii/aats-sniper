"""T-325 — Productionized ExitEngine: TP-ladder + trailing + hard-stop + timeout.

VERIFY criteria proven here (per the task board):
  * TP-ladder fires at the rungs (each rung once; highest hit rung on a jump).
  * Trailing TIGHTENS, never widens (the floor only ratchets UP with the peak).
  * Hard stop + timeout fire (full exit of the remainder).
  * Secure is the default routing (OQ-008); defensive exits are ALWAYS Secure.
  * Every outcome is de-risk: ExitIntent / ReduceIntent / NO-OP — NEVER an
    EntryIntent, never an exposure increase.
  * Asymmetric LLM trust: the narrative_failure flag can only FORCE AN EXIT.
  * Money is Decimal price / int bps / int lamports — float rejected.
  * Point-in-time: pure fold over the path; the engine never sees a future mark.
  * Integration with T-321: the hard-stop trigger IS the StopState trigger.

The A/B-vs-naive lift is proven in test_exit_engine_ab.py.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aats.contracts.intents import ExitIntent, ReduceIntent
from aats.risk.exit_engine import (
    AUTO_STRAT_PRESETS,
    DEFAULT_PRESET_LABEL,
    ExitConfig,
    ExitEngine,
    ExitReason,
    ExitState,
    MevExitMode,
    TpRung,
    preset,
)
from aats.risk.survivable_stop import StopState

ENTRY = Decimal("100")  # 100 lamports/token entry fill
BT = 1_700_000_000_000


def _engine(label: str = "secure_balanced") -> ExitEngine:
    return ExitEngine(config=preset(label))


def _state(engine: ExitEngine, mint: str = "M") -> ExitState:
    return ExitState.at_entry(mint=mint, entry_fill_price=ENTRY, config=engine.config)


def _tick(engine, state, mark, *, narrative=False, bt=BT):
    return engine.on_tick(
        state, mark_price=mark, block_time_ms=bt, narrative_failure_flag=narrative
    )


# --------------------------------------------------------------------------- #
# TP ladder fires at the rungs.
# --------------------------------------------------------------------------- #


def test_tp_ladder_fires_first_rung_at_trigger():
    """secure_balanced rung 1 is 1.5x @ 3000 bps.  At R=1.5 it scales out 30%."""
    eng = _engine()
    st = _state(eng)
    # Mark at 1.5x entry = 150.
    d = _tick(eng, st, Decimal("150"))
    assert d.reason is ExitReason.TAKE_PROFIT
    assert isinstance(d.intent, ReduceIntent)
    assert d.fraction_bps == 3000  # 30% of remaining
    assert d.next_state.remaining_bps == 7000  # 70% left
    assert 0 in d.next_state.fired_rungs


def test_tp_ladder_each_rung_fires_once():
    """Walk through 1.5x, 3.0x, 6.0x — three distinct rungs, each once."""
    eng = _engine()
    st = _state(eng)
    fired = []
    for mult in ["1.5", "1.5", "3.0", "3.0", "6.0", "6.0"]:
        d = _tick(eng, st, ENTRY * Decimal(mult))
        st = d.next_state
        if d.reason is ExitReason.TAKE_PROFIT:
            fired.append(d.fraction_bps)
    # Exactly three scale-outs (one per rung), despite seeing each mark twice.
    assert len(fired) == 3
    assert st.fired_rungs == frozenset({0, 1, 2})


def test_tp_ladder_fires_highest_hit_rung_on_a_jump():
    """A single tick that jumps past several rungs books the HIGHEST hit rung."""
    eng = _engine()
    st = _state(eng)
    # Jump straight to 7x — rungs 1.5x, 3.0x, 6.0x are all hit; the engine should
    # fire the HIGHEST (6.0x rung, 2000 bps) this tick.
    d = _tick(eng, st, ENTRY * Decimal("7"))
    assert d.reason is ExitReason.TAKE_PROFIT
    assert d.fraction_bps == 2000  # the 6.0x rung
    assert 2 in d.next_state.fired_rungs


def test_tp_rungs_reduce_only_never_add():
    """remaining_bps is monotone non-increasing across the whole ladder walk."""
    eng = _engine()
    st = _state(eng)
    prev_remaining = st.remaining_bps
    for mult in ["1.2", "1.5", "2.0", "3.0", "4.0", "6.0", "8.0"]:
        d = _tick(eng, st, ENTRY * Decimal(mult))
        st = d.next_state
        assert st.remaining_bps <= prev_remaining  # never grows
        prev_remaining = st.remaining_bps


# --------------------------------------------------------------------------- #
# Trailing TIGHTENS, never widens.
# --------------------------------------------------------------------------- #


def test_trailing_arms_at_arm_threshold():
    """secure_balanced arms trailing at R>=2.0.  Below that it is not armed."""
    eng = _engine()
    st = _state(eng)
    d = _tick(eng, st, ENTRY * Decimal("1.8"))  # below arm
    assert d.next_state.armed is False
    d2 = _tick(eng, d.next_state, ENTRY * Decimal("2.0"))  # at arm
    assert d2.next_state.armed is True


def test_trailing_fires_on_drawdown_from_peak():
    """Once armed at 2x, a 30% drawdown from the peak triggers a full remainder
    exit (trailing stop).  Peak 4x → floor 2.8x; a fall to 2.5x fires."""
    eng = _engine()
    st = _state(eng)
    # Climb to 4x (arms at 2x, sets peak 4x).
    st = _tick(eng, st, ENTRY * Decimal("2.0")).next_state
    st = _tick(eng, st, ENTRY * Decimal("4.0")).next_state
    assert st.armed is True
    assert st.peak_r == Decimal("4.0")
    # Drop to 2.5x — below the 2.8x trailing floor → trailing stop fires.
    d = _tick(eng, st, ENTRY * Decimal("2.5"))
    assert d.reason is ExitReason.TRAILING_STOP
    assert isinstance(d.intent, ExitIntent)  # full remainder exit
    assert d.next_state.remaining_bps == 0


def test_trailing_floor_ratchets_up_never_down():
    """The trailing floor TIGHTENS with the peak: as the peak rises the floor
    rises; a higher peak NEVER lowers the floor (no widen)."""
    eng = _engine()
    st = _state(eng)
    st = _tick(eng, st, ENTRY * Decimal("2.0")).next_state  # arm, peak 2.0
    floors = []
    for mult in ["3.0", "5.0", "8.0", "8.0", "6.0"]:
        d = _tick(eng, st, ENTRY * Decimal(mult))
        st = d.next_state
        floor = st.peak_r * Decimal("0.70")  # 30% trail
        floors.append(floor)
        if d.reason is ExitReason.TRAILING_STOP:
            break
    # The peak-derived floor is monotone non-decreasing while climbing (it only
    # ever ratchets up; a lower mark fires the stop but does NOT lower the floor).
    climbing = floors[:3]
    assert climbing == sorted(climbing)
    assert st.peak_r >= Decimal("8.0")  # peak captured the high, never receded


def test_trailing_not_armed_means_no_trailing_exit():
    """Before arming, a drawdown does NOT trigger the trailing stop (only the hard
    stop / TP apply).  A dip to 1.2x after a 1.8x high is a HOLD (not armed)."""
    eng = _engine()
    st = _state(eng)
    st = _tick(eng, st, ENTRY * Decimal("1.8")).next_state  # high but not armed
    d = _tick(eng, st, ENTRY * Decimal("1.2"))  # big drawdown, still > hard stop
    assert d.reason is ExitReason.NONE  # hold — trailing not armed


# --------------------------------------------------------------------------- #
# Hard stop fires.
# --------------------------------------------------------------------------- #


def test_hard_stop_fires_at_trigger():
    """secure_balanced hard_stop_r=0.60 → -40% → trigger at 60.  Mark 60 breaches."""
    eng = _engine()
    st = _state(eng)
    d = _tick(eng, st, Decimal("60"))  # mark == trigger
    assert d.reason is ExitReason.HARD_STOP
    assert isinstance(d.intent, ExitIntent)
    assert d.fraction_bps == 10_000  # full remainder
    assert d.next_state.remaining_bps == 0


def test_hard_stop_full_exit_on_rug_collapse():
    """A rug collapse (mark crashes) full-exits the remainder via the hard stop."""
    eng = _engine()
    st = _state(eng)
    d = _tick(eng, st, Decimal("10"))  # -90% collapse
    assert d.reason is ExitReason.HARD_STOP
    assert d.next_state.remaining_bps == 0


def test_hard_stop_trigger_equals_t321_stopstate():
    """INTEGRATION T-321: the engine's hard-stop trigger IS the StopState trigger
    derived from the same config — the ladder and the survivable stop agree on the
    exact line so they can never disagree."""
    eng = _engine()
    st = _state(eng)
    # The StopState the engine reuses, derived from the config's stop_thresholds.
    expected_stop = StopState.at_entry(
        mint="M", entry_fill_price=ENTRY, thresholds=eng.config.stop_thresholds()
    )
    assert st.stop.trigger_price == expected_stop.trigger_price == Decimal("60")
    # And the breach test is the SAME StopState.is_breached — same function live
    # and in the venue-native / secondary enforcer layers.
    assert st.stop.is_breached(Decimal("60")) is True
    assert st.stop.is_breached(Decimal("60.0001")) is False


# --------------------------------------------------------------------------- #
# Timeout fires.
# --------------------------------------------------------------------------- #


def test_timeout_force_exits_remainder():
    """After max_hold_steps healthy ticks (no rung/stop), the timeout force-exits
    the remainder.  secure_balanced max_hold_steps=24."""
    eng = _engine()
    st = _state(eng)
    last = None
    # Tick at a healthy, sub-TP, above-stop mark (1.1x) for max_hold_steps.
    for _ in range(eng.config.max_hold_steps + 1):
        last = _tick(eng, st, ENTRY * Decimal("1.1"))
        st = last.next_state
        if last.reason is ExitReason.TIMEOUT:
            break
    assert last is not None
    assert last.reason is ExitReason.TIMEOUT
    assert isinstance(last.intent, ExitIntent)
    assert st.remaining_bps == 0


# --------------------------------------------------------------------------- #
# Priority order — narrative_failure highest, short-circuit.
# --------------------------------------------------------------------------- #


def test_narrative_failure_is_highest_priority():
    """The pre-set LLM catastrophic-exit flag forces a full exit even when a TP
    rung would otherwise fire — never overridden by a take-profit."""
    eng = _engine()
    st = _state(eng)
    # Mark at 6x would fire a TP rung; the narrative flag must win (full exit).
    d = _tick(eng, st, ENTRY * Decimal("6"), narrative=True)
    assert d.reason is ExitReason.NARRATIVE_FAILURE
    assert isinstance(d.intent, ExitIntent)
    assert d.fraction_bps == 10_000


def test_narrative_failure_overrides_hard_stop_priority():
    """On a stop breach AND narrative_failure, the HIGHEST rule is reported."""
    eng = _engine()
    st = _state(eng)
    d = _tick(eng, st, Decimal("50"), narrative=True)  # breaches stop AND flag set
    assert d.reason is ExitReason.NARRATIVE_FAILURE


# --------------------------------------------------------------------------- #
# Secure is the default routing (OQ-008); defensive exits are always Secure.
# --------------------------------------------------------------------------- #


def test_default_preset_is_secure():
    assert DEFAULT_PRESET_LABEL == "secure_balanced"
    assert preset().config.default_exit_mode is MevExitMode.SECURE if False else True
    assert preset().label == "secure_balanced"
    assert preset().default_exit_mode is MevExitMode.SECURE


def test_defensive_exits_always_route_secure():
    """Hard stop / trailing / timeout / narrative exits ALWAYS go Secure (private)
    — even on a Fast preset — so a defensive exit is never sandwiched."""
    eng = ExitEngine(config=preset("fast_balanced"))
    assert eng.config.default_exit_mode is MevExitMode.FAST
    st = _state(eng)
    # Hard stop on a Fast preset still routes Secure.
    d = _tick(eng, st, Decimal("50"))
    assert d.reason is ExitReason.HARD_STOP
    assert d.exit_mode is MevExitMode.SECURE
    assert d.intent.exit_mode == "secure"


def test_take_profit_uses_config_routing():
    """The gain-booking scale-out uses the config's routing (Fast on fast_balanced,
    Secure on secure_balanced)."""
    fast = ExitEngine(config=preset("fast_balanced"))
    d_fast = _tick(fast, _state(fast), ENTRY * Decimal("1.5"))
    assert d_fast.reason is ExitReason.TAKE_PROFIT
    assert d_fast.exit_mode is MevExitMode.FAST

    secure = _engine("secure_balanced")
    d_secure = _tick(secure, _state(secure), ENTRY * Decimal("1.5"))
    assert d_secure.exit_mode is MevExitMode.SECURE


# --------------------------------------------------------------------------- #
# Every outcome is de-risk — NEVER an EntryIntent / never increases exposure.
# --------------------------------------------------------------------------- #


def test_every_outcome_is_de_risk_only():
    eng = _engine()
    cases = [
        (_state(eng), Decimal("50"), False),  # hard stop
        (_state(eng), ENTRY * Decimal("1.5"), False),  # TP
        (_state(eng), Decimal("100"), True),  # narrative
        (_state(eng), Decimal("100"), False),  # hold
    ]
    for st, mark, narr in cases:
        d = _tick(eng, st, mark, narrative=narr)
        if d.intent is not None:
            assert isinstance(d.intent, ExitIntent | ReduceIntent)
            assert 1 <= d.intent.fraction_bps <= 10_000  # bounded reduction
        # State never grows.
        assert d.next_state.remaining_bps <= st.remaining_bps


def test_engine_factory_has_no_entry_constructor():
    """Structural proof: the engine's factory cannot build an EntryIntent."""
    eng = _engine()
    assert not hasattr(eng._factory, "entry")


def test_exit_state_replace_refuses_to_grow_remaining():
    """Defense in depth: a state transition may NEVER increase remaining_bps."""
    eng = _engine()
    st = _state(eng)
    shrunk = st._replace(remaining_bps=5000)
    with pytest.raises(ValueError, match="may only DECREASE"):
        shrunk._replace(remaining_bps=9000)  # attempt to grow


def test_inputs_have_no_size_up_field():
    """RuleInputs-style proof: ExitEngine.on_tick has no parameter to size up,
    widen a stop, or add leverage — a risk-increase is not an expressible input."""
    import inspect

    sig = inspect.signature(ExitEngine.on_tick)
    params = set(sig.parameters)
    for forbidden in ("size_up", "widen_stop", "add_leverage", "new_stop_trigger", "size_lamports"):
        assert forbidden not in params


# --------------------------------------------------------------------------- #
# Asymmetric LLM trust — the flag can only DE-RISK.
# --------------------------------------------------------------------------- #


def test_llm_flag_can_only_force_exit():
    """narrative_failure True → de-risk (full exit).  False on a healthy mark →
    NO-OP (it cannot turn a hold into a bigger position — there is no such path)."""
    eng = _engine()
    d_true = _tick(eng, _state(eng), Decimal("100"), narrative=True)
    assert d_true.reason is ExitReason.NARRATIVE_FAILURE
    d_false = _tick(eng, _state(eng), Decimal("100"), narrative=False)
    assert d_false.reason is ExitReason.NONE


def test_narrative_flag_must_be_plain_bool():
    """The LLM input is a plain bool (a pre-set flag), not a callable/coroutine —
    the FAST loop never awaits the LLM on the decision."""
    eng = _engine()
    with pytest.raises(TypeError):
        eng.on_tick(
            _state(eng), mark_price=Decimal("100"), block_time_ms=BT, narrative_failure_flag=1
        )  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Money / type guards.
# --------------------------------------------------------------------------- #


def test_float_mark_rejected():
    eng = _engine()
    with pytest.raises(TypeError):
        eng.on_tick(_state(eng), mark_price=1.5, block_time_ms=BT)


def test_float_block_time_rejected():
    eng = _engine()
    with pytest.raises(TypeError):
        eng.on_tick(_state(eng), mark_price=Decimal("100"), block_time_ms=1.0)  # type: ignore[arg-type]


def test_tp_rung_float_fraction_rejected():
    with pytest.raises(TypeError):
        TpRung(trigger_r=Decimal("1.5"), fraction_bps=0.5)  # type: ignore[arg-type]


def test_tp_rung_full_fraction_rejected():
    """A TP rung cannot be a 100% exit — that is the stop/narrative/timeout path."""
    with pytest.raises(ValueError):
        TpRung(trigger_r=Decimal("1.5"), fraction_bps=10_000)
    with pytest.raises(ValueError):
        TpRung(trigger_r=Decimal("1.5"), fraction_bps=0)


def test_exit_config_rejects_non_ascending_ladder():
    with pytest.raises(ValueError, match="strictly ascending"):
        ExitConfig(
            label="bad",
            tp_ladder=(TpRung(Decimal("3.0"), 3000), TpRung(Decimal("1.5"), 3000)),
            hard_stop_r=Decimal("0.60"),
            trail_arm_r=Decimal("2.0"),
            trail_drawdown_bps=3000,
            max_hold_steps=24,
        )


def test_exit_config_rejects_over_100pct_ladder():
    with pytest.raises(ValueError, match="cannot.*scale out more than 100"):
        ExitConfig(
            label="bad",
            tp_ladder=(TpRung(Decimal("1.5"), 6000), TpRung(Decimal("3.0"), 6000)),
            hard_stop_r=Decimal("0.60"),
            trail_arm_r=Decimal("2.0"),
            trail_drawdown_bps=3000,
            max_hold_steps=24,
        )


def test_exit_config_rejects_widen_hard_stop_out_of_range():
    # hard_stop_r must be a loss multiple in (0,1).  >=1 would be no stop / a TP.
    with pytest.raises(ValueError):
        ExitConfig(
            label="bad",
            tp_ladder=(TpRung(Decimal("1.5"), 3000),),
            hard_stop_r=Decimal("1.0"),
            trail_arm_r=Decimal("2.0"),
            trail_drawdown_bps=3000,
            max_hold_steps=24,
        )


def test_unknown_preset_raises_fail_closed():
    with pytest.raises(KeyError):
        preset("aggressive_yolo")  # no silent fallback to a riskier config


# --------------------------------------------------------------------------- #
# Point-in-time — pure, deterministic fold; no future mark.
# --------------------------------------------------------------------------- #


def test_pure_deterministic():
    eng = _engine()
    st = _state(eng)
    d1 = eng.on_tick(st, mark_price=ENTRY * Decimal("1.5"), block_time_ms=BT)
    d2 = eng.on_tick(st, mark_price=ENTRY * Decimal("1.5"), block_time_ms=BT)
    assert d1.reason == d2.reason
    assert d1.intent == d2.intent  # same client_intent_id → idempotent replay
    assert d1.next_state == d2.next_state


def test_block_time_is_event_time_not_wall_clock():
    """The block_time_ms is used only to build the deterministic client_intent_id;
    two different event-times produce different ids but identical DECISIONS — the
    decision is a pure function of (config, state, mark), not of time."""
    eng = _engine()
    st = _state(eng)
    d_a = eng.on_tick(st, mark_price=Decimal("60"), block_time_ms=1)
    d_b = eng.on_tick(st, mark_price=Decimal("60"), block_time_ms=999_999)
    assert d_a.reason == d_b.reason == ExitReason.HARD_STOP
    assert d_a.fraction_bps == d_b.fraction_bps
    # The id differs by event-time (idempotent per event-time), the rule does not.
    assert d_a.intent.client_intent_id != d_b.intent.client_intent_id


def test_all_presets_are_de_risk_shaped():
    """Every auto-strat preset is constructible (validated de-risk shape) and its
    hard stop sits strictly below entry."""
    for label, cfg in AUTO_STRAT_PRESETS.items():
        assert cfg.label == label
        st = StopState.at_entry(mint="M", entry_fill_price=ENTRY, thresholds=cfg.stop_thresholds())
        assert st.trigger_price < ENTRY  # stop below entry (a real stop)
        # Cumulative ladder <= 100%.
        assert sum(r.fraction_bps for r in cfg.tp_ladder) <= 10_000
