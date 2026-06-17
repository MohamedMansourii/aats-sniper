"""Calibration / eval harness for the Reasoner's `confidence` (T-313).

WE SHIP A CALIBRATION CHECK, NOT A VIBE
=======================================
The Reasoner emits a `confidence` in [0, 1].  Downstream consumers (the audit log, any
operator review) read it as a probability that the de-risk verdict is correct.  So it
must be CALIBRATED: among the verdicts emitted with confidence ~0.8, about 80% should be
correct.  This module computes the reliability curve, the Expected Calibration Error
(ECE), and the Brier score on a held-out eval set of (confidence, was_correct) pairs.

These are STATISTICAL quantities (floats) — not money.  No win-rate metric is computed
here (HONESTY CLAUSE): "was the de-risk verdict correct" is a calibration label for the
confidence number, NOT a trading win-rate, and it is never surfaced as a headline metric.
The sole headline trading metric remains net-of-cost PnL + model-vs-baseline (owned by
the backtest harness, T-400/T-401).

POINT-IN-TIME
=============
The eval set is (confidence, correctness) pairs recorded at decision time and resolved
post-hoc by the clean-room harness.  This module does not fabricate labels; it consumes a
provided eval set.  No lookahead is introduced here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationBin:
    """One reliability-curve bin."""

    lower: float
    upper: float
    count: int
    mean_confidence: float  # average predicted confidence in this bin
    empirical_correct: float  # fraction actually correct in this bin


@dataclass(frozen=True)
class CalibrationReport:
    """The calibration verdict for the Reasoner's confidence on an eval set."""

    n: int
    brier_score: float  # lower is better (0 = perfect)
    ece: float  # Expected Calibration Error; lower is better
    bins: tuple[CalibrationBin, ...]
    within_tolerance: bool  # ECE <= tolerance


def brier_score(confidences: list[float], correct: list[bool]) -> float:
    """Mean squared error between confidence and the binary correctness outcome.

    confidence is the model's stated probability that its de-risk verdict is correct.
    Brier = mean((confidence - correct)^2).  0 = perfect, 0.25 = always-0.5 baseline.
    """
    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must be the same length")
    if not confidences:
        raise ValueError("eval set is empty")
    total = 0.0
    for c, y in zip(confidences, correct, strict=True):
        if not (0.0 <= c <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {c}")
        total += (c - (1.0 if y else 0.0)) ** 2
    return total / len(confidences)


def reliability_curve(
    confidences: list[float],
    correct: list[bool],
    *,
    n_bins: int = 10,
) -> list[CalibrationBin]:
    """Bin the (confidence, correct) pairs into a reliability curve.

    Each bin reports mean predicted confidence vs empirical correctness.  A
    well-calibrated model has mean_confidence ≈ empirical_correct in every populated bin.
    """
    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must be the same length")
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")

    edges = [i / n_bins for i in range(n_bins + 1)]
    bins: list[CalibrationBin] = []
    for b in range(n_bins):
        lo, hi = edges[b], edges[b + 1]
        # Last bin is inclusive of the upper edge.
        if b == n_bins - 1:
            members = [(c, y) for c, y in zip(confidences, correct, strict=True) if lo <= c <= hi]
        else:
            members = [(c, y) for c, y in zip(confidences, correct, strict=True) if lo <= c < hi]
        count = len(members)
        if count == 0:
            bins.append(
                CalibrationBin(
                    lower=lo, upper=hi, count=0, mean_confidence=0.0, empirical_correct=0.0
                )
            )
            continue
        mean_conf = sum(c for c, _ in members) / count
        emp = sum(1.0 for _, y in members if y) / count
        bins.append(
            CalibrationBin(
                lower=lo,
                upper=hi,
                count=count,
                mean_confidence=mean_conf,
                empirical_correct=emp,
            )
        )
    return bins


def expected_calibration_error(bins: list[CalibrationBin]) -> float:
    """Weighted average gap between confidence and empirical correctness across bins."""
    total = sum(b.count for b in bins)
    if total == 0:
        raise ValueError("no samples in any bin")
    ece = 0.0
    for b in bins:
        if b.count == 0:
            continue
        ece += (b.count / total) * abs(b.mean_confidence - b.empirical_correct)
    return ece


def run_calibration_check(
    confidences: list[float],
    correct: list[bool],
    *,
    n_bins: int = 10,
    ece_tolerance: float = 0.15,
) -> CalibrationReport:
    """Compute the full calibration report and the within-tolerance verdict.

    `ece_tolerance` is the declared acceptable ECE (the threshold gate / any operator
    review consumes confidence as if true, so a loosely-calibrated confidence must be
    flagged).  This is a check we SHIP — the self-check pastes the numbers.
    """
    bs = brier_score(confidences, correct)
    bins = reliability_curve(confidences, correct, n_bins=n_bins)
    ece = expected_calibration_error(bins)
    return CalibrationReport(
        n=len(confidences),
        brier_score=bs,
        ece=ece,
        bins=tuple(bins),
        within_tolerance=ece <= ece_tolerance,
    )
