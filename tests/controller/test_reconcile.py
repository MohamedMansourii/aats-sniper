"""T-340 — Reconciler tests (startup + continuous).

Self-check SC-6 (crash-recovery): kill mid-ENTERING, restart, reconcile rebuilds
correct FSM state; intent replay is idempotent.
"""
from __future__ import annotations

from aats.contracts.intents import CostStack
from aats.contracts.positions import FSMState
from aats.contracts.venue import FillResult
from aats.controller.fsm import PositionFSM, make_client_intent_id
from aats.controller.reconcile import Reconciler
from aats.controller.state import InMemoryStateStore


def _default_cost_stack() -> CostStack:
    return CostStack(
        expected_edge_bps=350, jito_tip_bps=10, priority_fee_bps=10,
        entry_slippage_bps=50, amm_fee_bps=50, exit_slippage_bps=50,
        adverse_selection_bps=150, total_cost_bps=320,
    )


def _create_entering(store: InMemoryStateStore, mint: str, slot: int = 1000) -> PositionFSM:
    return PositionFSM.create_entering(
        store,
        mint=mint,
        token=mint,
        surface="EH-001",
        entry_slot=slot,
        sol_in_lamports=10_000_000,
        tokens_out_base=0,
        entry_slippage_bps=0,
        exit_config_label="default",
        exit_mode="secure",
        tp_total=3,
        hard_stop_price_lamports=6_000_000,
        cost_breakdown=_default_cost_stack(),
        client_intent_id=make_client_intent_id(mint, slot, "ENTRY"),
    )


def test_startup_reconcile_entering_no_fill_vetoes(store, venue):
    """Startup: ENTERING position with no fill -> VETOED (crash-recovery path)."""
    from decimal import Decimal

    from aats.controller.fast_loop import FastLoop
    from aats.risk.circuit_breaker import CircuitBreaker, InMemoryBreakerStore
    from aats.risk.exit_engine import preset
    from tests.controller.conftest import FakeMarkPriceProvider

    class _NullFlattener:
        def emergency_flatten_all(self, reason: str) -> None:
            pass

    from aats.contracts.risk import RiskConfig
    config = RiskConfig()
    breaker = CircuitBreaker(
        config=config, store=InMemoryBreakerStore(), flatten_handler=_NullFlattener()
    )
    price_feed = FakeMarkPriceProvider(price=Decimal("100"))
    exit_cfg = preset("secure_balanced")

    _create_entering(store, "RECONCILE_MINT")

    fast = FastLoop(
        store=store, venue=venue, breaker=breaker,
        price_feed=price_feed, exit_config=exit_cfg,
    )
    reconciler = Reconciler(store=store, venue=venue)
    actions = reconciler.startup_reconcile(fast, block_time_ms=1_700_000_000_000)

    # Position should be vetoed (no fill found)
    assert any("entering" in a.lower() for a in actions), (
        f"Expected reconcile action for ENTERING state, got: {actions}"
    )
    pos = store.load_position("RECONCILE_MINT")
    # After veto, position is removed from store
    assert pos is None


def test_startup_reconcile_entering_with_fill_opens(store, venue):
    """Startup: ENTERING position with a fill -> OPEN (fill arrived while down)."""
    from decimal import Decimal

    from aats.controller.fast_loop import FastLoop
    from aats.risk.circuit_breaker import CircuitBreaker, InMemoryBreakerStore
    from aats.risk.exit_engine import preset
    from tests.controller.conftest import FakeMarkPriceProvider

    class _NullFlattener:
        def emergency_flatten_all(self, reason: str) -> None:
            pass

    from aats.contracts.risk import RiskConfig

    config = RiskConfig()
    breaker = CircuitBreaker(
        config=config, store=InMemoryBreakerStore(), flatten_handler=_NullFlattener()
    )
    price_feed = FakeMarkPriceProvider(price=Decimal("100"))
    exit_cfg = preset("secure_balanced")

    _create_entering(store, "FILL_RECONCILE_MINT")

    fill = FillResult(
        landed=True, reason="filled", land_slot=1001, slot_delay=1,
        tokens_out=100_000, effective_price_decimal="0.1",
        entry_slippage_bps=10, tip_lamports=200_000, priority_lamports=100,
    )

    class FakeVenueState:
        def get_fill_for_mint(self, mint: str, intent_id: str) -> FillResult | None:
            if mint == "FILL_RECONCILE_MINT":
                return fill
            return None

        def get_open_mints(self) -> set[str]:
            return {"FILL_RECONCILE_MINT"}

    fast = FastLoop(
        store=store, venue=venue, breaker=breaker,
        price_feed=price_feed, exit_config=exit_cfg,
    )
    reconciler = Reconciler(store=store, venue=venue, venue_state=FakeVenueState())
    reconciler.startup_reconcile(fast, block_time_ms=1_700_000_000_000)

    pos = store.load_position("FILL_RECONCILE_MINT")
    assert pos is not None
    assert pos.fsm_state == FSMState.OPEN, f"Expected OPEN after fill reconcile, got {pos.fsm_state}"


def test_continuous_reconcile_entering_timeout(store, venue):
    """Continuous: ENTERING position with age > TTL threshold is vetoed."""
    from decimal import Decimal

    from aats.controller.fast_loop import FastLoop
    from aats.risk.circuit_breaker import CircuitBreaker, InMemoryBreakerStore
    from aats.risk.exit_engine import preset
    from tests.controller.conftest import FakeMarkPriceProvider

    class _NullFlattener:
        def emergency_flatten_all(self, reason: str) -> None:
            pass

    from aats.contracts.risk import RiskConfig

    config = RiskConfig()
    breaker = CircuitBreaker(
        config=config, store=InMemoryBreakerStore(), flatten_handler=_NullFlattener()
    )
    price_feed = FakeMarkPriceProvider(price=Decimal("100"))
    exit_cfg = preset("secure_balanced")

    # Create an ENTERING position at slot 100
    _create_entering(store, "TIMEOUT_MINT", slot=100)

    fast = FastLoop(
        store=store, venue=venue, breaker=breaker,
        price_feed=price_feed, exit_config=exit_cfg,
    )
    reconciler = Reconciler(store=store, venue=venue, entering_ttl_seconds=1)

    # Current slot far ahead (1000 slots later = > TTL)
    actions = reconciler.continuous_reconcile_tick(
        fast, block_time_ms=1_700_000_001_000, current_slot=1100
    )

    assert any("timeout" in a.lower() for a in actions), (
        f"Expected timeout action for stale ENTERING, got: {actions}"
    )


def test_idempotent_reconcile_replay(store, venue):
    """Running reconcile twice on the same state is idempotent (no duplicate transitions)."""
    from decimal import Decimal

    from aats.controller.fast_loop import FastLoop
    from aats.risk.circuit_breaker import CircuitBreaker, InMemoryBreakerStore
    from aats.risk.exit_engine import preset
    from tests.controller.conftest import FakeMarkPriceProvider

    class _NullFlattener:
        def emergency_flatten_all(self, reason: str) -> None:
            pass

    from aats.contracts.risk import RiskConfig

    config = RiskConfig()
    breaker = CircuitBreaker(
        config=config, store=InMemoryBreakerStore(), flatten_handler=_NullFlattener()
    )
    price_feed = FakeMarkPriceProvider(price=Decimal("100"))
    exit_cfg = preset("secure_balanced")

    _create_entering(store, "IDEMPOTENT_MINT")

    fast = FastLoop(
        store=store, venue=venue, breaker=breaker,
        price_feed=price_feed, exit_config=exit_cfg,
    )
    reconciler = Reconciler(store=store, venue=venue)

    # First reconcile
    reconciler.startup_reconcile(fast, block_time_ms=1_700_000_000_000)
    # Second reconcile (no ENTERING positions left after first)
    actions2 = reconciler.startup_reconcile(fast, block_time_ms=1_700_000_000_000)

    # Second run should have no actions (no positions to reconcile)
    assert actions2 == [] or all(
        "purge" in a.lower() for a in actions2
    ), f"Second reconcile should be a no-op, got: {actions2}"
