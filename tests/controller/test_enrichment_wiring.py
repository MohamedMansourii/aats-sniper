"""aats/controller/enrichment_wiring.py — unit tests for the SlowLoopEnrichmentWiring
dispatcher itself (the E2E proof that the running loop force-exits lives in
tests/controller/test_e2e_catastrophic_exits.py).

VERIFY criteria proven here:
  * Every producer is OPTIONAL: a None producer makes the corresponding method
    a clean no-op (never raises, never silently pretends to have run).
  * register_creator / on_decoded_sell delegate exactly to InsiderDumpDetector,
    with is_open_position derived from the CURRENT store truth.
  * run_sellability_reprobe delegates exactly to SellabilityReprober.
  * run_lp_unlock_watch delegates exactly to LpUnlockExitWatcher, and SKIPS
    (counts, does not raise) a mint whose schedule source returns None — the
    "refuse-by-default vs. not-yet-wired" distinction is honoured.
  * stats counters increment on every real call (observability; never silent).
"""

from __future__ import annotations

from dataclasses import dataclass

from aats.contracts.intents import CostStack
from aats.contracts.positions import FSMState, Position
from aats.controller.enrichment_wiring import SlowLoopEnrichmentWiring
from aats.controller.state import InMemoryStateStore
from aats.execution.sell_sim import SellSimReason, SellSimResult
from aats.ingestion.insider_dump import (
    InsiderDumpDetector,
    SellObservation,
    StaticHeldSupplyProvider,
)
from aats.risk.lp_unlock_gate import LpLockStatus, LpUnlockExitWatcher, LpUnlockSchedule
from aats.risk.sellability_reprobe import SellabilityReprober

_MINT = "EnrichmentWiringTestMint1111111111111111111"


def _cost_stack() -> CostStack:
    return CostStack(
        expected_edge_bps=400,
        jito_tip_bps=10,
        priority_fee_bps=5,
        entry_slippage_bps=50,
        amm_fee_bps=25,
        exit_slippage_bps=50,
        adverse_selection_bps=150,
        total_cost_bps=290,
    )


def _open_position(store: InMemoryStateStore, mint: str = _MINT) -> None:
    pos = Position(
        mint=mint,
        token=mint,
        surface="EH-001",
        fsm_state=FSMState.OPEN,
        entry_slot=1000,
        sol_in_lamports=10_000_000,
        tokens_out_base=1_000_000,
        entry_slippage_bps=0,
        exit_config_label="secure_balanced",
        exit_mode="secure",
        tp_hit=0,
        tp_total=3,
        trailing_armed=False,
        hard_stop_price_lamports=1,
        realized_pnl_net_lamports=None,
        unrealized_pnl_net_lamports=0,
        cost_breakdown=_cost_stack(),
        status="open",
        completeness_status="complete",
    )
    store.save_position(pos)


# --------------------------------------------------------------------------- #
# register_creator / on_decoded_sell (E14a)
# --------------------------------------------------------------------------- #


def test_register_creator_noop_when_no_detector():
    store = InMemoryStateStore()
    wiring = SlowLoopEnrichmentWiring(store=store)
    wiring.register_creator(_MINT, "wallet")  # must not raise
    assert wiring.stats.creator_registrations == 0


def test_register_creator_delegates_and_counts():
    store = InMemoryStateStore()
    detector = InsiderDumpDetector(flag_sink=store)
    wiring = SlowLoopEnrichmentWiring(store=store, insider_dump_detector=detector)
    wiring.register_creator(_MINT, "wallet_A")
    assert wiring.stats.creator_registrations == 1
    # Prove delegation: a sell from "wallet_A" is now tracked as CREATOR role.
    assert detector._role_of(_MINT, "wallet_A") is not None  # noqa: SLF001


def test_on_decoded_sell_noop_when_no_detector():
    store = InMemoryStateStore()
    wiring = SlowLoopEnrichmentWiring(store=store)
    obs = SellObservation(
        mint=_MINT, wallet="w", token_amount_sold_base=1, event_slot=1,
        block_time_ms=1, signature="s1",
    )
    assert wiring.on_decoded_sell(obs) is None
    assert wiring.stats.sells_processed == 0


def test_on_decoded_sell_is_open_position_gate_derived_from_store():
    """is_open_position is derived from store.load_position, not a caller flag —
    a sell for a mint with NO open position must not count as an insider dump
    even if the fraction would otherwise cross the threshold."""
    store = InMemoryStateStore()
    held_supply = StaticHeldSupplyProvider()
    detector = InsiderDumpDetector(held_supply=held_supply, flag_sink=store)
    wiring = SlowLoopEnrichmentWiring(store=store, insider_dump_detector=detector)
    wiring.register_creator(_MINT, "wallet_A")
    held_supply.set_held_supply(_MINT, "wallet_A", amount_base=1_000_000)

    # NOTE: no _open_position(store) call — the mint has NO open position.
    obs = SellObservation(
        mint=_MINT, wallet="wallet_A", token_amount_sold_base=500_000,
        event_slot=10, block_time_ms=1_000, signature="s1",
    )
    signal = wiring.on_decoded_sell(obs)
    assert signal is None
    assert store.get_insider_dump_flag(_MINT) is False
    assert wiring.stats.sells_processed == 1  # still counted as processed


def test_on_decoded_sell_fires_when_position_open_and_threshold_crossed():
    store = InMemoryStateStore()
    _open_position(store)
    held_supply = StaticHeldSupplyProvider()
    detector = InsiderDumpDetector(held_supply=held_supply, flag_sink=store)
    wiring = SlowLoopEnrichmentWiring(store=store, insider_dump_detector=detector)
    wiring.register_creator(_MINT, "wallet_A")
    held_supply.set_held_supply(_MINT, "wallet_A", amount_base=1_000_000)

    obs = SellObservation(
        mint=_MINT, wallet="wallet_A", token_amount_sold_base=500_000,  # 50%
        event_slot=10, block_time_ms=1_000, signature="s1",
    )
    signal = wiring.on_decoded_sell(obs)
    assert signal is not None
    assert store.get_insider_dump_flag(_MINT) is True


# --------------------------------------------------------------------------- #
# run_sellability_reprobe (E17)
# --------------------------------------------------------------------------- #


@dataclass
class _FakeProbe:
    result: SellSimResult

    def simulate_sell(self, *, mint: str, event_slot: int) -> SellSimResult:
        return self.result


def test_run_sellability_reprobe_noop_when_no_reprober():
    store = InMemoryStateStore()
    wiring = SlowLoopEnrichmentWiring(store=store)
    assert wiring.run_sellability_reprobe(event_slot=1) == []


def test_run_sellability_reprobe_delegates_and_counts():
    store = InMemoryStateStore()
    _open_position(store)
    probe = _FakeProbe(
        result=SellSimResult(
            sellable=False, reason=SellSimReason.ZERO_OUTPUT, mint=_MINT, event_slot=5
        )
    )
    reprober = SellabilityReprober(probe=probe, flag_sink=store)
    wiring = SlowLoopEnrichmentWiring(store=store, sellability_reprober=reprober)

    results = wiring.run_sellability_reprobe(event_slot=5)
    assert len(results) == 1
    assert results[0].degraded is True
    assert wiring.stats.sellability_reprobes_run == 1
    assert store.get_sellability_degraded_flag(_MINT) is True


# --------------------------------------------------------------------------- #
# run_lp_unlock_watch (E19)
# --------------------------------------------------------------------------- #


class _StubScheduleSource:
    def __init__(self, schedules: dict[str, LpUnlockSchedule | None]) -> None:
        self._schedules = schedules

    def get_schedule(self, mint: str) -> LpUnlockSchedule | None:
        return self._schedules.get(mint)


def test_run_lp_unlock_watch_noop_when_no_watcher():
    store = InMemoryStateStore()
    _open_position(store)
    wiring = SlowLoopEnrichmentWiring(
        store=store, lp_unlock_schedule_source=_StubScheduleSource({})
    )
    assert wiring.run_lp_unlock_watch(event_slot=1) == []


def test_run_lp_unlock_watch_noop_when_no_schedule_source():
    store = InMemoryStateStore()
    _open_position(store)
    watcher = LpUnlockExitWatcher(flag_sink=store)
    wiring = SlowLoopEnrichmentWiring(store=store, lp_unlock_watcher=watcher)
    assert wiring.run_lp_unlock_watch(event_slot=1) == []
    assert wiring.stats.lp_unlock_checks_run == 0


def test_run_lp_unlock_watch_skips_absent_schedule_distinctly():
    """A mint with NO schedule-source data is SKIPPED and counted distinctly —
    it must NEVER raise the flag (that would conflate 'no decoder yet' with
    'decoded but unknown')."""
    store = InMemoryStateStore()
    _open_position(store)
    watcher = LpUnlockExitWatcher(flag_sink=store)
    wiring = SlowLoopEnrichmentWiring(
        store=store, lp_unlock_watcher=watcher, lp_unlock_schedule_source=_StubScheduleSource({})
    )
    results = wiring.run_lp_unlock_watch(event_slot=1)
    assert results == []
    assert wiring.stats.lp_unlock_schedule_absent_skipped == 1
    assert wiring.stats.lp_unlock_checks_run == 0
    assert store.get_lp_unlock_approaching_flag(_MINT) is False


def test_run_lp_unlock_watch_delegates_and_counts_when_schedule_present():
    store = InMemoryStateStore()
    _open_position(store)
    watcher = LpUnlockExitWatcher(flag_sink=store)
    schedule = LpUnlockSchedule(
        mint=_MINT, as_of_event_slot=1, status=LpLockStatus.TIME_LOCKED, unlock_slot=100
    )
    wiring = SlowLoopEnrichmentWiring(
        store=store,
        lp_unlock_watcher=watcher,
        lp_unlock_schedule_source=_StubScheduleSource({_MINT: schedule}),
    )
    results = wiring.run_lp_unlock_watch(event_slot=1)
    assert len(results) == 1
    assert results[0].raise_flag is True  # 99 slots to unlock <= default 216_000 horizon
    assert wiring.stats.lp_unlock_checks_run == 1
    assert wiring.stats.lp_unlock_schedule_absent_skipped == 0
    assert store.get_lp_unlock_approaching_flag(_MINT) is True


def test_run_lp_unlock_watch_burned_lp_never_raises():
    store = InMemoryStateStore()
    _open_position(store)
    watcher = LpUnlockExitWatcher(flag_sink=store)
    schedule = LpUnlockSchedule(mint=_MINT, as_of_event_slot=1, status=LpLockStatus.BURNED)
    wiring = SlowLoopEnrichmentWiring(
        store=store,
        lp_unlock_watcher=watcher,
        lp_unlock_schedule_source=_StubScheduleSource({_MINT: schedule}),
    )
    results = wiring.run_lp_unlock_watch(event_slot=1)
    assert results[0].raise_flag is False
    assert store.get_lp_unlock_approaching_flag(_MINT) is False
