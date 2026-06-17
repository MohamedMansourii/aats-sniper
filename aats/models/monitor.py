"""Model-vs-baseline auto-disable monitor (T-310) — emit "no signal" over a stale edge.

THE GUIDING FEAR MADE OPERATIONAL
=================================
A meme-coin predictor that BEATS naive momentum out-of-sample today routinely STOPS
beating it tomorrow (regime change, edge decay, the venue mechanics shift).  An
uncalibrated or stale edge that the sizer still trusts literally is how the agency sizes
into ruin.  This monitor runs the live model against the FROZEN naive-momentum baseline
on point-in-time, out-of-sample windows and AUTO-DISABLES the model — emits "no signal"
— the moment the model stops beating the baseline by the declared margin.

WHAT "BEATS THE BASELINE" MEANS HERE
====================================
The production GATE-B unit is net-PnL-per-unit-risk on RECORDED data (the harness owns
that).  This monitor is the CONTINUOUS, lightweight guard that runs on every rolling
window: it compares the model's ranking quality (AUC / PR-AUC of the CALIBRATED
probability against the realised label) to the baseline's on the SAME window.  If the
model's edge over the baseline drops below `min_edge`, the window's verdict is DISABLED.

NEVER A SIZE-UP.  This monitor can only TURN THE MODEL OFF (emit "no signal") — it can
never widen a stop, size up, or override a hard stop.  "no signal" is the safe default:
downstream, absent a model score, the SNIPE loop SKIPs (data-models §9.1).

DEGRADED / SHUFFLED INPUT => DISABLED (self-check 7).  Feed a degraded window (labels
shuffled, or the model's scores destroyed) and the monitor MUST emit "no signal" rather
than a stale edge.  The shuffle-collapse test exercises exactly this.

NO win-rate.  The metric is edge-over-baseline (a ranking/discrimination gap), never a
win-rate target.  There is no accuracy threshold that licenses capital — only "does the
model still beat dumb momentum, out-of-sample, on this window."

DETERMINISTIC / OFFLINE.  Pure function of (model scores, baseline scores, labels).  No
RNG, no network, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score


class MonitorVerdict(StrEnum):
    """The monitor's only outputs — both are safe (ENABLED or de-risk/off)."""

    ENABLED = "ENABLED"          # model still beats the baseline by >= min_edge -> use the model
    NO_SIGNAL = "NO_SIGNAL"      # model no longer beats the baseline -> emit no signal (SKIP)


@dataclass(frozen=True)
class WindowComparison:
    """Model-vs-baseline comparison on ONE rolling out-of-sample window."""

    n: int
    model_auc: float
    baseline_auc: float
    model_pr_auc: float
    baseline_pr_auc: float
    edge_auc: float              # model_auc - baseline_auc (the headline GATE-B-like delta)
    min_edge: float
    verdict: MonitorVerdict

    def summary(self) -> str:
        return (
            f"MONITOR window n={self.n}: model_AUC={self.model_auc:.4f} "
            f"baseline_AUC={self.baseline_auc:.4f} edge={self.edge_auc:+.4f} "
            f"(min_edge={self.min_edge}) -> {self.verdict.value}"
        )


def _safe_auc(y: np.ndarray, scores: np.ndarray) -> float:
    """AUC that degrades safely: a single-class window or NaN scores -> 0.5 (no information)."""
    y = np.asarray(y).reshape(-1)
    scores = np.asarray(scores, dtype=np.float64).reshape(-1)
    if len(np.unique(y)) < 2:
        return 0.5
    if not np.all(np.isfinite(scores)):
        return 0.5
    return float(roc_auc_score(y, scores))


def _safe_pr(y: np.ndarray, scores: np.ndarray) -> float:
    y = np.asarray(y).reshape(-1)
    scores = np.asarray(scores, dtype=np.float64).reshape(-1)
    if len(np.unique(y)) < 2 or not np.all(np.isfinite(scores)):
        return float(np.mean(y)) if len(y) else 0.0
    return float(average_precision_score(y, scores))


def compare_window(
    model_scores: np.ndarray,
    baseline_scores: np.ndarray,
    labels: np.ndarray,
    *,
    min_edge: float = 0.02,
) -> WindowComparison:
    """Compare the model to the baseline on ONE out-of-sample window.

    Args:
        model_scores: the model's CALIBRATED probabilities on the window.
        baseline_scores: the frozen baseline's signal (baseline_p) on the SAME window.
        labels: the realised binary outcomes.
        min_edge: the minimum AUC edge the model must hold over the baseline to stay
            ENABLED.  Below it (or non-finite scores, or shuffled-to-noise), the window
            verdict is NO_SIGNAL.  This is the auto-disable threshold (recorded in the card).

    Returns a WindowComparison.  ENABLED only when the model's OOS discrimination beats
    the baseline's by >= min_edge; otherwise NO_SIGNAL (emit no signal, do not trade a
    stale edge).
    """
    labels = np.asarray(labels).reshape(-1)
    model_scores = np.asarray(model_scores, dtype=np.float64).reshape(-1)
    baseline_scores = np.asarray(baseline_scores, dtype=np.float64).reshape(-1)

    m_auc = _safe_auc(labels, model_scores)
    b_auc = _safe_auc(labels, baseline_scores)
    m_pr = _safe_pr(labels, model_scores)
    b_pr = _safe_pr(labels, baseline_scores)
    edge = m_auc - b_auc

    verdict = MonitorVerdict.ENABLED if edge >= min_edge else MonitorVerdict.NO_SIGNAL
    return WindowComparison(
        n=int(len(labels)),
        model_auc=m_auc,
        baseline_auc=b_auc,
        model_pr_auc=m_pr,
        baseline_pr_auc=b_pr,
        edge_auc=edge,
        min_edge=min_edge,
        verdict=verdict,
    )


@dataclass(frozen=True)
class RollingMonitorResult:
    """Rolling auto-disable result across several consecutive OOS windows."""

    windows: list[WindowComparison]
    consecutive_no_signal_to_disable: int
    disabled: bool                  # True => the model is auto-disabled (emit no signal)
    final_verdict: MonitorVerdict

    def summary(self) -> str:
        head = (
            f"ROLLING MONITOR: {len(self.windows)} windows, "
            f"disable-after={self.consecutive_no_signal_to_disable} consecutive NO_SIGNAL "
            f"-> {'DISABLED (no signal)' if self.disabled else 'ENABLED'}"
        )
        return head + "\n" + "\n".join("  " + w.summary() for w in self.windows)


def rolling_auto_disable(
    windows: list[WindowComparison],
    *,
    consecutive_no_signal_to_disable: int = 2,
) -> RollingMonitorResult:
    """Auto-disable the model after N consecutive NO_SIGNAL windows.

    Walks the windows in event-time order; once `consecutive_no_signal_to_disable` NO_SIGNAL
    verdicts occur in a row, the model is DISABLED (emit no signal) and stays disabled
    (re-enabling is a deliberate operator action, not an automatic flip — the safe default
    is OFF).  A single NO_SIGNAL window does not disable (noise tolerance), but it never
    enables a size-up either.
    """
    streak = 0
    disabled = False
    for w in windows:
        if w.verdict == MonitorVerdict.NO_SIGNAL:
            streak += 1
            if streak >= consecutive_no_signal_to_disable:
                disabled = True
        else:
            streak = 0
    final = MonitorVerdict.NO_SIGNAL if disabled else (
        windows[-1].verdict if windows else MonitorVerdict.NO_SIGNAL
    )
    return RollingMonitorResult(
        windows=windows,
        consecutive_no_signal_to_disable=consecutive_no_signal_to_disable,
        disabled=disabled,
        final_verdict=final,
    )
