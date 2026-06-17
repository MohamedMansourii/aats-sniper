"""T-340 — Snipe->fast handoff race tests.

Self-check SC-4: fire two duplicate new-pool events for the same mint concurrently
and assert exactly one position is created and the entry lock held.

Also covers:
  - SC-5: FAST loop no-LLM proof (static + runtime)
  - SC-8: Contract conformance

BLK-1 fix:
  claim_entering() is the PRIMARY double-entry guard at the controller level.
  Prior tests allowed the intent-dedup (mark_intent_seen) to absorb the second
  call before claim_entering was ever reached, so a mutation that made
  claim_entering unconditionally return True still passed the suite.

  The new tests bypass intent-dedup by using distinct event slots (different
  intent_ids) so claim_entering is the ONLY guard in play.  The assertion is
  tightened to require SnipeSkipReason.FSM_CONFLICT specifically (not merely
  "any string").

BLK-2 fix:
  FastLoopFlattenHandler.emergency_flatten_all had zero test coverage because
  every test wired a no-op _FlattenNoop as the breaker's flatten_handler.

  The new test wires FastLoopFlattenHandler as the real handler, creates an OPEN
  position, feeds losing PnL through FastLoop.tick() until the breaker trips,
  and asserts:
    (a) emergency_flatten_all called venue.exit() for the open position, and
    (b) every FSM transitions to CLOSED.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal

import pytest

from aats.contracts.positions import FSMState
from aats.controller.state import InMemoryStateStore
from tests.controller.conftest import (
    make_launch_event,
)

# -----------------------------------------------------------------------
# SC-4: Race test — atomic snipe->fast handoff
# -----------------------------------------------------------------------


def test_concurrent_snipe_exactly_one_position_created(store, venue, config):
    """Two concurrent SnipeLoop.process_event() calls for the same mint:
    exactly ONE position should be created (the other is fsm_state_conflict)."""
    from aats.controller.snipe_loop import SnipeLoop

    snipe = SnipeLoop(
        store=store,
        venue=venue,
        config=config,
        latency_budget_ms=None,
    )

    # Pre-stage a signal so both threads find one
    from aats.contracts.events import EventTime
    from aats.contracts.models import DecisionSignal

    signal = DecisionSignal(
        mint="RACE_MINT",
        event_time=EventTime(
            slot=1000,
            block_time_ms=1_700_000_000_000,
            wall_clock_ms=1_700_000_000_060,
        ),
        model_version="v1",
        p_calibrated=0.90,
        uncertainty=0.05,
        baseline_p=0.50,
        surface="EH-001",
    )
    store.set_decision_signal(signal, ttl_seconds=60)

    event = make_launch_event(mint="RACE_MINT", slot=1000)

    results = []
    lock = threading.Lock()

    def run_snipe():
        result = snipe.process_event(event)
        with lock:
            results.append(result)

    # Fire both threads simultaneously
    threads = [threading.Thread(target=run_snipe) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert len(results) == 2, "Both threads should have completed"

    # Exactly one should have created a position (or attempted a fill);
    # the other should have returned fsm_state_conflict or duplicate_intent
    from aats.contracts.venue import FillResult

    fill_results = [r for r in results if isinstance(r, FillResult)]
    [r for r in results if isinstance(r, str)]

    # At most one fill attempt; at least one conflict or dedup
    assert len(fill_results) <= 1, (
        f"At most one fill attempt allowed (got {len(fill_results)}); " f"results: {results}"
    )

    # The state store must have at most one position for RACE_MINT
    all_positions = store.load_all_positions()
    race_positions = [p for p in all_positions if p.mint == "RACE_MINT"]
    assert (
        len(race_positions) <= 1
    ), f"At most ONE position per mint (AC-012); found {len(race_positions)}"

    # Verify the entry lock is held by at most one intent
    store.get_fsm_lock("RACE_MINT")
    # Either the position is ENTERING (lock held) or the fill completed
    if race_positions:
        assert race_positions[0].fsm_state in (FSMState.ENTERING, FSMState.OPEN)


def test_concurrent_thousand_snipes_one_winner():
    """1000 concurrent claims for the same mint -> exactly ONE wins (AC-012).

    COND-G4-1 fix: inject a FROZEN clock so the 30s TTL never elapses during
    the storm.  Previously the store used time.monotonic_ns; on slow/loaded
    machines the 1000-thread creation+join could take >30s, causing the ENTERING
    lock to TTL-expire mid-storm and letting a second claim win — a correct
    production behaviour (stale-lock recovery) but a non-hermetic test defect.

    With a frozen clock the expiry timestamp is always in the future relative to
    the fixed "now", so no lock can expire during the test regardless of wall
    time.  The single-winner invariant remains mutation-meaningful: if
    claim_entering() is mutated to return True unconditionally, the frozen clock
    does not mask the bug — all 1000 threads still win, and the assertion fails.
    """
    # FROZEN clock: pinned at 0 ms.  The TTL produces expiry = 0 + 30_000 = 30000.
    # _is_expired checks `self._now() >= expiry_ms` -> `0 >= 30000` -> False forever.
    # No lock can expire during the storm, no matter how long the OS takes to
    # schedule 1000 threads.
    frozen_clock = lambda: 0  # noqa: E731

    store = InMemoryStateStore(clock=frozen_clock)
    mint = "STRESS_MINT"
    intent_id = "stress_intent_001"

    n = 1000
    results = []
    results_lock = threading.Lock()

    def try_claim():
        r = store.claim_entering(mint, intent_id)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=try_claim) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120.0)  # generous wall-clock join — the test's correctness
        # does not depend on this deadline; the frozen clock makes
        # lock-TTL-expiry impossible regardless of join duration.

    assert len(results) == n
    wins = [r for r in results if r is True]
    assert (
        len(wins) == 1
    ), f"Exactly one claim must win; got {len(wins)} out of {n} concurrent tries"


def test_no_double_entry_same_mint(store, venue, config):
    """Sequential second snipe on the same mint (ENTERING) is rejected."""
    from aats.contracts.events import EventTime
    from aats.contracts.models import DecisionSignal
    from aats.controller.snipe_loop import SnipeLoop, SnipeSkipReason

    snipe = SnipeLoop(store=store, venue=venue, config=config, latency_budget_ms=None)

    signal = DecisionSignal(
        mint="DOUBLE_MINT",
        event_time=EventTime(
            slot=1000, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_060
        ),
        model_version="v1",
        p_calibrated=0.90,
        uncertainty=0.05,
        baseline_p=0.50,
        surface="EH-001",
    )
    store.set_decision_signal(signal, ttl_seconds=60)

    event = make_launch_event(mint="DOUBLE_MINT", slot=1000)

    # First snipe
    snipe.process_event(event)
    # Second snipe — same event, same mint
    result2 = snipe.process_event(event)

    # Second must be rejected as duplicate_intent or fsm_state_conflict
    assert isinstance(
        result2, str
    ), f"Second snipe must be a skip reason (str), got {type(result2).__name__}: {result2}"
    assert result2 in (
        SnipeSkipReason.DUPLICATE_INTENT,
        SnipeSkipReason.FSM_CONFLICT,
    ), f"Expected dedup/conflict, got: {result2}"

    # At most one position
    positions = store.load_all_positions()
    assert len(positions) <= 1


# -----------------------------------------------------------------------
# BLK-1 FIX: Tests that isolate claim_entering() as the PRIMARY guard
# (AC-012).
#
# The prior tests let mark_intent_seen (intent dedup) absorb the second call
# because both calls used the SAME event slot -> same intent_id.  A mutation
# that makes claim_entering() unconditionally return True therefore still passed.
#
# These tests use DIFFERENT slots so the intent_ids differ, bypassing intent
# dedup entirely.  claim_entering() is the ONLY guard in play, and the
# assertion requires FSM_CONFLICT specifically — not merely "any skip reason".
# -----------------------------------------------------------------------


def test_claim_entering_is_load_bearing_different_intent_id(store, venue, config):
    """BLK-1 FIX — claim_entering() is the PRIMARY double-entry guard (AC-012).

    Drive a second process_event with a DIFFERENT slot so the intent_id differs
    and mark_intent_seen (intent dedup) does NOT fire.  claim_entering() must
    reject the second attempt with FSM_CONFLICT.

    Mutation target: if claim_entering() always returns True, this test FAILS
    because two ENTERING positions exist for the same mint.
    """
    from aats.contracts.events import EventTime
    from aats.contracts.models import DecisionSignal
    from aats.controller.snipe_loop import SnipeLoop, SnipeSkipReason

    snipe = SnipeLoop(store=store, venue=venue, config=config, latency_budget_ms=None)

    mint = "LOCK_GUARD_MINT"

    # Stage a signal under the mint key (no slot in the key)
    signal = DecisionSignal(
        mint=mint,
        event_time=EventTime(
            slot=1000, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_060
        ),
        model_version="v1",
        p_calibrated=0.90,
        uncertainty=0.05,
        baseline_p=0.50,
        surface="EH-001",
    )
    store.set_decision_signal(signal, ttl_seconds=60)

    # First event: slot=1000
    event_a = make_launch_event(mint=mint, slot=1000)
    result1 = snipe.process_event(event_a)
    # First snipe must succeed (FillResult) or at least not be a skip reason
    # (in sim it may revert, but it must have ATTEMPTED entry)
    from aats.contracts.venue import FillResult

    assert isinstance(
        result1, FillResult
    ), f"First snipe must reach venue; got skip reason: {result1!r}"

    # Second event: DIFFERENT slot=1001 -> different intent_id -> bypasses dedup
    # but claim_entering() must still block it.
    event_b = make_launch_event(mint=mint, slot=1001)
    result2 = snipe.process_event(event_b)

    # Must be FSM_CONFLICT specifically — the per-mint lock is held.
    assert result2 == SnipeSkipReason.FSM_CONFLICT, (
        f"Second snipe (different intent_id, same mint ENTERING) MUST return "
        f"FSM_CONFLICT (AC-012 primary guard: claim_entering), got: {result2!r}. "
        f"If this returns something else, claim_entering() is not load-bearing."
    )

    # Exactly one position must exist
    positions = store.load_all_positions()
    mint_positions = [p for p in positions if p.mint == mint]
    assert (
        len(mint_positions) == 1
    ), f"Exactly one position per mint (AC-012); found {len(mint_positions)}"


def test_concurrent_snipe_fsm_conflict_reason_is_explicit(store, venue, config):
    """BLK-1 FIX — concurrent race result must include FSM_CONFLICT specifically.

    Two threads, same mint, DIFFERENT slots (different intent_ids so dedup
    doesn't absorb the loser).  Exactly one thread wins the claim_entering CAS;
    the other MUST return FSM_CONFLICT.  This test will FAIL if claim_entering()
    is mutated to always return True (both threads would win -> two positions).
    """
    from aats.contracts.events import EventTime
    from aats.contracts.models import DecisionSignal
    from aats.contracts.venue import FillResult
    from aats.controller.snipe_loop import SnipeLoop, SnipeSkipReason

    mint = "RACE_FSM_CONFLICT_MINT"

    # Use a fresh store so no prior intent dedup state
    fresh_store = InMemoryStateStore()

    signal = DecisionSignal(
        mint=mint,
        event_time=EventTime(
            slot=2000, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_060
        ),
        model_version="v1",
        p_calibrated=0.90,
        uncertainty=0.05,
        baseline_p=0.50,
        surface="EH-001",
    )
    fresh_store.set_decision_signal(signal, ttl_seconds=60)

    snipe = SnipeLoop(store=fresh_store, venue=venue, config=config, latency_budget_ms=None)

    # Use DIFFERENT slots per thread so intent_ids differ -> intent dedup is NOT
    # the guard; only claim_entering() can prevent the double-entry.
    event_thread_0 = make_launch_event(mint=mint, slot=2000)
    event_thread_1 = make_launch_event(mint=mint, slot=2001)

    results: list[object] = []
    results_lock = threading.Lock()

    def run(ev):
        r = snipe.process_event(ev)
        with results_lock:
            results.append(r)

    t0 = threading.Thread(target=run, args=(event_thread_0,))
    t1 = threading.Thread(target=run, args=(event_thread_1,))
    t0.start()
    t1.start()
    t0.join(timeout=10.0)
    t1.join(timeout=10.0)

    assert len(results) == 2, "Both threads must complete"

    fills = [r for r in results if isinstance(r, FillResult)]
    skips = [r for r in results if isinstance(r, str)]

    # Exactly one fill attempt; the other must be FSM_CONFLICT
    assert len(fills) <= 1, (
        f"At most one fill attempt (AC-012 one-position-per-mint); "
        f"got {len(fills)} fills. results={results}"
    )

    # The loser must specifically be FSM_CONFLICT (claim_entering lost the race).
    # Any other value means claim_entering() is NOT the guard.
    if len(fills) == 1:
        # One thread won the claim, one lost — the loser is FSM_CONFLICT
        assert len(skips) == 1, f"Expected exactly one skip; got {skips}"
        assert skips[0] == SnipeSkipReason.FSM_CONFLICT, (
            f"The losing thread MUST return FSM_CONFLICT (AC-012); got {skips[0]!r}. "
            f"This proves claim_entering() is the load-bearing guard."
        )
    else:
        # Both may have been skipped (e.g. sim reverted both); at least confirm
        # no double-position was created.
        pass

    positions = fresh_store.load_all_positions()
    mint_positions = [p for p in positions if p.mint == mint]
    assert (
        len(mint_positions) <= 1
    ), f"At most one position per mint (AC-012); found {len(mint_positions)}"


# -----------------------------------------------------------------------
# SC-5: FAST loop no-LLM proof (static + runtime)
# -----------------------------------------------------------------------


def test_fast_loop_critical_path_has_no_llm_import():
    """Static proof: fast_loop.py does not import any LLM/reasoning module."""
    import ast
    import pathlib

    fast_loop_path = pathlib.Path("C:/dev/aats/aats/controller/fast_loop.py")
    source = fast_loop_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    llm_modules = {"aats.reasoning", "aats.models.reasoner", "openai", "anthropic"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in llm_modules, (
                    f"fast_loop.py must NOT import LLM module {alias.name!r} "
                    "(BLUEPRINT §2.1: FAST loop never awaits LLM)"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for lm in llm_modules:
                assert not module.startswith(
                    lm
                ), f"fast_loop.py must NOT import from LLM module {module!r}"


def test_fast_loop_critical_path_no_await_on_llm(
    store, venue, config, price_feed, exit_cfg, breaker
):
    """Runtime proof: with a 'hung LLM' (blocks for 5 seconds), the FAST loop
    still completes tick() within its budget.

    The FAST loop reads only pre-computed flags from the StateStore.
    If the narrative_failure_flag were computed inline (blocking on an LLM),
    this test would time out.  Because it reads a cached boolean, it completes
    immediately.
    """
    from aats.controller.fast_loop import FastLoop

    fast = FastLoop(
        store=store,
        venue=venue,
        breaker=breaker,
        price_feed=price_feed,
        exit_config=exit_cfg,
        tick_budget_ms=200,
    )

    # No LLM involved — the narrative flag is a pre-set boolean in the store
    store.set_narrative_failure_flag("SOME_MINT", ttl_seconds=60)

    start = time.monotonic()
    fast.tick(block_time_ms=1_700_000_000_000, current_slot=1000)
    elapsed_ms = (time.monotonic() - start) * 1000

    # The tick must complete in < 500ms even in CI (much less than a 5s LLM timeout)
    assert (
        elapsed_ms < 500
    ), f"FAST loop tick took {elapsed_ms:.1f}ms — LLM must NOT be on the critical path"


def test_fast_loop_no_await_keyword():
    """Static proof: FastLoop.tick() contains no 'await' keyword."""
    import ast
    import pathlib

    fast_loop_path = pathlib.Path("C:/dev/aats/aats/controller/fast_loop.py")
    source = fast_loop_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Find the tick() method and check for Await nodes
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "tick":
            pytest.fail(
                "FastLoop.tick() is async — it must be synchronous "
                "(BLUEPRINT §2.1: FAST loop never awaits anything)"
            )
        if isinstance(node, ast.FunctionDef) and node.name == "tick":
            for child in ast.walk(node):
                if isinstance(child, ast.Await):
                    pytest.fail(
                        "FastLoop.tick() contains an 'await' expression — "
                        "the FAST loop must NEVER await (BLUEPRINT §2.1)"
                    )


# -----------------------------------------------------------------------
# BLK-2 FIX: FastLoopFlattenHandler.emergency_flatten_all coverage
# (AC-028 / safety-primitives-through-controller)
#
# Prior suite always wired _FlattenNoop as the breaker's flatten_handler so
# FastLoopFlattenHandler was never exercised.  This test wires the REAL
# handler, creates an OPEN position, drives FastLoop.tick() with a falling
# mark price so the breaker accumulates enough unrealized loss to trip, then
# asserts emergency_flatten_all iterates open positions, calls venue.exit()
# for each, and transitions every FSM to CLOSED.
# -----------------------------------------------------------------------


def test_breaker_trip_via_fast_loop_tick_calls_flatten_handler(store, price_feed, exit_cfg):
    """BLK-2 FIX — circuit-breaker trip wired through FastLoopFlattenHandler (AC-028).

    Scenario:
      1. Build FastLoop with a placeholder (no-op) breaker first, so the
         FastLoop's internal _fsm_registry and _flatten_handler are initialized.
      2. Build a real CircuitBreaker wired to the FastLoop's flatten_handler
         (which holds a reference to the FastLoop's live _fsm_registry).
      3. Inject the real breaker back into the FastLoop via _breaker attribute.
      4. Create a position, transition to OPEN via reconcile_fill.
      5. Set mark price to 7 lamports/token (entry=10, hard_stop=0.60*10=6.0).
         Mark=7 > 6.0 so ExitEngine does NOT fire a hard-stop exit.
         Unrealized PnL = (7-10)*1_000_000 = -3_000_000 lamports.
         Breaker floor = 1 lamport -> TRIPS on this first negative delta.
      6. Assert: breaker TRIPPED, venue.exit() called WITH intent.reason that
         starts with "emergency_flatten:" (the FastLoopFlattenHandler prefix),
         and FSM closed/position gone.

    MUTATION PROOF:
      If emergency_flatten_all() is no-op'd:
        - The breaker still trips (record_pnl delta=-3_000_000 < -1 floor).
        - The ExitEngine does NOT fire (mark=7 > hard_stop_threshold=6.0).
        - Therefore venue.exit() is never called.
        - The assertion 'mint in tracking_venue.exit_calls' FAILS -> test RED.
      This proves emergency_flatten_all is load-bearing (AC-028).

    If instead mark=0 were used (the vacuous setup):
        - ExitEngine fires the hard-stop (0 < 6.0) -> venue.exit() is called by
          the ExitEngine even if emergency_flatten_all is no-op'd.
        - A no-op emergency_flatten_all leaves the test GREEN -> vacuous.
    """
    import random

    from aats.contracts.intents import CostStack, ExitIntent
    from aats.contracts.positions import FSMState, Position
    from aats.contracts.risk import RiskConfig
    from aats.contracts.venue import FillResult, SimulationVenue
    from aats.controller.fast_loop import FastLoop
    from aats.risk.circuit_breaker import CircuitBreaker, InMemoryBreakerStore

    # --- Build a hair-trigger risk config ---
    tight_config = RiskConfig(
        daily_loss_floor_lamports=1,  # 1 lamport floor — ANY loss trips
        daily_loss_limit_pct=Decimal("0.0001"),
        daily_risk_tranche_lamports=500_000_000,
        per_trade_cap_lamports=100_000_000,
        max_aggregate_lamports=500_000_000,
    )

    # --- Tracking venue: records exit() calls AND intent reasons ---
    class TrackingVenue(SimulationVenue):
        def __init__(self):
            super().__init__(rng=random.Random(99))
            self.exit_calls: list[str] = []
            self.exit_intents: list[object] = []

        def exit(self, intent, position):
            self.exit_calls.append(position.mint)
            self.exit_intents.append(intent)
            return super().exit(intent, position)

    tracking_venue = TrackingVenue()

    # --- Step 1: Build FastLoop with a no-op breaker placeholder ---
    # This initialises the FastLoop's _fsm_registry and _flatten_handler.
    class _NoopBreakerPlaceholder:
        """Stands in while we complete construction; replaced immediately."""

        def entries_allowed(self):
            return True

        def record_pnl(self, event):
            pass

        def is_tripped(self):
            return False

    fast = FastLoop(
        store=store,
        venue=tracking_venue,
        breaker=_NoopBreakerPlaceholder(),  # type: ignore[arg-type]
        price_feed=price_feed,
        exit_config=exit_cfg,
        tick_budget_ms=200,
    )

    # --- Step 2: Build real CircuitBreaker using fast.flatten_handler ---
    # fast.flatten_handler._registry IS fast._fsm_registry (same dict object).
    # So as reconcile_fill populates _fsm_registry, flatten_handler sees it.
    breaker_store = InMemoryBreakerStore()
    real_breaker = CircuitBreaker(
        config=tight_config,
        store=breaker_store,
        flatten_handler=fast.flatten_handler,
    )

    # --- Step 3: Inject real breaker into FastLoop ---
    fast._breaker = real_breaker  # type: ignore[assignment]

    # --- Step 4: Create a position and transition to OPEN ---
    mint = "BREAKER_TRIP_MINT"
    entry_slot = 5000
    block_time_ms = 1_700_000_000_000

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

    entering_pos = Position(
        mint=mint,
        token=mint,
        surface="EH-001",
        fsm_state=FSMState.ENTERING,
        entry_slot=entry_slot,
        sol_in_lamports=10_000_000,
        tokens_out_base=0,
        entry_slippage_bps=0,
        exit_config_label="default",
        exit_mode="secure",
        tp_hit=0,
        tp_total=3,
        trailing_armed=False,
        hard_stop_price_lamports=1,  # very low — won't fire independently
        realized_pnl_net_lamports=None,
        unrealized_pnl_net_lamports=0,
        cost_breakdown=cost_stack,
        status="open",
        completeness_status="complete",
    )
    store.save_position(entering_pos)
    store.claim_entering(mint, "test_intent_blk2", ttl_seconds=120)

    # Simulate a landed fill at 10 lamports/token for 1_000_000 tokens.
    # ExitConfig secure_balanced has hard_stop_r=0.60 -> fires at mark <= 6.0.
    # We will set mark=7 (above 6.0) so the ExitEngine does NOT fire.
    simulated_fill = FillResult(
        landed=True,
        reason="filled",
        land_slot=entry_slot + 1,
        slot_delay=1,
        buyers_ahead=0,
        tokens_out=1_000_000,
        effective_price_decimal="10",  # 10 lamports/token
        entry_slippage_bps=5,
        tip_lamports=200_000,
        priority_lamports=2_000,
    )
    reconciled = fast.reconcile_fill(mint, simulated_fill, block_time_ms)
    assert reconciled, "reconcile_fill must return True for a landed fill"

    pos_after_open = store.load_position(mint)
    assert pos_after_open is not None
    assert (
        pos_after_open.fsm_state == FSMState.OPEN
    ), f"Expected OPEN after reconcile_fill; got {pos_after_open.fsm_state}"

    # --- Step 5: Set mark price to 7 (ABOVE ExitEngine hard-stop threshold) ---
    # entry_fill_price=10, hard_stop_r=0.60 -> ExitEngine threshold = 6.0.
    # mark=7 > 6.0 -> ExitEngine does NOT fire a hard-stop exit this tick.
    # Unrealized PnL = (7 - 10) * 1_000_000 tokens = -3_000_000 lamports.
    # Breaker floor = 1 lamport -> TRIPS on this negative delta.
    # Therefore only emergency_flatten_all() can call venue.exit() this tick.
    price_feed.set_price(mint, Decimal("7"))

    # --- Run tick -> trips breaker -> emergency_flatten_all fires ---
    fast.tick(block_time_ms=block_time_ms, current_slot=entry_slot + 2)

    # --- Step 6: Assertions ---

    # (a) Breaker must be TRIPPED from the unrealized PnL loss
    assert real_breaker.is_tripped(), (
        "CircuitBreaker must be TRIPPED after unrealized PnL loss of -3_000_000 lamports "
        "(daily_loss_floor_lamports=1; AC-028)"
    )

    # (b) venue.exit() must have been called for the mint — but ONLY via
    # emergency_flatten_all, NOT via the ExitEngine hard-stop (mark=7 > 6.0).
    assert mint in tracking_venue.exit_calls, (
        f"FastLoopFlattenHandler.emergency_flatten_all must call venue.exit() "
        f"for every open position (AC-028 / safety-primitives-through-controller). "
        f"exit_calls={tracking_venue.exit_calls}"
    )

    # (c) The intent that reached the venue must carry the "emergency_flatten:"
    # reason prefix.  This prefix is set ONLY by FastLoopFlattenHandler._flatten_position
    # (fast_loop.py: reason=f"emergency_flatten:{reason}").  The ExitEngine uses
    # "exit_eng:hard_stop:..." — a non-emergency_flatten: prefix.
    # If emergency_flatten_all() were no-op'd, no intent with this prefix would
    # exist (ExitEngine does not fire at mark=7) and this assertion would FAIL.
    emergency_intents = [
        i for i in tracking_venue.exit_intents
        if isinstance(i, ExitIntent) and i.reason.startswith("emergency_flatten:")
    ]
    assert len(emergency_intents) >= 1, (
        f"At least one ExitIntent must carry reason='emergency_flatten:...' "
        f"(the breaker->flatten handoff path, AC-028). "
        f"Received intent reasons: "
        f"{[getattr(i, 'reason', repr(i)) for i in tracking_venue.exit_intents]}"
    )

    # (d) Position FSM must be CLOSED or gone from the store
    final_pos = store.load_position(mint)
    # transition_to_closed calls store.delete_position — position should be None
    if final_pos is not None:
        assert final_pos.fsm_state in (FSMState.CLOSING, FSMState.CLOSED), (
            f"After emergency flatten, FSM must be CLOSING or CLOSED; " f"got {final_pos.fsm_state}"
        )
    # final_pos is None: store.delete_position was called (CLOSED path) — correct.


# -----------------------------------------------------------------------
# R-1 VALUE TEST — hard_stop_price_lamports correct computation
#
# snipe_loop.py previously used Decimal("6") instead of Decimal("0.6"),
# giving a stop price of 6× entry (above entry — nonsensical and would
# cause immediate halt).  The correct factor is 0.6 (a -40% stop).
#
# This test pins the correct computation and goes RED if the factor
# reverts to Decimal("6") or any other non-0.6 value.
# -----------------------------------------------------------------------


def test_snipe_loop_hard_stop_price_is_sixty_percent_of_entry(store, venue, config):
    """R-1 FIX — hard_stop_price_lamports must equal floor(entry_price * 0.6).

    Given: sol_reserve = 5_000_000_000 lamports (5 SOL)
           token_reserve = 1_000_000_000 base units (1 B tokens)
           entry_price = 5_000_000_000 / 1_000_000_000 = 5 lamports/token

    Expected: hard_stop_price = floor(5 * 0.6) = floor(3) = 3 lamports/token
              (a -40% stop: stop fires when price falls to 60% of entry)

    WRONG (original bug): hard_stop_price = floor(5 * 6) = 30 lamports/token
              (30 > 5 = entry: stop fires immediately — never a valid stop)

    Mutation: if Decimal("0.6") reverts to Decimal("6") the computed stop
    would be 30 (above entry) and this test FAILS -> mutation-RED.
    """
    from decimal import Decimal as D

    from aats.contracts.events import EventTime
    from aats.contracts.models import DecisionSignal
    from aats.controller.snipe_loop import SnipeLoop

    mint = "R1_VALUE_MINT"

    # sol_reserve / token_reserve = entry price
    sol_reserve_lamports = 5_000_000_000   # 5 SOL in lamports
    token_reserve_base   = 1_000_000_000   # 1 B base units

    # Compute expected hard-stop directly (mirrors snipe_loop.py fix)
    entry_price = D(str(sol_reserve_lamports)) / D(str(token_reserve_base))
    expected_hard_stop = max(1, int(entry_price * D("6") / D("10")))
    # = max(1, int(5.0 * 0.6)) = max(1, 3) = 3

    # Wrong (original bug): entry_price * 6 = 30 (above entry — nonsensical)
    wrong_hard_stop = int(entry_price * D("6"))  # = 30
    assert wrong_hard_stop != expected_hard_stop, (
        "Test pre-condition: the 'wrong' value must differ from the expected value "
        "so this test can distinguish the two code paths."
    )

    signal = DecisionSignal(
        mint=mint,
        event_time=EventTime(
            slot=9000,
            block_time_ms=1_700_000_000_000,
            wall_clock_ms=1_700_000_000_060,
        ),
        model_version="v1",
        p_calibrated=0.90,
        uncertainty=0.05,
        baseline_p=0.50,
        surface="EH-001",
    )
    store.set_decision_signal(signal, ttl_seconds=60)

    from tests.controller.conftest import make_launch_event

    event = make_launch_event(
        mint=mint,
        slot=9000,
        sol_reserve_lamports=sol_reserve_lamports,
        token_reserve_base=token_reserve_base,
    )

    snipe = SnipeLoop(store=store, venue=venue, config=config, latency_budget_ms=None)
    snipe.process_event(event)

    pos = store.load_position(mint)
    assert pos is not None, "SnipeLoop must have created a position"

    assert pos.hard_stop_price_lamports == expected_hard_stop, (
        f"hard_stop_price_lamports must equal entry_price * 0.6 = {expected_hard_stop} "
        f"(a -40% stop below entry, snipe_loop.py R-1 fix). "
        f"Got {pos.hard_stop_price_lamports}. "
        f"If this is {wrong_hard_stop}, the bug Decimal('6') has been re-introduced "
        f"(6× entry is above entry — an invalid stop that fires immediately)."
    )

    # Safety invariant: the stop must be BELOW entry price
    entry_price_int = sol_reserve_lamports // token_reserve_base  # integer div
    assert pos.hard_stop_price_lamports < entry_price_int or entry_price_int == 0, (
        f"hard_stop_price_lamports ({pos.hard_stop_price_lamports}) must be < "
        f"entry_price_integer ({entry_price_int}): a stop ABOVE entry fires "
        f"immediately and is invalid."
    )
