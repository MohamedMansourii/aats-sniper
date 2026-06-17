"""Prometheus metrics registry for AATS — promoted from sniper_sim/metrics.py.

Every metric here maps to a Grafana panel (infrastructure.md §7).
Naming convention: aats_<subsystem>_<unit>.

HARD RULES:
  - NO win_rate metric — HONESTY CLAUSE (BLUEPRINT §11, BUILD-DIRECTIVE).
  - net_pnl is always NET (gross minus costs).  Gross is a secondary label.
  - Money counters use integer lamports; rates use float (permitted for ratios).
  - model_vs_baseline_delta is the GATE-B headline panel (AC-037).
"""
from __future__ import annotations

import os
import threading
from typing import Optional

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        Info,
        CollectorRegistry,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )
    _PROM_AVAILABLE = True
except ImportError:  # noqa: BLE001 — optional at import time; test stubs replace it
    _PROM_AVAILABLE = False

# ---------------------------------------------------------------------------
# Histogram bucket sets (seconds unless noted)
# ---------------------------------------------------------------------------
_SNIPE_LATENCY_BUCKETS = (
    0.005, 0.010, 0.020, 0.030, 0.050,  # 5–50ms
    0.075, 0.100, 0.125, 0.150,          # 75–150ms (budget ceiling)
    0.200, 0.300, 0.500, 1.0,
)
_FAST_TICK_BUCKETS = (
    0.005, 0.010, 0.020, 0.050, 0.100,  # 100ms budget ceiling
    0.150, 0.200, 0.500, 1.0,
)
_INFERENCE_BUCKETS = (
    0.001, 0.002, 0.005, 0.010, 0.020, 0.050, 0.100, 0.200, 0.500,
)


class AATSMetrics:
    """All live Prometheus metrics for the AATS bot.

    Instantiate once per process via get_metrics().
    Pass a custom CollectorRegistry for test isolation.
    """

    def __init__(self, registry: Optional["CollectorRegistry"] = None) -> None:
        if not _PROM_AVAILABLE:
            raise RuntimeError(
                "prometheus_client is not installed. "
                "Run: pip install prometheus-client"
            )
        reg = registry  # None → default global registry

        # ------------------------------------------------------------------ #
        # Per-module heartbeats (Gauge: 1=alive, 0=dead; scraped by Prometheus)
        # ------------------------------------------------------------------ #
        kwargs = {"registry": reg} if reg is not None else {}

        self.heartbeat = Gauge(
            "aats_heartbeat",
            "Module liveness (1=alive 0=dead). Alert if 0 for >10s.",
            ["module"],  # hotcore, slow, controlplane, dms, telegram, signer
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # Data staleness — event-time gap (seconds since last Geyser event)
        # MUST use event-time, not wall-clock at log-write (C-5)
        # ------------------------------------------------------------------ #
        self.data_staleness_seconds = Gauge(
            "aats_data_staleness_seconds",
            "Age of the most-recent Geyser/Yellowstone event (event-time gap). "
            "Alert threshold: >1.2s (infrastructure.md §4, FR-057).",
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # Snipe loop latency (ingress→execute, internal compute only, C-1)
        # ------------------------------------------------------------------ #
        self.snipe_decision_latency_seconds = Histogram(
            "aats_snipe_decision_latency_seconds",
            "Internal snipe-loop latency: ingress-detect to venue execute() call. "
            "Budget: p50<=50ms p99<=150ms (FR-051, NFR-001). "
            "Block-engine RTT is NOT included here (C-1 honesty split).",
            buckets=_SNIPE_LATENCY_BUCKETS,
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # FAST loop tick time
        # ------------------------------------------------------------------ #
        self.fast_tick_seconds = Histogram(
            "aats_fast_tick_seconds",
            "FAST loop tick duration. Budget: p99<=100ms (FR-053, NFR-002). "
            "Alert on breach: fast_tick_budget_breach.",
            buckets=_FAST_TICK_BUCKETS,
            **kwargs,
        )

        self.fast_tick_budget_breach_total = Counter(
            "aats_fast_tick_budget_breach_total",
            "Count of FAST ticks that exceeded the 100ms budget.",
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # Model inference (SLOW loop only; NEVER on FAST/SNIPE path)
        # ------------------------------------------------------------------ #
        self.model_inference_seconds = Histogram(
            "aats_model_inference_seconds",
            "Model inference latency by model type (SLOW loop only). "
            "Budgets: onnx_classifier<=5ms, llm_veto<=200ms, tft<=500ms (NFR-003).",
            ["model"],  # onnx_classifier | tft_survivor | llm_veto
            buckets=_INFERENCE_BUCKETS,
            **kwargs,
        )

        self.llm_parse_fail_total = Counter(
            "aats_llm_parse_fail_total",
            "Count of LLM responses that failed schema validation / parse.",
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # Land rate and time-to-land
        # ------------------------------------------------------------------ #
        self.snipe_attempts_total = Counter(
            "aats_snipe_attempts_total",
            "Total snipe intents sent to the venue.",
            ["dry_run"],  # true | false
            **kwargs,
        )

        self.snipe_lands_total = Counter(
            "aats_snipe_lands_total",
            "Total snipe transactions that landed on-chain.",
            ["dry_run"],
            **kwargs,
        )

        self.land_rate = Gauge(
            "aats_land_rate",
            "Rolling land rate (lands/attempts). "
            "Alert if <0.35 sustained (NFR-005).",
            **kwargs,
        )

        self.time_to_land_seconds = Histogram(
            "aats_time_to_land_seconds",
            "Wall-clock time from intent-send to fill-ack (submission RTT). "
            "Separate from internal compute (C-1). Includes block-engine RTT.",
            buckets=(0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 2.0, 5.0),
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # Slot delay vs winner (the honest latency metric — C-1)
        # ------------------------------------------------------------------ #
        self.slot_delay_vs_winner = Histogram(
            "aats_slot_delay_vs_winner_slots",
            "Slots behind the LP-add event when our fill landed. "
            "Shows submission disadvantage honestly (C-1, latency-budget.md).",
            buckets=(0, 1, 2, 3, 5, 7, 10, 15, 20, 30, 50),
            **kwargs,
        )

        self.mean_slot_delay = Gauge(
            "aats_mean_slot_delay_slots",
            "Rolling mean slot delay behind LP-add.",
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # Order reject rate
        # ------------------------------------------------------------------ #
        self.order_reject_total = Counter(
            "aats_order_reject_total",
            "Snipe orders rejected before submission, by reason.",
            ["reason"],  # gate | cost_gate | model_threshold | fsm_conflict | staleness
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # PnL — NET (primary); gross is secondary and explicitly labeled
        # NO win_rate label or metric (HONESTY CLAUSE)
        # ------------------------------------------------------------------ #
        self.net_pnl_lamports = Gauge(
            "aats_net_pnl_lamports",
            "Cumulative net PnL in lamports (gross minus tips, priority, slippage). "
            "This is the PRIMARY PnL metric. Always net — never gross-only. "
            "dashboard /api/metrics net_pnl_sol derives from this.",
            **kwargs,
        )

        self.gross_pnl_lamports = Gauge(
            "aats_gross_pnl_lamports",
            "Cumulative gross PnL in lamports (before cost deduction). "
            "Secondary. Labeled explicitly gross to prevent confusion.",
            **kwargs,
        )

        self.daily_pnl_lamports = Gauge(
            "aats_daily_pnl_lamports",
            "Today's net PnL in lamports. Circuit-breaker watches this.",
            **kwargs,
        )

        self.daily_loss_limit_lamports = Gauge(
            "aats_daily_loss_limit_lamports",
            "Daily loss limit in lamports (negative floor). "
            "Trip circuit breaker when daily_pnl <= this.",
            **kwargs,
        )

        self.drawdown_pct = Gauge(
            "aats_drawdown_pct",
            "Current drawdown as a percentage of peak equity.",
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # Costs breakdown
        # ------------------------------------------------------------------ #
        self.tips_lamports_total = Counter(
            "aats_tips_lamports_total",
            "Cumulative Jito tips paid in lamports.",
            **kwargs,
        )

        self.priority_fee_lamports_total = Counter(
            "aats_priority_fee_lamports_total",
            "Cumulative priority-fee lamports paid.",
            **kwargs,
        )

        self.tip_efficiency = Gauge(
            "aats_tip_efficiency",
            "tips_sol / gross_pnl_sol. >1.0 means subsidizing validators.",
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # Model vs naive baseline (GATE-B headline — AC-037)
        # ------------------------------------------------------------------ #
        self.model_vs_baseline_delta = Gauge(
            "aats_model_vs_baseline_delta_net_pnl_per_sol",
            "GATE-B: model net PnL per SOL at risk minus naive-baseline. "
            "Positive = model adds value over naive momentum. "
            "This is the panel that distinguishes real edge from tip-subsidizing.",
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # Position drift (OMS vs on-chain truth)
        # ------------------------------------------------------------------ #
        self.position_drift_count = Gauge(
            "aats_position_drift_count",
            "Number of positions where OMS state diverges from on-chain truth. "
            "Non-zero triggers reconciliation alert.",
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # Rug avoidance rate
        # ------------------------------------------------------------------ #
        self.rug_avoid_rate = Gauge(
            "aats_rug_avoid_rate",
            "Fraction of gate-catchable rugs that were avoided by the safety gate.",
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # Circuit breaker state
        # ------------------------------------------------------------------ #
        self.circuit_breaker_tripped = Gauge(
            "aats_circuit_breaker_tripped",
            "1 if the daily-loss circuit breaker is tripped; 0 if armed. "
            "Alert immediately on trip (AC-053).",
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # Dead-man's switch heartbeat age
        # ------------------------------------------------------------------ #
        self.dms_heartbeat_age_seconds = Gauge(
            "aats_dms_heartbeat_age_seconds",
            "Seconds since the last FAST-loop heartbeat seen by the DMS watchdog. "
            "Alert if >30s; DMS fires flatten at T_DMS=60s (OQ-006).",
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # SWQOS / staked-connection metrics (latency-devops RPC benchmark)
        # ------------------------------------------------------------------ #
        self.rpc_get_latest_blockhash_seconds = Histogram(
            "aats_rpc_get_latest_blockhash_seconds",
            "Latency of getLatestBlockhash RPC call.",
            ["endpoint"],  # primary | secondary | swqos
            buckets=(0.005, 0.010, 0.020, 0.050, 0.100, 0.200, 0.500, 1.0),
            **kwargs,
        )

        # ------------------------------------------------------------------ #
        # Build info (pinned at startup for traceability)
        # ------------------------------------------------------------------ #
        self.build_info = Info(
            "aats_build",
            "Build metadata (version, git SHA, DRY_RUN flag).",
            **kwargs,
        )

    # ---------------------------------------------------------------------- #
    # Convenience updaters (called by each service)
    # ---------------------------------------------------------------------- #

    def mark_alive(self, module: str) -> None:
        """Set heartbeat=1 for the named module."""
        self.heartbeat.labels(module=module).set(1)

    def mark_dead(self, module: str) -> None:
        """Set heartbeat=0 for the named module."""
        self.heartbeat.labels(module=module).set(0)

    def update_land_rate(self, attempts: int, lands: int) -> None:
        """Recompute the rolling land-rate gauge."""
        if attempts > 0:
            self.land_rate.set(lands / attempts)

    def update_tip_efficiency(
        self, tips_sol: float, gross_pnl_sol: float
    ) -> None:
        """tips / gross; set to 9999 if gross <= 0 (bleeding)."""
        if gross_pnl_sol > 0:
            self.tip_efficiency.set(tips_sol / gross_pnl_sol)
        else:
            self.tip_efficiency.set(9999.0)


# ---------------------------------------------------------------------------
# Module-level singleton — get_metrics() is the access point
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_instance: Optional[AATSMetrics] = None


def get_metrics() -> AATSMetrics:
    """Return the process-level AATSMetrics singleton.

    First call creates it on the default Prometheus registry.
    Tests should use AATSMetrics(registry=CollectorRegistry()) for isolation.
    """
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = AATSMetrics()
    return _instance
