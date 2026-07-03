"""E19 (risk) — LP-unlock-approaching de-risk awareness tests.

VERIFY criteria (per the task board):
  * (a) ENTRY: a TIME-LOCKED LP whose unlock is APPROACHING within the near horizon
    REJECTS at entry (imminent band) / DOWN-WEIGHTS in the wider band.
  * (b) OPEN POSITION: the same approaching unlock RAISES the pre-set exit flag, which
    the ExitEngine consumes to force-exit SECURE (mutation-canary tests).
  * A BURNED LP or a FAR-OFF unlock does NOT reject / does NOT raise the flag.
  * An UNKNOWN / undecodable schedule -> REFUSE (reject at entry, raise the flag for an
    open position) — refuse-by-default.
  * De-risk only: conviction_multiplier structurally clamped to [0, 1]; the exit flag
    can only FORCE AN EXIT; nothing sizes up / widens / raises conviction.
  * Point-in-time: proximity is measured in EVENT-TIME slots (unlock_slot - event_slot),
    never wall-clock — a fixed schedule tightens as the event-slot advances.
"""

from __future__ import annotations

import dataclasses
import inspect
from decimal import Decimal

import pytest

from aats.contracts.intents import ExitIntent, ReduceIntent
from aats.risk import lp_unlock_gate as gate_mod
from aats.risk.exit_engine import ExitEngine, ExitReason, ExitState, MevExitMode, preset
from aats.risk.lp_unlock_gate import (
    LpLockStatus,
    LpUnlockDecision,
    LpUnlockExitReason,
    LpUnlockExitWatcher,
    LpUnlockGate,
    LpUnlockPreGateResult,
    LpUnlockReason,
    LpUnlockSchedule,
    LpUnlockThresholds,
    LpUnlockVerdict,
    classify_open_position_unlock,
    evaluate_with_lp_unlock,
)
from aats.risk.pretrade_gate import RedFlag, RedFlagHit

MINT = "LpMint1111111111111111111111111111111111111"
AS_OF = 300_000_000  # event-time slot
BT = 1_718_500_000_000

# Default horizons: reject<=216_000, downweight<=648_000 slots.
IMMINENT_UNLOCK = AS_OF + 100_000  # within reject horizon -> REJECT
ALREADY_UNLOCKED = AS_OF - 5_000  # lock already released -> REJECT
APPROACHING_UNLOCK = AS_OF + 400_000  # in downweight band -> DOWN_WEIGHT
FAR_OFF_UNLOCK = AS_OF + 1_000_000  # beyond downweight -> PASS


def _schedule(status: LpLockStatus, unlock_slot: int | None = None, as_of: int = AS_OF):
    return LpUnlockSchedule(
        mint=MINT, as_of_event_slot=as_of, status=status, unlock_slot=unlock_slot
    )


# ===========================================================================
# (a) ENTRY GATE
# ===========================================================================


def test_burned_lp_is_silent_pass():
    d = LpUnlockGate().evaluate(_schedule(LpLockStatus.BURNED))
    assert d.verdict is LpUnlockVerdict.PASS
    assert d.conviction_multiplier == 1
    assert d.red_flag_hit is None
    assert d.reason_codes == ()
    assert d.apply(1_000_000) == 1_000_000  # size UNCHANGED


def test_far_off_unlock_is_pass():
    d = LpUnlockGate().evaluate(_schedule(LpLockStatus.TIME_LOCKED, FAR_OFF_UNLOCK))
    assert d.verdict is LpUnlockVerdict.PASS
    assert d.conviction_multiplier == 1
    assert d.red_flag_hit is None
    assert d.apply(1_000_000) == 1_000_000


def test_approaching_unlock_downweights_not_rejects():
    d = LpUnlockGate().evaluate(_schedule(LpLockStatus.TIME_LOCKED, APPROACHING_UNLOCK))
    assert d.verdict is LpUnlockVerdict.DOWN_WEIGHT
    assert d.rejected is False
    assert d.passed is True
    assert 0 <= d.conviction_multiplier < 1
    assert d.red_flag_hit is None
    assert d.reason_codes == (LpUnlockReason.LP_UNLOCK_APPROACHING.value,)
    shrunk = d.apply(1_000_000)
    assert 0 <= shrunk < 1_000_000  # de-risk: never grows


def test_imminent_unlock_rejects_at_entry():
    d = LpUnlockGate().evaluate(_schedule(LpLockStatus.TIME_LOCKED, IMMINENT_UNLOCK))
    assert d.verdict is LpUnlockVerdict.REJECT
    assert d.rejected is True
    assert d.passed is False
    assert d.conviction_multiplier == 0
    assert d.red_flag_hit is not None
    assert d.red_flag_hit.flag is RedFlag.LP_UNLOCK_IMMINENT
    assert d.slots_to_unlock == IMMINENT_UNLOCK - AS_OF
    assert d.apply(1_000_000) == 0
    assert d.refused_unknown is False  # this is a CONFIRMED imminent unlock, not unknown


def test_already_released_lock_rejects():
    """A lock whose unlock slot is in the PAST relative to the decision slot (pullable
    NOW) falls into the imminent REJECT band — slots_to_unlock is non-positive."""
    d = LpUnlockGate().evaluate(_schedule(LpLockStatus.TIME_LOCKED, ALREADY_UNLOCKED))
    assert d.verdict is LpUnlockVerdict.REJECT
    assert d.slots_to_unlock == ALREADY_UNLOCKED - AS_OF < 0
    assert d.red_flag_hit.flag is RedFlag.LP_UNLOCK_IMMINENT


def test_unknown_schedule_refuses_by_default():
    d = LpUnlockGate().evaluate(_schedule(LpLockStatus.UNKNOWN))
    assert d.verdict is LpUnlockVerdict.REJECT
    assert d.refused_unknown is True
    assert d.red_flag_hit.flag is RedFlag.LP_UNLOCK_SCHEDULE_UNKNOWN
    assert d.slots_to_unlock is None


def test_time_locked_with_no_readable_unlock_slot_refuses():
    """A TIME_LOCKED lock carrying no readable unlock slot is treated as UNKNOWN
    (refuse-by-default), never as far-off/safe."""
    d = LpUnlockGate().evaluate(_schedule(LpLockStatus.TIME_LOCKED, unlock_slot=None))
    assert d.verdict is LpUnlockVerdict.REJECT
    assert d.refused_unknown is True
    assert d.red_flag_hit.flag is RedFlag.LP_UNLOCK_SCHEDULE_UNKNOWN


def test_point_in_time_verdict_tightens_as_event_slot_advances():
    """The SAME on-chain unlock_slot yields a tighter verdict as the decision slot
    advances toward it — proof proximity is pure EVENT-TIME slot arithmetic, not
    wall-clock.  PASS (far) -> DOWN_WEIGHT (approaching) -> REJECT (imminent)."""
    g = LpUnlockGate()
    unlock = 301_000_000  # fixed on-chain slot
    far = g.evaluate(_schedule(LpLockStatus.TIME_LOCKED, unlock, as_of=300_000_000))
    mid = g.evaluate(_schedule(LpLockStatus.TIME_LOCKED, unlock, as_of=300_500_000))
    near = g.evaluate(_schedule(LpLockStatus.TIME_LOCKED, unlock, as_of=300_900_000))
    assert far.verdict is LpUnlockVerdict.PASS
    assert mid.verdict is LpUnlockVerdict.DOWN_WEIGHT
    assert near.verdict is LpUnlockVerdict.REJECT


def test_widening_reject_horizon_only_rejects_more():
    """Widening reject_within_slots (a de-risk change) turns a previously DOWN_WEIGHT
    case into a REJECT — never the reverse."""
    loose = LpUnlockGate(LpUnlockThresholds(reject_within_slots=216_000))
    tight = LpUnlockGate(
        LpUnlockThresholds(reject_within_slots=500_000, downweight_within_slots=648_000)
    )
    sched = _schedule(LpLockStatus.TIME_LOCKED, APPROACHING_UNLOCK)  # +400_000 slots
    assert loose.evaluate(sched).verdict is LpUnlockVerdict.DOWN_WEIGHT
    assert tight.evaluate(sched).verdict is LpUnlockVerdict.REJECT


# ---------------------------------------------------------------------------
# Structural clamp — cannot construct a risk-increasing decision
# ---------------------------------------------------------------------------


def _decision(**kw):
    base = {
        "mint": MINT,
        "verdict": LpUnlockVerdict.PASS,
        "conviction_multiplier": Decimal(1),
        "reason_codes": (),
        "slots_to_unlock": None,
        "unlock_slot": None,
        "as_of_event_slot": AS_OF,
        "event_block_time_ms": BT,
        "red_flag_hit": None,
    }
    base.update(kw)
    return LpUnlockDecision(**base)


def test_multiplier_above_one_is_structurally_rejected():
    with pytest.raises(ValueError):
        _decision(conviction_multiplier=Decimal(2))  # attempted "size up"


def test_reject_must_carry_zero_multiplier():
    with pytest.raises(ValueError):
        _decision(
            verdict=LpUnlockVerdict.REJECT,
            conviction_multiplier=Decimal(1),
            red_flag_hit=RedFlagHit(RedFlag.LP_UNLOCK_IMMINENT, 1, 2),
        )


def test_pass_cannot_carry_a_red_flag():
    with pytest.raises(ValueError):
        _decision(red_flag_hit=RedFlagHit(RedFlag.LP_UNLOCK_IMMINENT, 1, 2))


def test_downweight_multiplier_of_one_is_structurally_rejected():
    with pytest.raises(ValueError):
        _decision(
            verdict=LpUnlockVerdict.DOWN_WEIGHT,
            conviction_multiplier=Decimal(1),
            reason_codes=(LpUnlockReason.LP_UNLOCK_APPROACHING.value,),
        )


def test_reject_red_flag_must_be_an_lp_unlock_flag():
    with pytest.raises(ValueError):
        _decision(
            verdict=LpUnlockVerdict.REJECT,
            conviction_multiplier=Decimal(0),
            red_flag_hit=RedFlagHit(RedFlag.FREEZE_AUTHORITY_SET, None, None),
        )


# ---------------------------------------------------------------------------
# Malformed schedule / thresholds — refuse-by-default, never silently trusted
# ---------------------------------------------------------------------------


def test_burned_schedule_with_unlock_slot_raises():
    with pytest.raises(ValueError):
        LpUnlockSchedule(
            mint=MINT, as_of_event_slot=AS_OF, status=LpLockStatus.BURNED, unlock_slot=123
        )


def test_unknown_schedule_with_unlock_slot_raises():
    with pytest.raises(ValueError):
        LpUnlockSchedule(
            mint=MINT, as_of_event_slot=AS_OF, status=LpLockStatus.UNKNOWN, unlock_slot=123
        )


def test_float_slot_rejected():
    with pytest.raises(TypeError):
        LpUnlockSchedule(
            mint=MINT,
            as_of_event_slot=AS_OF,
            status=LpLockStatus.TIME_LOCKED,
            unlock_slot=100.0,  # type: ignore[arg-type]
        )


def test_thresholds_downweight_below_reject_raises():
    with pytest.raises(ValueError):
        LpUnlockThresholds(reject_within_slots=500_000, downweight_within_slots=100_000)


def test_thresholds_multiplier_must_be_below_one():
    with pytest.raises(ValueError):
        LpUnlockThresholds(downweight_multiplier="1.0")


def test_thresholds_multiplier_cannot_be_negative():
    with pytest.raises(ValueError):
        LpUnlockThresholds(downweight_multiplier="-0.1")


def test_apply_rejects_float_size():
    d = LpUnlockGate().evaluate(_schedule(LpLockStatus.BURNED))
    with pytest.raises(TypeError):
        d.apply(1000.0)  # type: ignore[arg-type]


def test_from_env_defaults_and_overrides():
    assert LpUnlockThresholds.from_env({}) == LpUnlockThresholds()
    t = LpUnlockThresholds.from_env(
        {
            "LP_UNLOCK_REJECT_WITHIN_SLOTS": "1000",
            "LP_UNLOCK_DOWNWEIGHT_WITHIN_SLOTS": "5000",
            "LP_UNLOCK_DOWNWEIGHT_MULTIPLIER": "0.10",
            "LP_UNLOCK_EXIT_RAISE_WITHIN_SLOTS": "2000",
        }
    )
    assert t.reject_within_slots == 1000
    assert t.downweight_within_slots == 5000
    assert t.exit_raise_within_slots == 2000


# ---------------------------------------------------------------------------
# Pre-gate composition — REJECT short-circuits, DOWN_WEIGHT does not
# ---------------------------------------------------------------------------


class _NextStepSpy:
    def __init__(self, passed: bool = True) -> None:
        self.called = False
        self._passed = passed

    def __call__(self):
        self.called = True
        return _NextResult(self._passed)


@dataclasses.dataclass
class _NextResult:
    passed: bool


def test_reject_short_circuits_before_next_step_runs():
    spy = _NextStepSpy(passed=True)
    result = evaluate_with_lp_unlock(
        gate=LpUnlockGate(),
        schedule=_schedule(LpLockStatus.TIME_LOCKED, IMMINENT_UNLOCK),
        next_step=spy,
    )
    assert isinstance(result, LpUnlockPreGateResult)
    assert result.passed is False
    assert result.rejected_by_lp_unlock is True
    assert result.next_result is None
    assert spy.called is False  # PROOF the downstream step never ran


def test_unknown_schedule_short_circuits_before_next_step():
    spy = _NextStepSpy(passed=True)
    result = evaluate_with_lp_unlock(
        gate=LpUnlockGate(), schedule=_schedule(LpLockStatus.UNKNOWN), next_step=spy
    )
    assert result.passed is False
    assert spy.called is False


def test_downweight_does_not_short_circuit_next_step_runs():
    spy = _NextStepSpy(passed=True)
    result = evaluate_with_lp_unlock(
        gate=LpUnlockGate(),
        schedule=_schedule(LpLockStatus.TIME_LOCKED, APPROACHING_UNLOCK),
        next_step=spy,
    )
    assert result.passed is True
    assert result.rejected_by_lp_unlock is False
    assert spy.called is True
    assert result.decision.down_weighted is True


def test_burned_runs_next_step_and_reject_propagates():
    spy = _NextStepSpy(passed=False)
    result = evaluate_with_lp_unlock(
        gate=LpUnlockGate(), schedule=_schedule(LpLockStatus.BURNED), next_step=spy
    )
    assert spy.called is True
    assert result.passed is False  # downstream gate rejected -- honored


# ---------------------------------------------------------------------------
# No wall-clock / no rate field (grep-assert)
# ---------------------------------------------------------------------------


def test_no_wall_clock_in_module():
    src = inspect.getsource(gate_mod)
    for forbidden in ("time.time", "datetime.now", "datetime.utcnow", "perf_counter", "monotonic"):
        assert forbidden not in src


def test_no_rate_field_on_decision():
    names = {f.name for f in dataclasses.fields(LpUnlockDecision)}
    for forbidden in ("win_rate", "success_rate", "rug_rate", "rate"):
        assert forbidden not in names


# ===========================================================================
# (b) OPEN-POSITION exit classifier + watcher
# ===========================================================================


def _classify(status, unlock_slot=None, event_slot=AS_OF, thresholds=None):
    return classify_open_position_unlock(
        _schedule(status, unlock_slot),
        event_slot=event_slot,
        thresholds=thresholds or LpUnlockThresholds(),
    )


def test_exit_burned_is_healthy_no_raise():
    reason, slots, _ = _classify(LpLockStatus.BURNED)
    assert reason is LpUnlockExitReason.HEALTHY
    assert reason.should_raise is False
    assert slots is None


def test_exit_far_off_is_healthy_no_raise():
    reason, _, _ = _classify(LpLockStatus.TIME_LOCKED, FAR_OFF_UNLOCK)
    assert reason is LpUnlockExitReason.HEALTHY
    assert reason.should_raise is False


def test_exit_approaching_raises():
    reason, slots, _ = _classify(LpLockStatus.TIME_LOCKED, IMMINENT_UNLOCK)
    assert reason is LpUnlockExitReason.APPROACHING
    assert reason.should_raise is True
    assert slots == IMMINENT_UNLOCK - AS_OF


def test_exit_unknown_refuses_by_default_raises():
    reason, slots, _ = _classify(LpLockStatus.UNKNOWN)
    assert reason is LpUnlockExitReason.SCHEDULE_UNKNOWN
    assert reason.should_raise is True
    assert slots is None


def test_exit_time_locked_no_unlock_slot_refuses():
    reason, _, _ = _classify(LpLockStatus.TIME_LOCKED, unlock_slot=None)
    assert reason is LpUnlockExitReason.SCHEDULE_UNKNOWN
    assert reason.should_raise is True


def test_exit_point_in_time_flips_as_tick_slot_advances():
    """A fixed unlock_slot goes HEALTHY -> APPROACHING as the tick event-slot advances
    toward it — proximity is pure event-time slot arithmetic."""
    unlock = 301_000_000
    healthy, _, _ = _classify(LpLockStatus.TIME_LOCKED, unlock, event_slot=300_000_000)
    approaching, _, _ = _classify(LpLockStatus.TIME_LOCKED, unlock, event_slot=300_900_000)
    assert healthy is LpUnlockExitReason.HEALTHY
    assert approaching is LpUnlockExitReason.APPROACHING


class _FlagSinkSpy:
    def __init__(self):
        self.calls: list[tuple[str, str, int, int]] = []

    def set_lp_unlock_approaching_flag(self, mint, *, reason, event_slot, ttl_seconds=120):
        self.calls.append((mint, reason, event_slot, ttl_seconds))


def test_watcher_sets_flag_on_approaching():
    sink = _FlagSinkSpy()
    w = LpUnlockExitWatcher(flag_sink=sink)
    d = w.watch(_schedule(LpLockStatus.TIME_LOCKED, IMMINENT_UNLOCK), event_slot=AS_OF)
    assert d.raise_flag is True
    assert d.flag_set is True
    assert len(sink.calls) == 1
    assert sink.calls[0][0] == MINT
    assert sink.calls[0][1] == LpUnlockExitReason.APPROACHING.value


def test_watcher_does_not_set_flag_on_healthy():
    sink = _FlagSinkSpy()
    w = LpUnlockExitWatcher(flag_sink=sink)
    d = w.watch(_schedule(LpLockStatus.TIME_LOCKED, FAR_OFF_UNLOCK), event_slot=AS_OF)
    assert d.raise_flag is False
    assert d.flag_set is False
    assert sink.calls == []


def test_watcher_sets_flag_on_unknown_schedule():
    sink = _FlagSinkSpy()
    w = LpUnlockExitWatcher(flag_sink=sink)
    d = w.watch(_schedule(LpLockStatus.UNKNOWN), event_slot=AS_OF)
    assert d.raise_flag is True
    assert d.flag_set is True


def test_watcher_ttl_must_be_positive_int():
    with pytest.raises(ValueError):
        LpUnlockExitWatcher(flag_ttl_seconds=0)
    with pytest.raises(TypeError):
        LpUnlockExitWatcher(flag_ttl_seconds=True)


# ===========================================================================
# (b) ExitEngine consumption of the pre-set flag — mutation-canary tests
# ===========================================================================

ENTRY = Decimal("100")


def _eng(label: str = "secure_balanced") -> ExitEngine:
    return ExitEngine(config=preset(label))


def _state(eng: ExitEngine) -> ExitState:
    return ExitState.at_entry(mint=MINT, entry_fill_price=ENTRY, config=eng.config)


def _tick(eng, st, mark, *, lp_unlock=False, sellability=False, bt=BT):
    return eng.on_tick(
        st,
        mark_price=mark,
        block_time_ms=bt,
        lp_unlock_approaching_flag=lp_unlock,
        sellability_degraded_flag=sellability,
    )


def test_lp_unlock_flag_forces_secure_full_exit_on_healthy_mark():
    """MUTATION CANARY: a fully HEALTHY mark (mark == entry: would HOLD) is force-exited
    when the pre-set lp_unlock flag is on — full remainder, SECURE routing.  Remove the
    branch and this stops force-exiting."""
    eng = _eng()
    d = _tick(eng, _state(eng), Decimal("100"), lp_unlock=True)
    assert d.reason is ExitReason.LP_UNLOCK_APPROACHING
    assert isinstance(d.intent, ExitIntent)  # full flatten, never a ReduceIntent
    assert d.fraction_bps == 10_000
    assert d.next_state.remaining_bps == 0
    assert d.exit_mode is MevExitMode.SECURE
    assert d.intent.exit_mode == "secure"


def test_lp_unlock_flag_unset_is_no_change():
    eng = _eng()
    d_hold = _tick(eng, _state(eng), Decimal("100"), lp_unlock=False)
    assert d_hold.reason is ExitReason.NONE
    assert d_hold.intent is None
    d_tp = _tick(eng, _state(eng), ENTRY * Decimal("1.5"), lp_unlock=False)
    assert d_tp.reason is ExitReason.TAKE_PROFIT
    assert isinstance(d_tp.intent, ReduceIntent)


def test_lp_unlock_overrides_hard_stop():
    """On a stop breach AND lp_unlock, the lp-unlock exit is reported (it sits ABOVE
    the mechanical hard stop — exit on the unlock window, not after the LP is pulled)."""
    eng = _eng()
    d = _tick(eng, _state(eng), Decimal("50"), lp_unlock=True)  # breaches -40% stop
    assert d.reason is ExitReason.LP_UNLOCK_APPROACHING
    assert d.exit_mode is MevExitMode.SECURE


def test_lp_unlock_overrides_take_profit():
    eng = _eng()
    d = _tick(eng, _state(eng), ENTRY * Decimal("6"), lp_unlock=True)  # 6x -> TP rung
    assert d.reason is ExitReason.LP_UNLOCK_APPROACHING
    assert isinstance(d.intent, ExitIntent)
    assert d.fraction_bps == 10_000


def test_sellability_outranks_lp_unlock():
    """sellability_degraded (delayed honeypot) sits ABOVE lp_unlock_approaching.  With
    BOTH set, sellability is reported — pins the ordering RED under a swap."""
    eng = _eng()
    d = _tick(eng, _state(eng), Decimal("100"), sellability=True, lp_unlock=True)
    assert d.reason is ExitReason.SELLABILITY_DEGRADED


def test_lp_unlock_flag_must_be_plain_bool():
    eng = _eng()
    with pytest.raises(TypeError):
        eng.on_tick(
            _state(eng),
            mark_price=Decimal("100"),
            block_time_ms=BT,
            lp_unlock_approaching_flag=1,  # type: ignore[arg-type]
        )


def test_lp_unlock_secure_even_on_fast_preset():
    eng = ExitEngine(config=preset("fast_balanced"))
    assert eng.config.default_exit_mode is MevExitMode.FAST
    d = _tick(eng, _state(eng), Decimal("100"), lp_unlock=True)
    assert d.reason is ExitReason.LP_UNLOCK_APPROACHING
    assert d.exit_mode is MevExitMode.SECURE
    assert d.intent.exit_mode == "secure"


def test_lp_unlock_is_de_risk_only():
    eng = _eng()
    st = _state(eng)
    d = _tick(eng, st, Decimal("120"), lp_unlock=True)
    assert d.next_state.remaining_bps <= st.remaining_bps
    assert isinstance(d.intent, ExitIntent)
    assert 1 <= d.intent.fraction_bps <= 10_000
