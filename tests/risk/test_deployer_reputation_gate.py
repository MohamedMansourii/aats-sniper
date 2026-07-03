"""E15 (risk half) — serial-deployer reputation de-risk consumer tests.

Self-check items verified here (per the E15 directive):
  1. A wallet with a prior RUG (2+, past the reject threshold) triggers REJECT
     via the pre-gate veto path (same shape as blacklist.py's evaluate_with_denylist).
  2. A wallet with a single prior rug DOWN-WEIGHTS conviction (multiplier < 1,
     clamped, same shape as anti_fomo.py) WITHOUT rejecting.
  3. A clean/unknown wallet (prior_rugs == 0) is NOT down-weighted -- PASS,
     multiplier == 1, SILENT.
  4. STRUCTURAL clamp: conviction_multiplier can never exceed 1 -- an attempt to
     construct a decision that would violate this raises (not an assert, so it
     survives python -O).
  5. Pre-gate composition: REJECT short-circuits BEFORE next_step ever runs
     (next_result is None proves it) -- mirrors evaluate_with_denylist exactly.
  6. DOWN_WEIGHT does NOT short-circuit -- next_step still runs.
  7. No win_rate / success_rate / rug_rate field anywhere (grep-assert).
  8. Tightening (raising reject/downweight thresholds is NOT allowed by
     lowering being safe) -- LOWERING a threshold only rejects/down-weights MORE.
  9. apply() never exceeds the input size (de-risk only).
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from aats.contracts.events import LaunchOutcomeLabel
from aats.ingestion.deployer_reputation import (
    DeployerLaunchOutcome,
    DeployerReputationTally,
    InMemoryDeployerOutcomeStore,
    lookup_deployer_reputation,
)
from aats.risk import deployer_reputation_gate as gate_mod
from aats.risk.deployer_reputation_gate import (
    DeployerReputationDecision,
    DeployerReputationGate,
    DeployerReputationPreGateResult,
    DeployerReputationThresholds,
    DeployerReputationVerdict,
    evaluate_with_deployer_reputation,
)
from aats.risk.pretrade_gate import RedFlag

RUGGER = "RuggerWallet1111111111111111111111111111"
SINGLE_RUG = "SingleRugWallet22222222222222222222222222"
CLEAN = "CleanWallet3333333333333333333333333333333"
CURRENT_SLOT = 300_500_000
BLOCK_TIME_MS = 1_718_500_000_000


def _tally(creator_wallet: str, prior_launches: int, prior_rugs: int) -> DeployerReputationTally:
    return DeployerReputationTally(
        creator_wallet=creator_wallet,
        deploy_template_fingerprint=None,
        as_of_event_slot=CURRENT_SLOT,
        prior_launches=prior_launches,
        prior_rugs=prior_rugs,
    )


# ---------------------------------------------------------------------------
# 1. Repeat-rug wallet -> REJECT
# ---------------------------------------------------------------------------


def test_repeat_rug_wallet_rejected():
    g = DeployerReputationGate()
    decision = g.evaluate(_tally(RUGGER, prior_launches=2, prior_rugs=2))
    assert decision.verdict is DeployerReputationVerdict.REJECT
    assert decision.rejected is True
    assert decision.passed is False
    assert decision.conviction_multiplier == 0
    assert decision.red_flag_hit is not None
    assert decision.red_flag_hit.flag is RedFlag.SERIAL_DEPLOYER_RUGGED


def test_repeat_rug_end_to_end_from_ingestion_store():
    """A wallet with a prior RUG (outcome resolved BEFORE this launch) triggers
    reject/down-weight -- proven end-to-end from the ingestion store through the
    risk gate."""
    store = InMemoryDeployerOutcomeStore(
        [
            DeployerLaunchOutcome(
                creator_wallet=RUGGER,
                mint="MintA",
                launch_event_slot=CURRENT_SLOT - 100_000,
                outcome_slot=CURRENT_SLOT - 1_000,
                label=LaunchOutcomeLabel.RUGGED,
            ),
            DeployerLaunchOutcome(
                creator_wallet=RUGGER,
                mint="MintB",
                launch_event_slot=CURRENT_SLOT - 90_000,
                outcome_slot=CURRENT_SLOT - 500,
                label=LaunchOutcomeLabel.RUGGED,
            ),
        ]
    )
    tally = lookup_deployer_reputation(store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_SLOT)
    decision = DeployerReputationGate().evaluate(tally)
    assert decision.verdict is DeployerReputationVerdict.REJECT


# ---------------------------------------------------------------------------
# 2. Single prior rug -> DOWN_WEIGHT (not reject)
# ---------------------------------------------------------------------------


def test_single_prior_rug_downweights_not_rejects():
    g = DeployerReputationGate()
    decision = g.evaluate(_tally(SINGLE_RUG, prior_launches=3, prior_rugs=1))
    assert decision.verdict is DeployerReputationVerdict.DOWN_WEIGHT
    assert decision.rejected is False
    assert decision.passed is True
    assert decision.down_weighted is True
    assert 0 <= decision.conviction_multiplier < 1
    assert decision.red_flag_hit is None


def test_downweight_shrinks_size_never_grows_it():
    g = DeployerReputationGate()
    decision = g.evaluate(_tally(SINGLE_RUG, prior_launches=1, prior_rugs=1))
    shrunk = decision.apply(1_000_000)
    assert 0 <= shrunk < 1_000_000


# ---------------------------------------------------------------------------
# 3. Clean / unknown wallet is NOT down-weighted -- silent PASS
# ---------------------------------------------------------------------------


def test_clean_wallet_not_downweighted():
    g = DeployerReputationGate()
    decision = g.evaluate(_tally(CLEAN, prior_launches=5, prior_rugs=0))
    assert decision.verdict is DeployerReputationVerdict.PASS
    assert decision.conviction_multiplier == 1
    assert decision.red_flag_hit is None
    assert decision.apply(1_000_000) == 1_000_000  # size UNCHANGED


def test_unknown_wallet_zero_launches_is_silent_pass():
    g = DeployerReputationGate()
    decision = g.evaluate(_tally(CLEAN, prior_launches=0, prior_rugs=0))
    assert decision.verdict is DeployerReputationVerdict.PASS
    assert decision.conviction_multiplier == 1


# ---------------------------------------------------------------------------
# 4. Structural clamp -- cannot construct an invalid decision
# ---------------------------------------------------------------------------


def test_reject_decision_must_carry_zero_multiplier():
    with pytest.raises(ValueError):
        DeployerReputationDecision(
            creator_wallet=RUGGER,
            deploy_template_fingerprint=None,
            verdict=DeployerReputationVerdict.REJECT,
            conviction_multiplier=1,  # invalid: a REJECT can never carry multiplier=1
            reason_codes=("serial_deployer_rugged",),
            prior_launches=2,
            prior_rugs=2,
            as_of_event_slot=CURRENT_SLOT,
            event_block_time_ms=BLOCK_TIME_MS,
            red_flag_hit=None,
        )


def test_pass_decision_cannot_carry_a_red_flag():
    from aats.risk.pretrade_gate import RedFlagHit

    with pytest.raises(ValueError):
        DeployerReputationDecision(
            creator_wallet=CLEAN,
            deploy_template_fingerprint=None,
            verdict=DeployerReputationVerdict.PASS,
            conviction_multiplier=1,
            reason_codes=(),
            prior_launches=0,
            prior_rugs=0,
            as_of_event_slot=CURRENT_SLOT,
            event_block_time_ms=BLOCK_TIME_MS,
            red_flag_hit=RedFlagHit(RedFlag.SERIAL_DEPLOYER_RUGGED, 5, 2),
        )


def test_multiplier_above_one_is_structurally_rejected():
    with pytest.raises(ValueError):
        DeployerReputationDecision(
            creator_wallet=CLEAN,
            deploy_template_fingerprint=None,
            verdict=DeployerReputationVerdict.PASS,
            conviction_multiplier=2,  # attempted "size up" -- must raise
            reason_codes=(),
            prior_launches=0,
            prior_rugs=0,
            as_of_event_slot=CURRENT_SLOT,
            event_block_time_ms=BLOCK_TIME_MS,
            red_flag_hit=None,
        )


def test_downweight_multiplier_of_one_is_structurally_rejected():
    with pytest.raises(ValueError):
        DeployerReputationDecision(
            creator_wallet=SINGLE_RUG,
            deploy_template_fingerprint=None,
            verdict=DeployerReputationVerdict.DOWN_WEIGHT,
            conviction_multiplier=1,  # a DOWN_WEIGHT must be strictly < 1
            reason_codes=("prior_rug_downweight",),
            prior_launches=1,
            prior_rugs=1,
            as_of_event_slot=CURRENT_SLOT,
            event_block_time_ms=BLOCK_TIME_MS,
            red_flag_hit=None,
        )


# ---------------------------------------------------------------------------
# 5/6. Pre-gate composition -- REJECT short-circuits, DOWN_WEIGHT does not
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
    result = evaluate_with_deployer_reputation(
        gate=DeployerReputationGate(),
        tally=_tally(RUGGER, prior_launches=2, prior_rugs=2),
        next_step=spy,
    )
    assert isinstance(result, DeployerReputationPreGateResult)
    assert result.passed is False
    assert result.rejected_by_reputation is True
    assert result.next_result is None
    assert spy.called is False  # PROOF the downstream step never ran


def test_downweight_does_not_short_circuit_next_step_runs():
    spy = _NextStepSpy(passed=True)
    result = evaluate_with_deployer_reputation(
        gate=DeployerReputationGate(),
        tally=_tally(SINGLE_RUG, prior_launches=1, prior_rugs=1),
        next_step=spy,
    )
    assert result.passed is True
    assert result.rejected_by_reputation is False
    assert spy.called is True
    assert result.next_result is not None
    assert result.decision.down_weighted is True


def test_clean_wallet_runs_next_step_and_passes():
    spy = _NextStepSpy(passed=True)
    result = evaluate_with_deployer_reputation(
        gate=DeployerReputationGate(),
        tally=_tally(CLEAN, prior_launches=10, prior_rugs=0),
        next_step=spy,
    )
    assert spy.called is True
    assert result.passed is True


def test_next_step_reject_still_propagates_when_reputation_passes():
    """The reputation gate passing does NOT force an overall pass -- the
    downstream safety gate's own verdict is honored."""
    spy = _NextStepSpy(passed=False)
    result = evaluate_with_deployer_reputation(
        gate=DeployerReputationGate(),
        tally=_tally(CLEAN, prior_launches=0, prior_rugs=0),
        next_step=spy,
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
    field_names = {f.name for f in dataclasses.fields(DeployerReputationDecision)}
    for forbidden in ("win_rate", "success_rate", "rug_rate", "hit_rate", "rate"):
        assert forbidden not in field_names


# ---------------------------------------------------------------------------
# 8. Threshold validation -- tighten-only
# ---------------------------------------------------------------------------


def test_reject_threshold_below_downweight_threshold_raises():
    with pytest.raises(ValueError):
        DeployerReputationThresholds(downweight_min_prior_rugs=3, reject_min_prior_rugs=2)


def test_downweight_multiplier_must_be_strictly_below_one():
    with pytest.raises(ValueError):
        DeployerReputationThresholds(downweight_multiplier="1.0")


def test_downweight_multiplier_cannot_be_negative():
    with pytest.raises(ValueError):
        DeployerReputationThresholds(downweight_multiplier="-0.1")


def test_lowering_reject_threshold_only_rejects_more():
    """Tightening (lowering reject_min_prior_rugs to 1) turns a previously
    DOWN_WEIGHT case into a REJECT -- never the reverse."""
    loose = DeployerReputationGate(DeployerReputationThresholds(reject_min_prior_rugs=2))
    tight = DeployerReputationGate(DeployerReputationThresholds(reject_min_prior_rugs=1))

    tally = _tally(SINGLE_RUG, prior_launches=1, prior_rugs=1)
    loose_decision = loose.evaluate(tally)
    tight_decision = tight.evaluate(tally)

    assert loose_decision.verdict is DeployerReputationVerdict.DOWN_WEIGHT
    assert tight_decision.verdict is DeployerReputationVerdict.REJECT


def test_from_env_defaults_when_unset():
    thresholds = DeployerReputationThresholds.from_env({})
    assert thresholds == DeployerReputationThresholds()


def test_from_env_overrides():
    thresholds = DeployerReputationThresholds.from_env(
        {
            "DEPLOYER_REPUTATION_DOWNWEIGHT_MIN_PRIOR_RUGS": "1",
            "DEPLOYER_REPUTATION_REJECT_MIN_PRIOR_RUGS": "3",
            "DEPLOYER_REPUTATION_DOWNWEIGHT_MULTIPLIER": "0.10",
        }
    )
    assert thresholds.reject_min_prior_rugs == 3
    assert (
        thresholds.downweight_multiplier == 0.10 or str(thresholds.downweight_multiplier) == "0.10"
    )


# ---------------------------------------------------------------------------
# 9. apply() de-risk-only invariant
# ---------------------------------------------------------------------------


def test_apply_rejects_float_size():
    g = DeployerReputationGate()
    decision = g.evaluate(_tally(CLEAN, prior_launches=0, prior_rugs=0))
    with pytest.raises(TypeError):
        decision.apply(1000.0)  # type: ignore[arg-type]


def test_apply_reject_verdict_yields_zero():
    g = DeployerReputationGate()
    decision = g.evaluate(_tally(RUGGER, prior_launches=2, prior_rugs=2))
    assert decision.apply(1_000_000) == 0


# ---------------------------------------------------------------------------
# Malformed tally -- refuse-by-default, never silently trusted
# ---------------------------------------------------------------------------


def test_malformed_tally_more_rugs_than_launches_raises():
    class _BadTally:
        creator_wallet = "Bad"
        deploy_template_fingerprint = None
        as_of_event_slot = CURRENT_SLOT
        prior_launches = 1
        prior_rugs = 5

    with pytest.raises(ValueError):
        DeployerReputationGate().evaluate(_BadTally())
