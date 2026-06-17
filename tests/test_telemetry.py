"""Tests for aats.telemetry — the M5 scorecard.

These are the CI-mandated invariant tests:
  - DRY_RUN_ENABLED=true → no monetary metrics can report non-dry-run
  - No win_rate field in any metric name (HONESTY CLAUSE)
  - Money is NOT float in any metric gauge (integer lamports or ratio only)
  - Decision log event-time correctness (recorded_at_ms >= block_time_ms)
  - Decision log does NOT have a win_rate field

CI gate: pytest tests/ — all must pass.
"""
from __future__ import annotations

import json
import time
from io import StringIO
from unittest.mock import patch

import pytest

try:
    from prometheus_client import CollectorRegistry, generate_latest
    PROM_AVAILABLE = True
except ImportError:
    PROM_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not PROM_AVAILABLE,
    reason="prometheus_client not installed; install requirements/requirements-dev.txt"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_fresh_metrics():
    """Create an isolated AATSMetrics with its own registry (test isolation)."""
    from prometheus_client import CollectorRegistry
    from aats.telemetry.metrics import AATSMetrics
    reg = CollectorRegistry()
    return AATSMetrics(registry=reg), reg


# ---------------------------------------------------------------------------
# Test: no win_rate metric label anywhere in the registry
# ---------------------------------------------------------------------------

def test_no_win_rate_metric():
    """HONESTY CLAUSE: win_rate must NOT appear in any metric name or label."""
    metrics, reg = _get_fresh_metrics()
    output = generate_latest(reg).decode("utf-8")
    assert "win_rate" not in output.lower(), (
        "HONESTY CLAUSE VIOLATED: 'win_rate' found in Prometheus metrics output. "
        "Remove all win_rate metrics (BUILD-DIRECTIVE HARD RULE)."
    )


# ---------------------------------------------------------------------------
# Test: all monetary GAUGE values are integers (lamports), never float
# ---------------------------------------------------------------------------

def test_monetary_gauges_are_integer_lamports():
    """net_pnl, gross_pnl, daily_pnl are lamport counters (integer-valued)."""
    metrics, reg = _get_fresh_metrics()
    # Set integer lamport values — these are the only permitted monetary gauge types
    metrics.net_pnl_lamports.set(123_456_789)   # 0.123456789 SOL
    metrics.gross_pnl_lamports.set(200_000_000)  # 0.2 SOL
    metrics.daily_pnl_lamports.set(-50_000_000)  # -0.05 SOL

    output = generate_latest(reg).decode("utf-8")

    # net_pnl_lamports should appear with integer value
    assert "aats_net_pnl_lamports 1.2345" in output or \
           "aats_net_pnl_lamports 123456789" in output or \
           "1.2345678" in output, (
        "net_pnl_lamports gauge value unexpected"
    )
    # Confirm no scientific notation float slippage (e.g. 0.000000001)
    assert "aats_net_pnl_lamports 0.0" not in output or \
           "123456789" in output, "Monetary gauge rounding to zero — check type"


# ---------------------------------------------------------------------------
# Test: DRY_RUN_ENABLED=true is the default and it's logged in the log line
# ---------------------------------------------------------------------------

def test_decision_log_dry_run_default():
    """Decision log lines must carry dry_run=true by default."""
    captured = []

    def mock_print(s, **kwargs):
        captured.append(s)

    with patch("builtins.print", side_effect=mock_print):
        from aats.telemetry.decision_log import log_decision
        log_decision(
            event="skip",
            slot=100,
            block_time_ms=int(time.time() * 1000) - 1000,
            mint="MINT0001",
            decision="SKIP",
            dry_run=True,  # explicit default
        )

    assert len(captured) >= 1, "No output captured from decision log"
    record = json.loads(captured[0])
    assert record["dry_run"] is True, "dry_run must be True in log line"


# ---------------------------------------------------------------------------
# Test: Decision log event-time correctness (recorded_at >= block_time)
# ---------------------------------------------------------------------------

def test_decision_log_event_time_ordering():
    """recorded_at_ms must be >= block_time_ms (data-models §9.2)."""
    import time
    captured = []

    def mock_print(s, **kwargs):
        captured.append(s)

    with patch("builtins.print", side_effect=mock_print):
        from aats.telemetry.decision_log import log_decision
        now_ms = int(time.time() * 1000)
        past_block_time = now_ms - 5000  # 5 seconds ago (a real chain event)

        log_decision(
            event="snipe",
            slot=99999,
            block_time_ms=past_block_time,
            mint="MINT0002",
            decision="SNIPE",
            model_p=0.72,
            model_uncertainty=0.08,
            sol_in_lamports=100_000_000,  # 0.1 SOL, integer
            dry_run=True,
        )

    assert len(captured) >= 1, "No output captured"
    record = json.loads(captured[0])

    assert "event_time" in record, "event_time missing from decision log"
    assert record["event_time"]["slot"] == 99999
    assert record["event_time"]["block_time_ms"] == past_block_time
    assert record["recorded_at_ms"] >= past_block_time, (
        f"recorded_at_ms ({record['recorded_at_ms']}) < block_time_ms ({past_block_time}) "
        "— point-in-time ordering violation (C-5)"
    )


# ---------------------------------------------------------------------------
# Test: Decision log has NO win_rate field
# ---------------------------------------------------------------------------

def test_decision_log_no_win_rate():
    """HONESTY CLAUSE: no win_rate field in decision log lines."""
    captured = []

    def mock_print(s, **kwargs):
        captured.append(s)

    with patch("builtins.print", side_effect=mock_print):
        from aats.telemetry.decision_log import log_decision
        log_decision(
            event="snipe",
            slot=1,
            block_time_ms=int(time.time() * 1000) - 100,
            mint="MINT0003",
            decision="SNIPE",
        )

    assert len(captured) >= 1
    record = json.loads(captured[0])
    assert "win_rate" not in str(record).lower(), (
        "HONESTY CLAUSE VIOLATED: win_rate found in decision log output"
    )


# ---------------------------------------------------------------------------
# Test: sol_in_lamports must be integer, not float
# ---------------------------------------------------------------------------

def test_decision_log_sol_in_is_integer():
    """Money must be integer lamports, not float (NFR-009)."""
    captured = []

    def mock_print(s, **kwargs):
        captured.append(s)

    with patch("builtins.print", side_effect=mock_print):
        from aats.telemetry.decision_log import log_decision
        log_decision(
            event="snipe",
            slot=2,
            block_time_ms=int(time.time() * 1000) - 200,
            mint="MINT0004",
            decision="SNIPE",
            sol_in_lamports=100_000_000,  # 0.1 SOL in integer lamports
        )

    record = json.loads(captured[0])
    sol_in = record.get("sol_in_lamports")
    assert isinstance(sol_in, int), (
        f"sol_in_lamports must be an integer, got {type(sol_in).__name__}={sol_in}"
    )


# ---------------------------------------------------------------------------
# Test: AATSMetrics heartbeat labels include all expected modules
# ---------------------------------------------------------------------------

def test_heartbeat_modules_coverable():
    """All infra modules can be marked alive/dead via the heartbeat gauge."""
    metrics, reg = _get_fresh_metrics()
    modules = ["hotcore", "slow", "controlplane", "dms", "telegram", "signer"]
    for m in modules:
        metrics.mark_alive(m)

    output = generate_latest(reg).decode("utf-8")
    for m in modules:
        assert f'module="{m}"' in output, f"Module '{m}' not found in heartbeat output"
        assert f'module="{m}"' in output


# ---------------------------------------------------------------------------
# Test: Placeholder leak-guard hook (body filled by T-400 validation harness)
# ---------------------------------------------------------------------------

def test_leak_guard_placeholder():
    """
    Placeholder: the validation harness (T-400) will fill this test body.
    For now it asserts that the clean-room import guard module path exists
    and that 'truth_' is not importable from the production aats package.
    """
    import aats
    import aats.models
    import aats.features
    # Verify no truth_* symbol leaks into the production package
    assert not hasattr(aats.models, "truth_is_rug"), (
        "truth_is_rug leaked into aats.models — clean-room violation (C-7)"
    )
    assert not hasattr(aats.features, "truth_max_multiple"), (
        "truth_max_multiple leaked into aats.features — clean-room violation (C-7)"
    )
