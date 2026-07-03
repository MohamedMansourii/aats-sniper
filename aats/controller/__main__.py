"""M3 Controller — Live paper-bot runner.

USAGE
-----
    python -m aats.controller

    # Short bounded run (12 seconds):
    python -m aats.controller --max-ticks 120 --launch-interval 3.0

HONESTY NOTICE
--------------
This runner uses a SYNTHETIC launch generator.  Every LaunchEvent it emits is
clearly labelled SYNTHETIC / paper-only and carries NO real edge signal.
It exercises the full controller pipeline (SNIPE -> FAST -> SLOW loops) against
SimulationVenue so the dashboard displays live (simulated) activity.

HARD RULES (non-waivable):
  - DRY_RUN_ENABLED defaults to true; NO real submission, ever.
  - No private keys, no real wallet interaction.
  - No float money (all lamports are int, all Decimals stay Decimal).
  - No win-rate field anywhere.
  - Synthetic events are CLEARLY LABELLED at generation time.
  - Redis is OPTIONAL: absent -> in-process InMemoryStateStore (same correctness;
    FeedBus events publish in-process only, not to Redis stream).

CONTROL-PLANE
-------------
The runner starts the FastAPI control plane on http://localhost:8765 so the
dashboard's GET /api/state, GET /api/feed (SSE), and GET /api/positions reflect
live simulated activity.  Override with CONTROL_PLANE_PORT env var.

Redis URL is read from REDIS_URL env var (default redis://localhost:6379/0).
If Redis is not reachable the runner falls back to in-process state with a
logged warning — no crash, no hang.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import logging
import os
import random
import sys
import time
from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------------------
# Logging — structured, level from LOG_LEVEL env (default INFO)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("aats.controller.__main__")

# ---------------------------------------------------------------------------
# Hard-gate: DRY_RUN_ENABLED
# ---------------------------------------------------------------------------

DRY_RUN_ENABLED: bool = os.environ.get("DRY_RUN_ENABLED", "true").lower() != "false"

if not DRY_RUN_ENABLED:
    # Belt-and-suspenders: this runner is paper-only; refuse to proceed
    # if someone accidentally points a live env at it.
    log.critical(
        "DRY_RUN_ENABLED=false detected in controller/__main__.py — "
        "this runner is PAPER-ONLY and refuses to run without DRY_RUN_ENABLED=true. "
        "Set DRY_RUN_ENABLED=true to proceed."
    )
    sys.exit(1)

log.info("DRY_RUN_ENABLED=true — paper-only mode confirmed, no real submission possible.")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CONTROL_PLANE_PORT: int = int(os.environ.get("CONTROL_PLANE_PORT", "8765"))
FAST_TICK_INTERVAL_S: float = float(os.environ.get("FAST_TICK_INTERVAL_S", "0.1"))   # 100 ms
LAUNCH_INTERVAL_S: float = float(os.environ.get("LAUNCH_INTERVAL_S", "5.0"))

# ---------------------------------------------------------------------------
# Optional Redis client — in-process fallback if absent
# ---------------------------------------------------------------------------


def _try_connect_redis() -> object | None:
    """Try to connect to Redis; return the client or None."""
    try:
        import redis.asyncio as aioredis  # type: ignore[import]
        client = aioredis.from_url(REDIS_URL, decode_responses=False, socket_connect_timeout=2)
        return client
    except ImportError:
        log.warning("redis package not installed — using in-process InMemoryStateStore only.")
        return None
    except Exception as exc:
        log.warning("Redis connection attempt failed (%s) — using in-process fallback.", exc)
        return None


# ---------------------------------------------------------------------------
# SYNTHETIC launch-event generator
# CLEARLY LABELLED: these events carry NO real edge data.
# ---------------------------------------------------------------------------

# Slot and time anchors — synthetic, clearly not real chain data.
_BASE_SLOT = 350_000_000
_BASE_BLOCK_TIME_MS = 1_718_700_000_000  # synthetic, not real on-chain time

# Each call increments these so we get distinct, monotonically increasing events.
_slot_counter = _BASE_SLOT
_block_time_counter = _BASE_BLOCK_TIME_MS

# Program IDs are synthetic labels — NOT real on-chain program IDs.
# The ONLY place real program IDs appear is config/program-allowlist.json,
# loaded by ProgramRegistry at startup.  This generator never uses that file.
_SYNTHETIC_PROGRAM_ID = "SYNTHETICprogramID1111111111111111111111111111"


def _next_slot_and_time() -> tuple[int, int]:
    """Return (slot, block_time_ms) for the next synthetic event, advancing both."""
    global _slot_counter, _block_time_counter
    _slot_counter += random.randint(1, 5)
    # ~400 ms / slot
    _block_time_counter += random.randint(400, 600) * random.randint(1, 5)
    return _slot_counter, _block_time_counter


def make_synthetic_launch_event(rng: random.Random) -> Any:  # noqa: ANN401
    """Produce one SYNTHETIC LaunchEvent.

    SYNTHETIC / PAPER-ONLY: this event has NO real edge signal.
    It is used solely to exercise the controller pipeline in paper mode.
    """
    from aats.contracts.events import (
        DetectionTransport,
        EventTime,
        LaunchEvent,
        LaunchSource,
    )

    slot, block_time_ms = _next_slot_and_time()
    wall_ms = int(time.monotonic_ns() // 1_000_000)

    # Vary the pool size to make sim fills more interesting
    sol_reserve_lamports = rng.randint(50_000_000, 300_000_000)   # 0.05 – 0.3 SOL (int)
    token_reserve_base = rng.randint(500_000_000, 2_000_000_000)  # 500M – 2B tokens (int)

    # mint: synthetic address, clearly labeled SYNTHETIC
    mint = f"SYNTHETIC{slot:012d}{rng.randint(100, 999)}"

    event = LaunchEvent(
        mint=mint,
        venue_program_id=_SYNTHETIC_PROGRAM_ID,
        source=LaunchSource.PUMPFUN,
        event_time=EventTime(
            slot=slot,
            block_time_ms=block_time_ms,
            wall_clock_ms=wall_ms,
        ),
        observation_slot=slot,
        confirmation_slot=slot,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=sol_reserve_lamports,
        token_reserve_base=token_reserve_base,
        token_decimals=6,
        initial_holders=1,
        competitors=rng.randint(0, 5),
        creator_wallet=f"SYNTHETICcreator{slot:012d}",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=rng.randint(10, 200),  # synthetic staleness
    )
    return event


# ---------------------------------------------------------------------------
# Stub classifier — produces synthetic DecisionSignals (paper only)
# ---------------------------------------------------------------------------


class _SyntheticClassifier:
    """SYNTHETIC classifier: produces plausible DecisionSignals for paper runs.

    NOT a real model.  Signal is pseudo-random with a bias toward the
    snipe_threshold so some launches fire and some are skipped, making the
    dashboard display realistic activity.

    The per-mint seed uses a stable SHA-256 digest so replays produce the
    same output regardless of PYTHONHASHSEED.
    """

    def predict(self, event: Any) -> Any:
        from aats.contracts.models import DecisionSignal

        # Stable per-mint seed: SHA-256 truncated to 32 bits; immune to PYTHONHASHSEED.
        _seed = int.from_bytes(
            hashlib.sha256(event.mint.encode()).digest()[:4], "big"
        )
        rng = random.Random(_seed)
        p = round(rng.gauss(0.60, 0.15), 4)
        p = max(0.0, min(1.0, p))
        uncertainty = round(abs(rng.gauss(0.10, 0.05)), 4)
        uncertainty = max(0.0, min(1.0, uncertainty))

        return DecisionSignal(
            mint=event.mint,
            event_time=event.event_time,
            model_version="synthetic-paper-v0",
            p_calibrated=p,
            uncertainty=uncertainty,
            baseline_p=0.50,
            surface="EH-001",
        )


# ---------------------------------------------------------------------------
# Stub mark-price provider — deterministic per (mint, slot)
# ---------------------------------------------------------------------------


class _SyntheticMarkPriceProvider:
    """SYNTHETIC price provider: returns a drifting simulated price.

    NOT a real price feed.  Used only to drive the ExitEngine and unrealized PnL
    calculations in the FAST loop during paper runs.
    """

    def get_mark_price(self, mint: str, event_slot: int) -> Decimal | None:
        # Stable seed: SHA-256 of "mint:bucket" — immune to PYTHONHASHSEED.
        _key = f"{mint}:{event_slot // 20}".encode()
        _seed = int.from_bytes(hashlib.sha256(_key).digest()[:4], "big")
        rng = random.Random(_seed)
        # Drift upward with noise — 0.5–2.5 lamports/token (wildly small minted token)
        base = Decimal("1.0")
        drift = Decimal(str(round(rng.uniform(0.5, 2.5), 6)))
        return base * drift


# ---------------------------------------------------------------------------
# FeedBus publisher adapter — publishes to FeedBus (and optionally Redis stream)
# ---------------------------------------------------------------------------


async def _publish_snipe_event(
    feed_bus: Any,
    bus_producer: Any,
    event_type: str,
    data: dict,
) -> None:
    """Publish a control-plane event to the FeedBus (and Redis ops.feed if available).

    The FeedBus is the in-process SSE delivery mechanism.
    If a BusProducer (Redis-backed) is provided, we also write to Redis ops.feed.
    """
    frame: dict = {
        "type": event_type,
        "ts": int(time.time() * 1000),
        **data,
    }
    await feed_bus.publish(frame)

    if bus_producer is not None:
        try:
            await bus_producer.publish_ops_event(event_type, data)
        except Exception as exc:
            # Non-fatal: Redis down during SSE publish should not crash the loop.
            log.debug("ops.feed Redis publish failed: %s", exc)


# ---------------------------------------------------------------------------
# Main async runner
# ---------------------------------------------------------------------------


async def _run(
    *,
    max_ticks: int | None = None,
    launch_interval_s: float = LAUNCH_INTERVAL_S,
    fast_tick_s: float = FAST_TICK_INTERVAL_S,
) -> None:
    """Assemble the controller and drive the paper loop.

    Architecture (BLUEPRINT §4.2):
      - InMemoryStateStore (or RedisStateStore if Redis available)
      - SimulationVenue (DRY_RUN; submit_mode=SIMULATION, no network)
      - SnipeLoop + SlowLoop + FastLoop + Reconciler + ControllerOrchestrator
      - FeedBus + control-plane FastAPI served on CONTROL_PLANE_PORT
      - Synthetic launch generator fires every launch_interval_s seconds

    The three loops run as asyncio tasks so they share one event loop and
    there are no thread-racing concerns (all state store operations are
    synchronous / lock-protected InMemoryStateStore methods).
    """
    from aats.contracts.risk import RiskConfig
    from aats.contracts.venue import SimulationVenue
    from aats.control_plane.server import ControlPlaneConfig, FeedBus, build_app
    from aats.controller.control_api import KillSwitch
    from aats.controller.enrichment_wiring import SlowLoopEnrichmentWiring
    from aats.controller.fast_loop import FastLoop
    from aats.controller.loops import ControllerOrchestrator
    from aats.controller.reconcile import Reconciler
    from aats.controller.slow_loop import SlowLoop
    from aats.controller.snipe_loop import SnipeLoop
    from aats.controller.state import InMemoryStateStore
    from aats.execution.sell_sim import SellSimVenue
    from aats.ingestion.insider_dump import InsiderDumpDetector
    from aats.risk.circuit_breaker import (
        CircuitBreaker,
        InMemoryBreakerStore,
    )
    from aats.risk.exit_engine import ExitConfig, TpRung
    from aats.risk.lp_unlock_gate import LpUnlockExitWatcher
    from aats.risk.sellability_reprobe import SellabilityReprober

    # ------------------------------------------------------------------
    # 1. Redis (optional) + StateStore
    # ------------------------------------------------------------------
    redis_client: Any = _try_connect_redis()

    # Verify Redis connectivity asynchronously
    if redis_client is not None:
        try:
            await asyncio.wait_for(redis_client.ping(), timeout=2.0)
            log.info("Redis connected: %s", REDIS_URL)
        except Exception as exc:
            log.warning(
                "Redis ping failed (%s) — falling back to InMemoryStateStore.", exc
            )
            redis_client = None

    state_store = InMemoryStateStore()
    bus_producer: Any = None

    if redis_client is not None:
        # Attempt to wire BusProducer for ops.feed Redis stream
        try:
            from aats.ingestion.bus import BusProducer
            bus_producer = BusProducer(redis_client)
            log.info("BusProducer wired to Redis ops.feed stream.")
        except Exception as exc:
            log.warning("BusProducer wiring failed: %s — proceeding without Redis stream.", exc)

    # ------------------------------------------------------------------
    # 2. Simulation Venue (paper only; submit_mode=SIMULATION; no network)
    # ------------------------------------------------------------------
    rng = random.Random(42)
    venue = SimulationVenue(rng=rng)
    log.info("SimulationVenue initialised (submit_mode=%s)", venue.submit_mode)

    # ------------------------------------------------------------------
    # 3. Risk components
    # ------------------------------------------------------------------
    config = RiskConfig()

    # Circuit breaker (daily-loss guard)
    kill_switch = KillSwitch()
    breaker_store = InMemoryBreakerStore()

    # FlattenHandler shim: we wire the real flatten_handler once the FastLoop exists.
    # Use a simple wrapper that defers the call.
    class _DeferredFlattenHandler:
        """Thin shim so CircuitBreaker can be constructed before FastLoop exists."""
        _handler: Any = None

        def emergency_flatten_all(self, reason: str) -> None:
            if self._handler is not None:
                self._handler.emergency_flatten_all(reason)
            else:
                log.warning("flatten_all requested before FastLoop ready: reason=%s", reason)

    deferred_flatten = _DeferredFlattenHandler()
    breaker = CircuitBreaker(
        config=config,
        store=breaker_store,
        flatten_handler=deferred_flatten,
    )

    # Exit config — simple TP ladder + trailing stop (paper preset)
    exit_config = ExitConfig(
        label="paper-default",
        tp_ladder=(
            TpRung(trigger_r=Decimal("1.5"), fraction_bps=2000),  # +50%: sell 20%
            TpRung(trigger_r=Decimal("2.0"), fraction_bps=3000),  # +100%: sell 30%
            TpRung(trigger_r=Decimal("3.0"), fraction_bps=4000),  # +200%: sell 40%
        ),
        # Cumulative: 2000+3000+4000 = 9000 bps <= 10000 OK
        hard_stop_r=Decimal("0.60"),     # -40% hard stop
        trail_arm_r=Decimal("2.0"),       # arm trailing once +100%
        trail_drawdown_bps=2500,          # 25% trail drawdown
        max_hold_steps=500,               # timeout safety
        ratchet_ladder=(),
    )

    # ------------------------------------------------------------------
    # 4. Loops
    # ------------------------------------------------------------------

    # Mark-price provider (synthetic)
    price_feed = _SyntheticMarkPriceProvider()

    fast_loop = FastLoop(
        store=state_store,
        venue=venue,
        breaker=breaker,
        price_feed=price_feed,
        exit_config=exit_config,
        tick_budget_ms=100,
        hard_stop_budget_ms=50,
        cooldown_seconds=30,
    )

    # Wire the deferred flatten handler to the real one now that FastLoop exists.
    deferred_flatten._handler = fast_loop.flatten_handler

    slow_loop = SlowLoop(
        store=state_store,
        classifier=_SyntheticClassifier(),
        reasoner=None,          # no LLM in paper mode
        signal_ttl_seconds=30,
        veto_ttl_seconds=60,
        narrative_ttl_seconds=120,
    )

    snipe_loop = SnipeLoop(
        store=state_store,
        venue=venue,
        config=config,
        gate=None,              # no pre-trade gate in paper mode
        sizing=None,            # fallback min-size
        cost_model=None,        # fallback cost stack
        default_tip_lamports=200_000,
        default_cu_price_microlamports=10_000,
        latency_budget_ms=None,  # no wall-clock budget in paper mode
        cooldown_seconds=30,
        min_sol_lamports=10_000_000,
        wallet_id="paper",
    )

    reconciler = Reconciler(
        store=state_store,
        venue=venue,
        venue_state=None,       # NullVenueStateProvider in sim
        entering_ttl_seconds=30,
    )

    # ------------------------------------------------------------------
    # 4b. Enrichment-tier flag PRODUCERS (2026-07-03 program-review fix).
    #
    # Wires the E14 insider-dump / E17 sellability-reprobe / E19 lp-unlock-
    # approaching catastrophic-exit flags into the RUNNING paper loop, off
    # the FAST/SNIPE hot path.  See aats/controller/enrichment_wiring.py for
    # the full "REFUSE-BY-DEFAULT vs. NOT YET WIRED" rationale.
    #
    #   * E17 (sellability re-probe): fully LIVE — SellSimVenue() defaults to
    #     an offline MockRpcClient/MockSignerClient (DRY-RUN, no network).
    #   * E14 (insider dump): the detector is LIVE and wired, but there is
    #     currently NO on-chain SELL-event feed subscribed in this paper
    #     runner (PumpPortalTransport only emits CREATE/WITHDRAW) — TODO(M1):
    #     subscribe to trade events and forward decoded SELLs through
    #     orchestrator.on_decoded_sell().
    #   * E19 (LP-unlock): the watcher is LIVE and wired, but with NO schedule
    #     source (no on-chain LP-locker decoder exists in this codebase yet) —
    #     it cleanly no-ops (counted via stats.lp_unlock_schedule_absent_skipped)
    #     until TODO(M1) wires a real LpUnlockScheduleSource.
    # ------------------------------------------------------------------
    insider_dump_detector = InsiderDumpDetector(flag_sink=state_store)
    sellability_reprober = SellabilityReprober(probe=SellSimVenue(), flag_sink=state_store)
    lp_unlock_watcher = LpUnlockExitWatcher(flag_sink=state_store)
    enrichment = SlowLoopEnrichmentWiring(
        store=state_store,
        insider_dump_detector=insider_dump_detector,
        sellability_reprober=sellability_reprober,
        lp_unlock_watcher=lp_unlock_watcher,
        lp_unlock_schedule_source=None,  # TODO(M1): no LP-locker decoder yet
    )
    log.info(
        "enrichment wiring: E17 sellability_reprobe=LIVE (offline DRY_RUN sell-sim); "
        "E14 insider_dump=WIRED_NO_FEED (no live SELL subscription yet — TODO M1); "
        "E19 lp_unlock_watch=WIRED_NO_SOURCE (no LP-locker decoder yet — TODO M1)."
    )

    orchestrator = ControllerOrchestrator(
        store=state_store,
        venue=venue,
        snipe_loop=snipe_loop,
        fast_loop=fast_loop,
        slow_loop=slow_loop,
        reconciler=reconciler,
        kill_switch=kill_switch,
        enrichment=enrichment,
    )

    # ------------------------------------------------------------------
    # 5. FeedBus + control-plane FastAPI
    # ------------------------------------------------------------------
    feed_bus = FeedBus()

    cp_config = ControlPlaneConfig(
        operator_token=os.environ.get("OPERATOR_TOKEN", "dev-token"),
        ceo_token=os.environ.get("CEO_TOKEN", ""),
        dry_run_enabled=True,
        agent_mode="PAPER",
        wallet_pubkey="sim_pubkey_paper",
        wallet_cap_lamports=500_000_000,
    )

    app = build_app(
        state_store=state_store,
        fast_loop=fast_loop,
        kill_switch=kill_switch,
        breaker=breaker,
        feed_bus=feed_bus,
        config=cp_config,
    )

    # ------------------------------------------------------------------
    # 6. Startup reconciliation
    # ------------------------------------------------------------------
    startup_block_time_ms = int(time.time() * 1000)
    # block_time_ms must be > 0; use a real wall-clock value as the startup anchor.
    # This is startup-only and is not used for any trading decision.
    actions = orchestrator.startup(block_time_ms=startup_block_time_ms)
    log.info("Startup reconciliation complete: %d actions taken: %s", len(actions), actions)

    # ------------------------------------------------------------------
    # Shared coordination variables — declared before the task closures that
    # reference them so mypy can see the binding in the enclosing scope.
    # ------------------------------------------------------------------
    _shutdown_event: asyncio.Event = asyncio.Event()
    _tick_count_shared: int = 0  # updated by fast_loop_task; read by state_publisher
    _current_slot_shared: int = _BASE_SLOT  # updated by fast_loop_task; read by slow_enrichment_task

    # ------------------------------------------------------------------
    # 7. Launch the FAST-loop asyncio task
    # ------------------------------------------------------------------
    async def fast_loop_task() -> None:
        """Drive the FAST loop at ~100 ms ticks (event-triggered; no LLM)."""
        current_slot = _BASE_SLOT
        tick_count = 0
        log.info("FAST loop started (tick_interval=%.0fms)", fast_tick_s * 1000)

        nonlocal _tick_count_shared, _current_slot_shared
        while not _shutdown_event.is_set():
            tick_start = time.monotonic()

            # Advance slot by a random 0-2 slots per tick (sim clock)
            current_slot += random.randint(0, 2)
            block_time_ms = startup_block_time_ms + (current_slot - _BASE_SLOT) * 400

            tick_result = orchestrator.tick(
                block_time_ms=block_time_ms,
                current_slot=current_slot,
            )

            tick_count += 1
            _tick_count_shared = tick_count
            _current_slot_shared = current_slot

            # Publish heartbeat / tick to FeedBus (non-blocking)
            open_positions = state_store.load_all_positions()
            await _publish_snipe_event(
                feed_bus,
                bus_producer,
                "fast_tick",
                {
                    "tick": tick_count,
                    "slot": current_slot,
                    "open_positions": len(open_positions),
                    "fast_actions": tick_result.get("fast_actions", []),
                    "synthetic": True,   # LABELLED: synthetic paper data
                },
            )

            if max_ticks is not None and tick_count >= max_ticks:
                log.info("FAST loop: reached max_ticks=%d — initiating shutdown.", max_ticks)
                _shutdown_event.set()
                break

            elapsed = time.monotonic() - tick_start
            sleep_s = max(0.0, fast_tick_s - elapsed)
            await asyncio.sleep(sleep_s)

        log.info("FAST loop task exited after %d ticks.", tick_count)

    # ------------------------------------------------------------------
    # 8. Launch event generator (SYNTHETIC, clearly labelled)
    # ------------------------------------------------------------------
    async def launch_generator_task() -> None:
        """Emit synthetic LaunchEvents on a timer; drive SLOW then SNIPE loops.

        SYNTHETIC / PAPER-ONLY: Every event carries NO real edge data.
        """
        launch_rng = random.Random(99)
        launch_count = 0
        log.info(
            "SYNTHETIC launch generator started (interval=%.1fs) — "
            "PAPER-ONLY, NO real edge data.",
            launch_interval_s,
        )

        await asyncio.sleep(0.5)  # Let FAST loop tick once first

        while not _shutdown_event.is_set():
            event = make_synthetic_launch_event(launch_rng)
            launch_count += 1

            log.info(
                "[SYNTHETIC] launch #%d mint=%s sol_reserve=%d token_reserve=%d "
                "(PAPER-ONLY — not real on-chain data)",
                launch_count,
                event.mint,
                event.sol_reserve_lamports,
                event.token_reserve_base,
            )

            try:
                result = orchestrator.on_launch_event(event)

                signal = result.get("signal")
                snipe = result.get("snipe")

                # Publish to FeedBus so dashboard SSE receives the event.
                # getattr defaults handle both FillResult objects and str skip reasons
                # without hasattr branching, which mypy cannot narrow through.
                fill_landed: bool = getattr(snipe, "landed", False)
                fill_tokens: int = getattr(snipe, "tokens_out", 0)

                await _publish_snipe_event(
                    feed_bus,
                    bus_producer,
                    "snipe_event",
                    {
                        "mint": event.mint,
                        "launch_count": launch_count,
                        "slot": event.event_time.slot,
                        "block_time_ms": event.event_time.block_time_ms,
                        "sol_reserve_lamports": event.sol_reserve_lamports,
                        "token_reserve_base": event.token_reserve_base,
                        "signal_p": round(signal.p_calibrated, 4) if signal else None,
                        "signal_surface": signal.surface if signal else None,
                        "snipe_result": (
                            snipe if isinstance(snipe, str)
                            else ("filled" if fill_landed else "no_fill")
                        ),
                        "fill_landed": fill_landed,
                        "fill_tokens_out": fill_tokens,
                        "open_positions": len(state_store.load_all_positions()),
                        "synthetic": True,  # LABELLED: SYNTHETIC PAPER DATA
                    },
                )

                log.info(
                    "[SYNTHETIC] launch #%d result: snipe=%s open_positions=%d",
                    launch_count,
                    (snipe if isinstance(snipe, str) else getattr(snipe, "reason", "??")),
                    len(state_store.load_all_positions()),
                )

            except Exception as exc:
                log.error(
                    "[SYNTHETIC] launch #%d error: %s", launch_count, exc, exc_info=True
                )

            await asyncio.sleep(launch_interval_s)

        log.info("SYNTHETIC launch generator exited after %d launches.", launch_count)

    # ------------------------------------------------------------------
    # 9. State publisher — periodically pushes /api/state snapshot to FeedBus
    # ------------------------------------------------------------------
    async def state_publisher_task() -> None:
        """Publish periodic state snapshots so SSE clients see heartbeats."""
        while not _shutdown_event.is_set():
            await asyncio.sleep(2.0)  # every 2 seconds
            try:
                bs = state_store.load_breaker_state()
                hb_age = state_store.get_heartbeat_age_ms()
                positions = state_store.load_all_positions()
                await _publish_snipe_event(
                    feed_bus,
                    bus_producer,
                    "state_snapshot",
                    {
                        "mode": cp_config.agent_mode,
                        "dry_run_enabled": True,
                        "killed": kill_switch.is_killed,
                        "breaker_tripped": bs is not None and bs.state == "TRIPPED",
                        "heartbeat_age_ms": hb_age,
                        "open_positions": len(positions),
                        "fast_ticks": _tick_count_shared,
                        "synthetic": True,  # LABELLED
                    },
                )
            except Exception as exc:
                log.debug("state_publisher error: %s", exc)

        log.info("State publisher task exited.")

    # ------------------------------------------------------------------
    # 9b. SLOW-cadence enrichment task — drives the E17/E19 flag producers.
    #
    # OFF the FAST hot path by construction: this is a SEPARATE periodic task
    # (every SLOW_ENRICHMENT_INTERVAL_S seconds), never called from
    # fast_loop_task().  See orchestrator.run_slow_enrichment_tick() /
    # aats/controller/enrichment_wiring.py.
    # ------------------------------------------------------------------
    async def slow_enrichment_task() -> None:
        """Periodically re-probe sellability (E17) + check LP-unlock proximity
        (E19) for every OPEN position, at SLOW-loop cadence (seconds-to-minutes).
        """
        interval_s = float(os.environ.get("SLOW_ENRICHMENT_INTERVAL_S", "5.0"))
        log.info("Slow-enrichment task started (interval=%.1fs)", interval_s)
        while not _shutdown_event.is_set():
            await asyncio.sleep(interval_s)
            if not state_store.load_all_positions():
                continue  # nothing open — skip the probe entirely (no-op, cheap)
            try:
                result = orchestrator.run_slow_enrichment_tick(
                    event_slot=_current_slot_shared
                )
                sellability_results = result.get("sellability", [])
                lp_unlock_results = result.get("lp_unlock", [])
                degraded = [r for r in sellability_results if getattr(r, "degraded", False)]
                approaching = [
                    d for d in lp_unlock_results if getattr(d, "raise_flag", False)
                ]
                if degraded or approaching:
                    log.warning(
                        "slow_enrichment: %d sellability_degraded, %d lp_unlock_approaching "
                        "(flags set -> next FAST tick force-exits SECURE)",
                        len(degraded),
                        len(approaching),
                    )
            except Exception as exc:
                # Non-fatal: an enrichment-tier failure must never crash the
                # controller (it is de-risk-only and off the hot path).
                log.error("slow_enrichment_task error: %s", exc, exc_info=True)

        log.info("Slow-enrichment task exited.")

    # ------------------------------------------------------------------
    # 10. Control-plane HTTP server (uvicorn)
    # ------------------------------------------------------------------
    async def control_plane_task() -> None:
        """Serve the FastAPI control plane."""
        try:
            import uvicorn  # type: ignore[import]
        except ImportError:
            log.warning(
                "uvicorn not installed — control plane will NOT be served. "
                "Install uvicorn to enable the dashboard API."
            )
            # Wait until shutdown so this task doesn't exit immediately
            await _shutdown_event.wait()
            return

        if app is None:
            log.warning("FastAPI app could not be built — control plane disabled.")
            await _shutdown_event.wait()
            return

        uvicorn_config = uvicorn.Config(
            app=app,
            host="0.0.0.0",
            port=CONTROL_PLANE_PORT,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(uvicorn_config)
        log.info(
            "Control plane starting on http://0.0.0.0:%d "
            "(GET /api/state, GET /api/feed, GET /api/positions)",
            CONTROL_PLANE_PORT,
        )

        # Serve uvicorn on the SAME asyncio event loop via server.serve() (async).
        # This keeps FeedBus asyncio.Queue/Lock operations single-loop so
        # SSE subscriber wakeups from publish() are always on the same loop.
        # (R-02 / RUNNER-BLK-01: run_in_executor would give uvicorn its own loop,
        # making cross-loop queue put_nowait silently drop SSE frames.)
        serve_task = asyncio.create_task(server.serve(), name="uvicorn_serve")

        # Wait until shutdown is requested, then signal uvicorn to stop.
        await _shutdown_event.wait()
        server.should_exit = True
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(serve_task, timeout=5.0)
        log.info("Control plane stopped.")

    # ------------------------------------------------------------------
    # 11. (Coordination variables declared earlier before task closures)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 11b. Feed source selector — read at runtime so tests can monkeypatch
    #      AATS_FEED_SOURCE before calling _run().
    # ------------------------------------------------------------------
    feed_source: str = os.environ.get("AATS_FEED_SOURCE", "synthetic").lower()
    if feed_source not in ("synthetic", "pumpportal"):
        log.warning(
            "AATS_FEED_SOURCE=%r not recognised; falling back to 'synthetic'. "
            "Valid values: 'synthetic' (default) | 'pumpportal'.",
            feed_source,
        )
        feed_source = "synthetic"

    # ------------------------------------------------------------------
    # 11c. PumpPortal feed task — real mainnet launches in SHADOW/PAPER mode.
    #
    # HARD RULES (all enforced by this task):
    #   - SimulationVenue only; NO real venue, NO signer, NO real capital.
    #   - event_time.block_time_ms from transport (on-chain). NEVER wall-clock.
    #   - Frames carry synthetic=False + provenance="live_shadow".
    #   - PUMPPORTAL_WS_URL and RPC_PRIMARY are NEVER logged (may contain keys).
    #   - Feed drop / reconnect handled by PumpPortalTransport internally.
    #   - CancelledError propagates so shutdown is clean.
    # ------------------------------------------------------------------
    async def pumpportal_feed_task() -> None:
        """Consume real PumpPortal launches → existing SNIPE/FAST/SLOW pipeline.

        Uses PumpPortalTransport (same as shadow_record --source=pumpportal).
        Each decoded (LaunchEvent, sig, EventKind) is forwarded to the
        ControllerOrchestrator which runs it through the full paper pipeline
        against SimulationVenue — no real submission, no real capital.

        FeedBus frames carry:
          synthetic=False         — REAL on-chain launch event
          provenance="live_shadow" — real launch, paper/SimulationVenue execution

        Point-in-time: frame block_time_ms is event.event_time.block_time_ms
        (on-chain slot time from RPC getTransaction enrichment).
        Wall-clock is NEVER substituted (T-300a).
        """
        # Lazy imports so patching in tests works (imports execute at call time,
        # after test patches are active).
        import pathlib as _pathlib

        from aats.contracts.events import LaunchSource
        from aats.ingestion.registry import ProgramRegistry
        from aats.ingestion.transport import (
            PUMPPORTAL_DEFAULT_WS_URL,
            PumpPortalTransport,
        )

        # Credentials from env ONLY — never logged (may contain API keys).
        pp_ws_url: str = (
            os.environ.get("PUMPPORTAL_WS_URL", "") or PUMPPORTAL_DEFAULT_WS_URL
        )
        rpc_url: str = os.environ.get("RPC_PRIMARY", "")

        if not rpc_url:
            log.warning(
                "AATS_FEED_SOURCE=pumpportal: RPC_PRIMARY is not set. "
                "Per T-300a (no wall-clock substitution), LaunchEvents without "
                "on-chain block_time will be dropped. "
                "Set RPC_PRIMARY to an HTTP RPC endpoint for block_time enrichment. "
                "(URL is not logged — may contain an API key.)"
            )

        # Resolve the pump.fun program ID from the allowlist registry.
        # This is offline (verifier=None) — no live getAccountInfo call here.
        try:
            _project_root = _pathlib.Path(__file__).resolve().parent.parent.parent
            _allowlist = _project_root / "config" / "program-allowlist.json"
            _registry = ProgramRegistry.from_allowlist(
                _allowlist,
                verifier=None,           # offline; live verify is boot-time only
                filter_active_only=True,
            )
            _pump_program_id = _registry.program_id(LaunchSource.PUMPFUN)
        except Exception as exc:
            log.error(
                "pumpportal_feed_task: failed to resolve pump_program_id from "
                "allowlist (%s). No PumpPortal events will be processed. "
                "Check config/program-allowlist.json.",
                exc,
            )
            # Wait for shutdown rather than crashing the controller.
            await _shutdown_event.wait()
            return

        pp_transport = PumpPortalTransport(
            ws_url=pp_ws_url,           # NEVER logged
            rpc_url=rpc_url,            # NEVER logged
            pump_program_id=_pump_program_id,
            enrich_via_rpc=bool(rpc_url),
        )

        _pp_launch_count = 0
        log.info(
            "pumpportal_feed_task: started — REAL mainnet launches evaluated "
            "in PAPER mode (SimulationVenue, no real capital). "
            "RPC enrichment: %s. "
            "Frames will carry synthetic=False + provenance=live_shadow.",
            "ENABLED" if rpc_url else (
                "DISABLED (T-300a: events without on-chain block_time are dropped)"
            ),
        )

        try:
            async for _event, _sig, _event_kind in pp_transport.events(last_slot=0):
                if _shutdown_event.is_set():
                    break
                _pp_launch_count += 1

                try:
                    _result = orchestrator.on_launch_event(_event)
                    _signal = _result.get("signal")
                    _snipe = _result.get("snipe")
                    _fill_landed: bool = getattr(_snipe, "landed", False)
                    _fill_tokens: int = getattr(_snipe, "tokens_out", 0)

                    # Point-in-time: block_time_ms ONLY from on-chain event_time.
                    # The frame's ts field is wall-clock publication time (monitoring);
                    # block_time_ms is the AUTHORITATIVE event clock (C-5, T-300a).
                    await _publish_snipe_event(
                        feed_bus,
                        bus_producer,
                        "snipe_event",
                        {
                            "mint": _event.mint,
                            "launch_count": _pp_launch_count,
                            "slot": _event.event_time.slot,
                            # On-chain block_time — NEVER wall-clock (T-300a / C-5).
                            "block_time_ms": _event.event_time.block_time_ms,
                            "sol_reserve_lamports": _event.sol_reserve_lamports,
                            "token_reserve_base": _event.token_reserve_base,
                            "signal_p": (
                                round(_signal.p_calibrated, 4) if _signal else None
                            ),
                            "signal_surface": _signal.surface if _signal else None,
                            "snipe_result": (
                                _snipe
                                if isinstance(_snipe, str)
                                else ("filled" if _fill_landed else "no_fill")
                            ),
                            "fill_landed": _fill_landed,
                            "fill_tokens_out": _fill_tokens,
                            "open_positions": len(state_store.load_all_positions()),
                            # REAL on-chain launch event evaluated in paper mode.
                            "synthetic": False,
                            "provenance": "live_shadow",
                        },
                    )

                    log.info(
                        "[REAL] #%d mint=%.20s kind=%s slot=%d snipe=%s open=%d",
                        _pp_launch_count,
                        _event.mint,
                        _event_kind,
                        _event.event_time.slot,
                        (
                            _snipe
                            if isinstance(_snipe, str)
                            else getattr(_snipe, "reason", "??")
                        ),
                        len(state_store.load_all_positions()),
                    )

                except Exception as _exc:
                    # Non-fatal per-event error: log and continue.
                    log.error(
                        "[REAL] #%d event error: %s",
                        _pp_launch_count,
                        _exc,
                        exc_info=True,
                    )

        except asyncio.CancelledError:
            log.info(
                "pumpportal_feed_task: cancelled after %d launches.",
                _pp_launch_count,
            )
            raise
        except Exception as _task_exc:
            # Fatal transport error: log and let the task exit.
            # The controller continues (fast/slow loops unaffected).
            log.error(
                "pumpportal_feed_task: fatal error after %d launches: %s",
                _pp_launch_count,
                _task_exc,
                exc_info=True,
            )

        log.info(
            "pumpportal_feed_task: exited after %d launches.", _pp_launch_count
        )

    # ------------------------------------------------------------------
    # 12. Assemble and run all tasks
    # ------------------------------------------------------------------
    log.info(
        "Starting AATS controller — DRY_RUN_ENABLED=true, SimulationVenue, "
        "feed_source=%s, control_plane=http://0.0.0.0:%d",
        feed_source,
        CONTROL_PLANE_PORT,
    )
    if feed_source == "pumpportal":
        log.info(
            "Feed selector: PUMPPORTAL — real mainnet launches in SHADOW/PAPER mode. "
            "Dashboard will show frames with synthetic=False + provenance=live_shadow."
        )
    else:
        log.info(
            "Feed selector: SYNTHETIC (default) — "
            "PAPER-ONLY, NO real edge data. All frames carry synthetic=True."
        )

    _event_feed_task = (
        asyncio.create_task(pumpportal_feed_task(), name="pumpportal_feed")
        if feed_source == "pumpportal"
        else asyncio.create_task(launch_generator_task(), name="launch_gen")
    )

    tasks = [
        asyncio.create_task(fast_loop_task(), name="fast_loop"),
        _event_feed_task,
        asyncio.create_task(state_publisher_task(), name="state_pub"),
        asyncio.create_task(slow_enrichment_task(), name="slow_enrichment"),
        asyncio.create_task(control_plane_task(), name="control_plane"),
    ]

    try:
        # Run until either _shutdown_event is set (by fast_loop reaching max_ticks)
        # or any task raises, or a KeyboardInterrupt.
        await _shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received — shutting down.")

    # Graceful shutdown
    _shutdown_event.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # Final status report
    final_positions = state_store.load_all_positions()
    hb_age = state_store.get_heartbeat_age_ms()
    log.info(
        "Controller paper runner stopped. "
        "Total fast-loop ticks: %d  Open positions at shutdown: %d  "
        "Heartbeat age: %dms",
        _tick_count_shared,
        len(final_positions),
        hb_age,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m aats.controller",
        description=(
            "AATS M3 controller — paper-bot runner (SYNTHETIC events, no real capital).\n\n"
            "HONESTY: All launch events are SYNTHETIC and carry NO real edge data.\n"
            "DRY_RUN_ENABLED=true is enforced; no real submission is possible.\n"
            "The control plane is served on http://localhost:<port> for the dashboard."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help=(
            "Stop after this many FAST-loop ticks.  "
            "Default: run indefinitely.  "
            "Use e.g. --max-ticks 120 for a 12-second bounded test run."
        ),
    )
    parser.add_argument(
        "--launch-interval",
        type=float,
        default=LAUNCH_INTERVAL_S,
        help=f"Seconds between synthetic launch events (default {LAUNCH_INTERVAL_S}).",
    )
    parser.add_argument(
        "--fast-tick",
        type=float,
        default=FAST_TICK_INTERVAL_S,
        help=f"FAST loop tick interval in seconds (default {FAST_TICK_INTERVAL_S}).",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point for python -m aats.controller."""
    args = _parse_args()

    # Start /health + /metrics HTTP server on METRICS_PORT so the compose
    # healthcheck at localhost:9101/health confirms the slow-loop process is alive.
    _metrics_port = int(os.environ.get("METRICS_PORT", "9101"))
    try:
        from aats.telemetry.http_endpoint import run_metrics_server

        run_metrics_server(port=_metrics_port)
    except Exception:  # noqa: BLE001 — non-fatal; runner operates regardless
        pass

    log.info(
        "controller/__main__ starting: max_ticks=%s launch_interval=%.1fs fast_tick=%.3fs metrics_port=%d",
        args.max_ticks,
        args.launch_interval,
        args.fast_tick,
        _metrics_port,
    )

    asyncio.run(
        _run(
            max_ticks=args.max_ticks,
            launch_interval_s=args.launch_interval,
            fast_tick_s=args.fast_tick,
        )
    )


if __name__ == "__main__":
    main()
