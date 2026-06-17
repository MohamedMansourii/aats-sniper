"""T-341 — Dashboard api.ts ENDPOINTS reconciliation tests.

Verifies that every endpoint used in dashboard/src/lib/api.ts ENDPOINTS resolves
to a valid 200 (GET) or proper HTTP response (POST).

From api.ts:
  state:       /api/state
  feed:        /api/feed  (SSE)
  metrics:     /api/metrics
  positions:   /api/positions
  latency:     /api/latency
  sentiment:   /api/sentiment
  predictions: /api/predictions
  reasoning:   /api/reasoning
  riskConfig:  /api/risk-config
  health:      /api/health
  kill:        /api/kill
  flatten:     /api/flatten
  breakerReset:/api/breaker/reset
  mode:        /api/mode
  + flatten(mint) posts to /api/flatten/{mint}

All paths match exactly (api-contracts.md §13 reconciliation).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.control_plane.conftest import OPERATOR_HEADERS

# The exact ENDPOINTS from dashboard/src/lib/api.ts
DASHBOARD_GET_ENDPOINTS = {
    "state": "/api/state",
    "metrics": "/api/metrics",
    "positions": "/api/positions",
    "latency": "/api/latency",
    "sentiment": "/api/sentiment",
    "predictions": "/api/predictions",
    "reasoning": "/api/reasoning",
    "riskConfig": "/api/risk-config",
    "health": "/api/health",
}

DASHBOARD_POST_ENDPOINTS = {
    "kill": "/api/kill",
    "flatten": "/api/flatten",
    "breakerReset": "/api/breaker/reset",
    "mode": "/api/mode",
}


@pytest.mark.asyncio
async def test_all_dashboard_get_endpoints_resolve(client: AsyncClient):
    """Every api.ts ENDPOINTS GET path returns 200."""
    for key, path in DASHBOARD_GET_ENDPOINTS.items():
        resp = await client.get(path)
        assert resp.status_code == 200, (
            f"Dashboard endpoint '{key}' ({path}) returned {resp.status_code}: {resp.text}"
        )


@pytest.mark.asyncio
async def test_dashboard_post_kill_endpoint(client: AsyncClient):
    """api.ts killSwitch() -> POST /api/kill works with auth."""
    resp = await client.post(DASHBOARD_POST_ENDPOINTS["kill"], headers=OPERATOR_HEADERS)
    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_dashboard_post_flatten_endpoint(client: AsyncClient):
    """api.ts flattenAll() -> POST /api/flatten works with auth."""
    resp = await client.post(DASHBOARD_POST_ENDPOINTS["flatten"], headers=OPERATOR_HEADERS)
    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_dashboard_post_flatten_mint_endpoint(client: AsyncClient, store):
    """api.ts flatten(mint) -> POST /api/flatten/{mint} path pattern works.

    dashboard/src/lib/api.ts line 537: `${ENDPOINTS.flatten}/${encodeURIComponent(mint)}`
    = /api/flatten/MINT_XYZ
    """
    from aats.contracts.intents import CostStack
    from aats.controller.fsm import PositionFSM, make_client_intent_id

    mint = "DASH_MINT_001"
    intent_id = make_client_intent_id(mint, 500, "ENTRY")
    store.claim_entering(mint, intent_id)
    cost = CostStack(
        expected_edge_bps=200, jito_tip_bps=10, priority_fee_bps=5,
        entry_slippage_bps=50, amm_fee_bps=25, exit_slippage_bps=30,
        adverse_selection_bps=150, total_cost_bps=120,
    )
    PositionFSM.create_entering(
        store, mint=mint, token="DASH_TOK", surface="EH-001",
        entry_slot=500, sol_in_lamports=100_000_000, tokens_out_base=1_000_000,
        entry_slippage_bps=50, exit_config_label="secure_balanced",
        exit_mode="secure", tp_total=3, hard_stop_price_lamports=80_000_000,
        cost_breakdown=cost, client_intent_id=intent_id,
    )

    # This is exactly what api.ts does: `${ENDPOINTS.flatten}/${encodeURIComponent(mint)}`
    resp = await client.post(f"/api/flatten/{mint}", headers=OPERATOR_HEADERS)
    assert resp.status_code == 202
    data = resp.json()
    assert data["mint"] == mint


@pytest.mark.asyncio
async def test_dashboard_post_breaker_reset_endpoint_409_when_armed(client: AsyncClient):
    """api.ts resetBreaker() -> POST /api/breaker/reset returns 409 when ARMED (not TRIPPED)."""
    resp = await client.post(DASHBOARD_POST_ENDPOINTS["breakerReset"], headers=OPERATOR_HEADERS)
    assert resp.status_code == 409  # Breaker not tripped


@pytest.mark.asyncio
async def test_dashboard_post_mode_endpoint(client: AsyncClient):
    """api.ts setMode(m) -> POST /api/mode with {mode: m} body works."""
    resp = await client.post(
        DASHBOARD_POST_ENDPOINTS["mode"],
        json={"mode": "PAPER"},
        headers=OPERATOR_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "PAPER"


@pytest.mark.asyncio
async def test_dashboard_mode_enum_4_value(client: AsyncClient):
    """AgentMode enum has exactly 4 values: SHADOW, PAPER, LIVE_DRY_RUN, LIVE."""
    from aats.contracts.api_schemas import AgentMode
    valid_modes = {"SHADOW", "PAPER", "LIVE_DRY_RUN", "LIVE"}
    actual_modes = {m.value for m in AgentMode}
    assert actual_modes == valid_modes, (
        f"AgentMode must be {valid_modes}, got {actual_modes} (api-contracts.md §2)"
    )


@pytest.mark.asyncio
async def test_api_state_mode_is_canonical_enum(client: AsyncClient):
    """GET /api/state returns mode from the canonical 4-value enum."""
    resp = await client.get("/api/state")
    data = resp.json()
    assert data["mode"] in {"SHADOW", "PAPER", "LIVE_DRY_RUN", "LIVE"}


@pytest.mark.asyncio
async def test_risk_config_save_and_retrieve(client: AsyncClient):
    """POST /api/risk-config (tighten) -> GET /api/risk-config shows updated value."""
    # Get current
    get_resp = await client.get("/api/risk-config")
    current = get_resp.json()

    # Tighten the per_trade_cap
    tightened_cap = current["per_trade_cap_lamports"] // 2
    proposed = dict(current)
    proposed["per_trade_cap_lamports"] = tightened_cap

    post_resp = await client.post("/api/risk-config", json=proposed, headers=OPERATOR_HEADERS)
    assert post_resp.status_code == 200

    # Retrieve and verify
    get_resp2 = await client.get("/api/risk-config")
    updated = get_resp2.json()
    assert updated["per_trade_cap_lamports"] == tightened_cap
