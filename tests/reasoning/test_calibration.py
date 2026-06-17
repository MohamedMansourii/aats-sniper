"""Calibration check on `confidence` (self-check item 7).

We SHIP a calibration check, not a vibe.  This computes the reliability curve, ECE, and
Brier score on a deterministic eval set of (confidence, was_correct) pairs and asserts the
within-tolerance verdict behaves correctly for both a well-calibrated and a miscalibrated
set.
"""

from __future__ import annotations

import pytest

from aats.reasoning.calibration import (
    brier_score,
    expected_calibration_error,
    reliability_curve,
    run_calibration_check,
)


def _well_calibrated_set(n_per_bin: int = 50) -> tuple[list[float], list[bool]]:
    """Build a set where, in each confidence band, the empirical correctness matches.

    For confidence c, make exactly round(c * n_per_bin) of the n_per_bin samples correct.
    This is calibrated BY CONSTRUCTION, so ECE should be small.
    """
    confidences: list[float] = []
    correct: list[bool] = []
    for k in range(1, 10):
        c = k / 10.0
        n_correct = round(c * n_per_bin)
        for i in range(n_per_bin):
            confidences.append(c)
            correct.append(i < n_correct)
    return confidences, correct


def _miscalibrated_set(n: int = 200) -> tuple[list[float], list[bool]]:
    """Overconfident: confidence 0.95 everywhere but only 30% correct."""
    return [0.95] * n, [i < int(0.3 * n) for i in range(n)]


def test_well_calibrated_set_is_within_tolerance():
    conf, corr = _well_calibrated_set()
    report = run_calibration_check(conf, corr, n_bins=10, ece_tolerance=0.10)
    print(
        f"\n[calibration:well] n={report.n} brier={report.brier_score:.4f} "
        f"ece={report.ece:.4f} within_tolerance={report.within_tolerance}"
    )
    assert report.within_tolerance is True
    assert report.ece <= 0.10


def test_miscalibrated_set_is_flagged():
    conf, corr = _miscalibrated_set()
    report = run_calibration_check(conf, corr, n_bins=10, ece_tolerance=0.10)
    print(
        f"\n[calibration:miscal] n={report.n} brier={report.brier_score:.4f} "
        f"ece={report.ece:.4f} within_tolerance={report.within_tolerance}"
    )
    # Overconfident: gap ~ 0.65 => well above tolerance => flagged.
    assert report.within_tolerance is False
    assert report.ece > 0.10


def test_brier_bounds():
    # Perfect predictions => Brier 0.
    assert brier_score([1.0, 0.0, 1.0], [True, False, True]) == 0.0
    # Always-wrong-and-certain => Brier 1.
    assert brier_score([1.0, 1.0], [False, False]) == 1.0


def test_reliability_curve_bins_sum_to_n():
    conf, corr = _well_calibrated_set(n_per_bin=20)
    bins = reliability_curve(conf, corr, n_bins=10)
    assert sum(b.count for b in bins) == len(conf)


def test_empty_eval_set_raises():
    with pytest.raises(ValueError):
        brier_score([], [])


def test_ece_requires_samples():
    with pytest.raises(ValueError):
        expected_calibration_error(reliability_curve([], [], n_bins=5))
