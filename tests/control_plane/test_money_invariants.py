"""T-341 — Money invariants: NEVER float for monetary fields.

Tests that every monetary wire field is:
  - Integer lamports (int), or
  - Decimal-as-string (str parseable as Decimal)
  - NEVER a JSON number (float) for money.

References:
  - api-contracts.md §1 principle 2: "Money is integer base units / Decimal-as-string, NEVER float"
  - NFR-009, FR-042
  - data-models.md §0 money rule
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from httpx import AsyncClient

from aats.contracts.api_schemas import (
    CostBreakdown,
    FlattenResponse,
    KillResponse,
    LatencyHop,
    MetricsSnapshot,
    ModelVsBaseline,
    PositionRecord,
    WalletState,
)
from tests.control_plane.conftest import OPERATOR_HEADERS

# ---------------------------------------------------------------------------
# Schema-level money validation (Pydantic model tests)
# ---------------------------------------------------------------------------


def test_wallet_state_rejects_float_balance():
    """WalletState rejects float balance_lamports (must be int)."""
    import pytest
    with pytest.raises((ValueError, TypeError)):
        WalletState(
            pubkey="pk",
            balance_lamports=0.5,  # float: must reject
            cap_lamports=500_000_000,
        )


def test_wallet_state_rejects_float_cap():
    """WalletState rejects float cap_lamports (must be int)."""
    with pytest.raises((ValueError, TypeError)):
        WalletState(
            pubkey="pk",
            balance_lamports=0,
            cap_lamports=0.5,  # float: must reject
        )


def test_wallet_state_accepts_int():
    """WalletState accepts integer lamports."""
    ws = WalletState(pubkey="pk", balance_lamports=0, cap_lamports=500_000_000)
    assert isinstance(ws.balance_lamports, int)
    assert isinstance(ws.cap_lamports, int)


def test_cost_breakdown_rejects_float_lamports():
    """CostBreakdown rejects float tip_lamports and priority_lamports."""
    with pytest.raises((ValueError, TypeError)):
        CostBreakdown(
            tip_lamports=0.5,  # float: must reject
            priority_lamports=100,
            entry_slippage_bps=50,
            amm_fee_bps=25,
            exit_slippage_bps=30,
            adverse_selection_bps=150,
        )


def test_position_record_rejects_float_sol():
    """PositionRecord rejects float sol_in_lamports."""
    with pytest.raises((ValueError, TypeError)):
        PositionRecord(
            mint="MINT",
            token="TOK",
            entry_slot=100,
            sol_in_lamports=0.1,  # float: must reject
            unrealized_pnl_net_lamports=0,
            entry_slip_bps=50,
            tp_hit=0,
            tp_total=3,
            trailing_armed=False,
            hard_stop_pct=-40.0,
            exit_mode="secure",
            fsm_state="ENTERING",
            age_sec=0,
            status="open",
            realized_pnl_net_lamports=None,
            surface="EH-001",
            cost_breakdown=CostBreakdown(
                tip_lamports=100,
                priority_lamports=50,
                entry_slippage_bps=50,
                amm_fee_bps=25,
                exit_slippage_bps=30,
                adverse_selection_bps=150,
            ),
        )


def test_metrics_snapshot_pnl_fields_are_strings():
    """MetricsSnapshot PnL fields are str (decimal-string), not float."""
    snap = MetricsSnapshot(
        net_pnl_sol="-0.0123",
        gross_pnl_sol="0.0044",
        land_rate_pct=38.2,
        median_slot_delay=7,
        rug_avoidance_pct=61.0,
        tip_efficiency=0.42,
        model_vs_baseline=ModelVsBaseline(
            delta_net_pnl_per_sol_at_risk="0.013",
            lower_95_bound="0.004",
            n_test_windows=5,
            gate_b_pass=True,
        ),
        gate_a={
            "net_pnl_sol": "0.021",
            "lower_95_bound": "0.006",
            "gate_a_pass": True,
        },
        open_positions=2,
        daily_pnl_sol="-0.0123",
        daily_loss_limit_sol="-0.30",
        breaker_tripped=False,
    )

    # All PnL fields must be strings
    assert isinstance(snap.net_pnl_sol, str)
    assert isinstance(snap.gross_pnl_sol, str)
    assert isinstance(snap.daily_pnl_sol, str)
    assert isinstance(snap.daily_loss_limit_sol, str)

    # Must parse as Decimal
    Decimal(snap.net_pnl_sol)
    Decimal(snap.gross_pnl_sol)
    Decimal(snap.daily_pnl_sol)
    Decimal(snap.daily_loss_limit_sol)


def test_latency_hop_wire_key_is_class():
    """LatencyHop emits 'class' as the wire key (T-199b FIX)."""
    hop = LatencyHop(
        name="ingress_detect",
        ms=60,
        budget_ms=70,
        **{"class": "internal_compute"},  # use dict to pass reserved keyword
    )
    wire = json.loads(hop.model_dump_json(by_alias=True))
    assert "class" in wire, "Wire key must be 'class' (T-199b FIX)"
    assert "cls" not in wire, "Python field name 'cls' must not appear on the wire"
    assert wire["class"] == "internal_compute"


def test_latency_hop_cls_python_alias():
    """LatencyHop can be constructed with 'cls=' in Python (populate_by_name=True)."""
    hop = LatencyHop(name="hop", ms=10, budget_ms=20, cls="submission")
    assert hop.cls == "submission"
    wire = json.loads(hop.model_dump_json(by_alias=True))
    assert wire["class"] == "submission"


# ---------------------------------------------------------------------------
# HTTP endpoint money assertions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_state_wallet_balances_are_int(client: AsyncClient):
    """GET /api/state wallet balances are int lamports, never float."""
    resp = await client.get("/api/state")
    wallet = resp.json()["wallet"]
    assert isinstance(wallet["balance_lamports"], int)
    assert isinstance(wallet["cap_lamports"], int)
    # Explicitly not float
    assert not isinstance(wallet["balance_lamports"], float)
    assert not isinstance(wallet["cap_lamports"], float)


@pytest.mark.asyncio
async def test_api_metrics_pnl_are_decimal_strings(client: AsyncClient):
    """GET /api/metrics PnL fields are decimal-string, parseable as Decimal."""
    resp = await client.get("/api/metrics")
    data = resp.json()
    for field in ("net_pnl_sol", "gross_pnl_sol", "daily_pnl_sol"):
        val = data[field]
        assert isinstance(val, str), f"{field} must be str, got {type(val).__name__}"
        Decimal(val)  # must parse without error


@pytest.mark.asyncio
async def test_api_metrics_model_vs_baseline_decimal_strings(client: AsyncClient):
    """GET /api/metrics model_vs_baseline fields are decimal-string."""
    resp = await client.get("/api/metrics")
    mvb = resp.json()["model_vs_baseline"]
    for f in ("delta_net_pnl_per_sol_at_risk", "lower_95_bound"):
        val = mvb[f]
        assert isinstance(val, str), f"model_vs_baseline.{f} must be str, got {type(val).__name__}"
        Decimal(val)


@pytest.mark.asyncio
async def test_kill_response_schema(client: AsyncClient):
    """POST /api/kill response matches KillResponse schema."""
    resp = await client.post("/api/kill", headers=OPERATOR_HEADERS)
    assert resp.status_code == 202
    data = resp.json()
    kr = KillResponse(**data)
    assert kr.status == "halting"


@pytest.mark.asyncio
async def test_flatten_response_schema(client: AsyncClient):
    """POST /api/flatten response matches FlattenResponse schema."""
    resp = await client.post("/api/flatten", headers=OPERATOR_HEADERS)
    assert resp.status_code == 202
    data = resp.json()
    fr = FlattenResponse(**data)
    assert fr.status == "flattening"
    assert fr.mint is None


@pytest.mark.asyncio
async def test_no_json_number_for_lamport_fields(client: AsyncClient):
    """Verify raw JSON does not use JS number (float) for lamport fields.

    JSON float for lamports would be a violation of NFR-009.
    We parse raw JSON and check the Python types directly.
    """
    resp = await client.get("/api/state")
    raw_json = resp.text
    # Parse and check wallet.balance_lamports type from Python json module
    # (which preserves int vs float distinction)
    import json as _json
    data = _json.loads(raw_json)
    wallet = data["wallet"]
    # Python json.loads gives int for e.g. 0, and float for 0.0
    assert isinstance(wallet["balance_lamports"], int) and not isinstance(
        wallet["balance_lamports"], bool
    ), f"balance_lamports is {type(wallet['balance_lamports']).__name__}, must be int"
    assert isinstance(wallet["cap_lamports"], int) and not isinstance(
        wallet["cap_lamports"], bool
    ), f"cap_lamports is {type(wallet['cap_lamports']).__name__}, must be int"
