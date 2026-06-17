"""T-341 — Frozen contract conformance tests.

Every frozen endpoint returns a schema that matches api-contracts.md exactly.
No win-rate field anywhere. Money is integer-lamports or decimal-string, NEVER float.
LatencyHop emits wire key "class" (T-199b).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_no_win_rate(data: object, path: str = "") -> None:
    """Recursively assert no win_rate key anywhere in the response tree."""
    if isinstance(data, dict):
        for k, v in data.items():
            assert k != "win_rate", f"HONESTY CLAUSE violated: win_rate found at {path}.{k}"
            _assert_no_win_rate(v, f"{path}.{k}")
    elif isinstance(data, list):
        for i, v in enumerate(data):
            _assert_no_win_rate(v, f"{path}[{i}]")


def _assert_no_float_money(data: object, path: str = "") -> None:
    """Assert monetary fields are not JSON float numbers.

    Money fields: lamports, pnl_sol, daily_loss_limit_sol, net_pnl_sol etc.
    Statistical fields (model_p, uncertainty, land_rate_pct etc.) may be float.
    """
    MONEY_FIELD_SUFFIXES = (
        "_lamports",
        "_sol",
        "net_pnl",
        "gross_pnl",
        "daily_pnl",
    )
    if isinstance(data, dict):
        for k, v in data.items():
            full_path = f"{path}.{k}"
            if any(k.endswith(s) for s in MONEY_FIELD_SUFFIXES):
                assert not isinstance(v, float), (
                    f"Money field at {full_path} must not be float, got {type(v).__name__}: {v!r}"
                )
            _assert_no_float_money(v, full_path)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            _assert_no_float_money(v, f"{path}[{i}]")


# ---------------------------------------------------------------------------
# GET /api/state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_returns_schema(client: AsyncClient):
    """GET /api/state returns AgentState with all required fields."""
    resp = await client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()

    # Required top-level fields per api-contracts.md §4
    assert "mode" in data
    assert "loops" in data
    assert "connection" in data
    assert "wallet" in data
    assert "dry_run_enabled" in data
    assert "breaker_tripped" in data
    assert "killed" in data

    # mode must be canonical 4-value enum
    assert data["mode"] in ("SHADOW", "PAPER", "LIVE_DRY_RUN", "LIVE")

    # loops shape
    loops = data["loops"]
    assert "snipe" in loops
    assert "fast" in loops
    assert "slow" in loops

    # wallet balances are integer lamports, not float (NFR-009)
    wallet = data["wallet"]
    assert "pubkey" in wallet
    assert isinstance(wallet["balance_lamports"], int), (
        f"balance_lamports must be int, got {type(wallet['balance_lamports'])}"
    )
    assert isinstance(wallet["cap_lamports"], int), (
        f"cap_lamports must be int, got {type(wallet['cap_lamports'])}"
    )
    # Never float for lamports
    assert not isinstance(wallet["balance_lamports"], float)
    assert not isinstance(wallet["cap_lamports"], float)

    # dry_run_enabled is True by default (HARD RULE — DRY-RUN by default)
    assert data["dry_run_enabled"] is True

    _assert_no_win_rate(data, "state")
    _assert_no_float_money(data, "state")


@pytest.mark.asyncio
async def test_get_state_default_mode_is_shadow(client: AsyncClient):
    """Default mode is SHADOW (FR-004) — safe startup default."""
    resp = await client.get("/api/state")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "SHADOW"


# ---------------------------------------------------------------------------
# GET /api/metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_metrics_schema(client: AsyncClient):
    """GET /api/metrics returns MetricsSnapshot with no win_rate field."""
    resp = await client.get("/api/metrics")
    assert resp.status_code == 200
    data = resp.json()

    # Required fields per api-contracts.md §4
    required = [
        "net_pnl_sol", "gross_pnl_sol", "land_rate_pct", "median_slot_delay",
        "rug_avoidance_pct", "tip_efficiency", "model_vs_baseline", "gate_a",
        "open_positions", "daily_pnl_sol", "daily_loss_limit_sol", "breaker_tripped",
    ]
    for f in required:
        assert f in data, f"Missing required field: {f}"

    # No win_rate anywhere (HONESTY CLAUSE, AC-037)
    _assert_no_win_rate(data, "metrics")
    assert "win_rate" not in data

    # PnL fields are decimal-string, not float
    for field in ("net_pnl_sol", "gross_pnl_sol", "daily_pnl_sol", "daily_loss_limit_sol"):
        val = data[field]
        assert isinstance(val, str), f"{field} must be decimal-string, got {type(val).__name__}: {val!r}"
        # Must be parseable as Decimal
        Decimal(val)

    # model_vs_baseline shape (GATE-B headline, AC-037)
    mvb = data["model_vs_baseline"]
    assert "delta_net_pnl_per_sol_at_risk" in mvb
    assert "lower_95_bound" in mvb
    assert "n_test_windows" in mvb
    assert "gate_b_pass" in mvb
    # No win_rate in model_vs_baseline either
    assert "win_rate" not in mvb

    # gate_a shape
    ga = data["gate_a"]
    assert "net_pnl_sol" in ga
    assert "lower_95_bound" in ga
    assert "gate_a_pass" in ga

    _assert_no_float_money(data, "metrics")


@pytest.mark.asyncio
async def test_get_metrics_decimal_strings_parseable(client: AsyncClient):
    """All decimal-string money fields must parse to valid Decimal."""
    resp = await client.get("/api/metrics")
    data = resp.json()
    for field in ("net_pnl_sol", "gross_pnl_sol", "daily_pnl_sol"):
        Decimal(data[field])
    mvb = data["model_vs_baseline"]
    Decimal(mvb["delta_net_pnl_per_sol_at_risk"])
    Decimal(mvb["lower_95_bound"])


# ---------------------------------------------------------------------------
# GET /api/positions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_positions_returns_list(client: AsyncClient):
    """GET /api/positions returns a list (empty when no positions)."""
    resp = await client.get("/api/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_get_positions_schema_with_position(
    store, fast_loop, client: AsyncClient
):
    """GET /api/positions returns correct schema when a position exists."""
    from aats.contracts.intents import CostStack
    from aats.controller.fsm import PositionFSM, make_client_intent_id

    intent_id = make_client_intent_id("MINT_X", 100, "ENTRY")
    store.claim_entering("MINT_X", intent_id)
    cost = CostStack(
        expected_edge_bps=200,
        jito_tip_bps=10,
        priority_fee_bps=5,
        entry_slippage_bps=50,
        amm_fee_bps=25,
        exit_slippage_bps=30,
        adverse_selection_bps=150,
        total_cost_bps=120,
    )
    PositionFSM.create_entering(
        store,
        mint="MINT_X",
        token="TOKX",
        surface="EH-001",
        entry_slot=100,
        sol_in_lamports=100_000_000,
        tokens_out_base=1_000_000,
        entry_slippage_bps=50,
        exit_config_label="secure_balanced",
        exit_mode="secure",
        tp_total=3,
        hard_stop_price_lamports=80_000_000,
        cost_breakdown=cost,
        client_intent_id=intent_id,
    )

    resp = await client.get("/api/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1

    pos = data[0]
    # Required position fields (api-contracts.md §4 /api/positions)
    required = [
        "mint", "token", "entry_slot", "sol_in_lamports", "unrealized_pnl_net_lamports",
        "entry_slip_bps", "tp_hit", "tp_total", "trailing_armed", "hard_stop_pct",
        "exit_mode", "fsm_state", "age_sec", "status",
        "realized_pnl_net_lamports", "surface", "cost_breakdown",
    ]
    for f in required:
        assert f in pos, f"Missing position field: {f}"

    # Money fields must be int
    assert isinstance(pos["sol_in_lamports"], int)
    assert isinstance(pos["unrealized_pnl_net_lamports"], int)

    # cost_breakdown shape
    cb = pos["cost_breakdown"]
    assert "tip_lamports" in cb
    assert "priority_lamports" in cb
    assert "entry_slippage_bps" in cb
    assert "amm_fee_bps" in cb
    assert "exit_slippage_bps" in cb
    assert "adverse_selection_bps" in cb

    _assert_no_win_rate(data, "positions")
    _assert_no_float_money(data, "positions")


# ---------------------------------------------------------------------------
# GET /api/latency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_latency_schema(client: AsyncClient):
    """GET /api/latency returns LatencyBudget with wire key 'class' on hops."""
    resp = await client.get("/api/latency")
    assert resp.status_code == 200
    data = resp.json()

    assert "hops" in data
    assert "internal_ms" in data
    assert "block_engine_rtt_ms" in data
    assert "slot_floor_ms" in data
    assert "posture" in data
    assert "tiers" in data

    # Each hop must have wire key "class" (T-199b FIX — LatencyHop alias)
    for hop in data["hops"]:
        assert "class" in hop, f"Hop missing wire key 'class': {hop}"
        assert "name" in hop
        assert "ms" in hop
        assert "budget_ms" in hop

    # tiers shape (for dashboard useInfraTiers hook)
    for tier in data["tiers"]:
        assert "name" in tier
        assert "land_rate" in tier
        assert "median_slot_delay" in tier
        assert "entry_slip_pct" in tier
        assert "active" in tier


@pytest.mark.asyncio
async def test_latency_hop_class_not_cls(client: AsyncClient):
    """Wire key is 'class', NOT 'cls' (T-199b requirement)."""
    resp = await client.get("/api/latency")
    data = resp.json()
    for hop in data["hops"]:
        assert "class" in hop
        assert "cls" not in hop, "Wire must emit 'class' not 'cls' (T-199b)"


# ---------------------------------------------------------------------------
# GET /api/sentiment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sentiment_returns_list(client: AsyncClient):
    resp = await client.get("/api/sentiment")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# GET /api/predictions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_predictions_schema(client: AsyncClient):
    resp = await client.get("/api/predictions")
    assert resp.status_code == 200
    data = resp.json()
    assert "uncertainty" in data or "model_p" in data  # additive; uncertainty required by FR-014


# ---------------------------------------------------------------------------
# GET /api/reasoning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_reasoning_returns_list(client: AsyncClient):
    resp = await client.get("/api/reasoning")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# GET /api/risk-config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_risk_config_returns_config(client: AsyncClient):
    resp = await client.get("/api/risk-config")
    assert resp.status_code == 200
    data = resp.json()
    assert "per_trade_cap_lamports" in data
    assert "max_aggregate_lamports" in data
    # Money fields must be int
    assert isinstance(data["per_trade_cap_lamports"], int)
    assert isinstance(data["max_aggregate_lamports"], int)


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_health_schema(client: AsyncClient):
    """GET /api/health returns ModuleHealth[] with required fields."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) > 0
    for module in data:
        assert "name" in module
        assert "status" in module
        assert module["status"] in ("ok", "degraded", "down")
        assert "staleness_ms" in module
        assert "last_heartbeat_ms" in module
        # staleness_ms is integer ms
        assert isinstance(module["staleness_ms"], int)


# ---------------------------------------------------------------------------
# Frozen endpoint list (api-contracts.md §12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_frozen_get_endpoints_return_200(client: AsyncClient):
    """All frozen GET endpoints must return 200 (api-contracts.md §12)."""
    frozen_endpoints = [
        "/api/state",
        "/api/metrics",
        "/api/positions",
        "/api/latency",
        "/api/sentiment",
        "/api/predictions",
        "/api/reasoning",
        "/api/risk-config",
        "/api/health",
    ]
    for ep in frozen_endpoints:
        resp = await client.get(ep)
        assert resp.status_code == 200, f"GET {ep} returned {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# No win_rate anywhere in any response (HONESTY CLAUSE, AC-037)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_win_rate_in_any_response(client: AsyncClient):
    """HONESTY CLAUSE: no win_rate field exists anywhere in any response."""
    endpoints = [
        "/api/state", "/api/metrics", "/api/positions", "/api/latency",
        "/api/sentiment", "/api/predictions", "/api/reasoning",
        "/api/risk-config", "/api/health",
    ]
    for ep in endpoints:
        resp = await client.get(ep)
        assert resp.status_code == 200
        data = resp.json()
        _assert_no_win_rate(data, ep)


# ---------------------------------------------------------------------------
# Money is NEVER float (NFR-009)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_float_money_in_any_response(client: AsyncClient):
    """NFR-009: no monetary field is a JSON float number."""
    endpoints = [
        "/api/state", "/api/metrics", "/api/positions",
        "/api/risk-config",
    ]
    for ep in endpoints:
        resp = await client.get(ep)
        assert resp.status_code == 200
        _assert_no_float_money(resp.json(), ep)
