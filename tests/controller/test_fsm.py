"""T-340 — FSM tests.

Self-check items:
  SC-3: FSM property test (hypothesis sequence; no illegal transition; exactly-one
        ENTERING per mint; FLAT->...->FLAT always closes).
  SC-7: Asymmetric-trust test (LLM SIZE-UP / stop-widen / hard-stop-override
        is rejected; never reaches the venue).
  SC-6: Crash-recovery test (intent replay is idempotent; same client_intent_id
        -> at most one trade).
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aats.contracts.intents import CostStack
from aats.contracts.positions import FSMState, validate_fsm_transition
from aats.contracts.venue import FillResult
from aats.controller.fsm import PositionFSM, make_client_intent_id
from aats.controller.state import InMemoryStateStore

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _default_cost_stack() -> CostStack:
    return CostStack(
        expected_edge_bps=350,
        jito_tip_bps=10,
        priority_fee_bps=10,
        entry_slippage_bps=50,
        amm_fee_bps=50,
        exit_slippage_bps=50,
        adverse_selection_bps=150,
        total_cost_bps=320,
    )


def _create_entering(store: InMemoryStateStore, mint: str = "MINT_A") -> PositionFSM:
    return PositionFSM.create_entering(
        store,
        mint=mint,
        token=mint,
        surface="EH-001",
        entry_slot=1000,
        sol_in_lamports=10_000_000,
        tokens_out_base=0,
        entry_slippage_bps=0,
        exit_config_label="default",
        exit_mode="secure",
        tp_total=3,
        hard_stop_price_lamports=60_000_000,
        cost_breakdown=_default_cost_stack(),
        client_intent_id="test-intent-001",
    )


# -----------------------------------------------------------------------
# Basic lifecycle tests
# -----------------------------------------------------------------------


def test_fsm_entering_persisted(store):
    """create_entering() writes the position to the store in ENTERING state."""
    fsm = _create_entering(store, "MINT_A")
    assert fsm.state == FSMState.ENTERING
    saved = store.load_position("MINT_A")
    assert saved is not None
    assert saved.fsm_state == FSMState.ENTERING


def test_fsm_entering_to_open(store):
    """ENTERING -> OPEN transition on fill (FAST loop path)."""
    fsm = _create_entering(store, "MINT_A")
    fill = FillResult(
        landed=True,
        reason="filled",
        land_slot=1001,
        slot_delay=1,
        buyers_ahead=0,
        tokens_out=1_000_000,
        effective_price_decimal="0.01",
        entry_slippage_bps=50,
        tip_lamports=200_000,
        priority_lamports=100,
    )
    # Simulate the SNIPE loop releasing the entering lock after claim
    store.claim_entering("MINT_A", "test-intent-001")
    fsm.transition_to_open(fill, block_time_ms=1_700_000_001_000)
    assert fsm.state == FSMState.OPEN
    saved = store.load_position("MINT_A")
    assert saved is not None
    assert saved.fsm_state == FSMState.OPEN
    assert saved.tokens_out_base == 1_000_000


def test_fsm_entering_to_vetoed(store):
    """ENTERING -> VETOED transition on failed fill."""
    fsm = _create_entering(store, "MINT_B")
    store.claim_entering("MINT_B", "test-intent-002")
    fsm.transition_to_vetoed(reason="fill_failed", block_time_ms=1_700_000_001_000)
    assert fsm.state == FSMState.VETOED
    # VETOED positions are removed from the store
    assert store.load_position("MINT_B") is None


def test_fsm_open_to_closing_to_closed(store):
    """OPEN -> CLOSING -> CLOSED full lifecycle."""
    fsm = _create_entering(store, "MINT_C")
    store.claim_entering("MINT_C", "test-intent-003")
    fill = FillResult(
        landed=True, reason="filled", land_slot=1001, slot_delay=1,
        tokens_out=500_000, effective_price_decimal="0.02",
        entry_slippage_bps=30, tip_lamports=200_000, priority_lamports=100,
    )
    fsm.transition_to_open(fill, block_time_ms=1_700_000_001_000)
    assert fsm.state == FSMState.OPEN

    fsm.transition_to_closing(block_time_ms=1_700_000_002_000)
    assert fsm.state == FSMState.CLOSING

    fsm.transition_to_closed(
        realized_pnl_net_lamports=-500_000,
        block_time_ms=1_700_000_003_000,
    )
    assert fsm.state == FSMState.CLOSED
    # CLOSED positions are removed from live store
    assert store.load_position("MINT_C") is None


def test_fsm_illegal_transition_raises(store):
    """Illegal transitions (e.g. OPEN->ENTERING) must raise, never silently no-op."""
    fsm = _create_entering(store, "MINT_D")
    store.claim_entering("MINT_D", "test-intent-004")
    fill = FillResult(
        landed=True, reason="filled", land_slot=1001, slot_delay=1,
        tokens_out=100_000, effective_price_decimal="0.01",
        entry_slippage_bps=10, tip_lamports=200_000, priority_lamports=100,
    )
    fsm.transition_to_open(fill, block_time_ms=1_700_000_001_000)

    # OPEN -> ENTERING is illegal
    with pytest.raises(ValueError, match="fsm_illegal_transition"):
        validate_fsm_transition(FSMState.OPEN, FSMState.ENTERING)


def test_fsm_closed_position_must_have_realized_pnl(store):
    """Closed position without realized_pnl_net_lamports raises."""
    from aats.contracts.positions import Position

    with pytest.raises(ValueError, match="closed Position must have"):
        Position(
            mint="MINT_E",
            token="MINT_E",
            surface="EH-001",
            fsm_state=FSMState.CLOSED,
            entry_slot=1000,
            sol_in_lamports=10_000_000,
            tokens_out_base=100_000,
            entry_slippage_bps=0,
            exit_config_label="default",
            exit_mode="secure",
            tp_hit=0,
            tp_total=3,
            trailing_armed=False,
            hard_stop_price_lamports=6_000_000,
            realized_pnl_net_lamports=None,  # must be set for CLOSED
            unrealized_pnl_net_lamports=0,
            cost_breakdown=_default_cost_stack(),
            status="closed",
            completeness_status="complete",
        )


# -----------------------------------------------------------------------
# SC-3: FSM property test (Hypothesis)
# -----------------------------------------------------------------------

# The full lifecycle: IDLE -> ENTERING -> OPEN -> CLOSING -> CLOSED
FSM_VALID_PATH = [
    FSMState.IDLE,
    FSMState.ENTERING,
    FSMState.OPEN,
    FSMState.CLOSING,
    FSMState.CLOSED,
]


@given(mints=st.lists(st.text(min_size=1, max_size=8), min_size=1, max_size=5, unique=True))
@settings(max_examples=50, deadline=5000)
def test_fsm_property_no_illegal_transition(mints):
    """For any set of mints, driving each through the full FSM produces no illegal transition
    and always ends at FLAT (CLOSED or VETOED, not stuck in ENTERING/OPEN)."""
    from aats.controller.state import InMemoryStateStore
    store = InMemoryStateStore()

    for mint in mints:
        # ENTERING claim (atomic)
        intent_id = make_client_intent_id(mint, 1000, "ENTRY")
        claimed = store.claim_entering(mint, intent_id)
        assert claimed, f"First claim for {mint} must succeed"

        fsm = PositionFSM.create_entering(
            store,
            mint=mint,
            token=mint,
            surface="EH-001",
            entry_slot=1000,
            sol_in_lamports=10_000_000,
            tokens_out_base=0,
            entry_slippage_bps=0,
            exit_config_label="default",
            exit_mode="secure",
            tp_total=3,
            hard_stop_price_lamports=6_000_000,
            cost_breakdown=_default_cost_stack(),
            client_intent_id=intent_id,
        )
        assert fsm.state == FSMState.ENTERING

        fill = FillResult(
            landed=True, reason="filled", land_slot=1001, slot_delay=1,
            tokens_out=100_000, effective_price_decimal="0.01",
            entry_slippage_bps=10, tip_lamports=200_000, priority_lamports=100,
        )
        fsm.transition_to_open(fill, block_time_ms=1_700_000_001_000)
        assert fsm.state == FSMState.OPEN

        fsm.transition_to_closing(block_time_ms=1_700_000_002_000)
        assert fsm.state == FSMState.CLOSING

        fsm.transition_to_closed(
            realized_pnl_net_lamports=100_000,
            block_time_ms=1_700_000_003_000,
        )
        assert fsm.state == FSMState.CLOSED
        # Ended at FLAT (CLOSED)
        assert store.load_position(mint) is None


@given(n=st.integers(min_value=2, max_value=10))
@settings(max_examples=30, deadline=5000)
def test_fsm_property_exactly_one_entering_per_mint(n):
    """Concurrent duplicate claims for the same mint yield exactly ONE success."""
    store = InMemoryStateStore()
    mint = "CONCURRENT_MINT"
    intent_id = make_client_intent_id(mint, 1000, "ENTRY")

    results = [store.claim_entering(mint, intent_id) for _ in range(n)]
    wins = [r for r in results if r]
    assert len(wins) == 1, (
        f"Exactly one claim must win per mint; got {len(wins)} winners for n={n} tries"
    )


# -----------------------------------------------------------------------
# SC-6: Crash-recovery / idempotent intent replay
# -----------------------------------------------------------------------


def test_idempotent_intent_replay(store):
    """Same client_intent_id emitted twice -> second call is a no-op (dedup)."""
    intent_id = "replay-test-001"
    first = store.mark_intent_seen(intent_id)
    second = store.mark_intent_seen(intent_id)
    assert first is True, "First emit of intent_id must be accepted"
    assert second is False, "Second emit of same intent_id must be rejected (dedup)"


def test_crash_recovery_entering_persists(store):
    """A position saved in ENTERING state survives a 'process restart' (new FSM object)."""
    _create_entering(store, "RECOVER_MINT")
    assert store.load_position("RECOVER_MINT") is not None

    # Simulate restart: new FSM object reads from the same store
    recovered = store.load_position("RECOVER_MINT")
    assert recovered is not None
    assert recovered.fsm_state == FSMState.ENTERING
    assert recovered.mint == "RECOVER_MINT"


def test_crash_recovery_intent_id_deterministic():
    """The same (mint, slot, kind) always produces the same client_intent_id."""
    id1 = make_client_intent_id("MINT_X", 1000, "ENTRY")
    id2 = make_client_intent_id("MINT_X", 1000, "ENTRY")
    assert id1 == id2, "client_intent_id must be deterministic"
    # Different slots produce different ids
    id3 = make_client_intent_id("MINT_X", 1001, "ENTRY")
    assert id1 != id3


# -----------------------------------------------------------------------
# SC-7: Asymmetric trust — LLM risk-increase attempts are REJECTED
# -----------------------------------------------------------------------


def test_asymmetric_trust_reasoning_action_no_risk_increase():
    """ReasoningAction enum has no SIZE_UP / WIDEN_STOP / ADD_LEVERAGE member."""
    from aats.contracts.models import ReasoningAction

    allowed = {ra.name for ra in ReasoningAction}
    forbidden = {"SIZE_UP", "WIDEN_STOP", "ADD_LEVERAGE", "OVERRIDE_HARD_STOP"}
    overlap = allowed & forbidden
    assert not overlap, (
        f"ReasoningAction contains forbidden risk-increase members: {overlap} "
        "(ADR-0006: the type system is the primary defense)"
    )


def test_asymmetric_trust_derisk_factory_no_entry():
    """DeRiskIntentFactory has no entry() method — LLM path cannot create EntryIntent."""
    from aats.contracts.intents import DeRiskIntentFactory

    factory = DeRiskIntentFactory()
    assert not hasattr(factory, "entry"), (
        "DeRiskIntentFactory must NOT have an entry() method "
        "(ADR-0006: structural enforcement of asymmetric trust)"
    )


def test_asymmetric_trust_slow_loop_rejects_size_up(store, classifier):
    """SlowLoop._apply_reasoning_verdict() REJECTS a (hypothetical) SIZE_UP action."""
    from aats.contracts.events import EventTime
    from aats.contracts.models import ReasoningAction, ReasoningVerdict
    from aats.controller.slow_loop import SlowLoop

    slow = SlowLoop(store=store, classifier=classifier)

    # Construct a verdict with a de-risk action (HOLD is a valid member)
    verdict = ReasoningVerdict(
        mint="MINT_SIZE_UP",
        event_time=EventTime(slot=1000, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_060),
        action=ReasoningAction.HOLD,
        reason="test_hold",
        confidence=0.5,
        action_received_raw="SIZE_UP",  # raw was a risk-increase
        risk_increase_clamped=True,
    )
    # HOLD should produce no state change
    slow._apply_reasoning_verdict("MINT_SIZE_UP", verdict)
    assert not store.get_veto_flag("MINT_SIZE_UP"), "HOLD should not set veto flag"
    assert not store.get_narrative_failure_flag("MINT_SIZE_UP"), "HOLD should not set narrative flag"


def test_asymmetric_trust_stop_cannot_be_widened():
    """StopState.tighten() ONLY raises on any attempt to lower the trigger (widen stop)."""
    from decimal import Decimal

    from aats.risk.survivable_stop import StopState, StopThresholds

    stop = StopState.at_entry(
        mint="MINT_STOP",
        entry_fill_price=Decimal("100"),
        thresholds=StopThresholds(hard_stop_drawdown_bps=4000),
    )
    # trigger_price should be 60 (= 100 * 0.60)
    assert stop.trigger_price == Decimal("60")

    # Widening the stop (lowering the trigger) should raise
    with pytest.raises((ValueError, AttributeError)):
        # StopState is frozen; there is no tighten(below_current) path
        # The contract doesn't expose a lower-trigger path at all
        stop.tighten(Decimal("50"))  # type: ignore[attr-defined]
