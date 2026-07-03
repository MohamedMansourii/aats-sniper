"""T-340 — InMemoryStateStore tests.

Covers: TTL expiry, claim_entering atomicity, intent dedup, heartbeat.
"""
from __future__ import annotations

import threading

import pytest

from aats.controller.state import InMemoryStateStore


def test_claim_entering_basic(fake_clock):
    """claim_entering() returns True once, False on subsequent calls."""
    store = InMemoryStateStore(clock=fake_clock)
    assert store.claim_entering("MINT_A", "intent1") is True
    assert store.claim_entering("MINT_A", "intent2") is False


def test_claim_entering_ttl_expiry(fake_clock):
    """After TTL expires, a new claim on the same mint succeeds."""
    store = InMemoryStateStore(clock=fake_clock)
    store.claim_entering("MINT_A", "intent1", ttl_seconds=10)
    assert store.claim_entering("MINT_A", "intent2") is False

    # Advance clock past TTL
    fake_clock.advance(11_000)
    assert store.claim_entering("MINT_A", "intent3") is True


def test_release_entering(fake_clock):
    """release_entering() allows re-claiming a mint."""
    store = InMemoryStateStore(clock=fake_clock)
    store.claim_entering("MINT_A", "intent1")
    store.release_entering("MINT_A")
    assert store.claim_entering("MINT_A", "intent2") is True


def test_set_and_get_decision_signal_with_ttl(fake_clock):
    """Decision signal is returned before TTL and None after expiry."""
    from aats.contracts.events import EventTime
    from aats.contracts.models import DecisionSignal

    store = InMemoryStateStore(clock=fake_clock)
    signal = DecisionSignal(
        mint="MINT_X",
        event_time=EventTime(
            slot=100, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_060
        ),
        model_version="v1",
        p_calibrated=0.75,
        uncertainty=0.10,
        baseline_p=0.50,
        surface="EH-001",
    )
    store.set_decision_signal(signal, ttl_seconds=5)
    assert store.get_decision_signal("MINT_X") is not None

    fake_clock.advance(6_000)
    assert store.get_decision_signal("MINT_X") is None


def test_veto_flag_ttl(fake_clock):
    """Veto flag self-expires after TTL."""
    store = InMemoryStateStore(clock=fake_clock)
    store.set_veto_flag("MINT_V", ttl_seconds=3)
    assert store.get_veto_flag("MINT_V") is True

    fake_clock.advance(4_000)
    assert store.get_veto_flag("MINT_V") is False


def test_cooldown(fake_clock):
    """in_cooldown() returns True during cooldown, False after."""
    store = InMemoryStateStore(clock=fake_clock)
    store.set_cooldown("MINT_C", ttl_seconds=10)
    assert store.in_cooldown("MINT_C") is True

    fake_clock.advance(11_000)
    assert store.in_cooldown("MINT_C") is False


def test_intent_dedup(fake_clock):
    """mark_intent_seen() returns True first time, False on replay."""
    store = InMemoryStateStore(clock=fake_clock)
    assert store.mark_intent_seen("intent-001") is True
    assert store.mark_intent_seen("intent-001") is False
    # Different id -> True
    assert store.mark_intent_seen("intent-002") is True


def test_heartbeat_age(fake_clock):
    """emit_heartbeat() sets the age; get_heartbeat_age_ms() returns elapsed time."""
    store = InMemoryStateStore(clock=fake_clock)
    assert store.get_heartbeat_age_ms() == 2**31 - 1  # never emitted

    store.emit_heartbeat(block_time_ms=1_700_000_000_000)
    assert store.get_heartbeat_age_ms() == 0

    fake_clock.advance(5_000)
    assert store.get_heartbeat_age_ms() == 5_000


def test_aggregate_open_lamports(store):
    """get_aggregate_open_lamports() sums sol_in_lamports across open positions."""
    from aats.contracts.intents import CostStack
    from aats.contracts.positions import FSMState, Position

    def _mk_pos(mint: str, sol: int) -> Position:
        cs = CostStack(
            expected_edge_bps=350, jito_tip_bps=10, priority_fee_bps=10,
            entry_slippage_bps=50, amm_fee_bps=50, exit_slippage_bps=50,
            adverse_selection_bps=150, total_cost_bps=320,
        )
        return Position(
            mint=mint, token=mint, surface="EH-001",
            fsm_state=FSMState.OPEN, entry_slot=1000,
            sol_in_lamports=sol, tokens_out_base=100_000,
            entry_slippage_bps=50, exit_config_label="default",
            exit_mode="secure", tp_hit=0, tp_total=3,
            trailing_armed=False, hard_stop_price_lamports=6_000_000,
            realized_pnl_net_lamports=None, unrealized_pnl_net_lamports=0,
            cost_breakdown=cs, status="open", completeness_status="complete",
        )

    store.save_position(_mk_pos("M1", 50_000_000))
    store.save_position(_mk_pos("M2", 30_000_000))
    assert store.get_aggregate_open_lamports() == 80_000_000


def test_no_float_money_in_breaker_state_store():
    """BreakerState refuses float values for lamport fields."""
    from aats.contracts.risk import BreakerState

    with pytest.raises((ValueError, TypeError)):
        BreakerState(
            state="ARMED",
            tripped_at_utc=None,
            daily_net_pnl_lamports=1.5,  # float — must be rejected
            daily_net_pnl_day_utc=None,
            threshold_breached=None,
        )


def test_insider_dump_flag_absent_reads_false(fake_clock):
    """E14a: absent insider_dump flag reads False, detail reads None —
    identical default posture to get_narrative_failure_flag()."""
    store = InMemoryStateStore(clock=fake_clock)
    assert store.get_insider_dump_flag("MINT_D") is False
    assert store.get_insider_dump_detail("MINT_D") is None


def test_insider_dump_flag_set_then_get(fake_clock):
    """E14a: set_insider_dump_flag() makes get_insider_dump_flag() True and
    carries the (fraction_sold_bps, event_slot) payload via get_insider_dump_detail()."""
    store = InMemoryStateStore(clock=fake_clock)
    store.set_insider_dump_flag("MINT_D", fraction_sold_bps=3_500, event_slot=999)
    assert store.get_insider_dump_flag("MINT_D") is True
    assert store.get_insider_dump_detail("MINT_D") == (3_500, 999)


def test_insider_dump_flag_ttl_expiry_mirrors_narrative_failure(fake_clock):
    """E14a: TTL-based expiry — SAME mechanism as test_veto_flag_ttl /
    narrative:{mint} (no explicit clear method; expiry is the only path back
    to False)."""
    store = InMemoryStateStore(clock=fake_clock)
    store.set_insider_dump_flag("MINT_D", fraction_sold_bps=5_000, event_slot=1, ttl_seconds=3)
    assert store.get_insider_dump_flag("MINT_D") is True

    fake_clock.advance(4_000)
    assert store.get_insider_dump_flag("MINT_D") is False
    assert store.get_insider_dump_detail("MINT_D") is None


def test_insider_dump_flag_default_ttl_120s(fake_clock):
    """Default TTL matches set_narrative_failure_flag's default (120s)."""
    store = InMemoryStateStore(clock=fake_clock)
    store.set_insider_dump_flag("MINT_D", fraction_sold_bps=5_000, event_slot=1)
    fake_clock.advance(119_000)
    assert store.get_insider_dump_flag("MINT_D") is True
    fake_clock.advance(2_000)  # total 121s
    assert store.get_insider_dump_flag("MINT_D") is False


def test_insider_dump_flag_isolated_per_mint(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    store.set_insider_dump_flag("MINT_A", fraction_sold_bps=2_000, event_slot=1)
    assert store.get_insider_dump_flag("MINT_A") is True
    assert store.get_insider_dump_flag("MINT_B") is False


def test_insider_dump_flag_rejects_bool_fraction(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    with pytest.raises(TypeError):
        store.set_insider_dump_flag("MINT_D", fraction_sold_bps=True, event_slot=1)


def test_insider_dump_flag_rejects_float_fraction(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    with pytest.raises(TypeError):
        store.set_insider_dump_flag("MINT_D", fraction_sold_bps=50.5, event_slot=1)


def test_insider_dump_flag_rejects_fraction_over_10000(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    with pytest.raises(ValueError):
        store.set_insider_dump_flag("MINT_D", fraction_sold_bps=10_001, event_slot=1)


def test_insider_dump_flag_rejects_negative_fraction(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    with pytest.raises(ValueError):
        store.set_insider_dump_flag("MINT_D", fraction_sold_bps=-1, event_slot=1)


def test_insider_dump_flag_rejects_bool_event_slot(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    with pytest.raises(TypeError):
        store.set_insider_dump_flag("MINT_D", fraction_sold_bps=1, event_slot=True)


def test_insider_dump_flag_rejects_negative_event_slot(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    with pytest.raises(ValueError):
        store.set_insider_dump_flag("MINT_D", fraction_sold_bps=1, event_slot=-1)


# --- E17 sellability-degraded flag (mirrors insider_dump — same TTL semantics) ---


def test_sellability_degraded_flag_absent_reads_false(fake_clock):
    """E17: absent sellability flag reads False, detail reads None — refuse-by-
    default presence semantics identical to insider_dump/narrative_failure."""
    store = InMemoryStateStore(clock=fake_clock)
    assert store.get_sellability_degraded_flag("MINT_S") is False
    assert store.get_sellability_degraded_detail("MINT_S") is None


def test_sellability_degraded_flag_set_then_get(fake_clock):
    """E17: set makes get True and carries the (reason, event_slot) payload."""
    store = InMemoryStateStore(clock=fake_clock)
    store.set_sellability_degraded_flag("MINT_S", reason="degraded_sim_revert", event_slot=999)
    assert store.get_sellability_degraded_flag("MINT_S") is True
    assert store.get_sellability_degraded_detail("MINT_S") == ("degraded_sim_revert", 999)


def test_sellability_degraded_flag_ttl_expiry(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    store.set_sellability_degraded_flag("MINT_S", reason="inconclusive", event_slot=1, ttl_seconds=3)
    assert store.get_sellability_degraded_flag("MINT_S") is True
    fake_clock.advance(3_000)
    assert store.get_sellability_degraded_flag("MINT_S") is False
    assert store.get_sellability_degraded_detail("MINT_S") is None


def test_sellability_degraded_flag_default_ttl_120s(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    store.set_sellability_degraded_flag("MINT_S", reason="degraded_other", event_slot=1)
    fake_clock.advance(119_000)
    assert store.get_sellability_degraded_flag("MINT_S") is True
    fake_clock.advance(2_000)
    assert store.get_sellability_degraded_flag("MINT_S") is False


def test_sellability_degraded_flag_isolated_per_mint(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    store.set_sellability_degraded_flag("MINT_A", reason="degraded_zero_output", event_slot=1)
    assert store.get_sellability_degraded_flag("MINT_A") is True
    assert store.get_sellability_degraded_flag("MINT_B") is False


def test_sellability_degraded_flag_rejects_empty_reason(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    with pytest.raises(ValueError):
        store.set_sellability_degraded_flag("MINT_S", reason="", event_slot=1)


def test_sellability_degraded_flag_rejects_bool_event_slot(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    with pytest.raises(TypeError):
        store.set_sellability_degraded_flag("MINT_S", reason="x", event_slot=True)


def test_sellability_degraded_flag_rejects_negative_event_slot(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    with pytest.raises(ValueError):
        store.set_sellability_degraded_flag("MINT_S", reason="x", event_slot=-1)


# --- E19 lp_unlock_approaching flag (mirrors sellability_degraded — same TTL
# semantics).  2026-07-03 program review: this StateStore method DID NOT EXIST
# (neither Protocol nor InMemoryStateStore) prior to this fix — E19 was
# "doubly dead". These tests are the missing wiring proof. ---


def test_lp_unlock_approaching_flag_absent_reads_false(fake_clock):
    """E19: absent lp_unlock flag reads False, detail reads None — refuse-by-
    default presence semantics identical to insider_dump/sellability_degraded."""
    store = InMemoryStateStore(clock=fake_clock)
    assert store.get_lp_unlock_approaching_flag("MINT_L") is False
    assert store.get_lp_unlock_approaching_detail("MINT_L") is None


def test_lp_unlock_approaching_flag_set_then_get(fake_clock):
    """E19: set makes get True and carries the (reason, event_slot) payload."""
    store = InMemoryStateStore(clock=fake_clock)
    store.set_lp_unlock_approaching_flag(
        "MINT_L", reason="lp_unlock_approaching", event_slot=999
    )
    assert store.get_lp_unlock_approaching_flag("MINT_L") is True
    assert store.get_lp_unlock_approaching_detail("MINT_L") == ("lp_unlock_approaching", 999)


def test_lp_unlock_approaching_flag_ttl_expiry(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    store.set_lp_unlock_approaching_flag(
        "MINT_L", reason="lp_unlock_schedule_unknown", event_slot=1, ttl_seconds=3
    )
    assert store.get_lp_unlock_approaching_flag("MINT_L") is True
    fake_clock.advance(3_000)
    assert store.get_lp_unlock_approaching_flag("MINT_L") is False
    assert store.get_lp_unlock_approaching_detail("MINT_L") is None


def test_lp_unlock_approaching_flag_default_ttl_120s(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    store.set_lp_unlock_approaching_flag(
        "MINT_L", reason="lp_unlock_approaching", event_slot=1
    )
    fake_clock.advance(119_000)
    assert store.get_lp_unlock_approaching_flag("MINT_L") is True
    fake_clock.advance(2_000)
    assert store.get_lp_unlock_approaching_flag("MINT_L") is False


def test_lp_unlock_approaching_flag_isolated_per_mint(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    store.set_lp_unlock_approaching_flag(
        "MINT_A", reason="lp_unlock_approaching", event_slot=1
    )
    assert store.get_lp_unlock_approaching_flag("MINT_A") is True
    assert store.get_lp_unlock_approaching_flag("MINT_B") is False


def test_lp_unlock_approaching_flag_rejects_empty_reason(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    with pytest.raises(ValueError):
        store.set_lp_unlock_approaching_flag("MINT_L", reason="", event_slot=1)


def test_lp_unlock_approaching_flag_rejects_bool_event_slot(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    with pytest.raises(TypeError):
        store.set_lp_unlock_approaching_flag("MINT_L", reason="x", event_slot=True)


def test_lp_unlock_approaching_flag_rejects_negative_event_slot(fake_clock):
    store = InMemoryStateStore(clock=fake_clock)
    with pytest.raises(ValueError):
        store.set_lp_unlock_approaching_flag("MINT_L", reason="x", event_slot=-1)


def test_concurrent_claim_atomicity():
    """Concurrent claim_entering across 100 threads yields exactly one winner."""
    store = InMemoryStateStore()
    mint = "ATOM_MINT"
    n = 100

    wins = []
    lock = threading.Lock()

    def claim():
        result = store.claim_entering(mint, "intent_x")
        if result:
            with lock:
                wins.append(1)

    threads = [threading.Thread(target=claim) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(wins) == 1, (
        f"Exactly one concurrent claim must win; got {len(wins)}"
    )
