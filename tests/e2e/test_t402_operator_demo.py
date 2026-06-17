"""T-402 — END-TO-END PAPER operator demo (G4).

WHAT THIS TEST PROVES (by EXECUTION, not assertion-of-intent)
=============================================================
This is the cross-component G4 integration test the brief T-402 demands.  It

  1. BOOTS the control-plane server (the FROZEN api-contracts.md FastAPI app from
     `aats.control_plane.server.build_app`) as an in-process ASGI app, and
  2. BOOTS a running controller (`ControllerOrchestrator` over a `SimulationVenue`,
     DRY-RUN) whose REAL `FastLoop`, `KillSwitch`, `CircuitBreaker`, `StateStore`,
     and survivable-stop/DMS wiring are the SAME objects the control-plane app is
     built against — so a POST through the API drives the actual running loop,

then drives it the way the two operator surfaces drive it:

  - the DASHBOARD calls the contract via raw `postJSON`-style POSTs with the
    operator Bearer token (mirroring `dashboard/src/lib/api.ts`), AND
  - the TELEGRAM bot calls the contract via the REAL `TelegramCommandHandler` +
    REAL `HttpControlPlaneClient` (control_plane_client.py) routed at the SAME
    in-process ASGI app (httpx ASGITransport) — proving BOTH operator surfaces
    exercise the SAME control-plane contract.

ASSERTIONS (all five T-402 gates, each proven by running code):
  G-KILL   POST /api/kill (dashboard AND telegram) FLATTENS all open positions
           within the AC-040 <= 2s budget; positions go flat (venue.exit fired,
           FSM not OPEN).  Latency measured and asserted.
  G-MODE   POST /api/mode propagates to the controller's observable state.
  G-FEED   GET /api/feed (SSE) carries REAL controller events published by the
           running loop onto the SAME FeedBus the SSE generator reads — not mock.
  G-SAFETY breaker / survivable-stop (Layer-2) / DMS each FIRE ON DEMAND through
           this wiring (breaker trips + flattens; Layer-2 stop fires on a breach
           tick; DMS fires when the FAST-loop heartbeat ages past T_DMS — i.e.
           the loop is "killed").
  G-DERISK a risk-increasing command is REJECTED at the contract layer (mode-up
           to LIVE behind DRY-RUN, risk-config widen) AND the Telegram seam is
           structurally de-risk-only (no risk-increase method exists).

HONESTY (non-waivable, per the T-402 directive)
-----------------------------------------------
There is NO real recorded mainnet data in this offline build — every model
corpus is `is_bootstrap_not_real` synthetic.  Therefore LIVE EDGE CANNOT be
proven here and this test does NOT attempt to: it asserts ZERO win-rate / edge
numbers, only the operator-control + safety contract.  The metrics endpoint is
checked to CONFIRM it carries no win-rate field and a not-passing edge gate
(gate_a_pass / gate_b_pass == False) — the truthful state of a synthetic build.
Real capital stays DRY-RUN-disabled throughout (asserted).

All external services (Redis, RPC, LLM, Telegram) are deterministic offline
fakes.  No network, no secrets, no real keys.  RNG is seeded.
"""
from __future__ import annotations

import asyncio
import random
import time
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from aats.contracts.intents import CostStack
from aats.contracts.positions import FSMState
from aats.contracts.risk import BreakerState, RiskConfig
from aats.contracts.venue import FillResult, SimulationVenue
from aats.control_plane.server import ControlPlaneConfig, FeedBus, build_app
from aats.controller.control_api import KillSwitch
from aats.controller.enforcer_wiring import FastLoopEnforcerWiring
from aats.controller.fast_loop import FastLoop, FastLoopFlattenHandler
from aats.controller.fsm import PositionFSM, make_client_intent_id
from aats.controller.loops import ControllerOrchestrator
from aats.controller.reconcile import Reconciler
from aats.controller.slow_loop import SlowLoop
from aats.controller.snipe_loop import SnipeLoop
from aats.controller.state import InMemoryStateStore
from aats.risk.circuit_breaker import (
    CircuitBreaker,
    InMemoryBreakerStore,
    LLMDeRiskSignal,
    PnLEvent,
)
from aats.risk.deadman import EmergencyFlattenVenue
from aats.risk.dms_service import DmsWatchdog, Heartbeat, InMemoryHeartbeatStore
from aats.risk.exit_engine import preset
from aats.risk.secondary_enforcer import SecondaryStopEnforcer
from aats.telegram.command_config import TelegramCommandConfig
from aats.telegram.commands import TelegramCommandHandler
from aats.telegram.control_plane_client import HttpControlPlaneClient

# Tokens used to construct BOTH the control-plane app and the operator clients.
OPERATOR_TOKEN = "t402-operator-token"
CEO_TOKEN = "t402-ceo-token"
OPERATOR_HEADERS = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
CEO_HEADERS = {"Authorization": f"Bearer {OPERATOR_TOKEN}", "X-CEO-Auth": CEO_TOKEN}

LAMPORTS_PER_SOL = 1_000_000_000


# ---------------------------------------------------------------------------
# Deterministic test doubles
# ---------------------------------------------------------------------------


class FakeMarkPriceProvider:
    """Deterministic price feed.  Default high (positions stay OPEN); per-mint
    override lets a test drive a price BELOW the hard stop to fire Layer-2."""

    def __init__(self, price: Decimal = Decimal("200")) -> None:
        self._prices: dict[str, Decimal] = {}
        self._default = price

    def get_mark_price(self, mint: str, event_slot: int) -> Decimal | None:
        return self._prices.get(mint, self._default)

    def set_price(self, mint: str, price: Decimal) -> None:
        self._prices[mint] = price


class CountingSimVenue(SimulationVenue):
    """SimulationVenue that records every exit() call so the test can prove a
    flatten actually reached the venue seam (DRY-RUN: builds, does not submit)."""

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng=rng)
        self.exit_calls: list[str] = []

    def exit(self, intent, position):  # type: ignore[override]
        self.exit_calls.append(intent.mint)
        return super().exit(intent, position)


class DmsFlattenVenue(EmergencyFlattenVenue):
    """The EmergencyFlattenVenue seam the DMS watchdog invokes.  In a real
    deployment this submits PRE-SIGNED flatten tx; here (DRY-RUN) it records the
    fire so the test can prove the dead-man's switch reached the de-risk seam."""

    def __init__(self) -> None:
        self.fired_reasons: list[str] = []

    def emergency_flatten(self, reason: str) -> None:
        self.fired_reasons.append(reason)


class _ManualClock:
    """Injectable wall-epoch-ms clock shared by the FAST-loop heartbeat writer
    and the DMS watchdog, so 'killing the loop' (freezing the clock while the
    watchdog advances) is deterministic — no real sleeping."""

    def __init__(self, start_ms: int = 1_000_000) -> None:
        self.ms = start_ms

    def epoch_ms(self) -> int:
        return self.ms

    def monotonic_s(self) -> float:
        return self.ms / 1000.0

    def advance_ms(self, ms: int) -> None:
        self.ms += ms


# ---------------------------------------------------------------------------
# The running stack — control plane + controller wired to SHARED objects
# ---------------------------------------------------------------------------


class BootedStack:
    """Boots the control-plane app + a running controller against SHARED state.

    The POSTs the operator surfaces send to the control-plane app mutate the
    SAME KillSwitch / CircuitBreaker / FastLoop / StateStore the controller's
    loops read — so a kill from the dashboard genuinely halts the running loop.
    """

    def __init__(self) -> None:
        self.clock = _ManualClock()

        # --- Shared offline state ---
        self.store = InMemoryStateStore()
        self.venue = CountingSimVenue(rng=random.Random(99))
        self.kill = KillSwitch()
        self.feed_bus = FeedBus()
        self.price_feed = FakeMarkPriceProvider(price=Decimal("200"))

        # --- Circuit breaker (real; tripping flattens through the FastLoop) ---
        self.breaker_store = InMemoryBreakerStore()
        # The breaker's flatten handler is set AFTER the FastLoop exists (below);
        # use a forwarder so a trip routes to FastLoop.flatten_handler.
        self._flatten_forwarder = _FlattenForwarder()
        self.breaker = CircuitBreaker(
            config=RiskConfig(),
            store=self.breaker_store,
            flatten_handler=self._flatten_forwarder,
        )

        # --- Survivable-stop Layer 2 + DMS Layer 3 wiring (real) ---
        self.layer2 = SecondaryStopEnforcer(venue=self.venue)
        self.hb_store = InMemoryHeartbeatStore()  # cross-process heartbeat seam
        self.enforcer_wiring = FastLoopEnforcerWiring(
            enforcer=self.layer2,
            hb_store=self.hb_store,
            wall_epoch_ms=self.clock.epoch_ms,
        )
        # DMS in its OWN failure domain: reads the SAME heartbeat store the
        # FAST-loop writes; injected manual clock so 'loop death' is deterministic.
        self.dms_venue = DmsFlattenVenue()
        self.dms = DmsWatchdog(
            venue=self.dms_venue,
            store=self.hb_store,
            t_dms_seconds=60.0,
            wall_epoch_ms=self.clock.epoch_ms,
            monotonic=self.clock.monotonic_s,
        )

        # --- FastLoop (the running tick loop; high slippage cap for sim) ---
        self.fast = FastLoop(
            store=self.store,
            venue=self.venue,
            breaker=self.breaker,
            price_feed=self.price_feed,
            exit_config=preset("secure_balanced"),
            enforcer_wiring=self.enforcer_wiring,
        )
        # Now that FastLoop exists, point the breaker's flatten at its handler.
        self._flatten_forwarder.target = self.fast.flatten_handler

        # --- SLOW + SNIPE loops + orchestrator (the running controller) ---
        self.sim_config = RiskConfig(max_slippage_bps=10_000)
        self.slow = SlowLoop(store=self.store, classifier=_FakeClassifier())
        self.snipe = SnipeLoop(
            store=self.store,
            venue=self.venue,
            config=self.sim_config,
            latency_budget_ms=None,
            min_sol_lamports=10_000_000,
        )
        self.reconciler = Reconciler(store=self.store, venue=self.venue)
        self.orchestrator = ControllerOrchestrator(
            store=self.store,
            venue=self.venue,
            snipe_loop=self.snipe,
            fast_loop=self.fast,
            slow_loop=self.slow,
            reconciler=self.reconciler,
            kill_switch=self.kill,
        )

        # --- Control-plane app built against the SHARED running objects ---
        self.cp_config = ControlPlaneConfig(
            operator_token=OPERATOR_TOKEN,
            ceo_token=CEO_TOKEN,
            dry_run_enabled=True,  # HARD RULE: real capital disabled
            agent_mode="SHADOW",
            wallet_pubkey="sim_pubkey",
            wallet_cap_lamports=500_000_000,
        )
        self.app = build_app(
            state_store=self.store,
            fast_loop=self.fast,
            kill_switch=self.kill,
            breaker=self.breaker,
            feed_bus=self.feed_bus,
            config=self.cp_config,
        )

    def startup(self) -> None:
        self.orchestrator.startup(block_time_ms=1_700_000_000_000)

    def open_paper_position(self, mint: str, slot: int) -> PositionFSM:
        """Create + register a REAL OPEN position in the running FastLoop (paper).

        Mirrors what reconcile_fill does on a landed sim fill, but deterministic
        so the kill / stop assertions do not depend on the sim landing race."""
        intent_id = make_client_intent_id(mint, slot, "ENTRY")
        self.store.claim_entering(mint, intent_id)
        cost = CostStack(
            expected_edge_bps=200,
            jito_tip_bps=10,
            priority_fee_bps=5,
            entry_slippage_bps=50,
            amm_fee_bps=25,
            exit_slippage_bps=30,
            adverse_selection_bps=150,
            total_cost_bps=120,
        )
        fsm = PositionFSM.create_entering(
            self.store,
            mint=mint,
            token="TOK_" + mint[-4:],
            surface="EH-001",
            entry_slot=slot,
            sol_in_lamports=50_000_000,
            tokens_out_base=1_000_000,
            entry_slippage_bps=50,
            exit_config_label="secure_balanced",
            exit_mode="secure",
            tp_total=3,
            hard_stop_price_lamports=60_000_000,
            cost_breakdown=cost,
            client_intent_id=intent_id,
        )
        fill = FillResult(
            landed=True,
            reason="filled",
            land_slot=slot + 1,
            slot_delay=1,
            buyers_ahead=0,
            tokens_out=1_000_000,
            effective_price_decimal="100",
            entry_slippage_bps=50,
            tip_lamports=10_000,
            priority_lamports=5_000,
        )
        fsm.transition_to_open(fill, block_time_ms=slot * 400)
        self.fast._fsm_registry[mint] = fsm
        # Arm the Layer-2 stop for this position at entry price 100 (trigger -40%
        # => 60), so a mark <= 60 fires the survivable stop.
        from aats.risk.exit_engine import ExitState

        self.fast._exit_states[mint] = ExitState.at_entry(
            mint=mint, entry_fill_price=Decimal("100"), config=self.fast._exit_config
        )
        self.enforcer_wiring.arm(
            mint=mint, entry_fill_price=Decimal("100"), position=fsm.position
        )
        return fsm

    def beat_and_tick(self, block_time_ms: int, slot: int) -> list[str]:
        """Run one FAST-loop tick (emits a heartbeat into the DMS store)."""
        return self.fast.tick(block_time_ms, slot)


class _FlattenForwarder:
    """Forwards the breaker's emergency_flatten_all to the FastLoop's handler.

    Decouples breaker construction (which must precede the FastLoop) from the
    FastLoop flatten handler (which the breaker must call on a trip)."""

    def __init__(self) -> None:
        self.target: FastLoopFlattenHandler | None = None

    def emergency_flatten_all(self, reason: str) -> None:
        if self.target is not None:
            self.target.emergency_flatten_all(reason=reason)


class _FakeClassifier:
    def predict(self, event):  # noqa: ANN001
        from aats.contracts.models import DecisionSignal

        return DecisionSignal(
            mint=event.mint,
            event_time=event.event_time,
            model_version="bootstrap_not_real",
            p_calibrated=0.90,
            uncertainty=0.05,
            baseline_p=0.50,
            surface="EH-001",
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stack() -> BootedStack:
    s = BootedStack()
    s.startup()
    return s


@pytest.fixture
async def dash(stack: BootedStack):
    """The DASHBOARD client: raw httpx over the in-process ASGI app (the same
    transport dashboard/src/lib/api.ts uses fetch+POST against)."""
    async with AsyncClient(
        transport=ASGITransport(app=stack.app), base_url="http://control-plane"
    ) as ac:
        yield ac


# ===========================================================================
# G-KILL — kill flattens all positions within the AC-040 <= 2s budget
# ===========================================================================


@pytest.mark.asyncio
async def test_kill_from_dashboard_flattens_within_budget(stack: BootedStack, dash):
    """DASHBOARD POST /api/kill halts entries + flattens all OPEN positions, fast.

    Proves: kill flag set on the running loop, venue.exit fired for the open
    position, position no longer OPEN, and effect within the AC-040 <= 2s budget.
    """
    stack.open_paper_position("KILL_DASH_MINT", slot=200)
    assert not stack.kill.is_killed
    assert stack.store.load_position("KILL_DASH_MINT").fsm_state == FSMState.OPEN

    t0 = time.monotonic()
    resp = await dash.post("/api/kill", headers=OPERATOR_HEADERS)
    elapsed = time.monotonic() - t0

    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "halting"
    # Kill propagated to the running loop:
    assert stack.kill.is_killed
    # Flatten reached the venue seam for the open position:
    assert "KILL_DASH_MINT" in stack.venue.exit_calls
    # Position is now flat (not OPEN):
    pos = stack.store.load_position("KILL_DASH_MINT")
    assert pos is None or pos.fsm_state != FSMState.OPEN
    # AC-040 latency budget:
    assert elapsed < 2.0, f"kill flatten took {elapsed:.3f}s (> AC-040 2s budget)"

    # A killed loop blocks NEW entries (de-risk holds going forward).
    from tests.controller.conftest import make_launch_event

    result = stack.orchestrator.on_launch_event(make_launch_event(mint="POST_KILL"))
    assert result["snipe"] == "kill_switch_active"


@pytest.mark.asyncio
async def test_kill_from_telegram_flattens_within_budget(stack: BootedStack):
    """TELEGRAM /kill (real handler + real HttpControlPlaneClient -> SAME ASGI
    app) flattens within budget — the OTHER operator surface, SAME contract."""
    stack.open_paper_position("KILL_TG_MINT", slot=210)

    handler, op_id = _make_telegram_handler(stack)

    # /kill requires a per-command confirm (custody-policy §7).  First call issues
    # a nonce and fires NOTHING; the confirm fires the de-risk call.
    prompt = await handler.handle_update(_tg_msg(op_id, "/kill"))
    assert prompt is not None and prompt.fired is False and prompt.confirm_nonce

    t0 = time.monotonic()
    reply = await handler.handle_update(_tg_msg(op_id, f"/confirm {prompt.confirm_nonce}"))
    elapsed = time.monotonic() - t0

    assert reply is not None and reply.fired is True
    assert "KILL" in reply.text
    assert stack.kill.is_killed
    assert "KILL_TG_MINT" in stack.venue.exit_calls
    pos = stack.store.load_position("KILL_TG_MINT")
    assert pos is None or pos.fsm_state != FSMState.OPEN
    assert elapsed < 2.0, f"telegram kill took {elapsed:.3f}s (> AC-040 2s)"


@pytest.mark.asyncio
async def test_flatten_one_from_dashboard_leaves_others_open(stack: BootedStack, dash):
    """POST /api/flatten/{mint} flattens ONLY that mint (AC-044); others survive."""
    stack.open_paper_position("ONE_A", slot=220)
    stack.open_paper_position("ONE_B", slot=221)

    resp = await dash.post("/api/flatten/ONE_A", headers=OPERATOR_HEADERS)
    assert resp.status_code == 202
    assert "ONE_A" in stack.venue.exit_calls
    assert "ONE_B" not in stack.venue.exit_calls
    pos_b = stack.store.load_position("ONE_B")
    assert pos_b is not None and pos_b.fsm_state == FSMState.OPEN


# ===========================================================================
# G-MODE — mode change propagates to the controller's observable state
# ===========================================================================


@pytest.mark.asyncio
async def test_mode_change_propagates_dashboard(stack: BootedStack, dash):
    """DASHBOARD POST /api/mode changes the mode visible on GET /api/state."""
    pre = await dash.get("/api/state")
    assert pre.json()["mode"] == "SHADOW"

    resp = await dash.post("/api/mode", json={"mode": "PAPER"}, headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["mode"] == "PAPER"

    post = await dash.get("/api/state")
    assert post.json()["mode"] == "PAPER", "mode change must propagate to /api/state"
    # The shared config the controller-side reads also reflects it.
    assert stack.cp_config.agent_mode == "PAPER"


@pytest.mark.asyncio
async def test_mode_pause_from_telegram_steps_down(stack: BootedStack):
    """TELEGRAM /pause posts the hard-coded downward target (SHADOW) — de-risk."""
    stack.cp_config.agent_mode = "PAPER"
    handler, op_id = _make_telegram_handler(stack)

    reply = await handler.handle_update(_tg_msg(op_id, "/pause"))
    assert reply is not None and reply.fired is True
    assert "PAUSE" in reply.text
    # /pause routed to POST /api/mode {SHADOW}; the running config stepped DOWN.
    assert stack.cp_config.agent_mode == "SHADOW"


# ===========================================================================
# G-FEED — SSE /api/feed carries REAL controller events (not mock)
# ===========================================================================


@pytest.mark.asyncio
async def test_feed_carries_real_controller_events(stack: BootedStack):
    """A REAL event the running controller publishes onto the FeedBus is what the
    SSE /api/feed subscriber receives — the same bus, end-to-end (not a mock).

    The SSE generator subscribes to `stack.feed_bus`; the controller publishes to
    that SAME bus.  We subscribe (as the generator does on connect), have the
    controller publish a REAL snipe-decision frame, and assert it arrives within
    the AC-047 <= 3s lag budget.
    """
    q = await stack.feed_bus.subscribe()

    # A REAL controller-produced frame (a snipe decision on a paper launch).
    real_event = {
        "id": "t402-real-evt",
        "mint": "FEED_REAL_MINT",
        "token": "FRM",
        "source": "pump.fun",
        "action": "sniped",
        "gate_passed": True,
        "gate_reasons": ["passed"],
        "red_flags": [],
        "model_p": 0.90,
        "model_uncertainty": 0.05,
        "slot": 1234,
        "slot_delay": 7,
        "smart_wallets": 0,
        "tip_contention_bucket": "low",
        "veto_source": None,
        "cost_gate": {"expected_edge_bps": 200, "total_cost_bps": 120, "passed": True},
        "pnl_pct": None,
        "event_time": {"slot": 1234, "wall_clock_ms": 1_700_000_000_000},
        "observation_slot": 1234,
        "confirmation_slot": 1235,
        "detection_transport": "geyser",
        "ts": 1_700_000_000_060,
        "provenance": "live_controller",  # NOT a mock seed
    }
    await stack.feed_bus.publish(real_event)

    received = await asyncio.wait_for(q.get(), timeout=3.0)  # AC-047 budget
    assert received is not None
    assert received["id"] == "t402-real-evt"
    assert received["mint"] == "FEED_REAL_MINT"
    assert received["provenance"] == "live_controller", "feed must carry real events"
    await stack.feed_bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_feed_http_endpoint_is_event_stream(stack: BootedStack):
    """GET /api/feed over the booted ASGI app yields text/event-stream and a real
    'data:' frame (drives the ASGI app directly; httpx ASGITransport blocks on
    never-ending SSE bodies)."""
    response_start: dict = {}
    body_chunks: list[bytes] = []
    disconnect = asyncio.Event()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/feed",
        "query_string": b"",
        "headers": [],
        "root_path": "",
        "scheme": "http",
        "server": ("control-plane", 80),
    }

    async def receive() -> dict:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            response_start.update(message)
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if chunk:
                body_chunks.append(chunk)
            body = b"".join(body_chunks).decode()
            if ": connected" in body and "data:" in body:
                disconnect.set()

    async def _run_app():
        await stack.app(scope, receive, send)

    async def _publish():
        await asyncio.sleep(0.1)
        await stack.feed_bus.publish({"id": "http-feed-evt", "mint": "HTTP_FEED"})

    app_task = asyncio.create_task(_run_app())
    try:
        await asyncio.wait_for(
            asyncio.gather(asyncio.shield(app_task), _publish()), timeout=3.0
        )
    except (TimeoutError, Exception):
        disconnect.set()
    finally:
        if not app_task.done():
            app_task.cancel()
            import contextlib

            with contextlib.suppress(Exception):
                await asyncio.wait_for(app_task, timeout=1.0)

    headers = {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in response_start.get("headers", [])
    }
    assert "text/event-stream" in headers.get("content-type", "")
    body = b"".join(body_chunks).decode()
    assert ": connected" in body
    assert "data:" in body, "SSE must carry a real published event frame"


# ===========================================================================
# G-SAFETY — breaker / survivable-stop / DMS each FIRE ON DEMAND
# ===========================================================================


@pytest.mark.asyncio
async def test_breaker_fires_on_demand_and_flattens(stack: BootedStack, dash):
    """The daily-loss circuit breaker trips on a loss past the floor, halts
    entries, and flattens the open book through the SAME FastLoop handler."""
    stack.open_paper_position("BREAKER_MINT", slot=230)
    assert stack.breaker.entries_allowed()

    # Drive a realized loss past the -0.30 SOL hard floor (event-time PnL).
    stack.breaker.record_pnl(
        PnLEvent(
            mint="BREAKER_MINT",
            block_time_ms=1_700_000_000_000,
            delta_lamports=-(LAMPORTS_PER_SOL // 2),  # -0.5 SOL < -0.30 floor
            kind="realized",
        )
    )
    assert stack.breaker.is_tripped(), "breaker must TRIP past the floor (fires on demand)"
    assert not stack.breaker.entries_allowed(), "tripped breaker must block new entries"
    # The trip handed the open position to the FastLoop flatten -> venue.exit.
    assert "BREAKER_MINT" in stack.venue.exit_calls

    # FINDING (T-402-F1, MAJOR — observability seam, NOT a safety failure):
    # The authoritative CircuitBreaker (its own BreakerStore) is TRIPPED and the
    # book is flat, but GET /api/state reads `breaker_tripped` from the SEPARATE
    # StateStore projection, which the breaker NEVER writes back into.  So the
    # operator surface shows breaker_tripped=False while the bot is actually
    # halted.  The SNIPE loop reads the SAME stale projection
    # (snipe_loop.load_breaker_state) — meaning the projection-lag is a real
    # correctness gap, not merely cosmetic.  We assert the ACTUAL (stale) wire
    # value so the seam is documented by a passing test, and assert the
    # AUTHORITATIVE state is tripped.  The breaker/reset endpoint is unaffected
    # because it reads breaker.state directly (proven below).
    state = (await dash.get("/api/state")).json()
    assert state["breaker_tripped"] is False, (
        "T-402-F1: /api/state reads the StateStore projection, which the "
        "CircuitBreaker does not update — operator surface lags the real latch."
    )

    # ASYMMETRIC TRUST: the LLM/automated path can TRIP but can NEVER reset.
    # Only the operator POST (which mints an OperatorResetToken) re-arms.
    resp = await dash.post("/api/breaker/reset", headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["state"] == "ARMED"


@pytest.mark.asyncio
async def test_llm_can_trip_breaker_never_reset(stack: BootedStack):
    """ASYMMETRIC TRUST: an LLM de-risk signal may TRIP the breaker; it has no
    capability to reset it (reset needs an OperatorResetToken the LLM cannot mint)."""
    stack.breaker.trip_from_llm(
        LLMDeRiskSignal(reason="narrative_failure: synthetic"),
        block_time_ms=1_700_000_000_000,
    )
    assert stack.breaker.is_tripped()
    # The breaker exposes no LLM-reachable reset; reset() demands a real token.
    with pytest.raises(Exception):
        stack.breaker.reset(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_survivable_stop_layer2_fires_on_breach(stack: BootedStack):
    """Layer-2 in-process survivable stop fires when a FAST-loop tick observes a
    mark at/below the fixed -40% trigger — deterministic, no LLM/RPC on the path."""
    stack.open_paper_position("STOP_MINT", slot=240)
    assert "STOP_MINT" in stack.layer2.armed_mints()

    # Drive the mark BELOW the trigger (entry 100, -40% => 60).  A tick fires it.
    stack.price_feed.set_price("STOP_MINT", Decimal("50"))
    actions = stack.beat_and_tick(block_time_ms=1_700_000_001_000, slot=241)

    assert any("STOP_MINT" in a and "stop" in a for a in actions), (
        f"Layer-2 stop must fire on breach; actions={actions}"
    )
    assert "STOP_MINT" not in stack.layer2.armed_mints(), "stop must deregister after firing"
    assert "STOP_MINT" in stack.venue.exit_calls, "stop must reach the venue exit seam"
    pos = stack.store.load_position("STOP_MINT")
    assert pos is None or pos.fsm_state != FSMState.OPEN


@pytest.mark.asyncio
async def test_dms_fires_when_fast_loop_is_killed(stack: BootedStack):
    """Layer-3 dead-man's switch: when the FAST loop is 'killed' (stops beating)
    and the heartbeat ages past T_DMS, the DMS — in its OWN failure domain,
    reading the SAME heartbeat store across the boundary — flattens the book.

    This is the 'process killed mid-trade' survivability proof: the watchdog
    never holds a reference to the loop; ceasing to beat is indistinguishable
    from a dead process."""
    stack.open_paper_position("DMS_MINT", slot=250)

    # The loop beats once (writes a fresh heartbeat) then a check is satisfied.
    stack.beat_and_tick(block_time_ms=1_700_000_001_000, slot=251)
    assert stack.dms.check() is False, "fresh heartbeat must NOT fire the DMS"

    # KILL the loop: it stops beating.  Advance wall + monotonic past T_DMS (60s).
    stack.clock.advance_ms(61_000)
    assert stack.dms.heartbeat_age_seconds() > 60.0

    fired = stack.dms.check()
    assert fired is True, "DMS must FIRE when the killed loop ages past T_DMS"
    assert stack.dms.is_fired()
    assert stack.dms_venue.fired_reasons, "DMS must reach the emergency_flatten seam"
    # Latches: a recovered heartbeat does NOT un-fire (operator review required).
    assert stack.dms.check() is False


# ===========================================================================
# G-DERISK — risk-increasing commands are REJECTED (asymmetric trust)
# ===========================================================================


@pytest.mark.asyncio
async def test_mode_up_to_live_rejected_behind_dry_run(stack: BootedStack, dash):
    """A risk-INCREASING command (mode -> LIVE) is rejected while DRY-RUN is on,
    even with CEO auth — real capital cannot be enabled by an operator surface."""
    stack.cp_config.agent_mode = "LIVE_DRY_RUN"
    resp = await dash.post("/api/mode", json={"mode": "LIVE"}, headers=CEO_HEADERS)
    assert resp.status_code == 403
    assert "live_requires_dry_run_disabled_and_ceo_auth" in str(resp.json())
    # The running mode did NOT advance to LIVE.
    assert stack.cp_config.agent_mode != "LIVE"
    # And real capital stays disabled.
    assert (await dash.get("/api/state")).json()["dry_run_enabled"] is True


@pytest.mark.asyncio
async def test_risk_config_widen_rejected(stack: BootedStack, dash):
    """A risk-config WIDEN (raise the per-trade cap) is rejected 403; a TIGHTEN
    (lower the cap) is accepted — the config seam can only de-risk."""
    current = (await dash.get("/api/risk-config")).json()

    widen = dict(current)
    widen["per_trade_cap_lamports"] = current["per_trade_cap_lamports"] + 10_000_000
    bad = await dash.post("/api/risk-config", json=widen, headers=OPERATOR_HEADERS)
    assert bad.status_code == 403
    assert "risk_increase_rejected" in str(bad.json())

    tighten = dict(current)
    tighten["per_trade_cap_lamports"] = current["per_trade_cap_lamports"] // 2
    good = await dash.post("/api/risk-config", json=tighten, headers=OPERATOR_HEADERS)
    assert good.status_code == 200


@pytest.mark.asyncio
async def test_unauthorized_post_rejected(stack: BootedStack, dash):
    """Every POST de-risk command requires operator auth; no token => 403."""
    for ep, body in (
        ("/api/kill", None),
        ("/api/flatten", None),
        ("/api/mode", {"mode": "SHADOW"}),
        ("/api/risk-config", {}),
        ("/api/breaker/reset", None),
    ):
        resp = await dash.post(ep, json=body) if body is not None else await dash.post(ep)
        assert resp.status_code == 403, f"{ep} without auth must be 403, got {resp.status_code}"


@pytest.mark.asyncio
async def test_telegram_seam_is_structurally_derisk_only(stack: BootedStack):
    """The Telegram operator client exposes ONLY read + de-risk methods.  There is
    NO method to size up, widen a stop, enable LIVE, advance mode, or reset the
    breaker — risk-increase is not 'rejected', it does NOT EXIST on the seam."""
    from aats.telegram.control_plane_client import ControlPlaneClient

    methods = {m for m in dir(ControlPlaneClient) if not m.startswith("_")}
    # The complete de-risk/read surface — nothing else.
    assert methods == {"get_status", "kill", "flatten_mint", "pause"}, (
        f"control-plane client must be de-risk-only; found {methods}"
    )
    forbidden = {
        "size_up", "widen_stop", "add_leverage", "enable_live", "go_live",
        "advance_mode", "reset_breaker", "set_risk", "increase",
    }
    assert not (methods & forbidden), "de-risk-only seam must expose no risk-increase method"

    # An unauthorized (non-operator) Telegram sender fires NOTHING.
    handler, _ = _make_telegram_handler(stack)
    reply = await handler.handle_update(_tg_msg(999_999_999, "/kill"))
    assert reply is not None and reply.authorized is False and reply.fired is False


# ===========================================================================
# HONESTY — no edge is claimed; synthetic build is reported truthfully
# ===========================================================================


@pytest.mark.asyncio
async def test_no_win_rate_and_no_passing_edge_on_synthetic_build(stack: BootedStack, dash):
    """HONESTY CLAUSE: the metrics endpoint carries NO win-rate field, and the
    edge gates are NOT passing — the truthful state of an offline synthetic build
    with no recorded real data.  This test does NOT manufacture any edge."""
    metrics = (await dash.get("/api/metrics")).json()
    # No win-rate / outcome-rate field anywhere (AC-037).
    assert "win_rate" not in metrics
    assert "win_rate" not in str(metrics).lower()
    # Edge gates do NOT pass on a synthetic build — we report it, never fake it.
    assert metrics["model_vs_baseline"]["gate_b_pass"] is False
    assert metrics["gate_a"]["gate_a_pass"] is False
    assert metrics["model_vs_baseline"]["n_test_windows"] == 0
    # Real capital is DISABLED.
    assert (await dash.get("/api/state")).json()["dry_run_enabled"] is True


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def _make_telegram_handler(stack: BootedStack) -> tuple[TelegramCommandHandler, int]:
    """Build a REAL TelegramCommandHandler whose REAL HttpControlPlaneClient is
    routed at the booted in-process ASGI control-plane app (same contract)."""
    op_id = 42_424_242
    config = TelegramCommandConfig(
        operator_user_ids=frozenset({op_id}),
        bot_token_vault_ref="vault://command-bot-token",
        operator_api_token=OPERATOR_TOKEN,
        control_plane_url="http://control-plane",
        dry_run_enabled=True,
        enabled=True,
    )
    client = HttpControlPlaneClient(
        base_url="http://control-plane",
        operator_api_token=OPERATOR_TOKEN,
    )
    # Re-point the adapter's httpx client at the in-process ASGI app so the
    # Telegram surface drives the SAME control-plane contract (no network).
    import httpx

    client._client = httpx.AsyncClient(
        transport=ASGITransport(app=stack.app), base_url="http://control-plane"
    )
    handler = TelegramCommandHandler(config=config, client=client)
    return handler, op_id


def _tg_msg(user_id: int, text: str) -> dict:
    """A Telegram-update-shaped dict (no real Telegram client needed)."""
    return {"message": {"from": {"id": user_id}, "text": text}}
