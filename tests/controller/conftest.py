"""Shared fixtures for T-340 controller tests.

All external services (Redis, RPC, LLM, Telegram) are replaced with deterministic
offline fakes.  No network calls, no secrets, no real keys anywhere.
"""
from __future__ import annotations

import random
from decimal import Decimal

import pytest

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.contracts.models import DecisionSignal
from aats.contracts.risk import RiskConfig
from aats.contracts.venue import SimulationVenue
from aats.controller.control_api import KillSwitch
from aats.controller.fast_loop import FastLoop
from aats.controller.loops import ControllerOrchestrator
from aats.controller.reconcile import Reconciler
from aats.controller.slow_loop import SlowLoop
from aats.controller.snipe_loop import SnipeLoop
from aats.controller.state import InMemoryStateStore
from aats.risk.circuit_breaker import CircuitBreaker, InMemoryBreakerStore
from aats.risk.exit_engine import preset

# -----------------------------------------------------------------------
# Fake clock (deterministic TTL without sleeping)
# -----------------------------------------------------------------------


class FakeClock:
    """Monotonic fake clock for deterministic TTL tests."""

    def __init__(self, start_ms: int = 0) -> None:
        self._ms = start_ms

    def __call__(self) -> int:
        return self._ms

    def advance(self, ms: int) -> None:
        self._ms += ms


# -----------------------------------------------------------------------
# Fake MarkPriceProvider
# -----------------------------------------------------------------------


class FakeMarkPriceProvider:
    """Returns a deterministic price per mint."""

    def __init__(self, price: Decimal = Decimal("100")) -> None:
        self._prices: dict[str, Decimal] = {}
        self._default = price

    def get_mark_price(self, mint: str, event_slot: int) -> Decimal | None:
        return self._prices.get(mint, self._default)

    def set_price(self, mint: str, price: Decimal) -> None:
        self._prices[mint] = price


# -----------------------------------------------------------------------
# Fake classifier (always produces a signal above threshold)
# -----------------------------------------------------------------------


class FakeClassifier:
    """Always produces a DecisionSignal above the snipe_threshold."""

    def __init__(
        self,
        p_calibrated: float = 0.80,
        uncertainty: float = 0.10,
        surface: str = "EH-001",
    ) -> None:
        self._p = p_calibrated
        self._u = uncertainty
        self._surface = surface

    def predict(self, event: LaunchEvent) -> DecisionSignal | None:
        return DecisionSignal(
            mint=event.mint,
            event_time=event.event_time,
            model_version="test-v1",
            p_calibrated=self._p,
            uncertainty=self._u,
            baseline_p=0.50,
            surface=self._surface,
        )


# -----------------------------------------------------------------------
# Canonical LaunchEvent factory
# -----------------------------------------------------------------------


def make_launch_event(
    mint: str = "MINT_A",
    slot: int = 1000,
    block_time_ms: int = 1_700_000_000_000,
    sol_reserve_lamports: int = 100_000_000,
    token_reserve_base: int = 1_000_000_000_000,
    competitors: int = 0,
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
        sol_reserve_lamports=sol_reserve_lamports,
        token_reserve_base=token_reserve_base,
        token_decimals=6,
        initial_holders=1,
        competitors=competitors,
        creator_wallet="creator_wallet_pk",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=10,
    )


# -----------------------------------------------------------------------
# Core fixtures
# -----------------------------------------------------------------------


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(start_ms=0)


@pytest.fixture
def store(fake_clock: FakeClock) -> InMemoryStateStore:
    return InMemoryStateStore(clock=fake_clock)


@pytest.fixture
def venue() -> SimulationVenue:
    return SimulationVenue(rng=random.Random(42))


@pytest.fixture
def config() -> RiskConfig:
    return RiskConfig()


@pytest.fixture
def classifier() -> FakeClassifier:
    return FakeClassifier()


@pytest.fixture
def price_feed() -> FakeMarkPriceProvider:
    # Default price > hard_stop_price so positions stay OPEN by default
    return FakeMarkPriceProvider(price=Decimal("200"))


@pytest.fixture
def exit_cfg():
    return preset("secure_balanced")


@pytest.fixture
def breaker(store: InMemoryStateStore, venue: SimulationVenue) -> CircuitBreaker:
    breaker_store = InMemoryBreakerStore()

    class _FlattenNoop:
        def emergency_flatten_all(self, reason: str) -> None:
            pass

    return CircuitBreaker(
        config=RiskConfig(),
        store=breaker_store,
        flatten_handler=_FlattenNoop(),
    )


@pytest.fixture
def fast_loop(
    store: InMemoryStateStore,
    venue: SimulationVenue,
    breaker: CircuitBreaker,
    price_feed: FakeMarkPriceProvider,
    exit_cfg,
) -> FastLoop:
    return FastLoop(
        store=store,
        venue=venue,
        breaker=breaker,
        price_feed=price_feed,
        exit_config=exit_cfg,
    )


@pytest.fixture
def slow_loop(store: InMemoryStateStore, classifier: FakeClassifier) -> SlowLoop:
    return SlowLoop(store=store, classifier=classifier)


@pytest.fixture
def snipe_loop(
    store: InMemoryStateStore,
    venue: SimulationVenue,
    config: RiskConfig,
) -> SnipeLoop:
    return SnipeLoop(
        store=store,
        venue=venue,
        config=config,
        latency_budget_ms=None,  # disable budget enforcement in tests
    )


@pytest.fixture
def kill_switch() -> KillSwitch:
    return KillSwitch()


@pytest.fixture
def reconciler(store: InMemoryStateStore, venue: SimulationVenue) -> Reconciler:
    return Reconciler(store=store, venue=venue)


@pytest.fixture
def orchestrator(
    store: InMemoryStateStore,
    venue: SimulationVenue,
    snipe_loop: SnipeLoop,
    fast_loop: FastLoop,
    slow_loop: SlowLoop,
    reconciler: Reconciler,
    kill_switch: KillSwitch,
) -> ControllerOrchestrator:
    return ControllerOrchestrator(
        store=store,
        venue=venue,
        snipe_loop=snipe_loop,
        fast_loop=fast_loop,
        slow_loop=slow_loop,
        reconciler=reconciler,
        kill_switch=kill_switch,
    )
