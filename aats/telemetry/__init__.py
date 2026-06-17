"""M5 Immunity — Prometheus telemetry and structlog decision logging.

This module is the promoted form of sol-sniper/sniper_sim/metrics.py
(BLUEPRINT §3, T-250).  It exposes real Prometheus metrics so Grafana panels
show the same scorecard that the sim already surfaces.

Key exports:
  metrics     — AATSMetrics: the live Prometheus gauge/histogram registry
  decision_log — append-only structlog decision writer (event-time stamped)

HARD RULES enforced here:
  - No win_rate field or metric label — HONESTY CLAUSE.
  - net_pnl_sol is always NET (gross minus tips + priority + slippage).
  - All monetary values exported as Decimal-string labels or integer gauge;
    float is permitted only for ratio metrics (land_rate, tip_efficiency).
  - event_time (slot + block_time_ms) is the timestamp on every decision log
    line — NOT compute-time (wall clock at log-write would hide lookahead bias).
"""

from aats.telemetry.metrics import AATSMetrics, get_metrics
from aats.telemetry.decision_log import log_decision

__all__ = ["AATSMetrics", "get_metrics", "log_decision"]
