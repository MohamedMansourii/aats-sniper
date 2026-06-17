"""Shared fixtures for T-341 control-plane tests.

ALL external services (Redis, RPC, LLM, Telegram) are INJECTED offline fakes.
No network calls, no secrets, no real keys anywhere in this test suite.

The FeedBus is a deterministic in-process queue.
The StateStore is the InMemoryStateStore.
The CircuitBreaker uses InMemoryBreakerStore.
The ExecutionVenue is the SimulationVenue.
"""
from __future__ import annotations

import random
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.contracts.risk import RiskConfig
from aats.contracts.venue import SimulationVenue
from aats.control_plane.server import ControlPlaneConfig, FeedBus, build_app
from aats.controller.control_api import KillSwitch
from aats.controller.fast_loop import FastLoop
from aats.controller.state import InMemoryStateStore
from aats.risk.circuit_breaker import CircuitBreaker, InMemoryBreakerStore
from aats.risk.exit_engine import preset

# ---------------------------------------------------------------------------
# Fake price provider (deterministic)
# ---------------------------------------------------------------------------


class FakeMarkPriceProvider:
    def __init__(self, price: Decimal = Decimal("200")) -> None:
        self._prices: dict[str, Decimal] = {}
        self._default = price

    def get_mark_price(self, mint: str, event_slot: int) -> Decimal | None:
        return self._prices.get(mint, self._default)

    def set_price(self, mint: str, price: Decimal) -> None:
        self._prices[mint] = price


# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> InMemoryStateStore:
    return InMemoryStateStore()


@pytest.fixture
def venue() -> SimulationVenue:
    return SimulationVenue(rng=random.Random(42))


@pytest.fixture
def kill_switch() -> KillSwitch:
    return KillSwitch()


@pytest.fixture
def feed_bus() -> FeedBus:
    return FeedBus()


@pytest.fixture
def breaker_store() -> InMemoryBreakerStore:
    return InMemoryBreakerStore()


@pytest.fixture
def breaker(store: InMemoryStateStore, venue: SimulationVenue, breaker_store: InMemoryBreakerStore) -> CircuitBreaker:
    class _NullFlattener:
        def emergency_flatten_all(self, reason: str) -> None:
            pass

    return CircuitBreaker(
        config=RiskConfig(),
        store=breaker_store,
        flatten_handler=_NullFlattener(),
    )


@pytest.fixture
def price_feed() -> FakeMarkPriceProvider:
    return FakeMarkPriceProvider(price=Decimal("200"))


@pytest.fixture
def fast_loop(
    store: InMemoryStateStore,
    venue: SimulationVenue,
    breaker: CircuitBreaker,
    price_feed: FakeMarkPriceProvider,
) -> FastLoop:
    exit_cfg = preset("secure_balanced")
    return FastLoop(
        store=store,
        venue=venue,
        breaker=breaker,
        price_feed=price_feed,
        exit_config=exit_cfg,
    )


@pytest.fixture
def cp_config() -> ControlPlaneConfig:
    return ControlPlaneConfig(
        operator_token="test-operator-token",
        ceo_token="test-ceo-token",
        dry_run_enabled=True,  # DRY-RUN by default (HARD RULE)
        agent_mode="SHADOW",
        wallet_pubkey="test_pubkey",
        wallet_cap_lamports=500_000_000,
    )


@pytest.fixture
def app(
    store: InMemoryStateStore,
    fast_loop: FastLoop,
    kill_switch: KillSwitch,
    breaker: CircuitBreaker,
    feed_bus: FeedBus,
    cp_config: ControlPlaneConfig,
) -> Any:
    return build_app(
        state_store=store,
        fast_loop=fast_loop,
        kill_switch=kill_switch,
        breaker=breaker,
        feed_bus=feed_bus,
        config=cp_config,
    )


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# Auth headers for POST requests
OPERATOR_HEADERS = {"Authorization": "Bearer test-operator-token"}
CEO_HEADERS = {"Authorization": "Bearer test-operator-token", "X-CEO-Auth": "test-ceo-token"}


def make_launch_event(
    mint: str = "MINT_A",
    slot: int = 1000,
    block_time_ms: int = 1_700_000_000_000,
) -> LaunchEvent:
    return LaunchEvent(
        mint=mint,
        venue_program_id="pump_prog_id",
        source=LaunchSource.PUMPFUN,
        event_time=EventTime(
            slot=slot,
            block_time_ms=block_time_ms,
            wall_clock_ms=block_time_ms + 60,
        ),
        observation_slot=slot,
        confirmation_slot=slot,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=100_000_000,
        token_reserve_base=1_000_000_000_000,
        token_decimals=6,
        initial_holders=1,
        competitors=0,
        creator_wallet="creator_wallet_pk",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=10,
    )
