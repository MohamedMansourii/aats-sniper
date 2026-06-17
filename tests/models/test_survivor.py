"""Tests for the slow-loop SURVIVOR model (T-312).

Self-check criteria (charter §6; EDGE-VERDICT; data-models §4; BUILD-DIRECTIVE):
  1. NO-LEAK: a label/truth_* column CANNOT enter the survivor covariate matrix (lineage
     taint guard); the leak audit asserts feature event-time <= decision-time on every row.
  2. OOS > BASELINE: on a time-forward split the survivor model's calibrated probability
     beats the frozen naive-momentum baseline (AUC edge) out-of-sample.
  3. CALIBRATION: reliability curve + Brier + ECE delivered and within declared tolerance
     on a held-out fold.
  4. OUTPUT CONTRACT: SurvivorPrediction is calibrated prob + uncertainty + ORDERED
     quantiles (p10<=p50<=p90) + vol — NO price, NO win-rate, NO decision.
  5. SLOW-LOOP ONLY: the survivor model REFUSES to run on the FAST/SNIPE loop.
  6. ADVERSARIAL / DE-RISK SIGN: manufactured hype (synchronicity / coordinated shill) and
     being behind smart money / whale concentration / sell tax can only push P(survive)
     DOWN (monotone non-increasing) — never bullish.
  7. REPRODUCIBILITY: re-training from the pinned seed reproduces predictions bit-for-bit.
  8. BOOTSTRAP MARKER + NO WIN-RATE: artifact + prediction stamped is_bootstrap_not_real;
     no win-rate field anywhere.

Offline / injectable / deterministic: runs on the synthetic bootstrap corpus in-memory.
No RPC, no LLM, no Geyser, no Redis, no network, no keypair.  Fixed seed.
"""

from __future__ import annotations

import numpy as np
import pytest
from sklearn.metrics import roc_auc_score

from aats.contracts.models import MCSScore
from aats.models.baseline import evaluate_baseline, load_params
from aats.models.calibration import (
    brier_score,
    expected_calibration_error,
)
from aats.models.featureset import FEATURE_COLUMNS
from aats.models.survivor import (
    N_SURVIVOR_COVARIATES,
    SURVIVOR_COVARIATE_COLUMNS,
    SURVIVOR_LOOP,
    SURVIVOR_MONOTONE_NONDECREASING,
    SURVIVOR_MONOTONE_NONINCREASING,
    SurvivorLoopViolation,
    SurvivorPrediction,
    assert_slow_loop_only,
    build_survivor_covariate_row,
    survivor_monotone_vector,
    train_survivor_model,
)
from aats.models.synthetic import generate_synthetic_corpus
from aats.models.training import (
    assert_event_time_leq_decision,
    event_time_key,
    join_labels_by_event_time,
    time_forward_split,
)

SEED = 20260616
N = 3000


@pytest.fixture(scope="module")
def corpus():
    return generate_synthetic_corpus(n=N, seed=SEED)


@pytest.fixture(scope="module")
def survivor(corpus):
    return train_survivor_model(corpus, seed=SEED, calibration_method="isotonic")


@pytest.fixture(scope="module")
def split(corpus):
    return time_forward_split(join_labels_by_event_time(corpus))


def _survivor_oos_matrix(corpus, split):
    """Re-extract survivor covariates for the OOS test fold + the labels."""
    rows_by_key = {event_time_key(r.frame.event_time): r for r in corpus}
    X, y = [], []
    for ex in split.test:
        src = rows_by_key[event_time_key(ex.feature_event_time)]
        X.append(build_survivor_covariate_row(src.frame, getattr(src, "mcs", None)))
        y.append(ex.label)
    return np.asarray(X, dtype=np.float32), np.asarray(y, dtype=np.int32)


# ---------------------------------------------------------------------------
# 1. NO-LEAK
# ---------------------------------------------------------------------------


class TestNoLeak:
    def test_survivor_covariates_disjoint_from_labels(self):
        from aats.contracts.provenance import LABEL_FIELD_NAMES

        assert set(SURVIVOR_COVARIATE_COLUMNS).isdisjoint(LABEL_FIELD_NAMES)
        assert not any(c.startswith("truth_") for c in SURVIVOR_COVARIATE_COLUMNS)

    def test_lineage_taint_guard_rejects_label_in_survivor_matrix(self):
        from aats.contracts.provenance import ProvenanceTaintError
        from aats.models.featureset import assert_no_label_taint

        with pytest.raises(ProvenanceTaintError):
            assert_no_label_taint([*SURVIVOR_COVARIATE_COLUMNS, "truth_is_rug"])
        with pytest.raises(ProvenanceTaintError):
            assert_no_label_taint([*SURVIVOR_COVARIATE_COLUMNS, "realized_mult_decimal"])

    def test_training_runs_leak_audit(self, survivor):
        assert "LEAK AUDIT PASS" in survivor.leak_audit_summary

    def test_leak_audit_passes_on_clean_corpus(self, corpus):
        joined = join_labels_by_event_time(corpus)
        summary = assert_event_time_leq_decision(joined)
        assert "LEAK AUDIT PASS" in summary

    def test_covariate_row_length_and_floats(self, corpus):
        row = build_survivor_covariate_row(corpus[0].frame, None)
        assert len(row) == N_SURVIVOR_COVARIATES
        assert all(isinstance(v, float) for v in row)


# ---------------------------------------------------------------------------
# 2. OOS > BASELINE
# ---------------------------------------------------------------------------


class TestBeatsBaseline:
    def test_survivor_beats_baseline_oos_auc(self, survivor, corpus, split):
        X_test, y_test = _survivor_oos_matrix(corpus, split)
        cal = survivor.predict_calibrated(X_test)
        model_auc = roc_auc_score(y_test, cal)

        params = load_params()
        from decimal import Decimal

        from aats.features.buy_pressure import BuyPressureFeatures

        col = {c: i for i, c in enumerate(FEATURE_COLUMNS)}
        base = []
        for ex in split.test:
            bp = ex.feature_row[col["first_k_buy_pressure"]]
            vol = ex.feature_row[col["first_k_volume_lamports"]]
            feats = BuyPressureFeatures(
                first_k_buy_pressure=Decimal(str(int(bp))),
                first_k_volume_lamports=int(vol),
                first_k_buy_count=0,
                first_k_sell_count=0,
                first_k_unique_buyers=0,
                max_source_slot=ex.feature_event_time.slot + params.first_k_slots,
                event_time=ex.feature_event_time,
                first_k_slots=params.first_k_slots,
                provenance=None,  # type: ignore[arg-type]
            )
            base.append(evaluate_baseline(feats, params, mint="x").baseline_p)
        base_auc = roc_auc_score(y_test, base)

        assert model_auc > base_auc + 0.03, (
            f"survivor model_auc={model_auc:.4f} does not beat baseline_auc={base_auc:.4f} "
            "+0.03 OOS. On RECORDED data a failure here means STATUS != COMPLETE."
        )


# ---------------------------------------------------------------------------
# 3. CALIBRATION
# ---------------------------------------------------------------------------


class TestCalibration:
    def test_calibration_report_in_tolerance(self, survivor):
        rep = survivor.calibration_report
        assert rep.in_tolerance, rep.summary()
        assert rep.brier_score <= rep.brier_tolerance
        assert rep.ece <= rep.ece_tolerance

    def test_oos_calibration_within_tolerance(self, survivor, corpus, split):
        X_test, y_test = _survivor_oos_matrix(corpus, split)
        cal = survivor.predict_calibrated(X_test)
        ece = expected_calibration_error(cal, y_test, n_bins=10)
        brier = brier_score(cal, y_test)
        assert ece <= 0.10, f"OOS ECE={ece:.4f} exceeds tolerance 0.10"
        assert brier <= 0.25, f"OOS Brier={brier:.4f} exceeds tolerance 0.25"


# ---------------------------------------------------------------------------
# 4. OUTPUT CONTRACT (prob + uncertainty + ORDERED quantiles + vol; NO price/win-rate)
# ---------------------------------------------------------------------------


class TestOutputContract:
    def test_prediction_fields_are_prob_unc_quantiles_vol_only(self, survivor, corpus):
        pred = survivor.predict(corpus[0].frame, None)
        assert isinstance(pred, SurvivorPrediction)
        fields = set(pred.__dataclass_fields__.keys())
        for forbidden in ("price", "target_price", "win_rate", "winrate", "size", "decision"):
            assert forbidden not in fields
        assert fields == {
            "mint",
            "event_time",
            "model_version",
            "p_calibrated",
            "uncertainty",
            "p10",
            "p50",
            "p90",
            "vol_estimate",
            "is_bootstrap_not_real",
        }

    def test_quantiles_ordered_and_bounded(self, survivor, corpus):
        for r in corpus[:200]:
            pred = survivor.predict(r.frame, None)
            assert 0.0 <= pred.p10 <= pred.p50 <= pred.p90 <= 1.0
            assert 0.0 <= pred.p_calibrated <= 1.0
            assert 0.0 <= pred.uncertainty <= 1.0
            assert 0.0 <= pred.vol_estimate <= 1.0

    def test_quantile_spread_widens_with_uncertainty(self, survivor, corpus):
        """A near-0.5 prediction (max Bernoulli variance) has a WIDER p10..p90 spread than a
        confident one — the spread is a de-risk signal, monotone in uncertainty."""
        preds = [survivor.predict(r.frame, None) for r in corpus[:1000]]
        # sort by distance of p_calibrated from 0.5 (closer => more uncertain)
        confident = [p for p in preds if abs(p.p_calibrated - 0.5) > 0.3]
        uncertain = [p for p in preds if abs(p.p_calibrated - 0.5) < 0.1]
        if confident and uncertain:
            mean_conf_spread = np.mean([p.p90 - p.p10 for p in confident])
            mean_unc_spread = np.mean([p.p90 - p.p10 for p in uncertain])
            assert mean_unc_spread >= mean_conf_spread


# ---------------------------------------------------------------------------
# 5. SLOW-LOOP ONLY (never FAST/SNIPE)
# ---------------------------------------------------------------------------


class TestSlowLoopOnly:
    def test_assert_slow_loop_only_accepts_slow(self):
        assert_slow_loop_only(SURVIVOR_LOOP)  # must not raise

    @pytest.mark.parametrize("bad_loop", ["fast", "snipe", "hot"])
    def test_refuses_fast_or_snipe_loop(self, survivor, corpus, bad_loop):
        with pytest.raises(SurvivorLoopViolation):
            survivor.predict(corpus[0].frame, None, loop=bad_loop)

    def test_artifact_marked_slow_loop(self, survivor):
        assert survivor.loop == "slow"


# ---------------------------------------------------------------------------
# 6. ADVERSARIAL / DE-RISK SIGN (manufactured hype / behind smart money => P(survive) DOWN)
# ---------------------------------------------------------------------------


class TestDeRiskSign:
    def test_monotone_vector_signs(self):
        mono = survivor_monotone_vector()
        assert len(mono) == N_SURVIVOR_COVARIATES
        for i, col in enumerate(SURVIVOR_COVARIATE_COLUMNS):
            if col in SURVIVOR_MONOTONE_NONINCREASING:
                assert mono[i] == -1, f"{col} must be non-increasing (de-risk)"
            elif col in SURVIVOR_MONOTONE_NONDECREASING:
                assert mono[i] == 1, f"{col} must be non-decreasing"
            else:
                assert mono[i] == 0, f"{col} must be unconstrained"

    def test_synchronicity_constrained_nonincreasing(self):
        """Manufactured hype (high synchronicity) is contrarian — pinned non-increasing."""
        assert "mcs_synchronicity" in SURVIVOR_MONOTONE_NONINCREASING
        assert "mcs_coordinated_shill_flag" in SURVIVOR_MONOTONE_NONINCREASING

    def test_higher_concentration_does_not_raise_survival(self, survivor, corpus):
        col = {c: i for i, c in enumerate(SURVIVOR_COVARIATE_COLUMNS)}
        base = build_survivor_covariate_row(corpus[0].frame, None)
        low = list(base)
        low[col["holder_concentration_top10_bps"]] = 1000.0
        high = list(base)
        high[col["holder_concentration_top10_bps"]] = 9000.0
        X = np.asarray([low, high], dtype=np.float32)
        p = survivor.predict_calibrated(X)
        assert p[1] <= p[0] + 1e-9, (
            "higher whale concentration RAISED survival prob — the model treats a rug-risk "
            "signal as bullish, which is forbidden (adversarial / de-risk rule)."
        )

    def test_more_synchronicity_does_not_raise_survival(self, survivor, corpus):
        """Coordinated shill (high synchronicity) must push P(survive) DOWN, never up."""
        col = {c: i for i, c in enumerate(SURVIVOR_COVARIATE_COLUMNS)}
        base = build_survivor_covariate_row(corpus[0].frame, None)
        base[col["mcs_present"]] = 1.0
        low = list(base)
        low[col["mcs_synchronicity"]] = 0.0
        high = list(base)
        high[col["mcs_synchronicity"]] = 1.0
        X = np.asarray([low, high], dtype=np.float32)
        p = survivor.predict_calibrated(X)
        assert p[1] <= p[0] + 1e-9, (
            "higher synchronicity (manufactured hype) RAISED survival prob — forbidden "
            "(adversarial-sentiment rule: coordinated shill is a CONTRARIAN risk signal)."
        )

    def test_mcs_covariate_extraction_uses_real_mcs(self, corpus):
        """MCS covariates are read from a real MCSScore when present; sentinels when absent."""
        from aats.contracts.events import EventTime

        mcs = MCSScore(
            asset="x",
            event_time=EventTime(slot=1, block_time_ms=1, wall_clock_ms=1),
            conviction=0.2,
            momentum=0.3,
            novelty=0.4,
            synchronicity=0.9,
            account_age_median_days=2.0,
            coordinated_shill_flag=True,
            red_flags=["coordinated_shill"],
            post_count=100,
            reasoning="quoted untrusted text",
        )
        col = {c: i for i, c in enumerate(SURVIVOR_COVARIATE_COLUMNS)}
        with_mcs = build_survivor_covariate_row(corpus[0].frame, mcs)
        without = build_survivor_covariate_row(corpus[0].frame, None)
        assert with_mcs[col["mcs_present"]] == 1.0
        assert without[col["mcs_present"]] == 0.0
        assert with_mcs[col["mcs_synchronicity"]] == 0.9
        assert with_mcs[col["mcs_coordinated_shill_flag"]] == 1.0


# ---------------------------------------------------------------------------
# 7. REPRODUCIBILITY
# ---------------------------------------------------------------------------


class TestReproducibility:
    def test_retrain_same_seed_identical_predictions(self, corpus, split):
        a1 = train_survivor_model(corpus, seed=SEED)
        a2 = train_survivor_model(corpus, seed=SEED)
        X_test, _ = _survivor_oos_matrix(corpus, split)
        p1 = a1.predict_calibrated(X_test)
        p2 = a2.predict_calibrated(X_test)
        assert float(np.max(np.abs(p1 - p2))) == 0.0


# ---------------------------------------------------------------------------
# 8. BOOTSTRAP MARKER + NO WIN-RATE (honesty)
# ---------------------------------------------------------------------------


class TestBootstrapAndHonesty:
    def test_artifact_marked_bootstrap(self, survivor):
        assert survivor.is_bootstrap_not_real is True
        assert "BOOTSTRAP" in survivor.model_version

    def test_prediction_marked_bootstrap(self, survivor, corpus):
        pred = survivor.predict(corpus[0].frame, None)
        assert pred.is_bootstrap_not_real is True

    def test_no_win_rate_field_anywhere(self, survivor, corpus):
        pred = survivor.predict(corpus[0].frame, None)
        assert not any("win" in f.lower() for f in pred.__dataclass_fields__)

    def test_survivor_prediction_rejects_unordered_quantiles(self):
        from aats.contracts.events import EventTime

        with pytest.raises(ValueError):
            SurvivorPrediction(
                mint="x",
                event_time=EventTime(slot=1, block_time_ms=1, wall_clock_ms=1),
                model_version="survivor-test",
                p_calibrated=0.5,
                uncertainty=0.5,
                p10=0.8,  # p10 > p90 — must raise
                p50=0.5,
                p90=0.2,
                vol_estimate=0.5,
                is_bootstrap_not_real=True,
            )
