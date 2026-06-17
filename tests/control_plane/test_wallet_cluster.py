"""E11 — GET /api/wallet-cluster: wallet-cluster graph endpoint tests.

Verifies:
  1. GET /api/wallet-cluster returns 200 + typed list (WalletClusterGraph[]).
  2. Empty list when no wallet_cluster_provider wired (backward-compatible).
  3. Records from WalletClusterStore appear correctly shaped in the response.
  4. Read-only: no POST /api/wallet-cluster endpoint exists (no new control action).
  5. No win_rate field anywhere (HONESTY CLAUSE, AC-037).
  6. No money fields — supply shares are int bps, not lamports.
  7. manipulation_flags are documented values (de-risk only).
  8. Graph fields: nodes/edges correctly typed (WalletNode, WalletEdge).
  9. The endpoint does NOT affect any existing frozen endpoint.
 10. WalletClusterStore.snapshot() newest-first ordering.
 11. WalletClusterStore is bounded (maxlen evicts oldest entries).
 12. WalletClusterGraph Pydantic schema validates correctly.
 13. WalletNode rejects out-of-range supply_share_bps.
 14. WalletEdge rejects out-of-range weight.
 15. sniper_cluster_score out-of-range is rejected by WalletClusterGraph.
"""
from __future__ import annotations

import random
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from aats.control_plane.wallet_cluster_schemas import (
    WalletClusterGraph,
    WalletEdge,
    WalletNode,
)
from aats.controller.wallet_cluster_store import WalletClusterStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_no_win_rate(data: object, path: str = "") -> None:
    """Recursively assert no win_rate key in the response tree."""
    if isinstance(data, dict):
        for k, v in data.items():
            assert k != "win_rate", f"HONESTY CLAUSE violated: win_rate at {path}.{k}"
            _assert_no_win_rate(v, f"{path}.{k}")
    elif isinstance(data, list):
        for i, v in enumerate(data):
            _assert_no_win_rate(v, f"{path}[{i}]")


def _build_app_with_cluster_store(store: WalletClusterStore | None) -> Any:
    """Build a test app with the given WalletClusterStore injected."""
    from aats.contracts.risk import RiskConfig
    from aats.contracts.venue import SimulationVenue
    from aats.control_plane.server import ControlPlaneConfig, FeedBus, build_app
    from aats.controller.control_api import KillSwitch
    from aats.controller.fast_loop import FastLoop
    from aats.controller.state import InMemoryStateStore
    from aats.risk.circuit_breaker import CircuitBreaker, InMemoryBreakerStore
    from aats.risk.exit_engine import preset

    class FakeMarkPrice:
        def get_mark_price(self, mint: str, event_slot: int) -> Decimal | None:
            return Decimal("200")

    state_store = InMemoryStateStore()
    venue = SimulationVenue(rng=random.Random(42))
    kill_switch = KillSwitch()
    breaker_store = InMemoryBreakerStore()

    class _NullFlattener:
        def emergency_flatten_all(self, reason: str) -> None:
            pass

    breaker = CircuitBreaker(
        config=RiskConfig(),
        store=breaker_store,
        flatten_handler=_NullFlattener(),
    )
    feed_bus = FeedBus()
    exit_cfg = preset("secure_balanced")
    fast_loop = FastLoop(
        store=state_store,
        venue=venue,
        breaker=breaker,
        price_feed=FakeMarkPrice(),
        exit_config=exit_cfg,
    )
    cp_config = ControlPlaneConfig(
        operator_token="test-token",
        ceo_token="ceo-token",
        dry_run_enabled=True,
        agent_mode="SHADOW",
    )

    provider = store.snapshot if store is not None else None

    return build_app(
        state_store=state_store,
        fast_loop=fast_loop,
        kill_switch=kill_switch,
        breaker=breaker,
        feed_bus=feed_bus,
        config=cp_config,
        wallet_cluster_provider=provider,
    )


def _make_node(
    wallet: str = "wallet_A",
    node_type: str = "sniper",
    supply_share_bps: int = 500,
    buy_slot: int | None = 1001,
    manipulation_flags: list[str] | None = None,
) -> dict:
    return {
        "wallet": wallet,
        "node_type": node_type,
        "supply_share_bps": supply_share_bps,
        "buy_slot": buy_slot,
        "manipulation_flags": manipulation_flags or [],
    }


def _make_edge(
    source: str = "wallet_A",
    target: str = "wallet_B",
    edge_type: str = "same_bundle",
    weight: float = 0.9,
) -> dict:
    return {
        "source": source,
        "target": target,
        "edge_type": edge_type,
        "weight": weight,
    }


# ---------------------------------------------------------------------------
# 1. GET /api/wallet-cluster returns 200 + list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wallet_cluster_returns_200_and_list(client: AsyncClient):
    """GET /api/wallet-cluster returns 200 and a JSON list (may be empty)."""
    resp = await client.get("/api/wallet-cluster")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


# ---------------------------------------------------------------------------
# 2. Empty list when no store is wired (backward-compatible)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wallet_cluster_empty_when_no_provider(client: AsyncClient):
    """When wallet_cluster_provider is None (default), returns []."""
    # The default conftest app does NOT wire a wallet_cluster_provider.
    resp = await client.get("/api/wallet-cluster")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# 3. Records from WalletClusterStore appear in response with correct schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wallet_cluster_returns_records_from_store():
    """Records pushed into WalletClusterStore appear in GET /api/wallet-cluster."""
    store = WalletClusterStore(maxlen=50)

    store.record(
        mint="MINT_A",
        evaluated_at_slot=1000,
        sniper_cluster_score=0.75,
        bundling_detected=True,
        bundler_wallet_count=3,
        dev_bundle_cluster_bps=2000,
        nodes=[_make_node("wallet_creator", "creator", 1000, None)],
        edges=[_make_edge("wallet_creator", "wallet_sniper", "creator_funded", 1.0)],
        manipulation_flags=["bundling_detected", "high_cluster_share"],
    )
    store.record(
        mint="MINT_B",
        evaluated_at_slot=1010,
        sniper_cluster_score=0.1,
        bundling_detected=False,
        bundler_wallet_count=0,
        dev_bundle_cluster_bps=500,
        nodes=[],
        edges=[],
        manipulation_flags=[],
    )

    app = _build_app_with_cluster_store(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/wallet-cluster")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2

    # newest-first: MINT_B (slot=1010) should be first
    assert data[0]["mint"] == "MINT_B"
    assert data[1]["mint"] == "MINT_A"


# ---------------------------------------------------------------------------
# 4. Read-only: POST /api/wallet-cluster does not exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_post_wallet_cluster_endpoint(client: AsyncClient):
    """POST /api/wallet-cluster must not exist — read-only, no new control action."""
    resp = await client.post(
        "/api/wallet-cluster",
        headers={"Authorization": "Bearer test-operator-token"},
        json={"mint": "EVIL_MINT"},
    )
    # 405 Method Not Allowed or 404 Not Found — either acceptable; never 200/202.
    assert resp.status_code in (404, 405), (
        f"POST /api/wallet-cluster must not be a valid endpoint, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# 5. No win_rate anywhere (HONESTY CLAUSE, AC-037)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wallet_cluster_no_win_rate():
    """HONESTY CLAUSE: no win_rate field anywhere in GET /api/wallet-cluster response."""
    store = WalletClusterStore(maxlen=10)
    store.record(
        mint="MINT_A",
        evaluated_at_slot=100,
        sniper_cluster_score=0.5,
        bundling_detected=False,
        bundler_wallet_count=0,
        dev_bundle_cluster_bps=0,
    )

    app = _build_app_with_cluster_store(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/wallet-cluster")

    assert resp.status_code == 200
    _assert_no_win_rate(resp.json(), "/api/wallet-cluster")


# ---------------------------------------------------------------------------
# 6. No money fields — supply shares are int bps, not lamports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wallet_cluster_no_money_fields():
    """Cluster graph must not carry lamport fields — bps are ratios, not money."""
    store = WalletClusterStore(maxlen=10)
    store.record(
        mint="MINT_MONEY",
        evaluated_at_slot=200,
        sniper_cluster_score=0.3,
        bundling_detected=False,
        bundler_wallet_count=0,
        dev_bundle_cluster_bps=800,
        nodes=[_make_node("wlt", "sniper", supply_share_bps=800)],
    )

    app = _build_app_with_cluster_store(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/wallet-cluster")

    data = resp.json()
    assert len(data) == 1
    rec = data[0]

    # dev_bundle_cluster_bps is bps (int ≤ 10000), NOT lamports
    assert isinstance(rec["dev_bundle_cluster_bps"], int)
    assert rec["dev_bundle_cluster_bps"] <= 10_000

    # nodes carry supply_share_bps (int bps, not lamports)
    for node in rec.get("nodes", []):
        assert isinstance(node["supply_share_bps"], int)
        assert node["supply_share_bps"] <= 10_000

    # No field with "lamport" in the name anywhere in the response
    def _check_no_lamport(obj: object, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                assert "lamport" not in k.lower(), (
                    f"Unexpected lamport field at {path}.{k} — wallet-cluster is view-only bps"
                )
                _check_no_lamport(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _check_no_lamport(v, f"{path}[{i}]")

    _check_no_lamport(data, "/api/wallet-cluster")


# ---------------------------------------------------------------------------
# 7. manipulation_flags are documented values (de-risk only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wallet_cluster_manipulation_flags_are_documented():
    """manipulation_flags must contain only documented ManipulationFlag values."""
    VALID_FLAGS = {
        "bundling_detected",
        "high_cluster_share",
        "freeze_authority_set",
        "high_synchronicity",
    }

    store = WalletClusterStore(maxlen=10)
    store.record(
        mint="MINT_FLAGS",
        evaluated_at_slot=300,
        sniper_cluster_score=0.85,
        bundling_detected=True,
        bundler_wallet_count=5,
        dev_bundle_cluster_bps=3000,
        manipulation_flags=["bundling_detected", "high_cluster_share", "high_synchronicity"],
    )

    app = _build_app_with_cluster_store(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/wallet-cluster")

    data = resp.json()
    assert len(data) == 1
    for flag in data[0]["manipulation_flags"]:
        assert flag in VALID_FLAGS, f"Undocumented manipulation_flag: {flag!r}"


# ---------------------------------------------------------------------------
# 8. Graph fields: nodes/edges correctly typed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wallet_cluster_nodes_and_edges_shape():
    """nodes and edges carry the documented field shapes."""
    store = WalletClusterStore(maxlen=10)
    store.record(
        mint="MINT_GRAPH",
        evaluated_at_slot=400,
        sniper_cluster_score=0.6,
        bundling_detected=True,
        bundler_wallet_count=2,
        dev_bundle_cluster_bps=1500,
        nodes=[
            _make_node("wallet_creator", "creator", 5000, None, ["high_cluster_share"]),
            _make_node("wallet_sniper_1", "sniper", 1000, 401, ["high_synchronicity"]),
        ],
        edges=[
            _make_edge("wallet_creator", "wallet_sniper_1", "creator_funded", 1.0),
        ],
    )

    app = _build_app_with_cluster_store(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/wallet-cluster")

    data = resp.json()
    assert len(data) == 1
    rec = data[0]

    # Nodes
    nodes = rec["nodes"]
    assert len(nodes) == 2
    for node in nodes:
        assert "wallet" in node
        assert "node_type" in node
        assert isinstance(node["supply_share_bps"], int)
        assert node["node_type"] in ("creator", "bundler", "sniper", "holder", "unknown")

    # Edges
    edges = rec["edges"]
    assert len(edges) == 1
    for edge in edges:
        assert "source" in edge
        assert "target" in edge
        assert "edge_type" in edge
        assert isinstance(edge["weight"], float)
        assert 0.0 <= edge["weight"] <= 1.0
        assert edge["edge_type"] in (
            "same_bundle", "shared_funder", "co_timing", "creator_funded"
        )


# ---------------------------------------------------------------------------
# 9. Existing frozen endpoints unaffected by E11
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frozen_endpoints_unaffected_by_e11(client: AsyncClient):
    """Adding /api/wallet-cluster must not break any frozen §12 endpoints."""
    frozen = [
        "/api/state", "/api/metrics", "/api/positions", "/api/latency",
        "/api/sentiment", "/api/predictions", "/api/reasoning",
        "/api/risk-config", "/api/health",
        "/api/candidates",   # E3 additive
    ]
    for ep in frozen:
        resp = await client.get(ep)
        assert resp.status_code == 200, (
            f"Frozen endpoint {ep} broken after E11: status {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# 10. WalletClusterStore.snapshot() newest-first ordering
# ---------------------------------------------------------------------------


def test_wallet_cluster_store_newest_first():
    """WalletClusterStore.snapshot() newest-first (highest slot first)."""
    store = WalletClusterStore(maxlen=10)
    for i in range(5):
        store.record(
            mint=f"MINT_{i}",
            evaluated_at_slot=1000 + i,
            sniper_cluster_score=float(i) / 10,
            bundling_detected=False,
            bundler_wallet_count=0,
            dev_bundle_cluster_bps=0,
        )

    snap = store.snapshot(newest_first=True)
    assert len(snap) == 5
    # newest-first: slot 1004 should be first
    assert snap[0]["mint"] == "MINT_4"
    assert snap[-1]["mint"] == "MINT_0"

    snap_asc = store.snapshot(newest_first=False)
    assert snap_asc[0]["mint"] == "MINT_0"
    assert snap_asc[-1]["mint"] == "MINT_4"


# ---------------------------------------------------------------------------
# 11. WalletClusterStore is bounded (maxlen evicts oldest)
# ---------------------------------------------------------------------------


def test_wallet_cluster_store_bounded():
    """WalletClusterStore respects maxlen — oldest entries are evicted."""
    maxlen = 5
    store = WalletClusterStore(maxlen=maxlen)

    for i in range(10):
        store.record(
            mint=f"MINT_{i}",
            evaluated_at_slot=1000 + i,
            sniper_cluster_score=0.0,
            bundling_detected=False,
            bundler_wallet_count=0,
            dev_bundle_cluster_bps=0,
        )

    assert len(store) == maxlen
    snap = store.snapshot(newest_first=False)
    mints = [r["mint"] for r in snap]
    # Only the last 5 (MINT_5..MINT_9) should remain
    assert mints == ["MINT_5", "MINT_6", "MINT_7", "MINT_8", "MINT_9"]


# ---------------------------------------------------------------------------
# 12. WalletClusterGraph Pydantic schema validates correctly
# ---------------------------------------------------------------------------


def test_wallet_cluster_graph_schema_valid():
    """WalletClusterGraph Pydantic model validates a well-formed record."""
    node = WalletNode(
        wallet="creator_pk",
        node_type="creator",
        supply_share_bps=5000,
        buy_slot=None,
        manipulation_flags=["high_cluster_share"],
    )
    edge = WalletEdge(
        source="creator_pk",
        target="sniper_pk",
        edge_type="creator_funded",
        weight=0.95,
    )
    graph = WalletClusterGraph(
        mint="MINT_TEST",
        evaluated_at_slot=2000,
        sniper_cluster_score=0.7,
        bundling_detected=True,
        bundler_wallet_count=3,
        dev_bundle_cluster_bps=2500,
        nodes=[node],
        edges=[edge],
        manipulation_flags=["bundling_detected", "high_cluster_share"],
    )
    assert graph.mint == "MINT_TEST"
    assert graph.sniper_cluster_score == 0.7
    assert len(graph.nodes) == 1
    assert len(graph.edges) == 1
    assert graph.nodes[0].node_type == "creator"
    assert graph.edges[0].edge_type == "creator_funded"


# ---------------------------------------------------------------------------
# 13. WalletNode rejects out-of-range supply_share_bps
# ---------------------------------------------------------------------------


def test_wallet_node_rejects_out_of_range_bps():
    """WalletNode rejects supply_share_bps outside [0, 10000]."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        WalletNode(
            wallet="bad_wallet",
            node_type="sniper",
            supply_share_bps=15_000,  # > 10000 — invalid
            buy_slot=999,
        )


# ---------------------------------------------------------------------------
# 14. WalletEdge rejects out-of-range weight
# ---------------------------------------------------------------------------


def test_wallet_edge_rejects_out_of_range_weight():
    """WalletEdge rejects weight outside [0.0, 1.0]."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        WalletEdge(
            source="wallet_A",
            target="wallet_B",
            edge_type="same_bundle",
            weight=1.5,  # > 1.0 — invalid
        )


# ---------------------------------------------------------------------------
# 15. sniper_cluster_score out-of-range rejected by WalletClusterGraph
# ---------------------------------------------------------------------------


def test_wallet_cluster_graph_rejects_bad_score():
    """WalletClusterGraph rejects sniper_cluster_score outside [0.0, 1.0]."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        WalletClusterGraph(
            mint="MINT_BAD",
            evaluated_at_slot=100,
            sniper_cluster_score=1.5,  # > 1.0 — invalid
            bundling_detected=False,
            bundler_wallet_count=0,
            dev_bundle_cluster_bps=0,
        )
