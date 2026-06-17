"""Probability calibration layer (T-310) — isotonic / Platt + reliability diagram.

WHY CALIBRATION COMES BEFORE ACCURACY (the guiding fear)
========================================================
The position sizer (¼-Kelly) trusts the probability LITERALLY.  A model that says "0.7"
and is right 40% of the time sizes the agency into ruin.  So an UNCALIBRATED 0.85 is
worse than a calibrated 0.55.  This module maps the raw booster score to a probability
whose value equals an empirical frequency — and SHIPS the evidence (reliability curve,
Brier, ECE) so the sizer and the reviewer can verify it.

WHAT IT PROVIDES
================
  - fit_calibrator(): fit isotonic regression (default) or Platt scaling (logistic) on a
    HELD-OUT, time-forward calibration slice (training.py passes the calib split — never
    the train or test fold; calibrating on train is itself a leak of optimism).
  - ProbabilityCalibrator: transform(raw) -> calibrated probability in [0,1].  Pickled
    into the artifact; applied AFTER the ONNX tree score in inference.py.
  - reliability_curve(): binned (mean predicted, empirical frequency) pairs for the diagram.
  - brier_score(), expected_calibration_error(): the two scalar calibration metrics.
  - CalibrationReport: bundles them + an in-tolerance verdict for the self-check.

UNCERTAINTY (NEVER A POINT PRICE — probabilities + uncertainty only)
====================================================================
The classifier emits a calibrated probability AND a predictive-uncertainty band.  We
derive uncertainty from the calibration evidence + the Bernoulli variance of the
calibrated probability:
    uncertainty = sqrt(p*(1-p))  blended with the local reliability gap.
High uncertainty is a DE-RISK input downstream (¼-Kelly shrinks) — it can NEVER size up.
This module computes the calibration-evidence component; inference.py combines it per row.

NO win-rate.  ECE / Brier are CALIBRATION metrics, not win-rate.  There is no accuracy
threshold gate here — the acceptance metric is net-PnL + model-vs-baseline (harness).

DETERMINISTIC: isotonic and logistic fits are deterministic given the data.  No RNG.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression


@dataclass(frozen=True)
class ReliabilityBin:
    """One bin of the reliability diagram."""

    bin_lower: float
    bin_upper: float
    count: int
    mean_predicted: float      # average predicted probability in the bin
    empirical_frequency: float  # observed fraction of positives in the bin


@dataclass(frozen=True)
class CalibrationReport:
    """The calibration evidence the sizer + reviewer consume (self-check 3)."""

    method: str
    n: int
    brier_score: float
    ece: float                       # expected calibration error
    reliability_bins: list[ReliabilityBin]
    ece_tolerance: float
    brier_tolerance: float
    in_tolerance: bool               # ece <= ece_tolerance AND brier <= brier_tolerance

    def summary(self) -> str:
        return (
            f"CALIBRATION [{self.method}] n={self.n} "
            f"Brier={self.brier_score:.4f} (<= {self.brier_tolerance}) "
            f"ECE={self.ece:.4f} (<= {self.ece_tolerance}) "
            f"=> {'IN TOLERANCE' if self.in_tolerance else 'OUT OF TOLERANCE'}"
        )


class ProbabilityCalibrator:
    """A fitted calibrator that maps raw scores -> calibrated probabilities in [0,1].

    Wraps either an IsotonicRegression or a LogisticRegression (Platt).  transform() is
    a pure, deterministic, monotone non-decreasing map — it never re-orders candidates,
    only re-scales the score to a frequency.  This is the function applied AFTER the ONNX
    tree score in the inference wrapper.
    """

    def __init__(self, method: str, isotonic: IsotonicRegression | None, platt: LogisticRegression | None):
        self.method = method
        self._isotonic = isotonic
        self._platt = platt

    def transform(self, raw: np.ndarray) -> np.ndarray:
        raw = np.asarray(raw, dtype=np.float64).reshape(-1)
        if self.method == "isotonic":
            assert self._isotonic is not None
            out = self._isotonic.predict(raw)
        elif self.method == "platt":
            assert self._platt is not None
            out = self._platt.predict_proba(raw.reshape(-1, 1))[:, 1]
        else:  # pragma: no cover - guarded at fit time
            raise ValueError(f"unknown calibration method {self.method!r}")
        return np.clip(out, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def brier_score(p: np.ndarray, y: np.ndarray) -> float:
    """Mean squared error between predicted probability and outcome (lower = better)."""
    p = np.asarray(p, dtype=np.float64).reshape(-1)
    y = np.asarray(y, dtype=np.float64).reshape(-1)
    return float(np.mean((p - y) ** 2))


def reliability_curve(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> list[ReliabilityBin]:
    """Equal-width binned reliability curve: (mean predicted, empirical frequency) per bin."""
    p = np.asarray(p, dtype=np.float64).reshape(-1)
    y = np.asarray(y, dtype=np.float64).reshape(-1)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins: list[ReliabilityBin] = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        # include the right edge in the LAST bin only (half-open elsewhere)
        mask = (p >= lo) & (p <= hi) if i == n_bins - 1 else (p >= lo) & (p < hi)
        count = int(mask.sum())
        if count == 0:
            mean_pred = float((lo + hi) / 2)
            emp = float("nan")
        else:
            mean_pred = float(p[mask].mean())
            emp = float(y[mask].mean())
        bins.append(
            ReliabilityBin(
                bin_lower=float(lo),
                bin_upper=float(hi),
                count=count,
                mean_predicted=mean_pred,
                empirical_frequency=emp,
            )
        )
    return bins


def expected_calibration_error(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> float:
    """ECE: count-weighted mean |empirical frequency - mean predicted| over bins."""
    bins = reliability_curve(p, y, n_bins=n_bins)
    total = sum(b.count for b in bins)
    if total == 0:
        return float("nan")
    ece = 0.0
    for b in bins:
        if b.count == 0:
            continue
        ece += (b.count / total) * abs(b.empirical_frequency - b.mean_predicted)
    return float(ece)


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------


def fit_calibrator(
    raw_scores: np.ndarray,
    y: np.ndarray,
    *,
    method: str = "isotonic",
    n_bins: int = 10,
    ece_tolerance: float = 0.10,
    brier_tolerance: float = 0.25,
) -> tuple[ProbabilityCalibrator, CalibrationReport]:
    """Fit a calibrator on a HELD-OUT calibration slice and build the calibration report.

    Args:
        raw_scores: the booster's raw (uncalibrated) probabilities on the calib slice.
        y: the binary labels of the calib slice.
        method: "isotonic" (default) or "platt".
        n_bins: reliability-diagram bin count.
        ece_tolerance / brier_tolerance: the declared tolerances the report checks against.
            (These are CALIBRATION tolerances — not win-rate, not an accuracy gate.)

    Returns (calibrator, report).  The report's reliability curve / Brier / ECE are
    computed on the SAME calib slice the calibrator was fit on (in-sample calibration
    fit — the out-of-sample calibration check on the test fold is the harness's GATE-4).
    """
    raw = np.asarray(raw_scores, dtype=np.float64).reshape(-1)
    y = np.asarray(y, dtype=np.int32).reshape(-1)
    if raw.shape[0] != y.shape[0]:
        raise ValueError("raw_scores and y must have the same length")
    if raw.shape[0] == 0:
        raise ValueError("cannot fit a calibrator on an empty calibration slice")

    if method == "isotonic":
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(raw, y)
        calibrator = ProbabilityCalibrator("isotonic", iso, None)
    elif method == "platt":
        platt = LogisticRegression(C=1e6, solver="lbfgs")
        platt.fit(raw.reshape(-1, 1), y)
        calibrator = ProbabilityCalibrator("platt", None, platt)
    else:
        raise ValueError(f"unknown calibration method {method!r}; use 'isotonic' or 'platt'")

    calibrated = calibrator.transform(raw)
    brier = brier_score(calibrated, y)
    ece = expected_calibration_error(calibrated, y, n_bins=n_bins)
    bins = reliability_curve(calibrated, y, n_bins=n_bins)
    in_tol = (ece <= ece_tolerance) and (brier <= brier_tolerance)

    report = CalibrationReport(
        method=method,
        n=int(raw.shape[0]),
        brier_score=brier,
        ece=ece,
        reliability_bins=bins,
        ece_tolerance=ece_tolerance,
        brier_tolerance=brier_tolerance,
        in_tolerance=in_tol,
    )
    return calibrator, report


# ---------------------------------------------------------------------------
# Uncertainty (calibration-evidence component) — used by the inference wrapper
# ---------------------------------------------------------------------------


def bernoulli_uncertainty(p: float) -> float:
    """Predictive uncertainty band for a calibrated probability p in [0,1].

    sqrt(p*(1-p)) is the Bernoulli standard deviation — maximal (0.5) at p=0.5 and zero
    at the extremes.  This is the per-prediction uncertainty the inference wrapper emits.
    High uncertainty => DE-RISK downstream (¼-Kelly shrinks); it can NEVER size up.
    """
    p = float(min(max(p, 0.0), 1.0))
    return float(np.sqrt(p * (1.0 - p)))
