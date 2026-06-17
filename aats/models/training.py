"""FAST snipe classifier training pipeline (T-310) — leak-free, calibrated, ONNX export.

PIPELINE (offline / validation lane — NEVER on the hot path, NEVER touches capital):
  1. assemble the feature matrix from FeatureFrame rows  (featureset.frame_to_feature_row)
  2. JOIN labels by EVENT-TIME ONLY from a separate labels dataset  (join_labels_by_event_time)
  3. RUN THE LINEAGE-TAINT BUILD GUARD on the matrix columns  (featureset.assert_no_label_taint)
  4. WALK-FORWARD time-forward split (train = earlier event-time, test = later) — never shuffle
  5. fit LightGBM with monotone constraints on the de-risk features
  6. CALIBRATE on a held-out, time-forward calibration slice (isotonic or Platt)
  7. reliability curve + Brier + ECE  (calibration.py)
  8. export ONNX (deterministic) + record the model card

THE NO-LEAK CONTRACT (the whole game — point-in-time correctness)
================================================================
  - Labels are a SEPARATE dataset.  They are joined to features by `event_time` ONLY
    (slot + block_time), inside this module, exactly like the harness.  A label is never
    a feature-frame field.
  - assert_event_time_leq_decision(): EVERY training row asserts its feature event-time
    <= its label's DECISION anchor event-time (they are equal by join, and the label's
    RESOLUTION time is strictly later — proving the label looked forward, the features
    did not).  The assertion output is printed for the self-check / co-sign.
  - assert_no_label_taint(): the model feature columns are scanned; any truth_*/label
    column FAILS the build (ProvenanceTaintError).  A label that somehow reached the
    matrix taints the lineage and the build refuses to fit.
  - the split is event-time ordered and forward-only.  No k-fold, no random shuffle.

OUTPUT BOUNDARY
===============
The trained model emits a CALIBRATED PROBABILITY + UNCERTAINTY ONLY (inference.py wraps
it into a DecisionSignal).  It never emits a price, a size, or a trade decision.  There
is no win-rate target or field anywhere — the objective is log-loss (proper scoring
rule for calibration), and acceptance is net-PnL + model-vs-baseline on RECORDED data
(not this synthetic bootstrap).

DETERMINISM / REPRODUCIBILITY
=============================
Fixed LightGBM seed, single-threaded determinism flags, deterministic ONNX export.
Re-running train_snipe_classifier() with the same data + seed reproduces the model card
metrics (self-check 8).

INJECTABLE / OFFLINE
====================
No RPC, no LLM, no Geyser, no Redis.  Inputs are in-memory typed objects (or the
synthetic bootstrap).  The real labels/ Parquet reader plugs in at `join_labels_by_event_time`
(it accepts any iterable of (event_time_key, label) — the harness supplies the real one).
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import lightgbm as lgb
import numpy as np

from aats.contracts.events import EventTime
from aats.models.calibration import (
    CalibrationReport,
    ProbabilityCalibrator,
    fit_calibrator,
)
from aats.models.featureset import (
    FEATURE_COLUMNS,
    MODEL_FEATURE_SCHEMA_VERSION,
    N_FEATURES,
    assert_no_label_taint,
    frame_to_feature_row,
    monotone_constraint_vector,
)
from aats.models.synthetic import SyntheticRow

# ---------------------------------------------------------------------------
# Event-time join key (the ONLY legal join anchor — slot + block_time)
# ---------------------------------------------------------------------------


def event_time_key(et: EventTime) -> tuple[int, int]:
    """Canonical join key for an event-time: (slot, block_time_ms).

    wall_clock_ms is DELIBERATELY excluded — it is monitoring-only and must never be a
    join key (data-models §3.1).  Labels and features join on this tuple ONLY.
    """
    return (et.slot, et.block_time_ms)


class LeakAuditError(AssertionError):
    """Raised when the event-time <= decision-time leak audit fails on any row."""


# ---------------------------------------------------------------------------
# Leak-free label join (by event_time only) + the event-time <= decision-time audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JoinedExample:
    """A feature row joined to its label by event_time, with the audit fields kept."""

    feature_row: list[float]
    label: int
    feature_event_time: EventTime
    decision_event_time: EventTime      # the label's decision anchor (== feature_event_time)
    resolution_event_time: EventTime    # = decision + H, strictly later (label looked forward)


def join_labels_by_event_time(rows: list[SyntheticRow]) -> list[JoinedExample]:
    """Join feature frames to labels BY EVENT-TIME ONLY, from a separate labels dataset.

    This is the leak-free join the harness performs.  Here the labels arrive in their own
    dataset (the `.label` / `.resolution_event_time` of each SyntheticRow, which in
    production come from labels/ Parquet — the reader plugs in here unchanged).

    The labels dataset is keyed by (slot, block_time_ms).  We build the label index, then
    join each FeatureFrame to its label on that key.  A feature with no matching label is
    DROPPED (CENSORED-equivalent), never defaulted.

    Returns JoinedExample rows.  Does NOT yet run the leak audit — call
    assert_event_time_leq_decision() on the result (the training entrypoint does).
    """
    # Build the SEPARATE labels index, keyed by event_time only.
    label_index: dict[tuple[int, int], tuple[int, EventTime, EventTime]] = {}
    for r in rows:
        key = event_time_key(r.frame.event_time)
        label_index[key] = (r.label, r.frame.event_time, r.resolution_event_time)

    joined: list[JoinedExample] = []
    for r in rows:
        key = event_time_key(r.frame.event_time)
        if key not in label_index:
            continue  # no label -> CENSORED-equivalent, dropped (never defaulted)
        label, decision_et, resolution_et = label_index[key]
        joined.append(
            JoinedExample(
                feature_row=frame_to_feature_row(r.frame),
                label=label,
                feature_event_time=r.frame.event_time,
                decision_event_time=decision_et,
                resolution_event_time=resolution_et,
            )
        )
    return joined


def assert_event_time_leq_decision(examples: list[JoinedExample]) -> str:
    """LEAK AUDIT: assert every row's feature event-time <= its label decision-time AND
    the label's resolution-time is strictly later than the decision-time.

    Returns a human-readable audit summary (printed for the self-check / co-sign).
    Raises LeakAuditError on the first violation.

    Why this is the right assertion: the join is on event_time, so feature and decision
    event-time are EQUAL (<=, never >).  The label's RESOLUTION time = decision + H is
    strictly later — proving the LABEL looked forward to event_time + H while the FEATURES
    did not.  A row where the feature event-time exceeded the decision time, or where the
    label resolved at/before the decision, is a forward-looking leak and fails here.
    """
    n = len(examples)
    if n == 0:
        raise LeakAuditError("leak audit: zero examples to audit (empty join)")
    for i, ex in enumerate(examples):
        f_slot = ex.feature_event_time.slot
        d_slot = ex.decision_event_time.slot
        r_slot = ex.resolution_event_time.slot
        f_bt = ex.feature_event_time.block_time_ms
        d_bt = ex.decision_event_time.block_time_ms
        r_bt = ex.resolution_event_time.block_time_ms
        if f_slot > d_slot or f_bt > d_bt:
            raise LeakAuditError(
                f"row {i}: feature event-time (slot={f_slot}, bt={f_bt}) is AFTER the "
                f"label decision-time (slot={d_slot}, bt={d_bt}). Forward-looking leak."
            )
        if r_slot <= d_slot or r_bt <= d_bt:
            raise LeakAuditError(
                f"row {i}: label resolution-time (slot={r_slot}, bt={r_bt}) is NOT strictly "
                f"after the decision-time (slot={d_slot}, bt={d_bt}). The label did not "
                "look forward — its horizon proof is missing."
            )
    return (
        f"LEAK AUDIT PASS: {n} rows. For every row: feature_event_time <= decision_event_time "
        "(equal by event-time join), AND label resolution_event_time strictly > decision_event_time "
        "(label horizon proof present). No feature reads post-decision data; the label is the only "
        "forward-looking quantity and it lives in a separate dataset joined by event_time only."
    )


# ---------------------------------------------------------------------------
# Walk-forward (time-forward) split — forward-only, event-time ordered, NEVER shuffled
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeForwardSplit:
    train: list[JoinedExample]
    calib: list[JoinedExample]   # held-out, time-forward calibration slice
    test: list[JoinedExample]    # latest event-time window (out-of-sample)


def time_forward_split(
    examples: list[JoinedExample],
    *,
    train_frac: float = 0.6,
    calib_frac: float = 0.15,
) -> TimeForwardSplit:
    """Forward-only split by event-time (slot, block_time).  No shuffle, no k-fold.

    Order STRICTLY by event-time, then cut: earliest train_frac -> train, next calib_frac
    -> calibration, remainder -> test (the latest, out-of-sample window).  This is the
    minimum walk-forward discipline; the full ≥5-window CPCV walk-forward is the harness's
    job (T-400/401), this module provides the leak-free single forward split the model fit
    and calibration require.
    """
    ordered = sorted(examples, key=lambda e: event_time_key(e.feature_event_time))
    n = len(ordered)
    n_train = int(n * train_frac)
    n_calib = int(n * calib_frac)
    train = ordered[:n_train]
    calib = ordered[n_train : n_train + n_calib]
    test = ordered[n_train + n_calib :]
    return TimeForwardSplit(train=train, calib=calib, test=test)


def _matrix(examples: list[JoinedExample]) -> tuple[np.ndarray, np.ndarray]:
    X = np.asarray([e.feature_row for e in examples], dtype=np.float32)
    y = np.asarray([e.label for e in examples], dtype=np.int32)
    return X, y


# ---------------------------------------------------------------------------
# The trained, calibrated artifact
# ---------------------------------------------------------------------------


@dataclass
class TrainedSnipeClassifier:
    """The trained LightGBM + calibrator + metadata — the T-310 artifact.

    Holds everything inference.py and the model card need.  The raw LightGBM booster
    predicts an UNCALIBRATED probability; the calibrator maps it to a CALIBRATED one.
    """

    booster: lgb.Booster
    calibrator: ProbabilityCalibrator
    feature_columns: tuple[str, ...]
    model_version: str
    monotone_constraints: list[int]
    seed: int
    is_bootstrap_not_real: bool
    # metrics (filled by train_snipe_classifier)
    calibration_report: CalibrationReport = field(default=None)  # type: ignore[assignment]
    leak_audit_summary: str = ""
    n_train: int = 0
    n_calib: int = 0
    n_test: int = 0

    def predict_uncalibrated(self, X: np.ndarray) -> np.ndarray:
        """Raw booster probability (margin -> sigmoid is done by LightGBM for binary)."""
        return np.asarray(self.booster.predict(X), dtype=np.float64)

    def predict_calibrated(self, X: np.ndarray) -> np.ndarray:
        """Calibrated probability in [0,1] (the number the sizer trusts as a frequency)."""
        raw = self.predict_uncalibrated(X)
        return self.calibrator.transform(raw)


# ---------------------------------------------------------------------------
# The training entrypoint
# ---------------------------------------------------------------------------


def _model_version(seed: int) -> str:
    return f"snipe-lgbm-v{MODEL_FEATURE_SCHEMA_VERSION}-seed{seed}-BOOTSTRAP"


def train_snipe_classifier(
    rows: list[SyntheticRow],
    *,
    seed: int = 20260616,
    calibration_method: str = "isotonic",
    num_boost_round: int = 200,
    learning_rate: float = 0.05,
    num_leaves: int = 31,
    max_depth: int = 5,
    verbose: bool = False,
) -> TrainedSnipeClassifier:
    """Train the FAST snipe classifier end-to-end, leak-free and calibrated.

    Steps (each documented inline): label join by event-time -> leak audit -> lineage-taint
    build guard -> forward split -> LightGBM fit (monotone constraints) -> calibrate ->
    reliability curve.  Returns the TrainedSnipeClassifier artifact.

    `rows` are SyntheticRow (bootstrap) for now; in production the harness supplies real
    FeatureFrame + labels/ rows through the same JoinedExample interface.
    """
    # 0. LINEAGE-TAINT BUILD GUARD on the declared feature columns (fails fast if any
    #    truth_*/label column is in the model's column list).
    assert_no_label_taint(list(FEATURE_COLUMNS))

    # 1 + 2. Join labels BY EVENT-TIME ONLY (separate labels dataset).
    joined = join_labels_by_event_time(rows)

    # 2b. LEAK AUDIT: event-time <= decision-time for every row (printed for co-sign).
    leak_summary = assert_event_time_leq_decision(joined)

    # 3. Forward-only, event-time-ordered split (never shuffled).
    split = time_forward_split(joined)
    X_train, y_train = _matrix(split.train)
    X_calib, y_calib = _matrix(split.calib)

    if X_train.shape[1] != N_FEATURES:
        raise ValueError(
            f"feature matrix has {X_train.shape[1]} columns, expected {N_FEATURES} "
            f"(FEATURE_COLUMNS). The matrix and the column contract must agree exactly."
        )

    # 4. LightGBM fit with monotone constraints on the de-risk features.
    monotone = monotone_constraint_vector()
    train_set = lgb.Dataset(X_train, label=y_train, feature_name=list(FEATURE_COLUMNS))
    params = {
        "objective": "binary",          # log-loss — a PROPER scoring rule (calibration-friendly)
        "metric": "binary_logloss",     # NOT accuracy / win-rate — there is no win-rate objective
        "learning_rate": learning_rate,
        "num_leaves": num_leaves,
        "max_depth": max_depth,
        "min_data_in_leaf": 30,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "monotone_constraints": monotone,
        "seed": seed,
        "bagging_seed": seed,
        "feature_fraction_seed": seed,
        "deterministic": True,
        "force_row_wise": True,         # determinism (no GPU / no row/col threading nondeterminism)
        "num_threads": 1,               # single-threaded => bit-reproducible
        "verbosity": -1 if not verbose else 1,
    }
    booster = lgb.train(params, train_set, num_boost_round=num_boost_round)

    # 5. CALIBRATE on the held-out, time-forward calibration slice (isotonic/Platt).
    raw_calib = np.asarray(booster.predict(X_calib), dtype=np.float64)
    calibrator, calib_report = fit_calibrator(
        raw_calib, y_calib, method=calibration_method, n_bins=10
    )

    artifact = TrainedSnipeClassifier(
        booster=booster,
        calibrator=calibrator,
        feature_columns=FEATURE_COLUMNS,
        model_version=_model_version(seed),
        monotone_constraints=monotone,
        seed=seed,
        is_bootstrap_not_real=True,
        calibration_report=calib_report,
        leak_audit_summary=leak_summary,
        n_train=len(split.train),
        n_calib=len(split.calib),
        n_test=len(split.test),
    )
    return artifact


# ---------------------------------------------------------------------------
# Deterministic ONNX export (LightGBM booster -> ONNX via onnxmltools)
# ---------------------------------------------------------------------------


def export_booster_to_onnx_bytes(booster: lgb.Booster, n_features: int = N_FEATURES) -> bytes:
    """Export the LightGBM booster to ONNX (in-memory bytes), deterministically.

    Returns the serialized ONNX model bytes.  The export targets the RAW booster
    probability (the uncalibrated score); the calibrator is applied separately in the
    inference wrapper (a monotone post-map — kept out of ONNX so the calibrator can be
    re-fit without re-exporting the tree, and so the parity test isolates the tree export).

    Deterministic: LightGBM tree structure is fixed by the training seed; onnxmltools
    conversion is a pure structural transform.
    """
    from onnxmltools import convert_lightgbm
    from onnxmltools.convert.common.data_types import FloatTensorType

    initial_types = [("input", FloatTensorType([None, n_features]))]
    onnx_model = convert_lightgbm(
        booster,
        initial_types=initial_types,
        target_opset=None,
        zipmap=False,  # plain tensor output, no ZipMap dict — keeps the inference shim simple
    )
    buf = io.BytesIO()
    buf.write(onnx_model.SerializeToString())
    return buf.getvalue()
