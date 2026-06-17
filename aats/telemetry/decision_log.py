"""Append-only structured decision log — the forensic audit trail.

Every snipe / skip / exit decision is emitted as a JSON line stamped with
EVENT-TIME (slot + block_time_ms) and the full probability vector that drove it.

CRITICAL: timestamps are event-time, NOT compute-time (wall clock at log-write).
Using compute-time would hide the exact lookahead bias the whole system is built
to detect.  Violating this rule = C-5 failure (BLUEPRINT §7, data-models.md §9).

Schema per line (all fields required for the forensic record):
{
  "event":           "snipe" | "skip" | "exit" | "veto",
  "event_time": {
    "slot":          <int>,        -- the slot of the observed LaunchEvent
    "block_time_ms": <int>         -- chain block_time in epoch-ms (not wall clock)
  },
  "mint":            "<str>",
  "decision":        "SNIPE" | "SKIP" | "VETO",
  "model_p":         <float | null>,   -- calibrated classifier probability
  "model_uncertainty": <float | null>,
  "veto_source":     "gate" | "llm" | "cost_gate" | "staleness" | null,
  "gate_reasons":    [<str>, ...],
  "cost_breakdown": {
    "expected_edge_bps": <int>,
    "total_cost_bps":    <int>,
    "passed":            <bool>
  } | null,
  "tip_floor_at_decision_lamports": <int | null>,  -- C-3 stratification input
  "tip_contention_bucket":          "low" | "medium" | "high" | null,
  "sol_in_lamports":  <int | null>,     -- integer, never float (NFR-009)
  "dry_run":         <bool>,
  "recorded_at_ms":  <int>  -- wall-clock ms at log-write; separate from event_time
}

'recorded_at_ms' exists for audit latency measurement only; it MUST NOT be used
as the event-time for any backtesting or labeling join.  The harness enforces
recorded_at_ms >= event_time.block_time_ms (data-models §9.2).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import structlog
    _STRUCTLOG_AVAILABLE = True
except ImportError:
    _STRUCTLOG_AVAILABLE = False


# ---------------------------------------------------------------------------
# Module-level logger (falls back to stdlib logging if structlog not installed)
# ---------------------------------------------------------------------------
if _STRUCTLOG_AVAILABLE:
    _log = structlog.get_logger("aats.telemetry.decision_log")
else:
    _log = logging.getLogger("aats.telemetry.decision_log")


def log_decision(
    *,
    event: str,
    slot: int,
    block_time_ms: int,
    mint: str,
    decision: str,
    model_p: Optional[float] = None,
    model_uncertainty: Optional[float] = None,
    veto_source: Optional[str] = None,
    gate_reasons: Optional[List[str]] = None,
    cost_breakdown: Optional[Dict[str, Any]] = None,
    tip_floor_at_decision_lamports: Optional[int] = None,
    tip_contention_bucket: Optional[str] = None,
    sol_in_lamports: Optional[int] = None,
    dry_run: bool = True,
) -> None:
    """Emit one append-only decision log line.

    Args:
        event:       "snipe" | "skip" | "exit" | "veto"
        slot:        The slot of the detected LaunchEvent (event-time).
        block_time_ms: chain block_time in epoch-ms (event-time, NOT wall clock).
        mint:        Token mint address.
        decision:    "SNIPE" | "SKIP" | "VETO"
        model_p:     Calibrated classifier probability (None if score not available).
        model_uncertainty: Uncertainty band from the classifier.
        veto_source: Which gate fired the veto (if any).
        gate_reasons: List of gate failure reasons.
        cost_breakdown: Dict with expected_edge_bps, total_cost_bps, passed.
        tip_floor_at_decision_lamports: Live tip floor at decision time (C-3).
        tip_contention_bucket: "low" | "medium" | "high" (C-3 stratification).
        sol_in_lamports: Position size in lamports (integer, never float).
        dry_run:     Whether DRY_RUN_ENABLED was true at decision time.
    """
    # wall-clock at log-write — separate from event_time; used only for
    # audit latency, NEVER for backtesting join (see module docstring)
    recorded_at_ms = int(time.time() * 1000)

    # Enforce: recorded_at_ms >= block_time_ms (data-models §9.2)
    # A negative gap indicates a clock anomaly; log it but don't silently drop.
    clock_gap_ms = recorded_at_ms - block_time_ms
    if clock_gap_ms < 0:
        if _STRUCTLOG_AVAILABLE:
            _log.warning(
                "decision_log_clock_anomaly",
                recorded_at_ms=recorded_at_ms,
                block_time_ms=block_time_ms,
                gap_ms=clock_gap_ms,
                mint=mint,
            )
        else:
            _log.warning(
                "decision_log_clock_anomaly: recorded_at_ms=%d < block_time_ms=%d gap=%dms mint=%s",
                recorded_at_ms, block_time_ms, clock_gap_ms, mint,
            )

    record: Dict[str, Any] = {
        "event": event,
        "event_time": {
            "slot": slot,
            "block_time_ms": block_time_ms,
        },
        "mint": mint,
        "decision": decision,
        "model_p": model_p,
        "model_uncertainty": model_uncertainty,
        "veto_source": veto_source,
        "gate_reasons": gate_reasons or [],
        "cost_breakdown": cost_breakdown,
        "tip_floor_at_decision_lamports": tip_floor_at_decision_lamports,
        "tip_contention_bucket": tip_contention_bucket,
        "sol_in_lamports": sol_in_lamports,
        "dry_run": dry_run,
        "recorded_at_ms": recorded_at_ms,
    }

    # Always emit as JSON to stdout — this is the primary append-only audit trail.
    # Docker log drivers and Loki/CloudWatch capture it from stdout.
    # structlog (if available) is additionally notified for structured log routing,
    # but the authoritative record is the JSON line on stdout.
    print(json.dumps(record), flush=True)

    if _STRUCTLOG_AVAILABLE:
        # Also emit to structlog for log aggregation pipelines.
        # structlog reserves 'event' as its positional arg; our record field
        # is also named 'event', so we pop it out and pass as the positional.
        log_record = {k: v for k, v in record.items() if k != "event"}
        try:
            _log.info(record["event"], **log_record)
        except Exception:  # noqa: BLE001 — never let logging crash the hot path
            pass
