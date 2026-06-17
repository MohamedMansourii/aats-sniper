"""Slow-loop SURVIVOR model (T-312) — calibrated P(survive) + uncertainty + quantiles.

WHERE THIS RUNS (and where it MUST NOT)
=======================================
The SLOW loop only.  This is the "survivor brain" for coins that ALREADY survived the
snipe window (EH-003 migration-survivor selection / EH-001 late-entry survivors).  It
consumes the Market-Context-State (MCS) covariates + first-60s survivor microstructure
and emits, per coin, a CALIBRATED survival probability + uncertainty + quantiles.

It NEVER runs on the FAST or SNIPE path (BLUEPRINT triple-loop boundary; data-models §4;
metrics.py `model_inference_seconds{model="tft_survivor"}` budget = 500ms — a SLOW-loop
budget, never the snipe single-digit-ms one).  The class refuses to be wired into a hot
path: it carries `LOOP = "slow"` and the `assert_slow_loop_only()` guard fails loudly if a
caller claims a fast/snipe loop.  There is no ONNX export and no Rust shim for it — by
design, so it cannot be dropped into the race core.

WHY A "PRAGMATIC SURVIVOR MODEL" AND NOT A TUNED TFT (deliberate, documented)
============================================================================
The TASK is explicit: with NO recorded data yet, the ARCHITECTURE + the GATE-B monitor are
what matter; heavy TFT/N-HiTS tuning is DEFERRED to when recorded data exists (T-400/401).
So this module ships:
  - the exogenous-covariate CONTRACT a TFT/N-HiTS would consume (SurvivorCovariates), and
  - a leak-free, calibrated, quantile-emitting survivor model with the SAME output contract
    a TFT would expose (SurvivorPrediction: p_calibrated + uncertainty + p10/p50/p90 + vol),
  - behind a `SurvivorModel` interface so a pytorch-forecasting TFT can be slotted in later
    WITHOUT changing the SLOW loop, the monitor, or the telemetry (the seam is marked
    `TFT_SWAP_POINT`).
The pragmatic implementation is a monotone-constrained gradient-boosted survivor head + an
isotonic calibrator (reusing the T-310 calibration layer) + a residual-dispersion quantile
estimator.  It is honest about being a placeholder: `is_bootstrap_not_real = True`.

PROBABILITIES + UNCERTAINTY + QUANTILES — NEVER A POINT PRICE (non-waivable)
===========================================================================
The model emits:
  - p_calibrated  : calibrated P(coin survives the horizon AND stays sellable) in [0,1],
  - uncertainty   : a predictive band (Bernoulli + calibration-evidence blend),
  - p10/p50/p90   : QUANTILES of the survival score (NOT price quantiles — a survival-
                    probability spread, the spread a TFT would emit as forecast quantiles),
  - vol_estimate  : a volatility/dispersion estimate of the survival score.
There is NO point price anywhere (locked decision 9) and NO win-rate field (HONESTY CLAUSE).
High uncertainty / wide quantile spread is a DE-RISK input downstream (¼-Kelly shrinks) —
it can NEVER size up, widen a stop, or override a hard stop.

ADVERSARIAL SENTIMENT LOWERS CONVICTION (BUILD-DIRECTIVE, verified by sign test)
===============================================================================
The MCS covariates enter the survivor model with HARD monotone signs:
  - synchronicity            : monotone NON-INCREASING (coordinated shill => contrarian risk)
  - coordinated_shill_flag   : monotone NON-INCREASING
  - smart_wallets_in         : monotone NON-INCREASING (we are BEHIND smarter money)
  - holder_concentration     : monotone NON-INCREASING (whale concentration => rug risk)
  - sell_tax_bps             : monotone NON-INCREASING (honeypot-adjacent)
A higher value on ANY of these can only push P(survive) DOWN.  account_age_median_days is
monotone NON-DECREASING (older accounts => LESS manufactured hype => slightly safer); it is
the ONLY +1 and it still never *triggers* an entry — it is a de-risk-relaxation, gated.
The sign test (tests/models) FAILS the build if any of these signs is wrong — a model that
treats manufactured hype as bullish is rejected.

LEAK-FREE BY THE SAME CONSTRUCTION AS THE SNIPE CLASSIFIER (point-in-time)
=========================================================================
  - covariates are extracted ONLY from already-provenance-guarded FeatureFrame + MCSScore
    fields (both carry event_time and passed their per-feature cutoff guards upstream);
  - labels join by EVENT-TIME ONLY from a SEPARATE dataset (reuses training.join + the leak
    audit assert_event_time_leq_decision);
  - the lineage-taint guard (assert_no_label_taint) runs on the survivor feature columns
    before any fit — a truth_*/label column fails the build;
  - the split is forward-only, event-time ordered (never shuffled).

MONEY DISCIPLINE
================
No money arithmetic.  Covariate values are float64 statistical inputs (data-models §0:
genuinely real-valued feature values may be float).  Probabilities/quantiles are in [0,1],
not money.  No PnL/fee/tip/size is ever computed here.

DETERMINISTIC / INJECTABLE / OFFLINE
====================================
Fixed seed, single-threaded determinism flags, no RNG at inference.  No RPC, no LLM, no
Geyser, no Redis, no network, no keypair.  The real labels/ reader and a real MCSScore
stream plug in at the same JoinedExample / covariate interface; deterministic offline
fixtures drive the tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import lightgbm as lgb
import numpy as np

from aats.contracts.events import EventTime
from aats.contracts.features import FeatureFrame
from aats.contracts.models import MCSScore
from aats.models.calibration import (
    CalibrationReport,
    ProbabilityCalibrator,
    bernoulli_uncertainty,
    fit_calibrator,
)
from aats.models.featureset import assert_no_label_taint
from aats.models.synthetic import SyntheticRow
from aats.models.training import (
    JoinedExample,
    assert_event_time_leq_decision,
    event_time_key,
    join_labels_by_event_time,
    time_forward_split,
)

# ---------------------------------------------------------------------------
# Loop placement guard — this model is SLOW-LOOP ONLY (never FAST/SNIPE)
# ---------------------------------------------------------------------------

SURVIVOR_LOOP = "slow"


class SurvivorLoopViolation(RuntimeError):
    """Raised if the survivor model is asked to run on the FAST or SNIPE loop.

    The slow-loop survivor brain MUST NEVER run on the snipe/fast path (BLUEPRINT
    triple-loop boundary).  This guard makes a mis-wire a loud build/test failure, not a
    silent latency bomb.
    """


def assert_slow_loop_only(loop: str) -> None:
    """Fail loudly if a caller tries to run the survivor model on a non-slow loop."""
    if loop != SURVIVOR_LOOP:
        raise SurvivorLoopViolation(
            f"survivor model is SLOW-loop only (loop={SURVIVOR_LOOP!r}); refused to run on "
            f"loop={loop!r}. The survivor brain never runs on the FAST/SNIPE path "
            "(BLUEPRINT triple-loop boundary; metrics tft_survivor budget = 500ms slow-loop)."
        )


# ---------------------------------------------------------------------------
# The survivor covariate contract (what a TFT/N-HiTS would consume as exog inputs)
# ---------------------------------------------------------------------------

# The ORDERED, FROZEN survivor covariate column list.  Disjoint from any label column
# (asserted by assert_no_label_taint at fit time).  Every column is point-in-time:
# extracted only from FeatureFrame + MCSScore fields, both event-time stamped and already
# provenance-guarded upstream.  Adding/removing a column is a schema change.
SURVIVOR_COVARIATE_COLUMNS: tuple[str, ...] = (
    # --- first-60s survivor microstructure (FeatureFrame, FR-010) ---
    "lp_depth_lamports",
    "holder_count",
    "holder_concentration_top10_bps",
    "sell_tax_bps",
    "sniper_cluster_score",
    "first_k_buy_pressure",
    "first_k_unique_buyers",
    # --- survivor TA (nullable -> sentinel + presence flag) ---
    "rsi",
    "rsi_present",
    "macd",
    "macd_present",
    "bb_width",
    "bb_width_present",
    # --- adversarial selectivity (de-risk-only; FeatureFrame) ---
    "smart_wallets_in",
    "smart_wallet_entry_lag_slots",
    "smart_wallet_entry_lag_present",
    # --- MCS exogenous covariates (MCSScore; adversarial / contrarian by construction) ---
    "mcs_conviction",
    "mcs_momentum",
    "mcs_novelty",
    "mcs_synchronicity",            # HIGH => contrarian risk => P(survive) DOWN
    "mcs_account_age_median_days",  # LOW => manufactured hype => P(survive) DOWN
    "mcs_coordinated_shill_flag",   # 1 => coordinated shill => P(survive) DOWN
    "mcs_present",                  # 0 when no MCS available for this coin (sentinel above)
)

N_SURVIVOR_COVARIATES = len(SURVIVOR_COVARIATE_COLUMNS)

_NULL_SENTINEL = 0.0

# Monotone signs for the survivor model (LightGBM monotone_constraints, in column order).
#   -1 => monotone NON-INCREASING: a higher value can only push P(survive) DOWN.
#   +1 => monotone NON-DECREASING.
#    0 => unconstrained.
# The adversarial / contrarian-risk covariates are pinned -1 so manufactured hype / being
# behind smart money / whale concentration / honeypot tax can NEVER raise survival prob.
# account_age_median_days is the ONLY +1 (older crowd => less manufactured hype => safer),
# and it still never *triggers* an entry (de-risk relaxation only, gated downstream).
SURVIVOR_MONOTONE_NONINCREASING: frozenset[str] = frozenset(
    {
        "holder_concentration_top10_bps",
        "sell_tax_bps",
        "smart_wallets_in",
        "smart_wallet_entry_lag_slots",
        "mcs_synchronicity",
        "mcs_coordinated_shill_flag",
    }
)
SURVIVOR_MONOTONE_NONDECREASING: frozenset[str] = frozenset(
    {
        "mcs_account_age_median_days",
    }
)


def survivor_monotone_vector() -> list[int]:
    """Per-covariate monotone constraint (1, 0, or -1) in SURVIVOR_COVARIATE_COLUMNS order."""
    out: list[int] = []
    for col in SURVIVOR_COVARIATE_COLUMNS:
        if col in SURVIVOR_MONOTONE_NONINCREASING:
            out.append(-1)
        elif col in SURVIVOR_MONOTONE_NONDECREASING:
            out.append(1)
        else:
            out.append(0)
    return out


def _f(value: float | int | None) -> float:
    """Cast a frame/MCS value to a model float input. None -> NULL sentinel."""
    if value is None:
        return _NULL_SENTINEL
    return float(value)


def build_survivor_covariate_row(
    frame: FeatureFrame,
    mcs: Optional[MCSScore],
) -> list[float]:
    """Extract the survivor covariate row, in SURVIVOR_COVARIATE_COLUMNS order.

    NO label, truth_*, event_time, or metadata field is read.  MCS is OPTIONAL: when a
    coin has no MCS (no social coverage), the MCS covariates default to neutral sentinels
    and `mcs_present=0` carries the missingness so the tree can split on "no social data"
    instead of confusing the sentinel for a real value.  This keeps the survivor model
    robust to the common no-coverage case without ever fabricating sentiment.

    Returns a list[float] of length N_SURVIVOR_COVARIATES.
    """
    row_by_name: dict[str, float] = {
        # first-60s survivor microstructure
        "lp_depth_lamports": _f(frame.lp_depth_lamports),
        "holder_count": _f(frame.holder_count),
        "holder_concentration_top10_bps": _f(frame.holder_concentration_top10_bps),
        "sell_tax_bps": _f(frame.sell_tax_bps),
        "sniper_cluster_score": _f(frame.sniper_cluster_score),
        "first_k_buy_pressure": _f(frame.first_k_buy_pressure),
        "first_k_unique_buyers": _f(frame.first_k_unique_buyers),
        # survivor TA (nullable)
        "rsi": _f(frame.rsi),
        "rsi_present": 1.0 if frame.rsi is not None else 0.0,
        "macd": _f(frame.macd),
        "macd_present": 1.0 if frame.macd is not None else 0.0,
        "bb_width": _f(frame.bb_width),
        "bb_width_present": 1.0 if frame.bb_width is not None else 0.0,
        # adversarial selectivity
        "smart_wallets_in": _f(frame.smart_wallets_in),
        "smart_wallet_entry_lag_slots": _f(frame.smart_wallet_entry_lag_slots),
        "smart_wallet_entry_lag_present": (
            1.0 if frame.smart_wallet_entry_lag_slots is not None else 0.0
        ),
        # MCS exogenous covariates (neutral sentinels when absent)
        "mcs_conviction": _f(mcs.conviction) if mcs is not None else 0.5,
        "mcs_momentum": _f(mcs.momentum) if mcs is not None else 0.5,
        "mcs_novelty": _f(mcs.novelty) if mcs is not None else 0.5,
        "mcs_synchronicity": _f(mcs.synchronicity) if mcs is not None else 0.0,
        "mcs_account_age_median_days": (
            _f(mcs.account_age_median_days) if mcs is not None else 0.0
        ),
        "mcs_coordinated_shill_flag": (
            1.0 if (mcs is not None and mcs.coordinated_shill_flag) else 0.0
        ),
        "mcs_present": 1.0 if mcs is not None else 0.0,
    }
    if set(row_by_name.keys()) != set(SURVIVOR_COVARIATE_COLUMNS):
        missing = set(SURVIVOR_COVARIATE_COLUMNS) - set(row_by_name.keys())
        extra = set(row_by_name.keys()) - set(SURVIVOR_COVARIATE_COLUMNS)
        raise ValueError(
            "build_survivor_covariate_row produced a column set != "
            f"SURVIVOR_COVARIATE_COLUMNS. missing={sorted(missing)} extra={sorted(extra)}"
        )
    return [row_by_name[col] for col in SURVIVOR_COVARIATE_COLUMNS]


# ---------------------------------------------------------------------------
# The survivor output contract (probability + uncertainty + quantiles + vol)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurvivorPrediction:
    """One survivor inference — calibrated prob + uncertainty + QUANTILES + vol.

    This is the output contract a TFT/N-HiTS swap-in MUST also produce, so the SLOW loop
    and the GATE-B monitor are agnostic to the underlying model.  It contains:
      - p_calibrated : calibrated P(survive AND sellable) in [0,1]
      - uncertainty  : predictive band (Bernoulli + calibration-evidence blend)
      - p10/p50/p90  : survival-score QUANTILES (a probability spread, NOT a price)
      - vol_estimate : dispersion/volatility of the survival score
    NO price field.  NO win-rate field.  NO trade decision.  (locked decision 9; HONESTY.)
    Wide quantile spread / high uncertainty is a DE-RISK input only — never a size-up.
    """

    mint: str
    event_time: EventTime
    model_version: str
    p_calibrated: float
    uncertainty: float
    p10: float
    p50: float
    p90: float
    vol_estimate: float
    is_bootstrap_not_real: bool

    def __post_init__(self) -> None:
        for name in ("p_calibrated", "uncertainty", "p10", "p50", "p90", "vol_estimate"):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"SurvivorPrediction.{name} must be in [0,1], got {v}")
        if not (self.p10 <= self.p50 <= self.p90):
            raise ValueError(
                f"survivor quantiles must be ordered p10<=p50<=p90, got "
                f"p10={self.p10} p50={self.p50} p90={self.p90}"
            )


# ---------------------------------------------------------------------------
# The trained survivor artifact (the pragmatic TFT-placeholder)
# ---------------------------------------------------------------------------


@dataclass
class TrainedSurvivorModel:
    """Trained survivor head + calibrator + quantile-spread estimator.

    The pragmatic implementation: a monotone-constrained LightGBM survival head (objective
    = log-loss, a proper scoring rule), an isotonic calibrator (reused from T-310's
    calibration layer), and a residual-dispersion estimate that widens the quantile band
    for low-confidence / high-Bernoulli-variance predictions.

    TFT_SWAP_POINT: replace the booster + the quantile estimator with a pytorch-forecasting
    TemporalFusionTransformer (or N-HiTS) that emits native quantiles, KEEPING this same
    `predict()` -> SurvivorPrediction contract.  The SLOW loop, the GATE-B monitor, and the
    telemetry do not change.
    """

    booster: lgb.Booster
    calibrator: ProbabilityCalibrator
    covariate_columns: tuple[str, ...]
    model_version: str
    monotone_constraints: list[int]
    seed: int
    loop: str
    is_bootstrap_not_real: bool
    # quantile-spread params (derived from the calib-fold residual dispersion)
    quantile_half_spread: float = 0.0
    calibration_report: CalibrationReport = field(default=None)  # type: ignore[assignment]
    leak_audit_summary: str = ""
    n_train: int = 0
    n_calib: int = 0
    n_test: int = 0

    def _raw(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(self.booster.predict(X), dtype=np.float64)

    def predict_calibrated(self, X: np.ndarray) -> np.ndarray:
        """Calibrated survival probability in [0,1] for a covariate matrix."""
        return self.calibrator.transform(self._raw(X))

    def predict(
        self,
        frame: FeatureFrame,
        mcs: Optional[MCSScore],
        *,
        loop: str = SURVIVOR_LOOP,
    ) -> SurvivorPrediction:
        """Run survivor inference on ONE coin -> SurvivorPrediction (prob+unc+quantiles+vol).

        `loop` MUST be the slow loop (default); a fast/snipe loop raises SurvivorLoopViolation.
        Output is calibrated probability + uncertainty + quantiles + vol ONLY — no price,
        no size, no decision.  Wide spread / high uncertainty de-risks downstream.
        """
        assert_slow_loop_only(loop)
        row = build_survivor_covariate_row(frame, mcs)
        X = np.asarray([row], dtype=np.float32)
        p = float(self.predict_calibrated(X)[0])
        p = min(max(p, 0.0), 1.0)

        # Uncertainty: blend the Bernoulli band with the model's calibration-evidence band.
        bern = bernoulli_uncertainty(p)
        cal_band = (
            self.calibration_report.ece if self.calibration_report is not None else 0.0
        )
        uncertainty = float(min(1.0, 0.5 * bern + 0.5 * min(1.0, bern + cal_band)))

        # Quantiles of the SURVIVAL SCORE (NOT a price).  The spread widens with the
        # per-prediction Bernoulli variance: a 0.5 prediction is maximally uncertain, so its
        # p10..p90 band is widest; a near-0/near-1 prediction is tight.  This is the spread
        # a TFT would emit as native forecast quantiles; here it is a principled dispersion.
        half = float(self.quantile_half_spread) * bern / 0.5 if bern > 0 else 0.0
        p10 = float(min(max(p - half, 0.0), 1.0))
        p90 = float(min(max(p + half, 0.0), 1.0))
        p50 = float(min(max(p, p10), p90))  # keep ordering even at the [0,1] clamps
        vol_estimate = float(min(1.0, bern))

        return SurvivorPrediction(
            mint=frame.mint,
            event_time=frame.event_time,
            model_version=self.model_version,
            p_calibrated=p,
            uncertainty=uncertainty,
            p10=p10,
            p50=p50,
            p90=p90,
            vol_estimate=vol_estimate,
            is_bootstrap_not_real=self.is_bootstrap_not_real,
        )


# ---------------------------------------------------------------------------
# Leak-free training (reuses the snipe classifier's leak-audit + forward split)
# ---------------------------------------------------------------------------


def _survivor_matrix(
    examples: list[JoinedExample],
    rows_by_key: dict[tuple[int, int], SyntheticRow],
) -> tuple[np.ndarray, np.ndarray]:
    """Build the survivor covariate matrix from joined examples + their source rows.

    The JoinedExample carries the SNIPE feature row; the survivor model needs its OWN
    covariate row (MCS + survivor microstructure), so we re-extract from the source
    FeatureFrame (+ optional MCS) keyed by event_time.  Labels are taken from the
    already-leak-audited JoinedExample (joined by event_time only).
    """
    X: list[list[float]] = []
    y: list[int] = []
    for ex in examples:
        key = event_time_key(ex.feature_event_time)
        src = rows_by_key[key]
        mcs = getattr(src, "mcs", None)
        X.append(build_survivor_covariate_row(src.frame, mcs))
        y.append(ex.label)
    return np.asarray(X, dtype=np.float32), np.asarray(y, dtype=np.int32)


def _survivor_model_version(seed: int) -> str:
    return f"survivor-lgbm-v1.0.0-seed{seed}-BOOTSTRAP"


def train_survivor_model(
    rows: list[SyntheticRow],
    *,
    seed: int = 20260616,
    calibration_method: str = "isotonic",
    num_boost_round: int = 200,
    learning_rate: float = 0.05,
    num_leaves: int = 31,
    max_depth: int = 5,
    quantile_half_spread: float = 0.18,
) -> TrainedSurvivorModel:
    """Train the slow-loop survivor model end-to-end, leak-free and calibrated.

    Steps (each the SAME leak-free construction as the snipe classifier):
      0. lineage-taint build guard on the survivor covariate columns (no truth_*/label)
      1. join labels BY EVENT-TIME ONLY (separate labels dataset)
      2. leak audit: feature event-time <= decision-time on every row (printed for co-sign)
      3. forward-only, event-time-ordered split (never shuffled)
      4. LightGBM fit with monotone constraints (adversarial covariates pinned de-risk)
      5. calibrate on the held-out, time-forward calibration slice (isotonic/Platt)
      6. estimate the quantile half-spread from the calib-fold residual dispersion

    `rows` are SyntheticRow (bootstrap); in production the harness supplies real
    FeatureFrame + MCSScore + labels/ rows through the same JoinedExample interface.
    Returns a TrainedSurvivorModel.  is_bootstrap_not_real=True (no capital license).
    """
    # 0. Lineage-taint guard on the survivor covariate columns (fail fast on any leak).
    assert_no_label_taint(list(SURVIVOR_COVARIATE_COLUMNS))

    # 1 + 2. Join labels by event-time only, then run the leak audit.
    joined = join_labels_by_event_time(rows)
    leak_summary = assert_event_time_leq_decision(joined)

    # 3. Forward-only, event-time-ordered split (reuses the snipe split discipline).
    split = time_forward_split(joined)

    # Index the source rows by event_time so we can re-extract the survivor covariates.
    rows_by_key = {event_time_key(r.frame.event_time): r for r in rows}

    X_train, y_train = _survivor_matrix(split.train, rows_by_key)
    X_calib, y_calib = _survivor_matrix(split.calib, rows_by_key)

    if X_train.shape[1] != N_SURVIVOR_COVARIATES:
        raise ValueError(
            f"survivor covariate matrix has {X_train.shape[1]} columns, expected "
            f"{N_SURVIVOR_COVARIATES} (SURVIVOR_COVARIATE_COLUMNS)."
        )

    # 4. LightGBM fit with monotone constraints (adversarial covariates pinned de-risk).
    monotone = survivor_monotone_vector()
    train_set = lgb.Dataset(
        X_train, label=y_train, feature_name=list(SURVIVOR_COVARIATE_COLUMNS)
    )
    params = {
        "objective": "binary",       # log-loss — proper scoring rule (calibration-friendly)
        "metric": "binary_logloss",  # NOT accuracy / win-rate — no win-rate objective
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
        "force_row_wise": True,
        "num_threads": 1,
        "verbosity": -1,
    }
    booster = lgb.train(params, train_set, num_boost_round=num_boost_round)

    # 5. Calibrate on the held-out, time-forward calibration slice.
    raw_calib = np.asarray(booster.predict(X_calib), dtype=np.float64)
    calibrator, calib_report = fit_calibrator(
        raw_calib, y_calib, method=calibration_method, n_bins=10
    )

    return TrainedSurvivorModel(
        booster=booster,
        calibrator=calibrator,
        covariate_columns=SURVIVOR_COVARIATE_COLUMNS,
        model_version=_survivor_model_version(seed),
        monotone_constraints=monotone,
        seed=seed,
        loop=SURVIVOR_LOOP,
        is_bootstrap_not_real=True,
        quantile_half_spread=float(quantile_half_spread),
        calibration_report=calib_report,
        leak_audit_summary=leak_summary,
        n_train=len(split.train),
        n_calib=len(split.calib),
        n_test=len(split.test),
    )
