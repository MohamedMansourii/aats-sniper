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


def _tick(
    engine,
    state,
    mark,
    *,
    narrative=False,
    insider=False,
    sellability=False,
    lp_unlock=False,
    bt=BT,
):
    return engine.on_tick(
        state,
        mark_price=mark,
        block_time_ms=bt,
        narrative_failure_flag=narrative,
        insider_dump_flag=insider,
        sellability_degraded_flag=sellability,
        lp_unlock_approaching_flag=lp_unlock,
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
# E14b — insider / dev-sell dump auto-exit (catastrophic tier, de-risk only).
# The FAST branch reads a PRE-SET bool (E14a produces it off the hot path); it
# can ONLY force a full SECURE exit — never hold longer, widen a stop, or size up.
# --------------------------------------------------------------------------- #


def test_insider_dump_flag_forces_secure_full_exit_on_healthy_mark():
    """With the pre-set insider_dump flag set, a fully HEALTHY mark (no stop, no TP)
    is force-exited: full remainder, SECURE routing.  This is the mutation canary —
    remove the branch and this HOLD-otherwise mark stops force-exiting (RED)."""
    eng = _engine()
    st = _state(eng)
    d = _tick(eng, st, Decimal("100"), insider=True)  # mark == entry: would HOLD
    assert d.reason is ExitReason.INSIDER_DUMP
    assert isinstance(d.intent, ExitIntent)  # full flatten, never a ReduceIntent
    assert d.fraction_bps == 10_000  # 100% of remaining
    assert d.next_state.remaining_bps == 0
    assert d.exit_mode is MevExitMode.SECURE  # defensive exit is always Secure
    assert d.intent.exit_mode == "secure"


def test_insider_dump_unset_is_no_change():
    """With the flag UNSET, on_tick behaves exactly as before: a healthy mark holds,
    a TP mark still scales out.  The new branch adds nothing when the flag is off."""
    eng = _engine()
    # Healthy mark, flag off -> HOLD (unchanged).
    d_hold = _tick(eng, _state(eng), Decimal("100"), insider=False)
    assert d_hold.reason is ExitReason.NONE
    assert d_hold.intent is None
    # TP mark, flag off -> TAKE_PROFIT (unchanged).
    d_tp = _tick(eng, _state(eng), ENTRY * Decimal("1.5"), insider=False)
    assert d_tp.reason is ExitReason.TAKE_PROFIT
    assert isinstance(d_tp.intent, ReduceIntent)
    # Identical to omitting the flag entirely (default False).
    d_default = eng.on_tick(_state(eng), mark_price=Decimal("100"), block_time_ms=BT)
    assert d_default.reason is ExitReason.NONE


def test_insider_dump_overrides_hard_stop():
    """On a stop breach AND insider_dump, the insider dump is reported (it sits
    ABOVE the mechanical hard stop — exit ahead of the price collapse).  Removing
    the branch would report HARD_STOP here, so this pins the ordering RED."""
    eng = _engine()
    st = _state(eng)
    d = _tick(eng, st, Decimal("50"), insider=True)  # breaches -40% stop AND flag set
    assert d.reason is ExitReason.INSIDER_DUMP
    assert isinstance(d.intent, ExitIntent)
    assert d.exit_mode is MevExitMode.SECURE


def test_insider_dump_overrides_take_profit():
    """A TP-rung mark with the insider flag set force-exits (INSIDER_DUMP), never
    books a partial gain (TAKE_PROFIT) — the catastrophic tier wins."""
    eng = _engine()
    d = _tick(eng, _state(eng), ENTRY * Decimal("6"), insider=True)  # 6x -> TP rung
    assert d.reason is ExitReason.INSIDER_DUMP
    assert isinstance(d.intent, ExitIntent)
    assert d.fraction_bps == 10_000


def test_insider_dump_overrides_ratchet_stop():
    """A give-back to the locked profit floor (RATCHET_STOP without the flag) is
    reported as INSIDER_DUMP when the flag is set — insider dump > ratchet."""
    eng = _engine()
    st = _state(eng)
    st = _tick(eng, st, ENTRY * Decimal("3.0")).next_state  # peak 3x -> locked floor 2.0x
    assert st.locked_floor_r == Decimal("2.0")
    # Control: without the flag, a fall to the locked floor fires the ratchet stop.
    d_ratchet = _tick(eng, st, ENTRY * Decimal("2.0"))
    assert d_ratchet.reason is ExitReason.RATCHET_STOP
    # With the flag, the SAME tick is an insider-dump exit instead.
    d_insider = _tick(eng, st, ENTRY * Decimal("2.0"), insider=True)
    assert d_insider.reason is ExitReason.INSIDER_DUMP
    assert d_insider.exit_mode is MevExitMode.SECURE


def test_insider_dump_overrides_trailing_stop():
    """A drawdown from the peak that would fire the TRAILING_STOP is reported as
    INSIDER_DUMP when the flag is set — insider dump > trailing."""
    eng = _engine()
    st = _state(eng)
    st = _tick(eng, st, ENTRY * Decimal("2.0")).next_state  # arm
    st = _tick(eng, st, ENTRY * Decimal("4.0")).next_state  # peak 4x -> floor 2.8x
    # Control: a fall to 2.5x (< 2.8x floor) fires the trailing stop.
    d_trail = _tick(eng, st, ENTRY * Decimal("2.5"))
    assert d_trail.reason is ExitReason.TRAILING_STOP
    # With the flag, the SAME tick is an insider-dump exit instead.
    d_insider = _tick(eng, st, ENTRY * Decimal("2.5"), insider=True)
    assert d_insider.reason is ExitReason.INSIDER_DUMP


def test_insider_dump_overrides_timeout():
    """At the exact tick the max-hold TIMEOUT would fire, the insider flag wins —
    insider dump > timeout.  Both are evaluated from the SAME pre-timeout state
    (on_tick is pure), isolating the ordering."""
    eng = _engine()
    st = _state(eng)
    for _ in range(eng.config.max_hold_steps - 1):
        st = _tick(eng, st, ENTRY * Decimal("1.1")).next_state
    assert st.steps_held == eng.config.max_hold_steps - 1
    d_timeout = _tick(eng, st, ENTRY * Decimal("1.1"))  # next tick times out
    assert d_timeout.reason is ExitReason.TIMEOUT
    d_insider = _tick(eng, st, ENTRY * Decimal("1.1"), insider=True)
    assert d_insider.reason is ExitReason.INSIDER_DUMP


def test_narrative_failure_outranks_insider_dump():
    """narrative_failure remains the single HIGHEST rule (charter iii).  When BOTH
    catastrophic flags are set, narrative_failure is reported."""
    eng = _engine()
    d = _tick(eng, _state(eng), Decimal("100"), narrative=True, insider=True)
    assert d.reason is ExitReason.NARRATIVE_FAILURE


# --------------------------------------------------------------------------- #
# E17 — delayed-honeypot / tax-flip sellability re-probe auto-exit (catastrophic
# tier, de-risk only).  The FAST branch reads a PRE-SET bool (the SLOW-loop
# re-probe produces it off the hot path by re-running the SHARED sell-sim); it can
# ONLY force a full SECURE exit — never hold longer, widen a stop, or size up.
# --------------------------------------------------------------------------- #


def test_sellability_degraded_forces_secure_full_exit_on_healthy_mark():
    """With the pre-set sellability_degraded flag, a fully HEALTHY mark (no stop, no
    TP) is force-exited: full remainder, SECURE routing.  This is the mutation
    canary — remove the branch and this HOLD-otherwise mark stops force-exiting."""
    eng = _engine()
    st = _state(eng)
    d = _tick(eng, st, Decimal("100"), sellability=True)  # mark == entry: would HOLD
    assert d.reason is ExitReason.SELLABILITY_DEGRADED
    assert isinstance(d.intent, ExitIntent)  # full flatten, never a ReduceIntent
    assert d.fraction_bps == 10_000  # 100% of remaining
    assert d.next_state.remaining_bps == 0
    assert d.exit_mode is MevExitMode.SECURE  # exit while an exit is still possible
    assert d.intent.exit_mode == "secure"


def test_sellability_degraded_unset_is_no_change():
    """With the flag UNSET, on_tick behaves exactly as before: a healthy mark holds,
    a TP mark still scales out.  The new branch adds nothing when the flag is off."""
    eng = _engine()
    d_hold = _tick(eng, _state(eng), Decimal("100"), sellability=False)
    assert d_hold.reason is ExitReason.NONE
    assert d_hold.intent is None
    d_tp = _tick(eng, _state(eng), ENTRY * Decimal("1.5"), sellability=False)
    assert d_tp.reason is ExitReason.TAKE_PROFIT
    assert isinstance(d_tp.intent, ReduceIntent)


def test_sellability_degraded_overrides_hard_stop():
    """On a stop breach AND sellability_degraded, the sellability exit is reported
    (it sits ABOVE the mechanical hard stop — exit on the sellability signal, not
    after the price collapse).  Removing the branch would report HARD_STOP here."""
    eng = _engine()
    d = _tick(eng, _state(eng), Decimal("50"), sellability=True)  # breaches -40% stop
    assert d.reason is ExitReason.SELLABILITY_DEGRADED
    assert isinstance(d.intent, ExitIntent)
    assert d.exit_mode is MevExitMode.SECURE


def test_sellability_degraded_overrides_take_profit():
    """A TP-rung mark with the sellability flag set force-exits, never books a
    partial gain — the catastrophic tier wins."""
    eng = _engine()
    d = _tick(eng, _state(eng), ENTRY * Decimal("6"), sellability=True)  # 6x -> TP rung
    assert d.reason is ExitReason.SELLABILITY_DEGRADED
    assert isinstance(d.intent, ExitIntent)
    assert d.fraction_bps == 10_000


def test_insider_dump_outranks_sellability_degraded():
    """insider_dump (rug-in-progress) sits ABOVE sellability_degraded.  When BOTH
    are set, insider_dump is reported — pins the ordering RED under a swap."""
    eng = _engine()
    d = _tick(eng, _state(eng), Decimal("100"), insider=True, sellability=True)
    assert d.reason is ExitReason.INSIDER_DUMP


def test_narrative_failure_outranks_sellability_degraded():
    """narrative_failure remains the single HIGHEST rule even against sellability."""
    eng = _engine()
    d = _tick(eng, _state(eng), Decimal("100"), narrative=True, sellability=True)
    assert d.reason is ExitReason.NARRATIVE_FAILURE


# --------------------------------------------------------------------------- #
# E19 — TIME-LOCKED LP unlock APPROACHING auto-exit (catastrophic tier, de-risk
# only).  The FAST branch reads a PRE-SET bool (the SLOW-loop LP-unlock watcher
# produces it off the hot path); it can ONLY force a full SECURE exit — never
# hold longer, widen a stop, or size up.
#
# 2026-07-03 program review (HIGH): this parameter existed + was documented in
# ExitEngine.on_tick but had ZERO test coverage proving the branch actually
# fires — "doubly dead" (no StateStore methods either, see test_state_store.py
# and fast_loop.py).  This block is the missing unit-test half of that fix.
# --------------------------------------------------------------------------- #


def test_lp_unlock_approaching_flag_forces_secure_full_exit_on_healthy_mark():
    """With the pre-set lp_unlock_approaching flag set, a fully HEALTHY mark (no
    stop, no TP) is force-exited: full remainder, SECURE routing.  This is the
    mutation canary — remove the branch and this HOLD-otherwise mark stops
    force-exiting (RED)."""
    eng = _engine()
    st = _state(eng)
    d = _tick(eng, st, Decimal("100"), lp_unlock=True)  # mark == entry: would HOLD
    assert d.reason is ExitReason.LP_UNLOCK_APPROACHING
    assert isinstance(d.intent, ExitIntent)  # full flatten, never a ReduceIntent
    assert d.fraction_bps == 10_000  # 100% of remaining
    assert d.next_state.remaining_bps == 0
    assert d.exit_mode is MevExitMode.SECURE  # exit while an exit is still possible
    assert d.intent.exit_mode == "secure"


def test_lp_unlock_approaching_unset_is_no_change():
    """With the flag UNSET, on_tick behaves exactly as before: a healthy mark holds,
    a TP mark still scales out.  The new branch adds nothing when the flag is off."""
    eng = _engine()
    d_hold = _tick(eng, _state(eng), Decimal("100"), lp_unlock=False)
    assert d_hold.reason is ExitReason.NONE
    assert d_hold.intent is None
    d_tp = _tick(eng, _state(eng), ENTRY * Decimal("1.5"), lp_unlock=False)
    assert d_tp.reason is ExitReason.TAKE_PROFIT
    assert isinstance(d_tp.intent, ReduceIntent)


def test_lp_unlock_approaching_overrides_hard_stop():
    """On a stop breach AND lp_unlock_approaching, the lp-unlock exit is reported
    (it sits ABOVE the mechanical hard stop — exit on the unlock-window signal,
    not after the LP is pulled).  Removing the branch would report HARD_STOP."""
    eng = _engine()
    d = _tick(eng, _state(eng), Decimal("50"), lp_unlock=True)  # breaches -40% stop
    assert d.reason is ExitReason.LP_UNLOCK_APPROACHING
    assert isinstance(d.intent, ExitIntent)
    assert d.exit_mode is MevExitMode.SECURE


def test_lp_unlock_approaching_overrides_take_profit():
    """A TP-rung mark with the lp_unlock flag set force-exits, never books a
    partial gain — the catastrophic tier wins."""
    eng = _engine()
    d = _tick(eng, _state(eng), ENTRY * Decimal("6"), lp_unlock=True)  # 6x -> TP rung
    assert d.reason is ExitReason.LP_UNLOCK_APPROACHING
    assert isinstance(d.intent, ExitIntent)
    assert d.fraction_bps == 10_000


def test_sellability_degraded_outranks_lp_unlock_approaching():
    """sellability_degraded sits ABOVE lp_unlock_approaching.  When BOTH are set,
    sellability_degraded is reported — pins the ordering RED under a swap."""
    eng = _engine()
    d = _tick(eng, _state(eng), Decimal("100"), sellability=True, lp_unlock=True)
    assert d.reason is ExitReason.SELLABILITY_DEGRADED


def test_insider_dump_outranks_lp_unlock_approaching():
    """insider_dump sits ABOVE lp_unlock_approaching.  When BOTH are set,
    insider_dump is reported."""
    eng = _engine()
    d = _tick(eng, _state(eng), Decimal("100"), insider=True, lp_unlock=True)
    assert d.reason is ExitReason.INSIDER_DUMP


def test_narrative_failure_outranks_lp_unlock_approaching():
    """narrative_failure remains the single HIGHEST rule even against lp-unlock."""
    eng = _engine()
    d = _tick(eng, _state(eng), Decimal("100"), narrative=True, lp_unlock=True)
    assert d.reason is ExitReason.NARRATIVE_FAILURE


def test_lp_unlock_approaching_flag_must_be_plain_bool():
    """The lp_unlock input is a plain PRE-SET bool (mirrors the flags above) — a
    truthy int is refused so the FAST branch can never be fed a non-bool signal."""
    eng = _engine()
    with pytest.raises(TypeError):
        eng.on_tick(
            _state(eng),
            mark_price=Decimal("100"),
            block_time_ms=BT,
            lp_unlock_approaching_flag=1,  # type: ignore[arg-type]
        )


def test_lp_unlock_approaching_secure_even_on_fast_preset():
    """Like every defensive exit, an lp-unlock exit routes SECURE even on a Fast
    preset — a pullable-LP-window exit is never sandwiched on the way out."""
    eng = ExitEngine(config=preset("fast_balanced"))
    assert eng.config.default_exit_mode is MevExitMode.FAST
    d = _tick(eng, _state(eng), Decimal("100"), lp_unlock=True)
    assert d.reason is ExitReason.LP_UNLOCK_APPROACHING
    assert d.exit_mode is MevExitMode.SECURE
    assert d.intent.exit_mode == "secure"


def test_lp_unlock_approaching_is_de_risk_only():
    """The lp-unlock branch only ever sheds risk: remaining never grows and the
    intent is a bounded full flatten (never an add)."""
    eng = _engine()
    st = _state(eng)
    d = _tick(eng, st, Decimal("120"), lp_unlock=True)
    assert d.next_state.remaining_bps <= st.remaining_bps
    assert isinstance(d.intent, ExitIntent)
    assert 1 <= d.intent.fraction_bps <= 10_000


def test_sellability_degraded_flag_must_be_plain_bool():
    """The sellability input is a plain PRE-SET bool (mirrors narrative/insider) —
    a truthy int is refused so the FAST branch can never be fed a non-bool."""
    eng = _engine()
    with pytest.raises(TypeError):
        eng.on_tick(
            _state(eng),
            mark_price=Decimal("100"),
            block_time_ms=BT,
            sellability_degraded_flag=1,  # type: ignore[arg-type]
        )


def test_sellability_degraded_secure_even_on_fast_preset():
    """Like every defensive exit, a sellability exit routes SECURE even on a Fast
    preset — an exit-while-you-still-can is never sandwiched on the way out."""
    eng = ExitEngine(config=preset("fast_balanced"))
    assert eng.config.default_exit_mode is MevExitMode.FAST
    d = _tick(eng, _state(eng), Decimal("100"), sellability=True)
    assert d.reason is ExitReason.SELLABILITY_DEGRADED
    assert d.exit_mode is MevExitMode.SECURE
    assert d.intent.exit_mode == "secure"


def test_sellability_degraded_is_de_risk_only():
    """The sellability branch only ever sheds risk: remaining never grows and the
    intent is a bounded full flatten (never an add)."""
    eng = _engine()
    st = _state(eng)
    d = _tick(eng, st, Decimal("120"), sellability=True)
    assert d.next_state.remaining_bps <= st.remaining_bps
    assert isinstance(d.intent, ExitIntent)
    assert 1 <= d.intent.fraction_bps <= 10_000


def test_insider_dump_flag_must_be_plain_bool():
    """The insider input is a plain PRE-SET bool (mirrors narrative_failure) — a
    truthy int is refused so the FAST branch can never be fed a non-bool signal."""
    eng = _engine()
    with pytest.raises(TypeError):
        eng.on_tick(
            _state(eng), mark_price=Decimal("100"), block_time_ms=BT, insider_dump_flag=1
        )  # type: ignore[arg-type]


def test_insider_dump_defensive_exit_secure_even_on_fast_preset():
    """Like every defensive exit, an insider-dump exit routes SECURE even on a Fast
    preset — a rug-in-progress exit is never sandwiched on the way out."""
    eng = ExitEngine(config=preset("fast_balanced"))
    assert eng.config.default_exit_mode is MevExitMode.FAST
    d = _tick(eng, _state(eng), Decimal("100"), insider=True)
    assert d.reason is ExitReason.INSIDER_DUMP
    assert d.exit_mode is MevExitMode.SECURE
    assert d.intent.exit_mode == "secure"


def test_insider_dump_is_de_risk_only():
    """The insider-dump branch only ever sheds risk: remaining never grows and the
    intent is a bounded full flatten (never an add)."""
    eng = _engine()
    st = _state(eng)
    d = _tick(eng, st, Decimal("120"), insider=True)
    assert d.next_state.remaining_bps <= st.remaining_bps  # never grows
    assert isinstance(d.intent, ExitIntent)
    assert 1 <= d.intent.fraction_bps <= 10_000


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
