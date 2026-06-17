"""E3 — GET /api/candidates: candidate/watchlist queue endpoint tests.

Verifies:
  1. GET /api/candidates returns 200 + typed list (CandidateRecord[]).
  2. Empty list when no candidates provider wired (backward-compatible).
  3. Records from the CandidateQueue appear correctly shaped in the response.
  4. Read-only: no POST /api/candidates endpoint exists (no new control action).
  5. No win_rate field anywhere (HONESTY CLAUSE, AC-037).
  6. model_p is float in [0,1] or null (not money; no int-lamports constraint).
  7. Status values are the documented set.
  8. The endpoint does NOT affect any existing frozen endpoint.
  9. CandidateQueue.snapshot() newest-first ordering.
 10. CandidateQueue is bounded (maxlen evicts oldest entries).
"""
from __future__ import annotations

import time

import pytest
from httpx import AsyncClient

from aats.control_plane.candidate_schemas import CandidateRecord, SafetyReport
from aats.controller.candidate_store import CandidateQueue

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


def _build_app_with_queue(queue: CandidateQueue | None):
    """Build a test app with the given candidate queue."""
    import random
    from decimal import Decimal

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

    store = InMemoryStateStore()
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
        store=store,
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

    provider = queue.snapshot if queue is not None else None

    return build_app(
        state_store=store,
        fast_loop=fast_loop,
        kill_switch=kill_switch,
        breaker=breaker,
        feed_bus=feed_bus,
        config=cp_config,
        candidates_provider=provider,
    )


# ---------------------------------------------------------------------------
# 1. GET /api/candidates returns 200 + list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidates_returns_200_and_list(client: AsyncClient):
    """GET /api/candidates returns 200 and a JSON list (may be empty)."""
    resp = await client.get("/api/candidates")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


# ---------------------------------------------------------------------------
# 2. Empty list when no queue is wired (backward-compatible)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidates_empty_when_no_provider(client: AsyncClient):
    """When candidates_provider is None (default), GET /api/candidates returns []."""
    # The default conftest app does NOT wire a candidates_provider.
    resp = await client.get("/api/candidates")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# 3. Records from CandidateQueue appear in response with correct schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidates_returns_records_from_queue():
    """Records pushed into CandidateQueue appear in GET /api/candidates."""
    from httpx import ASGITransport, AsyncClient

    queue = CandidateQueue(maxlen=50)
    wall_ms = time.monotonic_ns() // 1_000_000

    # Push one skipped and one sniped record.
    queue.record(
        mint="MINT_SKIP",
        evaluated_at_slot=1001,
        evaluated_at_wall_ms=wall_ms,
        status="skipped",
        model_p=0.25,
        reason="below_snipe_threshold",
        gate_passed=False,
        gate_reasons=["below_snipe_threshold"],
    )
    queue.record(
        mint="MINT_SNIPE",
        evaluated_at_slot=1002,
        evaluated_at_wall_ms=wall_ms + 10,
        status="sniped",
        model_p=0.72,
        reason="entered",
        gate_passed=True,
        gate_reasons=["passed"],
    )

    app = _build_app_with_queue(queue)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/candidates")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2

    # newest-first: MINT_SNIPE (slot=1002) should be first
    assert data[0]["mint"] == "MINT_SNIPE"
    assert data[1]["mint"] == "MINT_SKIP"


# ---------------------------------------------------------------------------
# 4. Read-only: POST /api/candidates does not exist (no new control action)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_post_candidates_endpoint(client: AsyncClient):
    """POST /api/candidates must not exist — read-only, no new control action."""
    resp = await client.post(
        "/api/candidates",
        headers={"Authorization": "Bearer test-operator-token"},
        json={"mint": "EVIL_MINT"},
    )
    # 405 Method Not Allowed or 404 Not Found — either is acceptable; not 200/202.
    assert resp.status_code in (404, 405), (
        f"POST /api/candidates must not be a valid endpoint, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# 5. No win_rate anywhere (HONESTY CLAUSE, AC-037)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidates_no_win_rate():
    """HONESTY CLAUSE: no win_rate field anywhere in GET /api/candidates response."""
    from httpx import ASGITransport, AsyncClient

    queue = CandidateQueue(maxlen=10)
    queue.record(
        mint="MINT_A",
        evaluated_at_slot=100,
        status="skipped",
        model_p=0.3,
        reason="no_signal",
        gate_passed=False,
    )

    app = _build_app_with_queue(queue)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/candidates")

    assert resp.status_code == 200
    _assert_no_win_rate(resp.json(), "/api/candidates")


# ---------------------------------------------------------------------------
# 6. model_p is float [0,1] or null — not a lamport value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidates_model_p_is_float_or_null():
    """model_p must be float in [0,1] or null — not an int/lamport value."""
    from httpx import ASGITransport, AsyncClient

    queue = CandidateQueue(maxlen=10)
    # Record one with model_p, one without
    queue.record(
        mint="WITH_P",
        evaluated_at_slot=101,
        status="skipped",
        model_p=0.42,
        reason="below_snipe_threshold",
        gate_passed=False,
    )
    queue.record(
        mint="NO_P",
        evaluated_at_slot=100,
        status="monitoring",
        model_p=None,
        reason="no_signal",
        gate_passed=False,
    )

    app = _build_app_with_queue(queue)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/candidates")

    data = resp.json()
    for rec in data:
        p = rec.get("model_p")
        if p is not None:
            assert isinstance(p, float), f"model_p must be float or null, got {type(p)}"
            assert 0.0 <= p <= 1.0, f"model_p must be in [0,1], got {p}"


# ---------------------------------------------------------------------------
# 7. Status values are the documented set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidates_status_values():
    """status must be one of: monitoring, pending, skipped, sniped."""
    from httpx import ASGITransport, AsyncClient

    VALID_STATUSES = {"monitoring", "pending", "skipped", "sniped"}

    queue = CandidateQueue(maxlen=10)
    for status in VALID_STATUSES:
        queue.record(
            mint=f"MINT_{status}",
            evaluated_at_slot=200,
            status=status,
            model_p=None,
            reason="test",
            gate_passed=False,
        )

    app = _build_app_with_queue(queue)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/candidates")

    data = resp.json()
    assert len(data) == 4
    for rec in data:
        assert rec["status"] in VALID_STATUSES, (
            f"Unexpected status: {rec['status']!r}"
        )


# ---------------------------------------------------------------------------
# 8. Existing frozen endpoints unaffected by E3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frozen_endpoints_unaffected(client: AsyncClient):
    """Adding /api/candidates must not break any of the §12 frozen endpoints."""
    frozen = [
        "/api/state", "/api/metrics", "/api/positions", "/api/latency",
        "/api/sentiment", "/api/predictions", "/api/reasoning",
        "/api/risk-config", "/api/health",
    ]
    for ep in frozen:
        resp = await client.get(ep)
        assert resp.status_code == 200, (
            f"Frozen endpoint {ep} broken after E3: status {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# 9. CandidateQueue.snapshot() newest-first ordering
# ---------------------------------------------------------------------------


def test_candidate_queue_newest_first():
    """CandidateQueue.snapshot() returns newest-first (highest slot first)."""
    q = CandidateQueue(maxlen=10)
    for i in range(5):
        q.record(
            mint=f"MINT_{i}",
            evaluated_at_slot=1000 + i,
            status="skipped",
            model_p=None,
            reason="no_signal",
            gate_passed=False,
        )

    snap = q.snapshot(newest_first=True)
    assert len(snap) == 5
    # newest-first: slot 1004 should be first
    assert snap[0]["mint"] == "MINT_4"
    assert snap[-1]["mint"] == "MINT_0"

    snap_asc = q.snapshot(newest_first=False)
    assert snap_asc[0]["mint"] == "MINT_0"
    assert snap_asc[-1]["mint"] == "MINT_4"


# ---------------------------------------------------------------------------
# 10. CandidateQueue is bounded (maxlen evicts oldest)
# ---------------------------------------------------------------------------


def test_candidate_queue_bounded():
    """CandidateQueue respects maxlen — oldest entries are evicted."""
    maxlen = 5
    q = CandidateQueue(maxlen=maxlen)

    for i in range(10):
        q.record(
            mint=f"MINT_{i}",
            evaluated_at_slot=1000 + i,
            status="skipped",
            model_p=None,
            reason="no_signal",
            gate_passed=False,
        )

    assert len(q) == maxlen
    snap = q.snapshot(newest_first=False)
    # Only the last 5 entries (MINT_5..MINT_9) should remain
    mints = [r["mint"] for r in snap]
    assert mints == ["MINT_5", "MINT_6", "MINT_7", "MINT_8", "MINT_9"]


# ---------------------------------------------------------------------------
# 11. CandidateRecord Pydantic schema validates correctly
# ---------------------------------------------------------------------------


def test_candidate_record_schema_valid():
    """CandidateRecord Pydantic model validates a well-formed record."""
    rec = CandidateRecord(
        mint="MINT_A",
        evaluated_at_slot=1000,
        evaluated_at_wall_ms=1_700_000_000_000,
        status="skipped",
        model_p=0.35,
        reason="below_snipe_threshold",
        safety_report=SafetyReport(gate_passed=False, reasons=["below_snipe_threshold"]),
    )
    assert rec.mint == "MINT_A"
    assert rec.model_p == 0.35
    assert rec.status == "skipped"
    assert rec.safety_report.gate_passed is False


def test_candidate_record_schema_model_p_none():
    """CandidateRecord accepts model_p=None (no signal staged)."""
    rec = CandidateRecord(
        mint="MINT_B",
        evaluated_at_slot=999,
        evaluated_at_wall_ms=1_700_000_000_001,
        status="monitoring",
        model_p=None,
        reason="no_signal",
        safety_report=SafetyReport(gate_passed=False, reasons=["no_signal"]),
    )
    assert rec.model_p is None


def test_candidate_record_schema_rejects_out_of_range_model_p():
    """CandidateRecord rejects model_p outside [0,1]."""
    import pytest as _pytest
    from pydantic import ValidationError
    with _pytest.raises(ValidationError):
        CandidateRecord(
            mint="MINT_C",
            evaluated_at_slot=100,
            evaluated_at_wall_ms=0,
            status="skipped",
            model_p=1.5,  # out of range
            reason="test",
            safety_report=SafetyReport(gate_passed=False),
        )


# ---------------------------------------------------------------------------
# 12. SnipeLoop records candidates to queue when candidate_queue is wired
# ---------------------------------------------------------------------------


def test_snipe_loop_records_candidate_on_skip():
    """SnipeLoop.process_event() records a candidate skip into the CandidateQueue."""
    import random

    from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
    from aats.contracts.risk import RiskConfig
    from aats.contracts.venue import SimulationVenue
    from aats.controller.snipe_loop import SnipeLoop
    from aats.controller.state import InMemoryStateStore

    store = InMemoryStateStore()
    venue = SimulationVenue(rng=random.Random(0))
    config = RiskConfig()
    queue = CandidateQueue(maxlen=50)

    snipe = SnipeLoop(
        store=store,
        venue=venue,
        config=config,
        latency_budget_ms=None,
        candidate_queue=queue,
    )

    event = LaunchEvent(
        mint="MINT_SKIP",
        venue_program_id="prog_id",
        source=LaunchSource.PUMPFUN,
        event_time=EventTime(slot=500, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_060),
        observation_slot=500,
        confirmation_slot=500,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=100_000_000,
        token_reserve_base=1_000_000_000_000,
        token_decimals=6,
        initial_holders=1,
        competitors=0,
        creator_wallet="creator",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=10,
    )

    # No signal staged → NO_SIGNAL skip
    result = snipe.process_event(event)
    assert result == "no_signal"

    # Queue should have one record
    snap = queue.snapshot()
    assert len(snap) == 1
    rec = snap[0]
    assert rec["mint"] == "MINT_SKIP"
    assert rec["status"] == "monitoring"
    assert rec["reason"] == "no_signal"
    assert rec["model_p"] is None
    assert rec["safety_report"]["gate_passed"] is False


def test_snipe_loop_records_sniped_candidate():
    """SnipeLoop.process_event() records a sniped candidate into the CandidateQueue."""
    import random

    from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
    from aats.contracts.models import DecisionSignal
    from aats.contracts.risk import RiskConfig
    from aats.contracts.venue import SimulationVenue
    from aats.controller.snipe_loop import SnipeLoop
    from aats.controller.state import InMemoryStateStore

    store = InMemoryStateStore()
    venue = SimulationVenue(rng=random.Random(1))
    config = RiskConfig(snipe_threshold=0.1)
    queue = CandidateQueue(maxlen=50)

    snipe = SnipeLoop(
        store=store,
        venue=venue,
        config=config,
        latency_budget_ms=None,
        candidate_queue=queue,
    )

    event = LaunchEvent(
        mint="MINT_ENTER",
        venue_program_id="prog_id",
        source=LaunchSource.PUMPFUN,
        event_time=EventTime(slot=600, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_060),
        observation_slot=600,
        confirmation_slot=600,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=100_000_000,
        token_reserve_base=1_000_000_000_000,
        token_decimals=6,
        initial_holders=1,
        competitors=0,
        creator_wallet="creator",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=10,
    )

    # Stage a signal above threshold
    store.set_decision_signal(
        DecisionSignal(
            mint="MINT_ENTER",
            event_time=EventTime(slot=600, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_060),
            model_version="v1",
            p_calibrated=0.75,
            uncertainty=0.05,
            baseline_p=0.3,
            surface="EH-001",
        ),
        ttl_seconds=60,
    )

    snipe.process_event(event)

    # Should have a record in the queue
    snap = queue.snapshot()
    assert len(snap) == 1
    rec = snap[0]
    assert rec["mint"] == "MINT_ENTER"
    assert rec["model_p"] == pytest.approx(0.75, abs=0.001)
    # status is either "sniped" (landed) or "skipped" (fill_not_landed) — both recorded
    assert rec["status"] in ("sniped", "skipped")
    # reason is "entered" or "fill_not_landed"
    assert rec["reason"] in ("entered", "fill_not_landed")


def test_snipe_loop_no_candidate_queue_is_noop():
    """SnipeLoop without a candidate_queue does not crash — backward-compatible."""
    import random

    from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
    from aats.contracts.risk import RiskConfig
    from aats.contracts.venue import SimulationVenue
    from aats.controller.snipe_loop import SnipeLoop
    from aats.controller.state import InMemoryStateStore

    store = InMemoryStateStore()
    venue = SimulationVenue(rng=random.Random(0))
    config = RiskConfig()

    # No candidate_queue passed (default None)
    snipe = SnipeLoop(store=store, venue=venue, config=config, latency_budget_ms=None)

    event = LaunchEvent(
        mint="MINT_NOOP",
        venue_program_id="prog_id",
        source=LaunchSource.PUMPFUN,
        event_time=EventTime(slot=700, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_060),
        observation_slot=700,
        confirmation_slot=700,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=100_000_000,
        token_reserve_base=1_000_000_000_000,
        token_decimals=6,
        initial_holders=1,
        competitors=0,
        creator_wallet="creator",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=10,
    )

    # Should not raise
    result = snipe.process_event(event)
    assert result == "no_signal"  # no signal staged → skip, but no crash
