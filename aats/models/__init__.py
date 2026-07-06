"""M2 Engine — prediction models.

Implemented by: ml-prediction-engineer (T-310, T-311, T-312).

CRITICAL seam constraints (enforced by CI):
  - The ONNX snipe classifier is loaded and run by the Rust hot core (inference
    shim); the Python side only trains, exports, and validates it.  Python never
    runs inference on the SNIPE path.
  - The naive-momentum baseline is FROZEN in a committed hashed config; any
    post-first-fit change fails the build (C-4, T-311).
  - The truth_* fields of LaunchEvent are SIM-ONLY and must NEVER appear in
    production model code (clean-room import guard, T-400).

Public surface (T-311 — frozen naive-momentum baseline):
  - BaselineParams, BaselineSignal           : typed, money-correct outputs
  - load_params, assert_frozen_or_freeze     : config load + the C-4 freeze gate
  - evaluate_baseline                        : the selection rule (no future data)
  - canonical_params_hash                    : deterministic freeze identity
  - BaselineFrozenError, BaselineConfigError : C-4 failure modes
  - DEFAULT_CONFIG_PATH                       : the committed baseline.frozen.json
"""

from aats.models.baseline import (
    DEFAULT_CONFIG_PATH,
    BaselineConfigError,
    BaselineFrozenError,
    BaselineParams,
    BaselineSignal,
    assert_frozen_or_freeze,
    canonical_params_hash,
    evaluate_baseline,
    load_params,
)
from aats.models.calibration import (
    CalibrationReport,
    ProbabilityCalibrator,
    bernoulli_uncertainty,
    brier_score,
    expected_calibration_error,
    fit_calibrator,
    reliability_curve,
)
from aats.models.featureset import (
    FEATURE_COLUMNS,
    MODEL_FEATURE_SCHEMA_VERSION,
    MONOTONE_NONINCREASING_FEATURES,
    N_FEATURES,
    assert_no_label_taint,
    frame_to_feature_row,
    monotone_constraint_vector,
)
from aats.models.gate_a import (
    GateAResult,
    compute_gate_a,
)
from aats.models.gate_b import (
    DEFAULT_MIN_SAMPLE,
    GateBResult,
    TradeOutcome,
    UnitOfRisk,
    compute_gate_b_delta,
    emit_gate_b_to_telemetry,
)
from aats.models.inference import (
    InferenceResult,
    SnipeInference,
    build_inference_from_artifact,
)
from aats.models.monitor import (
    MonitorVerdict,
    RollingMonitorResult,
    WindowComparison,
    compare_window,
    rolling_auto_disable,
)
from aats.models.regime_labels import (
    ACCUMULATION_HOLDER_GROWTH_MIN_RATIO,
    ACCUMULATION_MAX_DRAWDOWN_RATIO,
    ACCUMULATION_NET_RETURN_MIN_RATIO,
    DISTRIBUTION_DRAWDOWN_MIN_RATIO,
    DISTRIBUTION_HOLDER_SHRINK_MAX_RATIO,
    DISTRIBUTION_NET_RETURN_MAX_RATIO,
    REGIME_DERISK_ACTION,
    REGIME_LABEL_CODES,
    REGIME_LABEL_FIELD_NAMES,
    REGIME_LABEL_TAXONOMY_VERSION,
    REGIME_LABELS_LOOP,
    RegimeDeRiskAction,
    RegimeLabel,
    RegimeLabelLeakError,
    RegimeOutcome,
    RegimeOutcomeSample,
    assert_no_regime_label_taint,
    build_regime_outcome,
    classify_regime,
    derisk_action,
    is_inert,
    join_regime_labels_by_event_time,
    regime_label_to_joined_example,
    remains_sellable_at_size,
    simulate_exit_slippage_bps,
)
from aats.models.survivor import (
    N_SURVIVOR_COVARIATES,
    SURVIVOR_COVARIATE_COLUMNS,
    SURVIVOR_LOOP,
    SURVIVOR_MONOTONE_NONDECREASING,
    SURVIVOR_MONOTONE_NONINCREASING,
    SurvivorLoopViolation,
    SurvivorPrediction,
    TrainedSurvivorModel,
    assert_slow_loop_only,
    build_survivor_covariate_row,
    survivor_monotone_vector,
    train_survivor_model,
)
from aats.models.synthetic import (
    IS_BOOTSTRAP_NOT_REAL,
    SyntheticRow,
    generate_synthetic_corpus,
)
from aats.models.training import (
    JoinedExample,
    LeakAuditError,
    TrainedSnipeClassifier,
    assert_event_time_leq_decision,
    event_time_key,
    export_booster_to_onnx_bytes,
    join_labels_by_event_time,
    time_forward_split,
    train_snipe_classifier,
)

__all__ = [
    # T-311 baseline
    "DEFAULT_CONFIG_PATH",
    "BaselineConfigError",
    "BaselineFrozenError",
    "BaselineParams",
    "BaselineSignal",
    "assert_frozen_or_freeze",
    "canonical_params_hash",
    "evaluate_baseline",
    "load_params",
    # T-310 feature set + lineage-taint guard
    "FEATURE_COLUMNS",
    "MODEL_FEATURE_SCHEMA_VERSION",
    "MONOTONE_NONINCREASING_FEATURES",
    "N_FEATURES",
    "assert_no_label_taint",
    "frame_to_feature_row",
    "monotone_constraint_vector",
    # T-310 synthetic bootstrap
    "IS_BOOTSTRAP_NOT_REAL",
    "SyntheticRow",
    "generate_synthetic_corpus",
    # T-310 training
    "JoinedExample",
    "LeakAuditError",
    "TrainedSnipeClassifier",
    "assert_event_time_leq_decision",
    "event_time_key",
    "export_booster_to_onnx_bytes",
    "join_labels_by_event_time",
    "time_forward_split",
    "train_snipe_classifier",
    # T-310 calibration
    "CalibrationReport",
    "ProbabilityCalibrator",
    "bernoulli_uncertainty",
    "brier_score",
    "expected_calibration_error",
    "fit_calibrator",
    "reliability_curve",
    # T-310 inference
    "InferenceResult",
    "SnipeInference",
    "build_inference_from_artifact",
    # T-310 model-vs-baseline auto-disable monitor (lightweight AUC guard)
    "MonitorVerdict",
    "RollingMonitorResult",
    "WindowComparison",
    "compare_window",
    "rolling_auto_disable",
    # M2-CP-02 regime label taxonomy + remains-sellable gate (SLOW-loop LABEL only)
    "ACCUMULATION_HOLDER_GROWTH_MIN_RATIO",
    "ACCUMULATION_MAX_DRAWDOWN_RATIO",
    "ACCUMULATION_NET_RETURN_MIN_RATIO",
    "DISTRIBUTION_DRAWDOWN_MIN_RATIO",
    "DISTRIBUTION_HOLDER_SHRINK_MAX_RATIO",
    "DISTRIBUTION_NET_RETURN_MAX_RATIO",
    "REGIME_DERISK_ACTION",
    "REGIME_LABEL_CODES",
    "REGIME_LABEL_FIELD_NAMES",
    "REGIME_LABEL_TAXONOMY_VERSION",
    "REGIME_LABELS_LOOP",
    "RegimeDeRiskAction",
    "RegimeLabel",
    "RegimeLabelLeakError",
    "RegimeOutcome",
    "RegimeOutcomeSample",
    "assert_no_regime_label_taint",
    "build_regime_outcome",
    "classify_regime",
    "derisk_action",
    "is_inert",
    "join_regime_labels_by_event_time",
    "regime_label_to_joined_example",
    "remains_sellable_at_size",
    "simulate_exit_slippage_bps",
    # T-312 slow-loop survivor model (calibrated prob + uncertainty + quantiles)
    "N_SURVIVOR_COVARIATES",
    "SURVIVOR_COVARIATE_COLUMNS",
    "SURVIVOR_LOOP",
    "SURVIVOR_MONOTONE_NONDECREASING",
    "SURVIVOR_MONOTONE_NONINCREASING",
    "SurvivorLoopViolation",
    "SurvivorPrediction",
    "TrainedSurvivorModel",
    "assert_slow_loop_only",
    "build_survivor_covariate_row",
    "survivor_monotone_vector",
    "train_survivor_model",
    # T-401 GATE-A monitor (aggregate net-of-cost PnL + lower-95% bootstrap bound)
    "GateAResult",
    "compute_gate_a",
    # T-312 GATE-B monitor (headline model-vs-baseline NET-PnL delta -> telemetry)
    "DEFAULT_MIN_SAMPLE",
    "GateBResult",
    "TradeOutcome",
    "UnitOfRisk",
    "compute_gate_b_delta",
    "emit_gate_b_to_telemetry",
]
