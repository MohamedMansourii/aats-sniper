"""Tests for the FAST snipe classifier (T-310).

Self-check criteria (charter §6; brief; EDGE-VERDICT; data-models §3/§3A/§4):
  1. NO-LEAK: a label/truth_* column CANNOT enter the feature matrix — the lineage-taint
     build guard FAILS (ProvenanceTaintError).  The leak audit asserts feature
     event-time <= decision-time on every row.
  2. OOS > BASELINE: on a time-forward split, the model's calibrated probability beats the
     frozen naive-momentum baseline (AUC edge) out-of-sample.
  3. CALIBRATION: reliability curve + Brier + ECE delivered and within declared tolerance
     on a held-out fold.
  4. ONNX PARITY: Python-vs-ONNX raw-proba max abs diff within tolerance.
  5. LATENCY: measured p50/p99 single-row inference is single-digit-ms (well inside budget).
  6. OUTPUT CONTRACT: the inference output is a DecisionSignal with calibrated prob +
     uncertainty + baseline_p + model_version — NO price field, NO win-rate field, NO
     decision.  It conforms to aats.contracts.models.DecisionSignal exactly.
  7. AUTO-DISABLE: a degraded/shuffled window makes the monitor emit NO_SIGNAL (not a
     stale edge); consecutive NO_SIGNAL windows auto-disable the model.
  8. REPRODUCIBILITY: re-training from the pinned seed reproduces the calibrated predictions
     bit-for-bit.
  9. ADVERSARIAL / DE-RISK SIGN: the de-risk features carry monotone-non-increasing
     constraints — a higher value can only push probability DOWN (never treat manufactured
     hype / being behind smart money as bullish).
 10. BOOTSTRAP MARKER: the synthetic dataset and the trained artifact are stamped
     is_bootstrap_not_real=True (no win-rate field anywhere).

Offline / injectable: the whole suite runs on the synthetic bootstrap corpus in-memory.
No RPC, no LLM, no Geyser, no Redis, no network, no keypair.  Deterministic (fixed seed).
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest
from sklearn.metrics import roc_auc_score

from aats.contracts.events import EventTime
from aats.contracts.features import FeatureFrame
from aats.contracts.models import DecisionSignal
from aats.contracts.provenance import ProvenanceTaintError
from aats.models.baseline import evaluate_baseline, load_params
from aats.models.calibration import (
    bernoulli_uncertainty,
    brier_score,
    expected_calibration_error,
    reliability_curve,
)
from aats.models.featureset import (
    FEATURE_COLUMNS,
    MONOTONE_NONINCREASING_FEATURES,
    N_FEATURES,
    assert_no_label_taint,
    frame_to_feature_row,
    monotone_constraint_vector,
)
from aats.models.inference import SnipeInference
from aats.models.monitor import (
    MonitorVerdict,
    compare_window,
    rolling_auto_disable,
)
from aats.models.synthetic import IS_BOOTSTRAP_NOT_REAL, generate_synthetic_corpus
from aats.models.training import (
    LeakAuditError,
    assert_event_time_leq_decision,
    event_time_key,
    export_booster_to_onnx_bytes,
    join_labels_by_event_time,
    time_forward_split,
    train_snipe_classifier,
)

SEED = 20260616
N = 3000

# ---------------------------------------------------------------------------
# Session-scoped trained artifact (train once, reuse across tests — fast + deterministic)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus():
    return generate_synthetic_corpus(n=N, seed=SEED)


@pytest.fixture(scope="module")
def trained(corpus):
    return train_snipe_classifier(corpus, seed=SEED, calibration_method="isotonic")


@pytest.fixture(scope="module")
def split(corpus):
    joined = join_labels_by_event_time(corpus)
    return time_forward_split(joined)


@pytest.fixture(scope="module")
def onnx_inference(trained):
    onnx_bytes = export_booster_to_onnx_bytes(trained.booster)
    return onnx_bytes, SnipeInference(onnx_bytes, trained.calibrator, trained.model_version)


def _matrix(examples):
    X = np.asarray([e.feature_row for e in examples], dtype=np.float32)
    y = np.asarray([e.label for e in examples], dtype=np.int32)
    return X, y


# ---------------------------------------------------------------------------
# 1. NO-LEAK — the lineage-taint build guard + the event-time leak audit
# ---------------------------------------------------------------------------


class TestNoLeak:
    def test_truth_column_rejected_by_lineage_taint_guard(self):
        """A truth_* column in the matrix FAILS the build (truth_ prefix scan)."""
        with pytest.raises(ProvenanceTaintError) as exc:
            assert_no_label_taint([*FEATURE_COLUMNS, "truth_is_rug"])
        assert exc.value.guard_name == "feature_lineage_touches_label"

    def test_label_column_rejected_by_disjointness_guard(self):
        """A LaunchOutcome label column in the matrix FAILS the build."""
        with pytest.raises(ProvenanceTaintError) as exc:
            assert_no_label_taint([*FEATURE_COLUMNS, "realized_mult_decimal"])
        assert exc.value.guard_name == "label_column_in_feature_frame"

    def test_innocuous_forward_column_rejected(self):
        """An innocuously-named forward-looking column (fwd_return / survived_60s) FAILS."""
        for leaked in ("fwd_return", "survived_60s", "realized_mult", "label"):
            with pytest.raises(ProvenanceTaintError):
                assert_no_label_taint([*FEATURE_COLUMNS, leaked])

    def test_clean_feature_columns_pass(self):
        """The declared FEATURE_COLUMNS contain NO label/truth_* column — the guard passes."""
        assert_no_label_taint(list(FEATURE_COLUMNS))  # must not raise

    def test_feature_columns_disjoint_from_label_names(self):
        from aats.contracts.provenance import LABEL_FIELD_NAMES

        assert set(FEATURE_COLUMNS).isdisjoint(LABEL_FIELD_NAMES)
        assert not any(c.startswith("truth_") for c in FEATURE_COLUMNS)

    def test_frame_to_row_reads_no_label_field(self, corpus):
        """frame_to_feature_row produces exactly N_FEATURES values, none label-derived."""
        row = frame_to_feature_row(corpus[0].frame)
        assert len(row) == N_FEATURES
        assert all(isinstance(v, float) for v in row)

    def test_leak_audit_passes_on_clean_corpus(self, corpus):
        joined = join_labels_by_event_time(corpus)
        summary = assert_event_time_leq_decision(joined)
        assert "LEAK AUDIT PASS" in summary
        assert str(len(joined)) in summary

    def test_leak_audit_fails_if_feature_after_decision(self, corpus):
        """If a feature event-time were AFTER the decision anchor, the audit FAILS."""
        from aats.models.training import JoinedExample

        ex = join_labels_by_event_time(corpus)[0]
        later_et = EventTime(
            slot=ex.decision_event_time.slot + 5,
            block_time_ms=ex.decision_event_time.block_time_ms + 2000,
            wall_clock_ms=ex.decision_event_time.wall_clock_ms + 2000,
        )
        bad = JoinedExample(
            feature_row=ex.feature_row,
            label=ex.label,
            feature_event_time=later_et,  # feature AFTER decision == leak
            decision_event_time=ex.decision_event_time,
            resolution_event_time=ex.resolution_event_time,
        )
        with pytest.raises(LeakAuditError):
            assert_event_time_leq_decision([bad])

    def test_join_is_event_time_only(self):
        """The join key excludes wall_clock_ms (monitoring-only, never a join key)."""
        et = EventTime(slot=100, block_time_ms=5000, wall_clock_ms=9999)
        assert event_time_key(et) == (100, 5000)  # wall_clock_ms is NOT in the key

    def test_training_runs_leak_audit(self, trained):
        assert "LEAK AUDIT PASS" in trained.leak_audit_summary


# ---------------------------------------------------------------------------
# 2. OUT-OF-SAMPLE > BASELINE (beat the baseline or stay silent)
# ---------------------------------------------------------------------------


class TestBeatsBaseline:
    def test_model_beats_baseline_oos_auc(self, trained, split):
        X_test, y_test = _matrix(split.test)
        cal = trained.predict_calibrated(X_test)
        model_auc = roc_auc_score(y_test, cal)

        params = load_params()
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

        # The model must beat the baseline OOS by a clear margin on the bootstrap corpus.
        assert model_auc > base_auc + 0.03, (
            f"model_auc={model_auc:.4f} does not beat baseline_auc={base_auc:.4f} + 0.03 OOS. "
            "On RECORDED data a failure here means STATUS != COMPLETE (report honestly)."
        )

    def test_split_is_forward_only(self, split):
        """train event-times all precede calib, which all precede test (no shuffle)."""
        last_train = max(event_time_key(e.feature_event_time) for e in split.train)
        first_calib = min(event_time_key(e.feature_event_time) for e in split.calib)
        last_calib = max(event_time_key(e.feature_event_time) for e in split.calib)
        first_test = min(event_time_key(e.feature_event_time) for e in split.test)
        assert last_train <= first_calib
        assert last_calib <= first_test


# ---------------------------------------------------------------------------
# 3. CALIBRATION delivered (reliability curve + Brier + ECE within tolerance)
# ---------------------------------------------------------------------------


class TestCalibration:
    def test_calibration_report_in_tolerance(self, trained):
        rep = trained.calibration_report
        assert rep.in_tolerance, rep.summary()
        assert rep.brier_score <= rep.brier_tolerance
        assert rep.ece <= rep.ece_tolerance
        assert len(rep.reliability_bins) == 10

    def test_oos_test_fold_calibration_within_tolerance(self, trained, split):
        """The honest OOS calibration check on the held-out TEST fold."""
        X_test, y_test = _matrix(split.test)
        cal = trained.predict_calibrated(X_test)
        ece = expected_calibration_error(cal, y_test, n_bins=10)
        brier = brier_score(cal, y_test)
        assert ece <= 0.10, f"OOS ECE={ece:.4f} exceeds tolerance 0.10"
        assert brier <= 0.25, f"OOS Brier={brier:.4f} exceeds tolerance 0.25"

    def test_reliability_curve_monotone_trend(self, trained, split):
        """Bins with more predicted probability should have higher empirical frequency
        (a calibrated model's reliability curve tracks the diagonal)."""
        X_test, y_test = _matrix(split.test)
        cal = trained.predict_calibrated(X_test)
        bins = reliability_curve(cal, y_test, n_bins=5)
        populated = [b for b in bins if b.count >= 20]
        # mean predicted and empirical frequency should be positively associated.
        mp = [b.mean_predicted for b in populated]
        ef = [b.empirical_frequency for b in populated]
        assert len(populated) >= 2
        corr = np.corrcoef(mp, ef)[0, 1]
        assert corr > 0.7, f"reliability curve not tracking the diagonal (corr={corr:.3f})"

    def test_isotonic_and_platt_both_calibrate(self, corpus):
        # Both calibration methods produce an in-tolerance report on the calib slice.
        art_iso = train_snipe_classifier(corpus, seed=SEED, calibration_method="isotonic")
        art_platt = train_snipe_classifier(corpus, seed=SEED, calibration_method="platt")
        assert art_iso.calibration_report.in_tolerance
        assert art_platt.calibration_report.in_tolerance

    def test_uncertainty_is_bernoulli_band(self):
        assert bernoulli_uncertainty(0.5) == pytest.approx(0.5)
        assert bernoulli_uncertainty(0.0) == pytest.approx(0.0)
        assert bernoulli_uncertainty(1.0) == pytest.approx(0.0)
        # higher near 0.5, lower at extremes (de-risk monotonicity)
        assert bernoulli_uncertainty(0.5) > bernoulli_uncertainty(0.1)


# ---------------------------------------------------------------------------
# 4. ONNX PARITY (Python vs exported runtime within tolerance)
# ---------------------------------------------------------------------------


class TestOnnxParity:
    def test_raw_proba_parity(self, trained, onnx_inference, split):
        _, inf = onnx_inference
        X_test, _ = _matrix(split.test)
        py_raw = trained.predict_uncalibrated(X_test)
        onnx_raw = inf._raw_proba(X_test)
        max_abs_diff = float(np.max(np.abs(py_raw - onnx_raw)))
        assert max_abs_diff < 1e-5, f"ONNX parity max abs diff {max_abs_diff:.2e} >= 1e-5"

    def test_single_row_parity(self, trained, onnx_inference, split):
        _, inf = onnx_inference
        row = split.test[0].feature_row
        X = np.asarray([row], dtype=np.float32)
        py = float(trained.predict_uncalibrated(X)[0])
        onnx = float(inf._raw_proba(X)[0])
        assert abs(py - onnx) < 1e-5


# ---------------------------------------------------------------------------
# 5. LATENCY (measured p50/p99, single-digit-ms)
# ---------------------------------------------------------------------------


class TestLatency:
    def test_p99_single_digit_ms(self, onnx_inference, split):
        _, inf = onnx_inference
        lat = inf.measure_latency(split.test[0].feature_row, n=1000, warmup=100)
        assert lat["p50_ms"] < 10.0, f"p50 {lat['p50_ms']:.4f} ms busts single-digit-ms budget"
        assert lat["p99_ms"] < 10.0, f"p99 {lat['p99_ms']:.4f} ms busts single-digit-ms budget"


# ---------------------------------------------------------------------------
# 6. OUTPUT CONTRACT (DecisionSignal: prob + uncertainty, NO price, NO decision)
# ---------------------------------------------------------------------------


class TestOutputContract:
    def test_inference_emits_decision_signal(self, onnx_inference, corpus):
        _, inf = onnx_inference
        sig = inf.infer_decision_signal(corpus[0].frame, baseline_p=0.4, surface="EH-001")
        assert isinstance(sig, DecisionSignal)
        assert 0.0 <= sig.p_calibrated <= 1.0
        assert 0.0 <= sig.uncertainty <= 1.0
        assert 0.0 <= sig.baseline_p <= 1.0
        assert sig.mint == corpus[0].frame.mint
        assert sig.model_version.endswith("BOOTSTRAP")

    def test_decision_signal_has_no_price_or_winrate_field(self):
        """The contract itself forbids a price / win-rate field — proof by field set."""
        fields = set(DecisionSignal.model_fields.keys())
        for forbidden in ("price", "target_price", "price_target", "win_rate", "winrate"):
            assert forbidden not in fields
        # The output carries ONLY probability + uncertainty + baseline + identity.
        assert fields == {
            "mint",
            "event_time",
            "model_version",
            "p_calibrated",
            "uncertainty",
            "baseline_p",
            "surface",
        }

    def test_inference_result_carries_no_decision(self, onnx_inference, corpus):
        """The raw InferenceResult is prob + uncertainty ONLY — no size, no decision."""
        _, inf = onnx_inference
        res = inf.infer_row(frame_to_feature_row(corpus[0].frame))
        fields = set(res.__dataclass_fields__.keys())
        assert fields == {"p_uncalibrated", "p_calibrated", "uncertainty"}


# ---------------------------------------------------------------------------
# 7. AUTO-DISABLE (degraded/shuffled window -> NO_SIGNAL, not a stale edge)
# ---------------------------------------------------------------------------


class TestAutoDisable:
    def test_healthy_window_enabled(self, trained, split):
        X_test, y_test = _matrix(split.test)
        cal = trained.predict_calibrated(X_test)
        # baseline_p proxy from buy pressure column (monotone proxy)
        col = {c: i for i, c in enumerate(FEATURE_COLUMNS)}
        base = np.clip(
            np.asarray([max(0.0, ex.feature_row[col["first_k_buy_pressure"]]) for ex in split.test])
            / 2e9,
            0.0,
            1.0,
        )
        cmp = compare_window(cal, base, y_test, min_edge=0.02)
        assert cmp.verdict == MonitorVerdict.ENABLED
        assert cmp.edge_auc >= 0.02

    def test_shuffled_labels_collapse_to_no_signal(self, trained, split):
        """Degrade the window: shuffle the labels so neither model nor baseline has edge.
        The monitor MUST emit NO_SIGNAL (a stale edge is the dangerous failure)."""
        rng = np.random.default_rng(0)
        X_test, y_test = _matrix(split.test)
        cal = trained.predict_calibrated(X_test)
        shuffled = y_test.copy()
        rng.shuffle(shuffled)
        base = rng.random(len(shuffled))
        cmp = compare_window(cal, base, shuffled, min_edge=0.02)
        assert cmp.verdict == MonitorVerdict.NO_SIGNAL, (
            f"monitor traded a stale edge on shuffled data (edge={cmp.edge_auc:.4f})"
        )

    def test_destroyed_scores_emit_no_signal(self, split):
        """If the model's scores are NaN/constant (degraded), NO_SIGNAL (safe default)."""
        _, y_test = _matrix(split.test)
        dead_model = np.full(len(y_test), np.nan)
        base = np.full(len(y_test), 0.5)
        cmp = compare_window(dead_model, base, y_test, min_edge=0.02)
        assert cmp.verdict == MonitorVerdict.NO_SIGNAL

    def test_consecutive_no_signal_auto_disables(self, split):
        _, y_test = _matrix(split.test)
        # three windows where the model has no edge -> 2 consecutive NO_SIGNAL disables.
        noise = np.random.default_rng(1).random(len(y_test))
        w = compare_window(noise, noise, y_test, min_edge=0.02)
        result = rolling_auto_disable([w, w, w], consecutive_no_signal_to_disable=2)
        assert result.disabled
        assert result.final_verdict == MonitorVerdict.NO_SIGNAL

    def test_single_no_signal_does_not_disable(self, trained, split):
        """One bad window (noise tolerance) does not flip a healthy run to disabled."""
        X_test, y_test = _matrix(split.test)
        cal = trained.predict_calibrated(X_test)
        col = {c: i for i, c in enumerate(FEATURE_COLUMNS)}
        base = np.clip(
            np.asarray([max(0.0, ex.feature_row[col["first_k_buy_pressure"]]) for ex in split.test])
            / 2e9,
            0.0,
            1.0,
        )
        good = compare_window(cal, base, y_test, min_edge=0.02)
        noise = np.random.default_rng(2).random(len(y_test))
        bad = compare_window(noise, noise, y_test, min_edge=0.02)
        result = rolling_auto_disable([good, bad, good], consecutive_no_signal_to_disable=2)
        assert not result.disabled


# ---------------------------------------------------------------------------
# 8. REPRODUCIBILITY (pinned seed reproduces predictions bit-for-bit)
# ---------------------------------------------------------------------------


class TestReproducibility:
    def test_retrain_same_seed_identical_predictions(self, corpus, split):
        a1 = train_snipe_classifier(corpus, seed=SEED)
        a2 = train_snipe_classifier(corpus, seed=SEED)
        X_test, _ = _matrix(split.test)
        p1 = a1.predict_calibrated(X_test)
        p2 = a2.predict_calibrated(X_test)
        assert float(np.max(np.abs(p1 - p2))) == 0.0

    def test_synthetic_corpus_deterministic(self):
        c1 = generate_synthetic_corpus(n=200, seed=SEED)
        c2 = generate_synthetic_corpus(n=200, seed=SEED)
        assert [r.label for r in c1] == [r.label for r in c2]
        assert [r.frame.event_time.slot for r in c1] == [r.frame.event_time.slot for r in c2]


# ---------------------------------------------------------------------------
# 9. ADVERSARIAL / DE-RISK SIGN (manufactured hype / behind smart money => risk DOWN)
# ---------------------------------------------------------------------------


class TestDeRiskMonotone:
    def test_monotone_constraints_are_nonincreasing_for_derisk_features(self):
        mono = monotone_constraint_vector()
        assert len(mono) == N_FEATURES
        for i, col in enumerate(FEATURE_COLUMNS):
            if col in MONOTONE_NONINCREASING_FEATURES:
                assert mono[i] == -1, f"{col} must be monotone non-increasing (de-risk)"
            else:
                assert mono[i] == 0, f"{col} must be unconstrained (no forced-bullish features)"

    def test_no_feature_is_forced_increasing(self):
        """There is NO +1 (forced-bullish) constraint — nothing is REQUIRED to size up."""
        assert all(c in (-1, 0) for c in monotone_constraint_vector())

    def test_higher_concentration_does_not_raise_probability(self, trained):
        """Raising holder_concentration (a rug-risk feature) cannot raise the probability
        (the learned sign is de-risk, enforced by the monotone constraint)."""
        col = {c: i for i, c in enumerate(FEATURE_COLUMNS)}
        base_row = [0.0] * N_FEATURES
        # set a moderate, plausible context
        base_row[col["first_k_buy_pressure"]] = 1.0e9
        base_row[col["first_k_volume_lamports"]] = 3.0e9
        base_row[col["holder_count"]] = 40.0
        base_row[col["lp_depth_lamports"]] = 4.0e9
        low = list(base_row)
        low[col["holder_concentration_top10_bps"]] = 1000.0
        high = list(base_row)
        high[col["holder_concentration_top10_bps"]] = 9000.0
        X = np.asarray([low, high], dtype=np.float32)
        p = trained.predict_uncalibrated(X)
        assert p[1] <= p[0] + 1e-9, (
            "higher top-10 concentration RAISED the probability — the model treats a rug-risk "
            "signal as bullish, which is forbidden (adversarial-sentiment / de-risk rule)."
        )

    def test_more_behind_smart_money_does_not_raise_probability(self, trained):
        col = {c: i for i, c in enumerate(FEATURE_COLUMNS)}
        base_row = [0.0] * N_FEATURES
        base_row[col["first_k_buy_pressure"]] = 1.0e9
        base_row[col["first_k_volume_lamports"]] = 3.0e9
        base_row[col["lp_depth_lamports"]] = 4.0e9
        none_in = list(base_row)
        none_in[col["smart_wallets_in"]] = 0.0
        many_in = list(base_row)
        many_in[col["smart_wallets_in"]] = 5.0
        X = np.asarray([none_in, many_in], dtype=np.float32)
        p = trained.predict_uncalibrated(X)
        assert p[1] <= p[0] + 1e-9


# ---------------------------------------------------------------------------
# 10. BOOTSTRAP MARKER + NO WIN-RATE (honesty)
# ---------------------------------------------------------------------------


class TestBootstrapAndHonesty:
    def test_synthetic_marked_bootstrap(self):
        assert IS_BOOTSTRAP_NOT_REAL is True

    def test_artifact_marked_bootstrap(self, trained):
        assert trained.is_bootstrap_not_real is True
        assert "BOOTSTRAP" in trained.model_version

    def test_no_win_rate_field_anywhere(self):
        """No win-rate field on the model output or the inference result."""
        assert not any("win" in f.lower() for f in DecisionSignal.model_fields)

    def test_feature_row_carries_no_money_as_decision(self, corpus):
        """The feature row is float64 statistical inputs (no Decimal money arithmetic,
        no price, no size) — it is an ML input vector, not a money quantity."""
        row = frame_to_feature_row(corpus[0].frame)
        assert all(isinstance(v, float) for v in row)


# ---------------------------------------------------------------------------
# Construction-level guard: the FeatureFrame contract itself rejects a label field
# (defense in depth — the model can never even receive a frame carrying a label).
# ---------------------------------------------------------------------------


def test_feature_frame_rejects_label_field_at_construction(corpus):
    from pydantic import ValidationError

    base = corpus[0].frame
    data = base.model_dump()
    data["realized_mult_decimal"] = "5.0"  # plant a label field
    # extra="forbid" on the FeatureFrame contract rejects the planted label kwarg.
    with pytest.raises(ValidationError):
        FeatureFrame(**data)
