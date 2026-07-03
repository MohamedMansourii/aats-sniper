"""E16 (risk half) — dev-funded-just-before-launch de-risk consumer tests.

Self-check items verified here (per the E16 directive):
  1. A wallet first-funded WITHIN the reject window before deploy is REJECTED
     via the pre-gate veto path (same shape as blacklist.py / E15).
  2. A wallet first-funded in the wider down-weight band DOWN-WEIGHTS
     conviction (multiplier < 1, clamped) WITHOUT rejecting.
  3. An OLD/established wallet (funding age beyond the down-weight band) is
     NOT down-weighted -- PASS, multiplier == 1, SILENT.
  4. UNDECODABLE funding history is REJECTED (refuse-by-default; unknown !=
     safe) -- via a DISTINCT reason/red-flag from a confirmed fresh-funded
     reject (honest tagging).
  5. STRUCTURAL clamp: conviction_multiplier can never exceed 1 -- an attempt
     to construct a decision that would violate this raises (not an assert,
     so it survives python -O).
  6. Pre-gate composition: REJECT short-circuits BEFORE next_step ever runs;
     DOWN_WEIGHT/PASS do not.
  7. No win_rate / success_rate / rug_rate field anywhere (grep-assert).
  8. Tighten-only thresholds (widening the reject/down-weight window only
     rejects/down-weights MORE).
  9. apply() never exceeds the input size (de-risk only); rejects float/bool.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from aats.ingestion.dev_funding_age import DevFundingAgeResult, FundingAgeStatus
from aats.risk import dev_funding_age_gate as gate_mod
from aats.risk.dev_funding_age_gate import (
    DevFundingAgeDecision,
    DevFundingAgeGate,
    DevFundingAgePreGateResult,
    DevFundingAgeThresholds,
    DevFundingAgeVerdict,
    evaluate_with_dev_funding_age,
)
from aats.risk.pretrade_gate import RedFlag, RedFlagHit

FRESH_WALLET = "FreshBurnerWallet111111111111111111111111"
MEDIUM_WALLET = "MediumAgeWallet2222222222222222222222222"
OLD_WALLET = "OldEstablishedWallet33333333333333333333"
UNKNOWN_WALLET = "UnknownWallet44444444444444444444444444"
DEPLOY_SLOT = 300_500_000
DEPLOY_BLOCK_TIME_MS = 1_750_000_000_000


def _found(wallet: str, *, age_ms: int, age_slots: int = 100) -> DevFundingAgeResult:
    return DevFundingAgeResult(
        creator_wallet=wallet,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
        status=FundingAgeStatus.FOUND,
        first_funding_slot=DEPLOY_SLOT - age_slots,
        first_funding_block_time_ms=DEPLOY_BLOCK_TIME_MS - age_ms,
        first_funding_amount_lamports=5_000_000_000,
        first_funding_signature="sigX",
        funding_age_slots=age_slots,
        funding_age_ms=age_ms,
    )


def _undecodable(wallet: str) -> DevFundingAgeResult:
    return DevFundingAgeResult(
        creator_wallet=wallet,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
        status=FundingAgeStatus.UNDECODABLE,
    )


# ---------------------------------------------------------------------------
# 1. Fresh-funded (within reject window) -> REJECT
# ---------------------------------------------------------------------------


def test_wallet_funded_seconds_before_deploy_rejected():
    g = DevFundingAgeGate()
    decision = g.evaluate(_found(FRESH_WALLET, age_ms=30_000))  # 30s before deploy
    assert decision.verdict is DevFundingAgeVerdict.REJECT
    assert decision.rejected is True
    assert decision.passed is False
    assert decision.conviction_multiplier == 0
    assert decision.red_flag_hit is not None
    assert decision.red_flag_hit.flag is RedFlag.DEV_FUNDED_JUST_BEFORE_LAUNCH
    assert decision.refused_undecodable is False


def test_wallet_funded_at_reject_boundary_rejected():
    """Boundary inclusive: exactly at reject_max_age_seconds still REJECTS."""
    g = DevFundingAgeGate(DevFundingAgeThresholds(reject_max_age_seconds=120))
    decision = g.evaluate(_found(FRESH_WALLET, age_ms=120_000))
    assert decision.verdict is DevFundingAgeVerdict.REJECT


# ---------------------------------------------------------------------------
# 2. Down-weight band
# ---------------------------------------------------------------------------


def test_wallet_funded_minutes_before_deploy_downweighted():
    g = DevFundingAgeGate()
    decision = g.evaluate(_found(MEDIUM_WALLET, age_ms=600_000))  # 10 minutes before deploy
    assert decision.verdict is DevFundingAgeVerdict.DOWN_WEIGHT
    assert decision.rejected is False
    assert decision.passed is True
    assert decision.down_weighted is True
    assert 0 <= decision.conviction_multiplier < 1
    assert decision.red_flag_hit is None


def test_downweight_shrinks_size_never_grows_it():
    g = DevFundingAgeGate()
    decision = g.evaluate(_found(MEDIUM_WALLET, age_ms=600_000))
    shrunk = decision.apply(1_000_000)
    assert 0 <= shrunk < 1_000_000


# ---------------------------------------------------------------------------
# 3. Old/established wallet -> PASS, silent
# ---------------------------------------------------------------------------


def test_old_established_wallet_not_downweighted():
    g = DevFundingAgeGate()
    thirty_days_ms = 30 * 24 * 60 * 60 * 1_000
    decision = g.evaluate(_found(OLD_WALLET, age_ms=thirty_days_ms))
    assert decision.verdict is DevFundingAgeVerdict.PASS
    assert decision.conviction_multiplier == 1
    assert decision.red_flag_hit is None
    assert decision.apply(1_000_000) == 1_000_000  # size UNCHANGED


# ---------------------------------------------------------------------------
# 4. UNDECODABLE -> REJECT (refuse-by-default; distinct from confirmed-fresh)
# ---------------------------------------------------------------------------


def test_undecodable_funding_history_rejected():
    g = DevFundingAgeGate()
    decision = g.evaluate(_undecodable(UNKNOWN_WALLET))
    assert decision.verdict is DevFundingAgeVerdict.REJECT
    assert decision.conviction_multiplier == 0
    assert decision.red_flag_hit is not None
    assert decision.red_flag_hit.flag is RedFlag.DEV_FUNDING_UNDECODABLE
    assert decision.refused_undecodable is True
    assert decision.funding_age_ms is None
    assert decision.funding_age_slots is None


def test_undecodable_reject_is_distinct_from_confirmed_fresh_reject():
    """Honest tagging: 'we couldn't check' must never be conflated with 'we
    proved this wallet is a rug tell' -- different RedFlag values."""
    g = DevFundingAgeGate()
    undecodable_decision = g.evaluate(_undecodable(UNKNOWN_WALLET))
    fresh_decision = g.evaluate(_found(FRESH_WALLET, age_ms=1_000))
    assert undecodable_decision.red_flag_hit.flag != fresh_decision.red_flag_hit.flag
    assert undecodable_decision.refused_undecodable is True
    assert fresh_decision.refused_undecodable is False


# ---------------------------------------------------------------------------
# 5. Structural clamp -- cannot construct an invalid decision
# ---------------------------------------------------------------------------


def test_reject_decision_must_carry_zero_multiplier():
    with pytest.raises(ValueError):
        DevFundingAgeDecision(
            creator_wallet=FRESH_WALLET,
            verdict=DevFundingAgeVerdict.REJECT,
            conviction_multiplier=1,  # invalid
            reason_codes=("dev_funded_just_before_launch",),
            funding_age_slots=100,
            funding_age_ms=30_000,
            deploy_event_slot=DEPLOY_SLOT,
            event_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            red_flag_hit=None,
        )


def test_pass_decision_cannot_carry_a_red_flag():
    with pytest.raises(ValueError):
        DevFundingAgeDecision(
            creator_wallet=OLD_WALLET,
            verdict=DevFundingAgeVerdict.PASS,
            conviction_multiplier=1,
            reason_codes=(),
            funding_age_slots=1_000_000,
            funding_age_ms=999_999_999,
            deploy_event_slot=DEPLOY_SLOT,
            event_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            red_flag_hit=RedFlagHit(RedFlag.DEV_FUNDED_JUST_BEFORE_LAUNCH, 5, 120),
        )


def test_multiplier_above_one_is_structurally_rejected():
    with pytest.raises(ValueError):
        DevFundingAgeDecision(
            creator_wallet=OLD_WALLET,
            verdict=DevFundingAgeVerdict.PASS,
            conviction_multiplier=2,  # attempted "size up" -- must raise
            reason_codes=(),
            funding_age_slots=1_000_000,
            funding_age_ms=999_999_999,
            deploy_event_slot=DEPLOY_SLOT,
            event_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            red_flag_hit=None,
        )


def test_downweight_multiplier_of_one_is_structurally_rejected():
    with pytest.raises(ValueError):
        DevFundingAgeDecision(
            creator_wallet=MEDIUM_WALLET,
            verdict=DevFundingAgeVerdict.DOWN_WEIGHT,
            conviction_multiplier=1,  # a DOWN_WEIGHT must be strictly < 1
            reason_codes=("fresh_funding_downweight",),
            funding_age_slots=100,
            funding_age_ms=600_000,
            deploy_event_slot=DEPLOY_SLOT,
            event_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            red_flag_hit=None,
        )


def test_reject_red_flag_must_be_one_of_the_two_e16_flags():
    with pytest.raises(ValueError):
        DevFundingAgeDecision(
            creator_wallet=FRESH_WALLET,
            verdict=DevFundingAgeVerdict.REJECT,
            conviction_multiplier=0,
            reason_codes=("dev_funded_just_before_launch",),
            funding_age_slots=100,
            funding_age_ms=30_000,
            deploy_event_slot=DEPLOY_SLOT,
            event_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            red_flag_hit=RedFlagHit(RedFlag.SERIAL_DEPLOYER_RUGGED, 1, 1),  # wrong flag
        )


# ---------------------------------------------------------------------------
# 6. Pre-gate composition
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
    result = evaluate_with_dev_funding_age(
        gate=DevFundingAgeGate(), result=_found(FRESH_WALLET, age_ms=1_000), next_step=spy
    )
    assert isinstance(result, DevFundingAgePreGateResult)
    assert result.passed is False
    assert result.rejected_by_funding_age is True
    assert result.next_result is None
    assert spy.called is False  # PROOF the downstream step never ran


def test_undecodable_reject_short_circuits_before_next_step_runs():
    spy = _NextStepSpy(passed=True)
    result = evaluate_with_dev_funding_age(
        gate=DevFundingAgeGate(), result=_undecodable(UNKNOWN_WALLET), next_step=spy
    )
    assert result.passed is False
    assert spy.called is False


def test_downweight_does_not_short_circuit_next_step_runs():
    spy = _NextStepSpy(passed=True)
    result = evaluate_with_dev_funding_age(
        gate=DevFundingAgeGate(), result=_found(MEDIUM_WALLET, age_ms=600_000), next_step=spy
    )
    assert result.passed is True
    assert result.rejected_by_funding_age is False
    assert spy.called is True
    assert result.next_result is not None
    assert result.decision.down_weighted is True


def test_pass_runs_next_step_and_passes():
    spy = _NextStepSpy(passed=True)
    thirty_days_ms = 30 * 24 * 60 * 60 * 1_000
    result = evaluate_with_dev_funding_age(
        gate=DevFundingAgeGate(), result=_found(OLD_WALLET, age_ms=thirty_days_ms), next_step=spy
    )
    assert spy.called is True
    assert result.passed is True


def test_next_step_reject_still_propagates_when_funding_age_passes():
    spy = _NextStepSpy(passed=False)
    thirty_days_ms = 30 * 24 * 60 * 60 * 1_000
    result = evaluate_with_dev_funding_age(
        gate=DevFundingAgeGate(), result=_found(OLD_WALLET, age_ms=thirty_days_ms), next_step=spy
    )
    assert spy.called is True
    assert result.passed is False  # downstream gate rejected -- honored


# ---------------------------------------------------------------------------
# 7. No win_rate / success_rate / rug_rate field anywhere (grep-assert)
# ---------------------------------------------------------------------------


def test_no_rate_field_anywhere_in_gate_module():
    src = inspect.getsource(gate_mod)
    for forbidden in ("win_rate", "success_rate", "rug_rate", "hit_rate"):
        assert forbidden not in src


def test_no_rate_field_on_decision_dataclass():
    field_names = {f.name for f in dataclasses.fields(DevFundingAgeDecision)}
    for forbidden in ("win_rate", "success_rate", "rug_rate", "hit_rate", "rate"):
        assert forbidden not in field_names


# ---------------------------------------------------------------------------
# 8. Threshold validation -- tighten-only
# ---------------------------------------------------------------------------


def test_downweight_threshold_below_reject_threshold_raises():
    with pytest.raises(ValueError):
        DevFundingAgeThresholds(reject_max_age_seconds=200, downweight_max_age_seconds=100)


def test_downweight_multiplier_must_be_strictly_below_one():
    with pytest.raises(ValueError):
        DevFundingAgeThresholds(downweight_multiplier="1.0")


def test_downweight_multiplier_cannot_be_negative():
    with pytest.raises(ValueError):
        DevFundingAgeThresholds(downweight_multiplier="-0.1")


def test_widening_reject_window_only_rejects_more():
    """Tightening (raising reject_max_age_seconds) turns a previously
    DOWN_WEIGHT case into a REJECT -- never the reverse."""
    narrow = DevFundingAgeGate(DevFundingAgeThresholds(reject_max_age_seconds=60))
    wide = DevFundingAgeGate(DevFundingAgeThresholds(reject_max_age_seconds=700))

    result = _found(MEDIUM_WALLET, age_ms=600_000)  # 600s
    narrow_decision = narrow.evaluate(result)
    wide_decision = wide.evaluate(result)

    assert narrow_decision.verdict is DevFundingAgeVerdict.DOWN_WEIGHT
    assert wide_decision.verdict is DevFundingAgeVerdict.REJECT


def test_from_env_defaults_when_unset():
    thresholds = DevFundingAgeThresholds.from_env({})
    assert thresholds == DevFundingAgeThresholds()


def test_from_env_overrides():
    thresholds = DevFundingAgeThresholds.from_env(
        {
            "DEV_FUNDING_AGE_REJECT_MAX_SECONDS": "60",
            "DEV_FUNDING_AGE_DOWNWEIGHT_MAX_SECONDS": "900",
            "DEV_FUNDING_AGE_DOWNWEIGHT_MULTIPLIER": "0.10",
        }
    )
    assert thresholds.reject_max_age_seconds == 60
    assert thresholds.downweight_max_age_seconds == 900
    assert (
        thresholds.downweight_multiplier == 0.10 or str(thresholds.downweight_multiplier) == "0.10"
    )


# ---------------------------------------------------------------------------
# 9. apply() de-risk-only invariant
# ---------------------------------------------------------------------------


def test_apply_rejects_float_size():
    g = DevFundingAgeGate()
    thirty_days_ms = 30 * 24 * 60 * 60 * 1_000
    decision = g.evaluate(_found(OLD_WALLET, age_ms=thirty_days_ms))
    with pytest.raises(TypeError):
        decision.apply(1000.0)  # type: ignore[arg-type]


def test_apply_reject_verdict_yields_zero():
    g = DevFundingAgeGate()
    decision = g.evaluate(_found(FRESH_WALLET, age_ms=1_000))
    assert decision.apply(1_000_000) == 0


def test_apply_undecodable_reject_yields_zero():
    g = DevFundingAgeGate()
    decision = g.evaluate(_undecodable(UNKNOWN_WALLET))
    assert decision.apply(1_000_000) == 0


# ---------------------------------------------------------------------------
# Malformed FOUND result -- refuse-by-default, never silently trusted
# ---------------------------------------------------------------------------


def test_malformed_found_result_negative_age_raises():
    class _BadResult:
        creator_wallet = "Bad"
        deploy_event_slot = DEPLOY_SLOT
        deploy_block_time_ms = DEPLOY_BLOCK_TIME_MS
        status = FundingAgeStatus.FOUND
        funding_age_slots = 100
        funding_age_ms = -1  # malformed

    with pytest.raises(ValueError):
        DevFundingAgeGate().evaluate(_BadResult())
