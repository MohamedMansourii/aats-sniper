"""Pinned, reproducible RETRAINING SCAFFOLD for the SLOW-loop chart-path / REGIME model (M2-CP-08).

Run:  python -m aats.models.train_regime

WHAT THIS IS (a scaffold, not the trained production model)
===========================================================
The REAL regime model (M2-CP-03) is DATA-BLOCKED: it needs >=3,000 RECORDED launches with
post-migration price-path history, which do not exist yet.  This module is the *retraining
harness scaffold* that:
  - runs END-TO-END, DETERMINISTICALLY, on the synthetic bootstrap corpus TODAY (proving the
    train -> calibrate -> RegimeSignal pipeline is leak-free, calibrated, and reproducible), and
  - is READY TO POINT AT THE REAL CORPUS later — the labels/ reader, the M2-CP-01
    `PricePathTensor` input, and the deep temporal head plug in at marked swap points WITHOUT
    changing the SLOW loop, the `RegimeSignal` output contract, or the de-risk wiring.

DEEP-MODEL DEP — DECLARED, LAZILY IMPORTED (never on the bootstrap path)
=======================================================================
The production regime model's primary input is the M2-CP-01 `aats.features.ta.PricePathTensor`
(SLOW-loop only), consumed by a DEEP TEMPORAL head (`DEEP_MODEL_DEP` = pytorch-forecasting
TFT / N-HiTS, BLUEPRINT §4.2).  That dependency is HEAVY and OPTIONAL: it is DECLARED
(`DEEP_MODEL_DEP`) but only imported by `_maybe_import_deep_model(use_deep_model=True)` — the
default bootstrap run NEVER imports it and trains a deterministic LightGBM MULTICLASS
placeholder head (`REGIME_MODEL_SWAP_POINT`).  So this scaffold runs with the pinned deps
alone; the deep path is wired but dormant until the recorded tensor corpus exists (R1).

SLOW-LOOP ONLY (BLUEPRINT triple-loop boundary)
===============================================
The chart-path / regime model is SLOW-loop only — NO ONNX export, NO Rust shim, NEVER on the
FAST/SNIPE path.  `TrainedRegimeModel.predict()` takes a `loop` kwarg and REUSES the canonical
`aats.models.survivor.assert_slow_loop_only` guard; a fast/snipe caller raises loudly.

A multiclass STATE + calibrated probs + uncertainty — NEVER a win-rate or a price
=================================================================================
The output is the frozen `aats.contracts.models.RegimeSignal` (ADR-0014): the four CALIBRATED
class probabilities (a genuine distribution) + the argmax STATE + a predictive uncertainty
band.  There is NO price, NO size, and NO win-rate / success-rate / realized-mult field
anywhere (HONESTY CLAUSE, AC-037).  A regime is a STATE, never a success rate.

ASYMMETRIC TRUST — the accumulation/bullish class is provably INERT (de-risk-only)
=================================================================================
A regime output can reach a position ONLY through the SLOW-loop de-risk wiring
(`aats.controller.regime_wiring`), which maps the STATE via `RegimeDeRiskDirective`
(codomain {NONE, REDUCE, FORCE_EXIT, VETO_ENTRY} — a risk-INCREASING directive is
INEXPRESSIBLE BY TYPE).  `ACCUMULATION` and `NEUTRAL` map to `NONE`: a confident bullish
prediction carries ZERO control authority — it can never delay an exit, relax a stop, or size
up.  This model NEVER emits a size, a stop, or a trade decision.

POINT-IN-TIME LEAK-FREEDOM (T-300a) — the whole game
====================================================
Bootstrap regime LABELS are produced by the M2-CP-02 machinery (`build_regime_outcome`): the
forward-window gate asserts every outcome sample slot in (decision.slot, resolution.slot], and
the leak audit REUSES the frozen `assert_event_time_leq_decision`.  FEATURES (survivor
exogenous covariates) are decision-time only; LABELS join to features by event_time ONLY.  The
lineage-taint guard (`assert_no_regime_label_taint`) fails the build if any label column
reaches the feature matrix.  No wall-clock, no forward-fill from future bars.

DETERMINISM / REPRODUCIBILITY (self-check: run twice -> same numbers)
====================================================================
Fixed seed, single-threaded deterministic LightGBM, deterministic isotonic calibration, seeded
numpy trajectory generation.  Re-running reproduces the model-card metrics bit-for-bit.

MONEY DISCIPLINE — reserves/size are INTEGER lamports/base units; PRICES ARE RATIOS (float ok
per data-models §0).  No PnL/fee/tip/size is ever computed here.

NO SECRETS, NO KEYS, NO EXECUTION.  This script trains and evaluates a model.  It never builds,
signs, or lands a transaction; it touches no keypair, RPC key, or swap code.  `is_bootstrap_
not_real=True` on every artifact — NO capital license until the R1 recorded corpus exists.
"""

from __future__ import annotations

import importlib
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import lightgbm as lgb
import numpy as np
from sklearn.metrics import roc_auc_score

from aats.contracts.features import FeatureFrame
from aats.contracts.models import MCSScore, RegimeSignal, RegimeState
from aats.models.calibration import (
    CalibrationReport,
    ProbabilityCalibrator,
    expected_calibration_error,
    fit_calibrator,
    reliability_curve,
)
from aats.models.regime_labels import (
    REGIME_LABEL_TAXONOMY_VERSION,
    RegimeLabel,
    RegimeOutcomeSample,
    assert_no_regime_label_taint,
    build_regime_outcome,
    regime_label_to_joined_example,
)
from aats.models.survivor import (
    N_SURVIVOR_COVARIATES,
    SURVIVOR_COVARIATE_COLUMNS,
    SURVIVOR_LOOP,
    assert_slow_loop_only,
    build_survivor_covariate_row,
)
from aats.models.synthetic import SyntheticRow, generate_synthetic_corpus
from aats.models.training import (
    assert_event_time_leq_decision,
    time_forward_split,
)

# ---------------------------------------------------------------------------
# Pinned constants (reproducibility)
# ---------------------------------------------------------------------------

FIXED_SEED = 20260706          # ADR-0014 date-aligned; deterministic re-train -> same numbers
CORPUS_N = 3000                # bootstrap corpus size
ARTIFACT_DIR = Path(__file__).with_name("artifacts")

# The DECLARED deep temporal dependency for the production regime head (M2-CP-03).  It is
# LAZILY imported only when use_deep_model=True AND installed — never on the bootstrap path.
DEEP_MODEL_DEP = "pytorch-forecasting"  # TemporalFusionTransformer / N-HiTS (BLUEPRINT §4.2)
# The seam a real deep temporal head slots into, keeping the same predict()->RegimeSignal API.
REGIME_MODEL_SWAP_POINT = "aats.models.train_regime._fit_regime_head"

# The multiclass label order — MUST match REGIME_LABEL_CODES (0..3).  Index c of the model's
# probability vector corresponds to REGIME_CLASS_ORDER[c].
REGIME_CLASS_ORDER: tuple[RegimeLabel, ...] = (
    RegimeLabel.ACCUMULATION,     # code 0 (INERT / bullish)
    RegimeLabel.NEUTRAL,          # code 1 (INERT)
    RegimeLabel.DISTRIBUTION,     # code 2 (DE-RISK)
    RegimeLabel.RUG_IN_PROGRESS,  # code 3 (DE-RISK-MAXIMAL)
)
N_REGIME_CLASSES = len(REGIME_CLASS_ORDER)

_LABEL_TO_STATE: dict[RegimeLabel, RegimeState] = {
    RegimeLabel.ACCUMULATION: RegimeState.ACCUMULATION,
    RegimeLabel.NEUTRAL: RegimeState.NEUTRAL,
    RegimeLabel.DISTRIBUTION: RegimeState.DISTRIBUTION,
    RegimeLabel.RUG_IN_PROGRESS: RegimeState.RUG_IN_PROGRESS,
}

# --- bootstrap forward-trajectory constants (synthetic; reserves/size are INTEGER) ---
REGIME_SELLABILITY_MAX_SLIPPAGE_BPS = 2000   # a decision-relevant exit above 20% = NOT sellable
REGIME_FORWARD_SAMPLES = 6                    # strictly-forward outcome-window samples per launch
_ANCHOR_PRICE_RATIO = 1.0                     # normalized decision-anchor price (a ratio, not money)
_TOKEN_RESERVE0 = 1_000_000_000_000           # nominal token-side pool reserve (base units)
_POOL_EXIT_FRACTION_DENOM = 50                # decision-relevant exit size = 2% of the pool
# LP-pull collapse: token reserve drops well BELOW the exit size -> exit slippage -> ~100%.
_RUG_TOKEN_RESERVE = max(1, (_TOKEN_RESERVE0 // _POOL_EXIT_FRACTION_DENOM) // 50)

# --- auto-disable / disable-threshold constants ---
REGIME_AUTO_DISABLE_MIN_EDGE_AUC = 0.02       # min macro-OVR-AUC edge over baseline per window
REGIME_CONSECUTIVE_NO_SIGNAL_TO_DISABLE = 2   # consecutive no-signal windows -> auto-disable

# The forbidden success-rate / price field names — scanned by the tests + the card guard.
_FORBIDDEN_METRIC_NAMES = frozenset(
    {"win_rate", "winrate", "success_rate", "realized_mult", "price", "pnl", "size"}
)


# ---------------------------------------------------------------------------
# The deep-model dep — DECLARED, LAZILY imported (never on the bootstrap path)
# ---------------------------------------------------------------------------


def _maybe_import_deep_model(use_deep_model: bool):
    """Lazily import the DECLARED deep temporal dep, or return None for the bootstrap head.

    The production regime head is a deep temporal model (`DEEP_MODEL_DEP`, TFT / N-HiTS)
    consuming the M2-CP-01 `PricePathTensor`.  It is a HEAVY, OPTIONAL dependency that is
    declared here but only imported when `use_deep_model=True` AND installed.  The default
    bootstrap run returns None and trains the deterministic LightGBM multiclass placeholder
    head, so this scaffold runs end-to-end with the pinned deps alone.
    """
    if not use_deep_model:
        return None
    try:  # pragma: no cover - the deep dep is optional and not pinned for the bootstrap
        return importlib.import_module(DEEP_MODEL_DEP.replace("-", "_"))
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            f"use_deep_model=True requires the optional deep dependency {DEEP_MODEL_DEP!r} "
            "(TFT / N-HiTS). Install it, or run the deterministic LightGBM bootstrap head "
            "(use_deep_model=False, the default)."
        ) from exc


# ---------------------------------------------------------------------------
# Bootstrap regime dataset — leak-free labels via the M2-CP-02 machinery
# ---------------------------------------------------------------------------


def _forward_trajectory(
    frame: FeatureFrame,
    decision_slot: int,
    resolution_slot: int,
    horizon: int,
    rng: np.random.Generator,
    *,
    k: int = REGIME_FORWARD_SAMPLES,
) -> tuple[list[RegimeOutcomeSample], float, int, int]:
    """Generate a STRICTLY-FORWARD outcome window for one launch (bootstrap only).

    The latent forward regime is biased by DECISION-TIME risk proxies (concentration, sell
    tax, being behind smart money, net buy pressure) plus seeded noise — so the label is
    (synthetically) LEARNABLE from the decision-time covariates without ever being copied
    into them.  The LABEL itself is decided by the real `classify_regime` over this window.

    Returns (samples, decision_reference_price_ratio, decision_reference_holder_count,
    decision_relevant_size_base).  Reserves/size are INTEGER base units; price is a ratio.
    """
    ref_price = _ANCHOR_PRICE_RATIO
    ref_holders = max(1, int(frame.holder_count))
    size = max(1, _TOKEN_RESERVE0 // _POOL_EXIT_FRACTION_DENOM)
    sol0 = max(1, int(frame.lp_depth_lamports))

    # Decision-time, observable risk proxies (bias the latent forward regime).
    conc = min(max(int(frame.holder_concentration_top10_bps), 0), 10_000) / 10_000.0
    tax = min(max(int(frame.sell_tax_bps), 0), 5_000) / 5_000.0
    behind = 1.0 if int(frame.smart_wallets_in) > 0 else 0.0
    bp = float(frame.first_k_buy_pressure)  # lamports; a ratio-scale statistic (Decimal->float ok)
    health = math.tanh(bp / 1e9) - conc - tax - 0.3 * behind + float(rng.normal(0.0, 0.5))
    rug_logit = 2.2 * conc + 1.8 * tax + 0.9 * behind - 2.3 + float(rng.normal(0.0, 0.5))
    rug_p = 1.0 / (1.0 + math.exp(-rug_logit))

    # Strictly-forward, strictly-increasing slot grid; last sample == resolution slot.
    slots = [decision_slot + max(1, round(j * horizon / k)) for j in range(1, k + 1)]
    slots[-1] = resolution_slot
    for i in range(1, len(slots)):
        if slots[i] <= slots[i - 1]:
            slots[i] = slots[i - 1] + 1

    samples: list[RegimeOutcomeSample] = []

    if float(rng.random()) < rug_p:
        # RUG: sellability collapse in the back half (LP token pull) — price is irrelevant.
        net = float(rng.uniform(-0.5, 0.2))
        hdelta = float(rng.uniform(-0.4, 0.0))
        collapse_from = k // 2
        for j in range(k):
            frac = (j + 1) / k
            price = ref_price * (1.0 + net * frac)
            holders = max(1, int(round(ref_holders * (1.0 + hdelta * frac))))
            token_res = _TOKEN_RESERVE0 if j < collapse_from else _RUG_TOKEN_RESERVE
            samples.append(
                RegimeOutcomeSample(
                    outcome_slot=slots[j],
                    sol_reserve_lamports=sol0,
                    token_reserve_base=token_res,
                    holder_count=holders,
                    spot_price_ratio=price,
                )
            )
        return samples, ref_price, ref_holders, size

    # SELLABLE trajectories: the class is decided by the price/holder summary vs the anchor.
    if health > 0.6:            # ACCUMULATION bias: net up, holders grow, monotone (shallow dd)
        net = float(rng.uniform(0.25, 0.8))
        hdelta = float(rng.uniform(0.12, 0.5))
    elif health < -0.6:         # DISTRIBUTION bias: net down past -0.25 / holders shrink
        net = float(rng.uniform(-0.7, -0.28))
        hdelta = float(rng.uniform(-0.35, -0.05))
    else:                       # NEUTRAL: mild, in-between, no trigger fires
        net = float(rng.uniform(-0.12, 0.15))
        hdelta = float(rng.uniform(-0.08, 0.06))

    for j in range(k):
        frac = (j + 1) / k
        price = ref_price * (1.0 + net * frac)
        holders = max(1, int(round(ref_holders * (1.0 + hdelta * frac))))
        samples.append(
            RegimeOutcomeSample(
                outcome_slot=slots[j],
                sol_reserve_lamports=sol0,
                token_reserve_base=_TOKEN_RESERVE0,
                holder_count=holders,
                spot_price_ratio=price,
            )
        )
    return samples, ref_price, ref_holders, size


def build_regime_bootstrap_dataset(
    rows: list[SyntheticRow],
    *,
    seed: int = FIXED_SEED,
    loop: str = SURVIVOR_LOOP,
) -> list:
    """Build leak-free (survivor-covariate-row, RegimeOutcome) pairs -> frozen JoinedExamples.

    Each launch gets a strictly-forward outcome window (`_forward_trajectory`) that the real
    M2-CP-02 `build_regime_outcome` classifies into a leak-free `RegimeOutcome`.  The FEATURE
    side is the decision-time survivor exogenous covariate row (already provenance/monotone
    guarded).  Labels join to features by event_time ONLY (`regime_label_to_joined_example`).
    Returns a list of `aats.models.training.JoinedExample` carrying the integer regime code.
    """
    rng = np.random.default_rng(seed + 101)  # a distinct stream from the corpus generator
    joined = []
    for r in rows:
        frame = r.frame
        decision = frame.event_time
        resolution = r.resolution_event_time
        horizon = r.label_horizon_h_slots
        samples, ref_price, ref_holders, size = _forward_trajectory(
            frame, decision.slot, resolution.slot, horizon, rng
        )
        outcome = build_regime_outcome(
            mint=frame.mint,
            decision_event_time=decision,
            resolution_event_time=resolution,
            label_horizon_h_slots=horizon,
            decision_reference_price_ratio=ref_price,
            decision_reference_holder_count=ref_holders,
            outcome_samples=samples,
            decision_relevant_size_base=size,
            max_slippage_bps=REGIME_SELLABILITY_MAX_SLIPPAGE_BPS,
            loop=loop,
            is_bootstrap_not_real=True,
        )
        covariate_row = build_survivor_covariate_row(frame, None)
        joined.append(
            regime_label_to_joined_example(
                feature_row=covariate_row,
                feature_event_time=decision,
                outcome=outcome,
            )
        )
    return joined


# ---------------------------------------------------------------------------
# The multiclass regime head (the deterministic LightGBM bootstrap placeholder)
# ---------------------------------------------------------------------------


def _fit_regime_head(
    x_train: np.ndarray,
    y_train: np.ndarray,
    *,
    seed: int,
    num_boost_round: int,
    learning_rate: float,
    num_leaves: int,
    max_depth: int,
) -> lgb.Booster:
    """REGIME_MODEL_SWAP_POINT: fit the deterministic multiclass regime head.

    A real deep temporal head (`DEEP_MODEL_DEP`) replaces THIS function later, keeping the
    same (X -> per-class raw probabilities) contract and the `predict()->RegimeSignal` API.
    Objective = multiclass log-loss (a PROPER scoring rule — NOT accuracy / NOT a win-rate).

    NOTE ON MONOTONE CONSTRAINTS: LightGBM applies one `monotone_constraints` vector to ALL
    class trees identically, so a per-class de-risk sign is not expressible here.  The
    de-risk-INERT guarantee is enforced STRUCTURALLY by `RegimeDeRiskDirective` + the
    argmax->directive mapping (ACCUMULATION/NEUTRAL -> NONE), not by the head — the bullish
    class carries zero control authority regardless of the head's internals.
    """
    train_set = lgb.Dataset(
        x_train, label=y_train, feature_name=list(SURVIVOR_COVARIATE_COLUMNS)
    )
    params = {
        "objective": "multiclass",       # multiclass log-loss — proper scoring rule
        "num_class": N_REGIME_CLASSES,
        "metric": "multi_logloss",       # NOT accuracy / win-rate — no win-rate objective
        "learning_rate": learning_rate,
        "num_leaves": num_leaves,
        "max_depth": max_depth,
        "min_data_in_leaf": 30,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "seed": seed,
        "bagging_seed": seed,
        "feature_fraction_seed": seed,
        "deterministic": True,
        "force_row_wise": True,          # determinism (no row/col threading nondeterminism)
        "num_threads": 1,                # single-threaded => bit-reproducible
        "verbosity": -1,
    }
    return lgb.train(params, train_set, num_boost_round=num_boost_round)


def _predict_raw_matrix(booster: lgb.Booster, x: np.ndarray) -> np.ndarray:
    """Raw per-class probabilities (n, N_REGIME_CLASSES) from the multiclass booster."""
    raw = np.asarray(booster.predict(x), dtype=np.float64)
    return raw.reshape(-1, N_REGIME_CLASSES)


def _calibrate_and_normalize(
    raw: np.ndarray, calibrators: list[ProbabilityCalibrator]
) -> np.ndarray:
    """Per-class isotonic calibration + row-renormalization to a genuine distribution.

    Each class's raw probability is passed through its own calibrator (fit one-vs-rest on the
    held-out calib fold), then each row is renormalized to sum to 1 (uniform fallback if a row
    calibrates to all-zero).  The result is a valid `RegimeSignal` distribution.
    """
    cal = np.column_stack(
        [calibrators[c].transform(raw[:, c]) for c in range(N_REGIME_CLASSES)]
    )
    row_sums = cal.sum(axis=1, keepdims=True)
    uniform = 1.0 / N_REGIME_CLASSES
    return np.where(row_sums <= 0.0, uniform, cal / np.where(row_sums <= 0.0, 1.0, row_sums))


# ---------------------------------------------------------------------------
# The trained regime artifact (pragmatic deep-head placeholder)
# ---------------------------------------------------------------------------


@dataclass
class TrainedRegimeModel:
    """Trained multiclass regime head + per-class calibrators + metadata (bootstrap).

    Emits the frozen `RegimeSignal` (ADR-0014): calibrated class probabilities + argmax STATE
    + uncertainty.  SLOW-loop only (`predict(loop=...)` reuses `assert_slow_loop_only`).  NO
    price, NO size, NO win-rate.  is_bootstrap_not_real=True (NO capital license until R1).
    """

    booster: lgb.Booster
    calibrators: list[ProbabilityCalibrator]
    covariate_columns: tuple[str, ...]
    model_version: str
    taxonomy_version: str
    seed: int
    loop: str
    is_bootstrap_not_real: bool
    class_order: tuple[RegimeLabel, ...] = REGIME_CLASS_ORDER
    calibration_reports: list[CalibrationReport] = field(default_factory=list)
    leak_audit_summary: str = ""
    n_train: int = 0
    n_calib: int = 0
    n_test: int = 0
    # OOS test fold kept for deterministic metric computation (never a training input).
    test_x: np.ndarray = field(default=None, repr=False)  # type: ignore[assignment]
    test_y: np.ndarray = field(default=None, repr=False)  # type: ignore[assignment]
    class_counts: dict[str, int] = field(default_factory=dict)

    def predict_calibrated_matrix(self, x: np.ndarray) -> np.ndarray:
        """Calibrated, row-normalized per-class probabilities for a covariate matrix."""
        raw = _predict_raw_matrix(self.booster, x)
        return _calibrate_and_normalize(raw, self.calibrators)

    @staticmethod
    def _renorm_exact(probs: np.ndarray) -> np.ndarray:
        """Renormalize a single 4-vector to sum EXACTLY 1.0 (RegimeSignal validator tolerance)."""
        p = np.clip(np.asarray(probs, dtype=np.float64), 0.0, 1.0)
        s = float(p.sum())
        p = np.full(N_REGIME_CLASSES, 1.0 / N_REGIME_CLASSES) if s <= 0.0 else p / s
        top = int(np.argmax(p))
        p[top] += 1.0 - float(p.sum())  # absorb float residual into the argmax class
        return p

    def predict(
        self,
        frame: FeatureFrame,
        mcs: MCSScore | None = None,
        *,
        loop: str = SURVIVOR_LOOP,
        creator_outflow_velocity_bps: float | None = None,
    ) -> RegimeSignal:
        """Run regime inference on ONE coin -> RegimeSignal (STATE + calibrated probs + unc).

        `loop` MUST be the slow loop (default); a fast/snipe loop raises `SurvivorLoopViolation`
        (the canonical SLOW-loop guard).  Output is a calibrated distribution + argmax STATE +
        uncertainty ONLY — NO price, NO size, NO win-rate, NO trade decision.  A confident
        ACCUMULATION/NEUTRAL prediction is INERT downstream (RegimeDeRiskDirective -> NONE).
        """
        assert_slow_loop_only(loop)
        row = build_survivor_covariate_row(frame, mcs, creator_outflow_velocity_bps)
        x = np.asarray([row], dtype=np.float32)
        probs = self._renorm_exact(self.predict_calibrated_matrix(x)[0])
        top = int(np.argmax(probs))
        state = _LABEL_TO_STATE[self.class_order[top]]

        # Uncertainty = normalized Shannon entropy of the distribution in [0,1].  High entropy
        # (a flat, undecided distribution) => high uncertainty => DE-RISK only, never size-up.
        eps = 1e-12
        entropy = float(-np.sum(probs * np.log(probs + eps)))
        uncertainty = float(min(1.0, max(0.0, entropy / math.log(N_REGIME_CLASSES))))

        return RegimeSignal(
            mint=frame.mint,
            event_time=frame.event_time,
            model_version=self.model_version,
            taxonomy_version=self.taxonomy_version,
            regime=state,
            p_accumulation=float(probs[0]),
            p_neutral=float(probs[1]),
            p_distribution=float(probs[2]),
            p_rug_in_progress=float(probs[3]),
            uncertainty=uncertainty,
            is_bootstrap_not_real=self.is_bootstrap_not_real,
        )


def _regime_model_version(seed: int) -> str:
    return f"regime-lgbm-mc-v{REGIME_LABEL_TAXONOMY_VERSION}-seed{seed}-BOOTSTRAP"


def train_regime_model(
    rows: list[SyntheticRow],
    *,
    seed: int = FIXED_SEED,
    use_deep_model: bool = False,
    num_boost_round: int = 200,
    learning_rate: float = 0.05,
    num_leaves: int = 31,
    max_depth: int = 5,
) -> TrainedRegimeModel:
    """Train the SLOW-loop regime model end-to-end, leak-free and calibrated (bootstrap).

    Steps (each the SAME leak-free construction as the snipe/survivor models):
      0. lineage-taint build guard on the survivor covariate columns (no truth_*/label)
      1. build leak-free (covariate, RegimeOutcome) JoinedExamples (M2-CP-02 machinery)
      2. leak audit: feature event-time <= decision-time on every row (printed for co-sign)
      3. forward-only, event-time-ordered split (never shuffled)
      4. fit the multiclass regime head (deterministic LightGBM; deep head is the swap point)
      5. per-class isotonic calibration on the held-out, time-forward calib fold

    `rows` are SyntheticRow (bootstrap); in production the harness supplies real
    FeatureFrame + PricePathTensor + labels/ rows through the same JoinedExample interface.
    Returns a TrainedRegimeModel.  is_bootstrap_not_real=True (NO capital license).
    """
    _maybe_import_deep_model(use_deep_model)  # dormant on the bootstrap path (returns None)

    # 0. Lineage-taint build guard on the feature (covariate) columns.
    assert_no_regime_label_taint(list(SURVIVOR_COVARIATE_COLUMNS))

    # 1 + 2. Leak-free join by event-time only, then the reused leak audit.
    joined = build_regime_bootstrap_dataset(rows, seed=seed)
    leak_summary = assert_event_time_leq_decision(joined)

    # 3. Forward-only, event-time-ordered split (never shuffled).
    split = time_forward_split(joined)
    x_train = np.asarray([e.feature_row for e in split.train], dtype=np.float32)
    y_train = np.asarray([e.label for e in split.train], dtype=np.int32)
    x_calib = np.asarray([e.feature_row for e in split.calib], dtype=np.float32)
    y_calib = np.asarray([e.label for e in split.calib], dtype=np.int32)
    x_test = np.asarray([e.feature_row for e in split.test], dtype=np.float32)
    y_test = np.asarray([e.label for e in split.test], dtype=np.int32)

    if x_train.shape[1] != N_SURVIVOR_COVARIATES:
        raise ValueError(
            f"regime covariate matrix has {x_train.shape[1]} columns, expected "
            f"{N_SURVIVOR_COVARIATES} (SURVIVOR_COVARIATE_COLUMNS)."
        )

    # 4. Fit the multiclass head (deterministic).
    booster = _fit_regime_head(
        x_train,
        y_train,
        seed=seed,
        num_boost_round=num_boost_round,
        learning_rate=learning_rate,
        num_leaves=num_leaves,
        max_depth=max_depth,
    )

    # 5. Per-class one-vs-rest isotonic calibration on the held-out calib fold.
    raw_calib = _predict_raw_matrix(booster, x_calib)
    calibrators: list[ProbabilityCalibrator] = []
    reports: list[CalibrationReport] = []
    for c in range(N_REGIME_CLASSES):
        y_c = (y_calib == c).astype(np.int32)
        cal, rep = fit_calibrator(raw_calib[:, c], y_c, method="isotonic", n_bins=10)
        calibrators.append(cal)
        reports.append(rep)

    all_y = np.asarray([e.label for e in joined], dtype=np.int32)
    class_counts = {
        REGIME_CLASS_ORDER[c].value: int(np.sum(all_y == c)) for c in range(N_REGIME_CLASSES)
    }

    return TrainedRegimeModel(
        booster=booster,
        calibrators=calibrators,
        covariate_columns=SURVIVOR_COVARIATE_COLUMNS,
        model_version=_regime_model_version(seed),
        taxonomy_version=REGIME_LABEL_TAXONOMY_VERSION,
        seed=seed,
        loop=SURVIVOR_LOOP,
        is_bootstrap_not_real=True,
        calibration_reports=reports,
        leak_audit_summary=leak_summary,
        n_train=len(split.train),
        n_calib=len(split.calib),
        n_test=len(split.test),
        test_x=x_test,
        test_y=y_test,
        class_counts=class_counts,
    )


# ---------------------------------------------------------------------------
# Frozen naive baseline + multiclass metrics (the honest baseline-gap machinery)
# ---------------------------------------------------------------------------


def frozen_regime_baseline_probs(x: np.ndarray) -> np.ndarray:
    """FROZEN naive-momentum regime baseline (never fit) — soft class distribution.

    A naive momentum reader maps first-K net buy pressure to ACCUMULATION vs DISTRIBUTION and
    is STRUCTURALLY BLIND to sellability collapse: the RUG_IN_PROGRESS logit is a fixed floor.
    So the model's honest edge (if any) is exactly the DISTRIBUTION/RUG detection the momentum
    baseline cannot see.  Constants are frozen — a test fails the build if they are re-fit.
    """
    col = {c: i for i, c in enumerate(SURVIVOR_COVARIATE_COLUMNS)}
    bp = np.asarray(x[:, col["first_k_buy_pressure"]], dtype=np.float64)
    m = np.tanh(bp / 1e9)  # frozen scale (1 SOL = 1e9 lamports)
    logits = np.column_stack(
        [
            1.5 * m,                    # ACCUMULATION
            np.zeros_like(m),           # NEUTRAL
            -1.5 * m,                   # DISTRIBUTION
            np.full_like(m, -2.0),      # RUG_IN_PROGRESS — momentum is BLIND to sellability
        ]
    )
    e = np.exp(logits - logits.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


def _macro_ovr_auc(y: np.ndarray, prob: np.ndarray) -> float:
    """Macro one-vs-rest AUC over classes that have both positives and negatives present."""
    aucs: list[float] = []
    for c in range(N_REGIME_CLASSES):
        y_c = (y == c).astype(np.int32)
        pos = int(y_c.sum())
        if pos == 0 or pos == len(y_c):
            continue
        aucs.append(float(roc_auc_score(y_c, prob[:, c])))
    return float(np.mean(aucs)) if aucs else float("nan")


def _multiclass_logloss(y: np.ndarray, prob: np.ndarray) -> float:
    p = np.clip(prob, 1e-12, 1.0)
    return float(-np.mean(np.log(p[np.arange(len(y)), y])))


def _multiclass_brier(y: np.ndarray, prob: np.ndarray) -> float:
    onehot = np.zeros_like(prob)
    onehot[np.arange(len(y)), y] = 1.0
    return float(np.mean(np.sum((prob - onehot) ** 2, axis=1)))


def compute_regime_report(model: TrainedRegimeModel) -> dict:
    """Compute all card metrics on the OOS test fold — deterministic, JSON-serializable.

    Reports multiclass calibration (Brier + top-label ECE + reliability), the model-vs-frozen-
    baseline gap (macro-OVR-AUC + multiclass log-loss), and the rolling auto-disable windows.
    There is NO win-rate / accuracy gate — acceptance is net-PnL + model-vs-baseline on
    RECORDED data (the harness), NEVER this synthetic bootstrap.
    """
    y = model.test_y
    x = model.test_x
    model_prob = model.predict_calibrated_matrix(x)
    base_prob = frozen_regime_baseline_probs(x)

    # Multiclass calibration on the OOS test fold (top-label reliability).
    conf = model_prob.max(axis=1)
    pred = model_prob.argmax(axis=1)
    correct = (pred == y).astype(np.int32)
    test_ece = expected_calibration_error(conf, correct, n_bins=10)
    test_bins = reliability_curve(conf, correct, n_bins=10)
    test_brier = _multiclass_brier(y, model_prob)

    # Model vs frozen baseline (the honest baseline gap).
    model_macro_auc = _macro_ovr_auc(y, model_prob)
    base_macro_auc = _macro_ovr_auc(y, base_prob)
    model_ll = _multiclass_logloss(y, model_prob)
    base_ll = _multiclass_logloss(y, base_prob)
    edge_auc = (
        model_macro_auc - base_macro_auc
        if not (math.isnan(model_macro_auc) or math.isnan(base_macro_auc))
        else float("nan")
    )

    # Rolling auto-disable over thirds of the OOS test fold (point-in-time order).
    thirds = np.array_split(np.arange(len(y)), 3)
    windows: list[dict] = []
    consecutive = 0
    disabled = False
    for idx in thirds:
        m_auc = _macro_ovr_auc(y[idx], model_prob[idx])
        b_auc = _macro_ovr_auc(y[idx], base_prob[idx])
        w_edge = (
            m_auc - b_auc if not (math.isnan(m_auc) or math.isnan(b_auc)) else float("nan")
        )
        no_signal = math.isnan(w_edge) or (w_edge < REGIME_AUTO_DISABLE_MIN_EDGE_AUC)
        consecutive = consecutive + 1 if no_signal else 0
        if consecutive >= REGIME_CONSECUTIVE_NO_SIGNAL_TO_DISABLE:
            disabled = True
        windows.append(
            {
                "n": int(len(idx)),
                "model_macro_auc": _jnum(m_auc),
                "baseline_macro_auc": _jnum(b_auc),
                "edge_auc": _jnum(w_edge),
                "no_signal": bool(no_signal),
            }
        )

    return {
        "is_bootstrap_not_real": True,
        "no_capital_license": True,
        "model_version": model.model_version,
        "taxonomy_version": model.taxonomy_version,
        "deep_model_dep": DEEP_MODEL_DEP,
        "regime_model_swap_point": REGIME_MODEL_SWAP_POINT,
        "seed": model.seed,
        "loop": model.loop,
        "n_train": model.n_train,
        "n_calib": model.n_calib,
        "n_test": model.n_test,
        "class_order": [s.value for s in REGIME_CLASS_ORDER],
        "class_counts": model.class_counts,
        "leak_audit_summary": model.leak_audit_summary,
        "test_fold_calibration": {
            "multiclass_brier": _jnum(test_brier),
            "top_label_ece": _jnum(test_ece),
            "reliability_bins": [asdict(b) for b in test_bins],
        },
        "per_class_calibration": [
            {
                "class": REGIME_CLASS_ORDER[c].value,
                "method": model.calibration_reports[c].method,
                "brier": _jnum(model.calibration_reports[c].brier_score),
                "ece": _jnum(model.calibration_reports[c].ece),
                "in_tolerance": bool(model.calibration_reports[c].in_tolerance),
            }
            for c in range(N_REGIME_CLASSES)
        ],
        "model_vs_baseline_test": {
            "model_macro_ovr_auc": _jnum(model_macro_auc),
            "baseline_macro_ovr_auc": _jnum(base_macro_auc),
            "edge_macro_ovr_auc": _jnum(edge_auc),
            "model_multiclass_logloss": _jnum(model_ll),
            "baseline_multiclass_logloss": _jnum(base_ll),
        },
        "rolling_monitor": {
            "disabled": bool(disabled),
            "min_edge_auc": REGIME_AUTO_DISABLE_MIN_EDGE_AUC,
            "consecutive_no_signal_to_disable": REGIME_CONSECUTIVE_NO_SIGNAL_TO_DISABLE,
            "windows": windows,
        },
        "covariate_columns": list(SURVIVOR_COVARIATE_COLUMNS),
    }


def _jnum(v: float) -> float | None:
    """JSON-clean a float: NaN -> None (deterministic, strict-JSON safe)."""
    f = float(v)
    return None if math.isnan(f) else f


# ---------------------------------------------------------------------------
# The training entrypoint (writes the model card + metrics + reliability)
# ---------------------------------------------------------------------------


def run_training(
    seed: int = FIXED_SEED, corpus_n: int = CORPUS_N, *, write: bool = True
) -> dict:
    """Train, calibrate, and compute all card metrics.  Writes the artifacts when `write`.

    Deterministic: the same (seed, corpus_n) reproduces the metrics dict bit-for-bit.
    """
    rows = generate_synthetic_corpus(n=corpus_n, seed=seed)
    model = train_regime_model(rows, seed=seed)
    metrics = compute_regime_report(model)

    if write:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        with (ARTIFACT_DIR / "metrics_regime.json").open("w", encoding="utf-8") as fh:
            json.dump(metrics, fh, indent=2, sort_keys=True)
        with (ARTIFACT_DIR / "reliability_regime.json").open("w", encoding="utf-8") as fh:
            json.dump(
                metrics["test_fold_calibration"]["reliability_bins"], fh, indent=2
            )
        _write_model_card(metrics)
    return metrics


def _write_model_card(m: dict) -> None:
    mvb = m["model_vs_baseline_test"]
    tf = m["test_fold_calibration"]
    cc = m["class_counts"]
    rm = m["rolling_monitor"]
    card = f"""# MODEL CARD — SLOW-loop chart-path / REGIME model (M2-CP-08)

> **BOOTSTRAP, NOT REAL — NO CAPITAL LICENSE.** `is_bootstrap_not_real = {m["is_bootstrap_not_real"]}`,
> `no_capital_license = {m["no_capital_license"]}`.  This is a **retraining-harness SCAFFOLD**: the
> trained production regime model (M2-CP-03) is DATA-BLOCKED until the **R1 recorded corpus**
> (>=3,000 recorded launches with post-migration price-path history) exists.  The numbers below are
> computed on the SYNTHETIC bootstrap corpus and prove the pipeline is **leak-free, calibrated,
> reproducible, and de-risk-inert** — NOT that the model has live edge.  The sole production
> acceptance metric is **net-of-cost PnL + model-vs-baseline on RECORDED data** (the clean-room
> harness), never this synthetic set.  **NO win-rate anywhere.**  Real capital stays **DISABLED
> behind DRY-RUN** regardless of any number here.

## Identity
- **model_version:** `{m["model_version"]}`
- **taxonomy_version:** `{m["taxonomy_version"]}` (`aats.models.regime_labels`)
- **seed:** `{m["seed"]}` (deterministic; re-training reproduces these metrics bit-for-bit).
- **loop:** `{m["loop"]}` — SLOW-loop only.  **No ONNX export, no Rust shim, never FAST/SNIPE**
  (`assert_slow_loop_only`; BLUEPRINT triple-loop boundary).
- **algorithm:** multiclass gradient-boosted trees (LightGBM, `objective=multiclass`, multiclass
  log-loss — a proper scoring rule) with per-class isotonic calibration.  The production form is a
  **deep temporal head** (`deep_model_dep = {m["deep_model_dep"]}`, TFT / N-HiTS) consuming the
  M2-CP-01 `PricePathTensor`; it is **declared but lazily imported** and slots into
  `{m["regime_model_swap_point"]}` without changing the output contract.
- **output:** the frozen `aats.contracts.models.RegimeSignal` (ADR-0014) — a multiclass STATE +
  the four CALIBRATED class probabilities (a genuine distribution) + a predictive uncertainty band.
  **No price field.  No size field.  No win-rate / success-rate / realized-mult field.**

## Asymmetric trust — the accumulation/bullish class is provably INERT (de-risk-only)
A regime output reaches a position ONLY via the SLOW-loop de-risk wiring
(`aats.controller.regime_wiring.SlowLoopRegimeWiring`), which maps the STATE through
`RegimeDeRiskDirective` (codomain `{{NONE, REDUCE, FORCE_EXIT, VETO_ENTRY}}` — a risk-INCREASING
directive is **inexpressible by type**):

| STATE | directive | effect |
|---|---|---|
| `ACCUMULATION` | `NONE` | **INERT** — sets no flag; cannot delay an exit, relax a stop, or size up |
| `NEUTRAL` | `NONE` | **INERT** |
| `DISTRIBUTION` | `REDUCE` | de-risk: shrink / veto-half (`set_veto_flag`) |
| `RUG_IN_PROGRESS` | `FORCE_EXIT` | de-risk-maximal: `set_narrative_failure_flag` -> ExitEngine |

The SNIPE/FAST loops NEVER consume `RegimeSignal` or call this model — they read the SAME pre-set
scalar de-risk flags (`veto` / `narrative_failure`) they already read.  No hot-path model call.

## Features (input) — {len(m["covariate_columns"])} exogenous covariates (bootstrap)
Decision-time survivor exogenous covariates (`SURVIVOR_COVARIATE_COLUMNS`): first-60s
microstructure (LP depth, holder count, top-10 concentration, sell tax, sniper-cluster, first-K
buy pressure / unique buyers), survivor TA (rsi/macd/bb_width + presence flags), adversarial
selectivity (smart_wallets_in, entry lag), MCS exogenous covariates (adversarial / contrarian by
construction), and the M2-CP-07 creator-outflow-velocity feature.  **In production the PRIMARY
input is the M2-CP-01 `PricePathTensor`** (log-return / drawdown / holder- & volume-delta channels
with a CENSORED mask), consumed by the deep temporal head; these covariates enter as exogenous
inputs.  **No truth_*/label column can enter the matrix** — `assert_no_regime_label_taint()` fails
the build on any (lineage taint).

## Label (leak-free, co-owned with backtest-qa-engineer) — M2-CP-02 taxonomy
A multiclass STATE over the survivor/exit horizon `[decision, resolution]`, produced by
`build_regime_outcome` / `classify_regime`: `ACCUMULATION | NEUTRAL | DISTRIBUTION |
RUG_IN_PROGRESS` (exhaustive + mutually exclusive), gated by the "remains-sellable at
decision-relevant size" constant-product exit-depth probe.  **A regime is a STATE, never a
win-rate, success-rate, realized multiple, or price.**
- Labels live in a **separate dataset**, joined to features **by event_time ONLY**.
- **Point-in-time (T-300a):** every outcome sample slot in `(decision.slot, resolution.slot]`, in
  strictly increasing order (`build_regime_outcome` forward-window gate); `resolution_event_time =
  decision + H` is stamped and strictly later (horizon proof).  No wall-clock, no forward-fill.
- **Leak audit:** every row asserts `feature_event_time <= decision_event_time` AND
  `resolution_event_time > decision_event_time` (reused `assert_event_time_leq_decision`).
  Result: `{m["leak_audit_summary"].splitlines()[0] if m["leak_audit_summary"] else "PASS"}`
- **Bootstrap class distribution:** ACCUMULATION={cc.get("ACCUMULATION", 0)},
  NEUTRAL={cc.get("NEUTRAL", 0)}, DISTRIBUTION={cc.get("DISTRIBUTION", 0)},
  RUG_IN_PROGRESS={cc.get("RUG_IN_PROGRESS", 0)}.

## Training window
- corpus n = {m["n_train"] + m["n_calib"] + m["n_test"]} (synthetic bootstrap).
- forward-only, event-time-ordered split (**never shuffled**): train={m["n_train"]} /
  calib={m["n_calib"]} / test={m["n_test"]} (the latest event-time window is OOS).
- The real corpus (R1) plugs in at the same `SyntheticRow` / `JoinedExample` interface.

## Calibration (calibration before accuracy)
- method: **per-class isotonic**, fit one-vs-rest on the held-out, time-forward calibration slice
  (never the train or test fold), then row-renormalized to a genuine distribution.
- **OOS test-fold multiclass Brier = {_fmt(tf["multiclass_brier"])}**,
  **top-label ECE = {_fmt(tf["top_label_ece"])}**.
- Reliability diagram (top-label): `artifacts/reliability_regime.json` (10 bins).
- Uncertainty = normalized Shannon entropy of the class distribution in [0,1]; a flat / undecided
  distribution => high uncertainty => **DE-RISK only** (shrinks size downstream); it can NEVER
  size up, widen a stop, or override a hard stop.

## Baseline gap (beat the baseline or stay silent) — OOS test fold, NOT a win-rate
- FROZEN naive-momentum regime baseline (`frozen_regime_baseline_probs`, fixed constants, never
  fit): maps first-K net buy pressure to ACCUMULATION vs DISTRIBUTION and is **structurally blind
  to sellability collapse** (RUG logit is a fixed floor).  The model's honest edge, if any, is
  exactly the DISTRIBUTION/RUG detection the momentum reader cannot see.
- model macro-OVR-AUC = **{_fmt(mvb["model_macro_ovr_auc"])}** vs baseline macro-OVR-AUC =
  **{_fmt(mvb["baseline_macro_ovr_auc"])}** -> **edge = {_fmt(mvb["edge_macro_ovr_auc"])}**.
- model multiclass log-loss = {_fmt(mvb["model_multiclass_logloss"])} vs baseline
  {_fmt(mvb["baseline_multiclass_logloss"])} (lower is better).
- **HONEST CAVEAT:** these are SYNTHETIC-corpus numbers.  They do NOT establish live edge; they
  prove the baseline-gap machinery is correct and computable.  The binding acceptance gate is
  net-PnL + model-vs-baseline on RECORDED data (GATE-B), never this bootstrap.

## Disable thresholds (auto-disable -> INERT, never risk-up)
- min macro-OVR-AUC edge over baseline per rolling OOS window: **{rm["min_edge_auc"]}**.
- consecutive no-signal windows to auto-disable: **{rm["consecutive_no_signal_to_disable"]}**.
- rolling monitor `disabled` on the bootstrap test fold: **{rm["disabled"]}**.
- When the model auto-disables (or a signal is absent/stale), the SLOW loop sets **no de-risk
  flag** — the regime STATE goes **INERT**.  Because the model is de-risk-only, disabling it
  removes an *extra* de-risk layer but can NEVER increase risk; the survivable-stop layers
  (breaker / survivable stop / DMS) remain fully in force.  "No signal -> inert" is the safe default.

## Known failure regimes / honest caveats
- **Synthetic data is not the live distribution** — this card's numbers do not transfer; they prove
  the *pipeline* is leak-free, calibrated, and de-risk-inert, not that the model has live edge.
- The bootstrap regime LABELS come from fixed, never-data-fit classification thresholds
  (M2-CP-02); re-examined against the R1 corpus at M2-CP-03 (a taxonomy-version bump if changed).
- The sellability gate uses a single-pool constant-product exit probe: multi-pool / routed exit,
  MEV-front-run exit, and time-varying honeypot taxes are conservative-by-omission (the gate can
  only be too STRICT, never too loose).
- The bootstrap head is a **deep-temporal-head PLACEHOLDER** (`{m["regime_model_swap_point"]}`);
  native temporal-quantile modelling on the `PricePathTensor` is deferred to the R1 corpus.
- Multiclass monotone (de-risk) constraints are not expressible per-class in LightGBM; the
  de-risk-INERT guarantee is enforced STRUCTURALLY by `RegimeDeRiskDirective` + the argmax->
  directive mapping, not by the head.

## Boundaries (non-waivable)
Outputs a multiclass STATE + calibrated probabilities + uncertainty, **never a price, a size, a
win-rate, or a trade decision**.  SLOW-loop only — never on the FAST/SNIPE path.  No execution
code, no keypair, no RPC key, no swap building.  Any downstream use may only **DE-RISK** — never
size up, widen a stop, or override a hard stop (enforced by the type system + the OMS).  Real
capital DISABLED behind DRY-RUN.  **NO capital license until the R1 recorded corpus exists.**
"""
    (ARTIFACT_DIR / "MODEL_CARD_regime.md").write_text(card, encoding="utf-8")


def _fmt(v: float | None) -> str:
    """Format a JSON-cleaned metric for the card (None -> N/A, floats to 4 dp)."""
    return "N/A" if v is None else f"{float(v):.4f}"


if __name__ == "__main__":
    result = run_training()
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "covariate_columns"},
            indent=2,
            sort_keys=True,
        )
    )
