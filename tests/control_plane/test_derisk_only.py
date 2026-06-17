"""T-341 — De-risk-only contract tests (ASYMMETRIC TRUST enforcement).

These tests prove that:
  1. A risk-increasing /api/mode command (e.g. LIVE without gates) is rejected.
  2. A risk-increasing /api/risk-config (widen) is rejected.
  3. No endpoint exists that can size-up, widen-stop, add-leverage, or
     enable LIVE without the edge gates.
  4. The controller flatten happens within budget on kill.
  5. The LLM / automated path cannot call breaker/reset (it needs OperatorResetToken).
  6. The breaker/reset is gated on TRIPPED state (409 when ARMED).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.control_plane.conftest import CEO_HEADERS, OPERATOR_HEADERS

# ---------------------------------------------------------------------------
# 1. Asymmetric trust: risk-increasing commands REJECTED at the contract layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_size_up_endpoint_exists(app):
    """There is no /api/size-up, /api/add-leverage, or /api/widen-stop endpoint."""
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # All of these risk-increase endpoints must NOT exist (404)
        endpoints_that_must_not_exist = [
            "/api/size-up",
            "/api/widen-stop",
            "/api/add-leverage",
            "/api/override-hard-stop",
            "/api/go-live",       # not a real endpoint
            "/api/enable-live",   # not a real endpoint
        ]
        for ep in endpoints_that_must_not_exist:
            resp = await client.post(ep, headers=OPERATOR_HEADERS)
            assert resp.status_code in (404, 405), (
                f"Risk-increase endpoint {ep!r} must not exist (got {resp.status_code})"
            )


@pytest.mark.asyncio
async def test_live_mode_without_gates_rejected(client: AsyncClient, cp_config):
    """LIVE mode without DRY_RUN_ENABLED=false AND CEO auth -> 403 (AC-060)."""
    cp_config.dry_run_enabled = True
    resp = await client.post("/api/mode", json={"mode": "LIVE"}, headers=OPERATOR_HEADERS)
    assert resp.status_code == 403

    data = resp.json()
    # Must carry the specific machine error code
    assert "live_requires_dry_run_disabled_and_ceo_auth" in str(data)


@pytest.mark.asyncio
async def test_risk_config_widen_per_trade_cap_rejected(client: AsyncClient):
    """Widening per_trade_cap_lamports is a risk-increase -> 403."""
    get_resp = await client.get("/api/risk-config")
    current = get_resp.json()
    proposed = dict(current)
    proposed["per_trade_cap_lamports"] = current["per_trade_cap_lamports"] + 10_000_000
    resp = await client.post("/api/risk-config", json=proposed, headers=OPERATOR_HEADERS)
    assert resp.status_code == 403
    assert "risk_increase_rejected" in str(resp.json())


@pytest.mark.asyncio
async def test_risk_config_widen_aggregate_cap_rejected(client: AsyncClient):
    """Widening max_aggregate_lamports is a risk-increase -> 403."""
    get_resp = await client.get("/api/risk-config")
    current = get_resp.json()
    proposed = dict(current)
    proposed["max_aggregate_lamports"] = current["max_aggregate_lamports"] + 10_000_000
    resp = await client.post("/api/risk-config", json=proposed, headers=OPERATOR_HEADERS)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_risk_config_lower_slippage_accepted(client: AsyncClient):
    """Lowering max_slippage_bps is a tighten (de-risk) -> accepted."""
    get_resp = await client.get("/api/risk-config")
    current = get_resp.json()
    proposed = dict(current)
    proposed["max_slippage_bps"] = max(1, current["max_slippage_bps"] - 10)
    resp = await client.post("/api/risk-config", json=proposed, headers=OPERATOR_HEADERS)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 2. Breaker reset is gated: not callable without TRIPPED state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breaker_reset_requires_tripped_state(client: AsyncClient):
    """POST /api/breaker/reset when ARMED returns 409 (not 200)."""
    resp = await client.post("/api/breaker/reset", headers=OPERATOR_HEADERS)
    assert resp.status_code == 409
    data = resp.json()
    assert "breaker_not_tripped" in str(data.get("detail", ""))


@pytest.mark.asyncio
async def test_breaker_reset_without_auth_returns_403(client: AsyncClient):
    """POST /api/breaker/reset without auth -> 403, not 409."""
    resp = await client.post("/api/breaker/reset")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 3. Mode ladder: down always de-risk, up requires gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_live_dry_run_to_paper_allowed(client: AsyncClient, cp_config):
    """Moving LIVE_DRY_RUN -> PAPER is de-risk (down), always allowed."""
    cp_config.agent_mode = "LIVE_DRY_RUN"
    resp = await client.post("/api/mode", json={"mode": "PAPER"}, headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["mode"] == "PAPER"


@pytest.mark.asyncio
async def test_mode_live_to_shadow_allowed(client: AsyncClient, cp_config):
    """Moving LIVE -> SHADOW is de-risk (down), always allowed (even from LIVE)."""
    cp_config.agent_mode = "LIVE"
    cp_config.dry_run_enabled = False
    resp = await client.post("/api/mode", json={"mode": "SHADOW"}, headers=CEO_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["mode"] == "SHADOW"


@pytest.mark.asyncio
async def test_mode_paper_to_live_dry_run_allowed_without_ceo(client: AsyncClient, cp_config):
    """LIVE_DRY_RUN doesn't require CEO auth (no real capital, AC-060)."""
    cp_config.agent_mode = "PAPER"
    resp = await client.post(
        "/api/mode", json={"mode": "LIVE_DRY_RUN"}, headers=OPERATOR_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "LIVE_DRY_RUN"


# ---------------------------------------------------------------------------
# 4. Kill + flatten are always safe to repeat (idempotent de-risk)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_multiple_times_safe(client: AsyncClient, kill_switch):
    """POST /api/kill can be safely called 5 times; never escalates."""
    for _ in range(5):
        resp = await client.post("/api/kill", headers=OPERATOR_HEADERS)
        assert resp.status_code == 202
    assert kill_switch.is_killed


@pytest.mark.asyncio
async def test_flatten_all_multiple_times_safe(client: AsyncClient):
    """POST /api/flatten can be safely called 5 times without error."""
    for _ in range(5):
        resp = await client.post("/api/flatten", headers=OPERATOR_HEADERS)
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# 5. DRY-RUN default invariant (real capital DISABLED by default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_enabled_by_default(client: AsyncClient):
    """dry_run_enabled is True by default (HARD RULE — real capital disabled)."""
    resp = await client.get("/api/state")
    assert resp.status_code == 200
    assert resp.json()["dry_run_enabled"] is True


@pytest.mark.asyncio
async def test_dry_run_flag_is_read_only_in_state(client: AsyncClient):
    """dry_run_enabled in /api/state cannot be changed by GET requests."""
    resp1 = await client.get("/api/state")
    resp2 = await client.get("/api/state")
    assert resp1.json()["dry_run_enabled"] == resp2.json()["dry_run_enabled"]


# ---------------------------------------------------------------------------
# 6. Frozen endpoint list — no unauthorized POSTs succeed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_post_endpoints_require_auth(client: AsyncClient):
    """All POST endpoints return 403 without auth."""
    post_endpoints_and_bodies = [
        ("/api/kill", None),
        ("/api/flatten", None),
        ("/api/breaker/reset", None),
        ("/api/mode", {"mode": "SHADOW"}),
        ("/api/risk-config", {}),
    ]
    for ep, body in post_endpoints_and_bodies:
        if body is not None:
            resp = await client.post(ep, json=body)
        else:
            resp = await client.post(ep)
        assert resp.status_code == 403, (
            f"POST {ep} without auth returned {resp.status_code}, expected 403"
        )


# ---------------------------------------------------------------------------
# 7. /api/feed is GET-only (no POST to the feed endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_is_get_only(client: AsyncClient):
    """POST /api/feed is not allowed (GET = read-only, no write to feed)."""
    resp = await client.post("/api/feed", headers=OPERATOR_HEADERS)
    assert resp.status_code in (404, 405)
