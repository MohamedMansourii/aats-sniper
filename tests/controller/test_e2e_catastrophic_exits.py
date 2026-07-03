"""2026-07-03 program review (HIGH) — E2E proof that the Wave-2 catastrophic
exits (E14 insider-dump, E17 sellability-degraded, E19 lp-unlock-approaching)
actually FIRE inside the RUNNING controller loop, not merely in an isolated
unit test.

THE REVIEW'S LESSON: "green unit tests != wired live".  Every piece exercised
here already had unit-test coverage in isolation (aats/risk/exit_engine.py's
own on_tick branch tests, aats/risk/sellability_reprobe.py's reprobe tests,
aats/risk/lp_unlock_gate.py's watcher tests) — but NOTHING previously proved
that:
  (a) a StateStore method existed for E19 at all (it did not — "doubly dead"),
  (b) FastLoop.tick() actually READS the flag from the store and PASSES it
      into ExitEngine.on_tick() (it did not for E19; E14/E17 were read but had
      zero integration-level test coverage either), and
  (c) a PRODUCER (InsiderDumpDetector / SellabilityReprober / LpUnlockExitWatcher)
      driven through the REAL orchestrator entrypoint (never by manually poking
      `store.set_*_flag` directly) results in the position being force-closed
      by the REAL FastLoop.tick() -> ExitEngine -> venue.exit() -> FSM CLOSED.

Every test below builds a REAL ControllerOrchestrator (real FastLoop, real
StateStore, real SimulationVenue) with a REAL SlowLoopEnrichmentWiring wired to
the REAL producer classes (only the innermost data source — e.g. the sell-sim
RPC client or the LP-unlock schedule decoder — is faked, exactly as
production would inject a test double there).  No test calls
`store.set_insider_dump_flag` / `set_sellability_degraded_flag` /
`set_lp_unlock_approaching_flag` directly — the flag must be set BY the
producer, driven THROUGH the orchestrator's public entrypoint.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

from aats.contracts.intents import CostStack
from aats.contracts.positions import FSMState, Position
from aats.contracts.risk import RiskConfig
from aats.contracts.venue import FillResult, SimulationVenue
from aats.controller.control_api import KillSwitch
from aats.controller.enrichment_wiring import SlowLoopEnrichmentWiring
from aats.controller.fast_loop import FastLoop
from aats.controller.loops import ControllerOrchestrator
from aats.controller.reconcile import Reconciler
from aats.controller.slow_loop import SlowLoop
from aats.controller.snipe_loop import SnipeLoop
from aats.controller.state import InMemoryStateStore
from aats.execution.sell_sim import SellSimReason, SellSimResult
from aats.ingestion.insider_dump import (
    InsiderDumpDetector,
    SellObservation,
    StaticHeldSupplyProvider,
)
from aats.risk.circuit_breaker import CircuitBreaker, InMemoryBreakerStore
from aats.risk.exit_engine import ExitReason, preset
from aats.risk.lp_unlock_gate import (
    LpLockStatus,
    LpUnlockExitWatcher,
    LpUnlockSchedule,
    LpUnlockThresholds,
)
from aats.risk.sellability_reprobe import SellabilityReprober
from tests.controller.conftest import FakeClassifier, FakeMarkPriceProvider

_MINT = "CatastrophicExitMint1111111111111111111111"
_ENTRY_SLOT = 5_000
_BLOCK_TIME_MS = 1_700_000_000_000
_ENTRY_PRICE = Decimal("10")  # lamports/token


class _NullFlattener:
    def emergency_flatten_all(self, reason: str) -> None:
        pass


def _build_orchestrator(
    *,
    enrichment: SlowLoopEnrichmentWiring | None,
    store: InMemoryStateStore | None = None,
    price: Decimal = _ENTRY_PRICE,  # HEALTHY mark: no stop, no TP -> would HOLD
):
    """Assemble a REAL ControllerOrchestrator (no mocked loops) with the given
    (optional) enrichment wiring.  Returns (orchestrator, store, fast_loop).

    ``store`` MUST be the SAME InMemoryStateStore instance the caller's
    producers (detector/reprober/watcher) were wired against via ``enrichment``
    — pass it in explicitly so the flag the producer sets is the flag
    FastLoop.tick() actually reads (the whole point of this test module).
    """
    store = store if store is not None else InMemoryStateStore()
    venue = SimulationVenue(rng=random.Random(7))

    config = RiskConfig()
    breaker = CircuitBreaker(
        config=config, store=InMemoryBreakerStore(), flatten_handler=_NullFlattener()
    )
    price_feed = FakeMarkPriceProvider(price=price)

    fast = FastLoop(
        store=store,
        venue=venue,
        breaker=breaker,
        price_feed=price_feed,
        exit_config=preset("secure_balanced"),
    )
    slow = SlowLoop(store=store, classifier=FakeClassifier())
    snipe = SnipeLoop(store=store, venue=venue, config=config, latency_budget_ms=None)
    kill = KillSwitch()
    reconciler = Reconciler(store=store, venue=venue)

    orchestrator = ControllerOrchestrator(
        store=store,
        venue=venue,
        snipe_loop=snipe,
        fast_loop=fast,
        slow_loop=slow,
        reconciler=reconciler,
        kill_switch=kill,
        enrichment=enrichment,
    )
    orchestrator.startup(block_time_ms=_BLOCK_TIME_MS)
    return orchestrator, store, fast


def _open_position(store: InMemoryStateStore, fast: FastLoop, mint: str = _MINT) -> None:
    """Deterministically create an OPEN position (bypasses the probabilistic
    SimulationVenue fill so the exit-branch assertions are not flaky) — mirrors
    the pattern already used by test_e2e_paper_run.py's T-402-F1 test."""
    cost_stack = CostStack(
        expected_edge_bps=400,
        jito_tip_bps=10,
        priority_fee_bps=5,
        entry_slippage_bps=50,
        amm_fee_bps=25,
        exit_slippage_bps=50,
        adverse_selection_bps=150,
        total_cost_bps=290,
    )
    entering = Position(
        mint=mint,
        token=mint,
        surface="EH-001",
        fsm_state=FSMState.ENTERING,
        entry_slot=_ENTRY_SLOT,
        sol_in_lamports=10_000_000,
        tokens_out_base=0,
        entry_slippage_bps=0,
        exit_config_label="secure_balanced",
        exit_mode="secure",
        tp_hit=0,
        tp_total=3,
        trailing_armed=False,
        hard_stop_price_lamports=1,
        realized_pnl_net_lamports=None,
        unrealized_pnl_net_lamports=0,
        cost_breakdown=cost_stack,
        status="open",
        completeness_status="complete",
    )
    store.save_position(entering)
    store.claim_entering(mint, "catastrophic_exit_test_intent", ttl_seconds=120)

    fill = FillResult(
        landed=True,
        reason="filled",
        land_slot=_ENTRY_SLOT + 1,
        slot_delay=1,
        buyers_ahead=0,
        tokens_out=1_000_000,
        effective_price_decimal=str(_ENTRY_PRICE),
        entry_slippage_bps=5,
        tip_lamports=200_000,
        priority_lamports=2_000,
    )
    ok = fast.reconcile_fill(mint, fill, _BLOCK_TIME_MS)
    assert ok, "reconcile_fill must succeed to establish the OPEN baseline"
    pos = store.load_position(mint)
    assert pos is not None and pos.fsm_state == FSMState.OPEN, (
        "test setup failed: position must be OPEN before the catastrophic-exit probe"
    )


def _assert_force_closed(store: InMemoryStateStore, tick_result: dict, mint: str, reason: str) -> None:
    actions = tick_result["fast_actions"]
    assert any(f"exit:full:{mint}:{reason}" in a for a in actions), (
        f"expected a full exit with reason={reason!r} in fast_actions, got {actions!r}"
    )
    final = store.load_position(mint)
    assert final is None or final.fsm_state == FSMState.CLOSED, (
        f"position must be CLOSED (or removed) after the {reason} force-exit, "
        f"got {final.fsm_state if final else None!r}"
    )


# --------------------------------------------------------------------------- #
# CONTROL: with the enrichment wiring present but NO producer ever driven, a
# healthy mark just HOLDS.  Isolates "the exit was CAUSED by the producer",
# not a coincidental stop/TP/timeout on this price path.
# --------------------------------------------------------------------------- #


def test_control_no_producer_driven_position_holds():
    store = InMemoryStateStore()
    detector = InsiderDumpDetector(flag_sink=None)
    enrichment = SlowLoopEnrichmentWiring(store=store, insider_dump_detector=detector)
    orchestrator, store, _fast = _build_orchestrator(enrichment=enrichment, store=store)
    _open_position(store, _fast)

    result = orchestrator.tick(block_time_ms=_BLOCK_TIME_MS + 1_000, current_slot=_ENTRY_SLOT + 2)
    assert not any("exit:" in a for a in result["fast_actions"])
    pos = store.load_position(_MINT)
    assert pos is not None and pos.fsm_state == FSMState.OPEN


# --------------------------------------------------------------------------- #
# E14 — insider/dev-sell dump: InsiderDumpDetector -> StateStore -> FastLoop
# --------------------------------------------------------------------------- #


def test_e14_insider_dump_producer_fires_through_running_loop():
    """A real decoded SELL, fed through orchestrator.on_decoded_sell (the ONLY
    real entrypoint — never store.set_insider_dump_flag directly), sets the
    flag, and the NEXT orchestrator.tick() force-exits the OPEN position SECURE."""
    # NOTE: _build_orchestrator() constructs its OWN InMemoryStateStore, so the
    # detector's flag_sink must be wired to that SAME store — build the
    # orchestrator FIRST, then construct the detector/enrichment against its store.
    held_supply = StaticHeldSupplyProvider()
    store = InMemoryStateStore()
    detector = InsiderDumpDetector(held_supply=held_supply, flag_sink=store)
    enrichment = SlowLoopEnrichmentWiring(store=store, insider_dump_detector=detector)

    orchestrator, store, fast = _build_orchestrator(enrichment=enrichment, store=store)
    _open_position(store, fast)

    creator_wallet = "DevWallet1111111111111111111111111111111111"
    # E14a registration: the real path (SNIPE attempted an entry -> register).
    orchestrator.enrichment.register_creator(_MINT, creator_wallet)
    held_supply.set_held_supply(_MINT, creator_wallet, amount_base=1_000_000_000)

    # Confirm nothing forces an exit BEFORE the sell is observed.
    pre_tick = orchestrator.tick(block_time_ms=_BLOCK_TIME_MS + 400, current_slot=_ENTRY_SLOT + 2)
    assert not any("exit:" in a for a in pre_tick["fast_actions"])

    # A dev-wallet SELL of 25% of its held-supply baseline (> the 20% default
    # threshold) — decoded exactly as the real ingestion adapter would produce.
    obs = SellObservation(
        mint=_MINT,
        wallet=creator_wallet,
        token_amount_sold_base=250_000_000,
        event_slot=_ENTRY_SLOT + 3,
        block_time_ms=_BLOCK_TIME_MS + 1_200,
        signature="sig_dev_dump_1",
    )
    signal = orchestrator.on_decoded_sell(obs)
    assert signal is not None, "the threshold-crossing sell must produce a signal"
    assert signal.fraction_sold_bps >= 2_000

    # The flag must now be visible in the SAME store FastLoop.tick() reads.
    assert store.get_insider_dump_flag(_MINT) is True

    tick_result = orchestrator.tick(block_time_ms=_BLOCK_TIME_MS + 1_600, current_slot=_ENTRY_SLOT + 4)
    _assert_force_closed(store, tick_result, _MINT, ExitReason.INSIDER_DUMP.value)


def test_e14_below_threshold_sell_does_not_force_exit():
    """A sell BELOW the threshold must not set the flag or force an exit — proves
    the wiring does not over-fire."""
    held_supply = StaticHeldSupplyProvider()
    store = InMemoryStateStore()
    detector = InsiderDumpDetector(held_supply=held_supply, flag_sink=store)
    enrichment = SlowLoopEnrichmentWiring(store=store, insider_dump_detector=detector)
    orchestrator, store, fast = _build_orchestrator(enrichment=enrichment, store=store)
    _open_position(store, fast)

    creator_wallet = "DevWalletSmallSell11111111111111111111111"
    orchestrator.enrichment.register_creator(_MINT, creator_wallet)
    held_supply.set_held_supply(_MINT, creator_wallet, amount_base=1_000_000_000)

    obs = SellObservation(
        mint=_MINT,
        wallet=creator_wallet,
        token_amount_sold_base=10_000_000,  # 1% — well below the 20% default
        event_slot=_ENTRY_SLOT + 3,
        block_time_ms=_BLOCK_TIME_MS + 1_200,
        signature="sig_small_sell",
    )
    signal = orchestrator.on_decoded_sell(obs)
    assert signal is None
    assert store.get_insider_dump_flag(_MINT) is False

    tick_result = orchestrator.tick(block_time_ms=_BLOCK_TIME_MS + 1_600, current_slot=_ENTRY_SLOT + 4)
    assert not any("exit:" in a for a in tick_result["fast_actions"])


# --------------------------------------------------------------------------- #
# E17 — sellability re-probe: SellabilityReprober -> StateStore -> FastLoop
# --------------------------------------------------------------------------- #


@dataclass
class _FakeSellSimProbe:
    """Configurable fake exposing the SAME simulate_sell(...) surface the
    shared SellSimVenue exposes — no honeypot mechanics re-implemented here."""

    result: SellSimResult

    def simulate_sell(self, *, mint: str, event_slot: int) -> SellSimResult:
        return self.result


def test_e17_sellability_reprobe_producer_fires_through_running_loop():
    """A DEGRADED sell-sim re-probe, driven through
    orchestrator.run_slow_enrichment_tick (the ONLY real entrypoint — never
    store.set_sellability_degraded_flag directly), sets the flag, and the NEXT
    orchestrator.tick() force-exits the OPEN position SECURE."""
    store = InMemoryStateStore()
    probe = _FakeSellSimProbe(
        result=SellSimResult(
            sellable=False,
            reason=SellSimReason.SIM_REVERT,
            mint=_MINT,
            event_slot=_ENTRY_SLOT + 3,
        )
    )
    reprober = SellabilityReprober(probe=probe, flag_sink=store)
    enrichment = SlowLoopEnrichmentWiring(store=store, sellability_reprober=reprober)

    orchestrator, store, fast = _build_orchestrator(enrichment=enrichment, store=store)
    _open_position(store, fast)

    pre_tick = orchestrator.tick(block_time_ms=_BLOCK_TIME_MS + 400, current_slot=_ENTRY_SLOT + 2)
    assert not any("exit:" in a for a in pre_tick["fast_actions"])

    result = orchestrator.run_slow_enrichment_tick(event_slot=_ENTRY_SLOT + 3)
    assert len(result["sellability"]) == 1
    assert result["sellability"][0].degraded is True
    assert store.get_sellability_degraded_flag(_MINT) is True

    tick_result = orchestrator.tick(block_time_ms=_BLOCK_TIME_MS + 1_600, current_slot=_ENTRY_SLOT + 4)
    _assert_force_closed(store, tick_result, _MINT, ExitReason.SELLABILITY_DEGRADED.value)


def test_e17_healthy_reprobe_does_not_force_exit():
    """Control: a HEALTHY re-probe never sets the flag or forces an exit."""
    store = InMemoryStateStore()
    probe = _FakeSellSimProbe(
        result=SellSimResult(
            sellable=True, reason=SellSimReason.SELLABLE, mint=_MINT, event_slot=_ENTRY_SLOT + 3
        )
    )
    reprober = SellabilityReprober(probe=probe, flag_sink=store)
    enrichment = SlowLoopEnrichmentWiring(store=store, sellability_reprober=reprober)
    orchestrator, store, fast = _build_orchestrator(enrichment=enrichment, store=store)
    _open_position(store, fast)

    result = orchestrator.run_slow_enrichment_tick(event_slot=_ENTRY_SLOT + 3)
    assert result["sellability"][0].degraded is False
    assert store.get_sellability_degraded_flag(_MINT) is False

    tick_result = orchestrator.tick(block_time_ms=_BLOCK_TIME_MS + 1_600, current_slot=_ENTRY_SLOT + 4)
    assert not any("exit:" in a for a in tick_result["fast_actions"])


# --------------------------------------------------------------------------- #
# E19 — LP-unlock-approaching: LpUnlockExitWatcher -> StateStore -> FastLoop
# --------------------------------------------------------------------------- #


class _StubScheduleSource:
    """Injectable LpUnlockScheduleSource — the ONLY faked piece (no on-chain
    LP-locker decoder exists in this codebase yet; see enrichment_wiring.py)."""

    def __init__(self, schedules: dict[str, LpUnlockSchedule | None]) -> None:
        self._schedules = schedules

    def get_schedule(self, mint: str) -> LpUnlockSchedule | None:
        return self._schedules.get(mint)


def test_e19_lp_unlock_approaching_producer_fires_through_running_loop():
    """A TIME_LOCKED LP whose unlock slot is within the exit horizon, driven
    through orchestrator.run_slow_enrichment_tick (the ONLY real entrypoint —
    never store.set_lp_unlock_approaching_flag directly), sets the flag, and
    the NEXT orchestrator.tick() force-exits the OPEN position SECURE."""
    store = InMemoryStateStore()
    watcher = LpUnlockExitWatcher(
        thresholds=LpUnlockThresholds(exit_raise_within_slots=1_000),
        flag_sink=store,
    )
    current_slot = _ENTRY_SLOT + 3
    schedule = LpUnlockSchedule(
        mint=_MINT,
        as_of_event_slot=current_slot,
        status=LpLockStatus.TIME_LOCKED,
        unlock_slot=current_slot + 500,  # within the 1_000-slot exit horizon
    )
    source = _StubScheduleSource({_MINT: schedule})
    enrichment = SlowLoopEnrichmentWiring(
        store=store, lp_unlock_watcher=watcher, lp_unlock_schedule_source=source
    )

    orchestrator, store, fast = _build_orchestrator(enrichment=enrichment, store=store)
    _open_position(store, fast)

    pre_tick = orchestrator.tick(block_time_ms=_BLOCK_TIME_MS + 400, current_slot=_ENTRY_SLOT + 2)
    assert not any("exit:" in a for a in pre_tick["fast_actions"])

    result = orchestrator.run_slow_enrichment_tick(event_slot=current_slot)
    assert len(result["lp_unlock"]) == 1
    assert result["lp_unlock"][0].raise_flag is True
    assert store.get_lp_unlock_approaching_flag(_MINT) is True

    tick_result = orchestrator.tick(block_time_ms=_BLOCK_TIME_MS + 1_600, current_slot=_ENTRY_SLOT + 4)
    _assert_force_closed(store, tick_result, _MINT, ExitReason.LP_UNLOCK_APPROACHING.value)


def test_e19_far_off_unlock_does_not_force_exit():
    """Control: an unlock far beyond the exit horizon never sets the flag."""
    store = InMemoryStateStore()
    watcher = LpUnlockExitWatcher(
        thresholds=LpUnlockThresholds(exit_raise_within_slots=1_000),
        flag_sink=store,
    )
    current_slot = _ENTRY_SLOT + 3
    schedule = LpUnlockSchedule(
        mint=_MINT,
        as_of_event_slot=current_slot,
        status=LpLockStatus.TIME_LOCKED,
        unlock_slot=current_slot + 50_000,  # far beyond the 1_000-slot horizon
    )
    source = _StubScheduleSource({_MINT: schedule})
    enrichment = SlowLoopEnrichmentWiring(
        store=store, lp_unlock_watcher=watcher, lp_unlock_schedule_source=source
    )
    orchestrator, store, fast = _build_orchestrator(enrichment=enrichment, store=store)
    _open_position(store, fast)

    result = orchestrator.run_slow_enrichment_tick(event_slot=current_slot)
    assert result["lp_unlock"][0].raise_flag is False
    assert store.get_lp_unlock_approaching_flag(_MINT) is False

    tick_result = orchestrator.tick(block_time_ms=_BLOCK_TIME_MS + 1_600, current_slot=_ENTRY_SLOT + 4)
    assert not any("exit:" in a for a in tick_result["fast_actions"])


def test_e19_schedule_source_absent_skips_not_raises():
    """A mint with NO schedule-source data at all is SKIPPED (counted), never
    force-exited — the "refuse-by-default vs. not-yet-wired" distinction the
    module docstring documents.  Proves the wiring never turns "we have not
    built the decoder yet" into an unconditional kill-switch."""
    store = InMemoryStateStore()
    watcher = LpUnlockExitWatcher(flag_sink=store)
    source = _StubScheduleSource({})  # no data for ANY mint
    enrichment = SlowLoopEnrichmentWiring(
        store=store, lp_unlock_watcher=watcher, lp_unlock_schedule_source=source
    )
    orchestrator, store, fast = _build_orchestrator(enrichment=enrichment, store=store)
    _open_position(store, fast)

    result = orchestrator.run_slow_enrichment_tick(event_slot=_ENTRY_SLOT + 3)
    assert result["lp_unlock"] == []
    assert enrichment.stats.lp_unlock_schedule_absent_skipped == 1
    assert store.get_lp_unlock_approaching_flag(_MINT) is False

    tick_result = orchestrator.tick(block_time_ms=_BLOCK_TIME_MS + 1_600, current_slot=_ENTRY_SLOT + 4)
    assert not any("exit:" in a for a in tick_result["fast_actions"])
