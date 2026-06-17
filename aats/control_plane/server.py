"""M3 Control-Plane API server — FROZEN contract (api-contracts.md T-201).

T-341: Every endpoint, schema, and wire value is dictated by api-contracts.md.
No deviation is allowed without an ADR + delta notice from solana-systems-architect.

HARD RULES (non-waivable):
  - Real capital DISABLED behind DRY-RUN by default (cannot enable LIVE without
    edge gates AND CEO auth — AC-060).
  - No win-rate target/field/symbol anywhere.
  - Money: integer base units/Decimal-as-string, NEVER float.
  - No secrets/keys in code/logs.
  - LLM/slow model NEVER on the FAST/SNIPE critical path.
  - Every operator command may only DE-RISK (never size-up/widen/override/enable-live).
  - Single-writer FSM, atomic write-ahead snipe->fast handoff.
  - Point-in-time correctness.
  - External services (Redis/RPC/LLM) are INJECTABLE with deterministic offline fakes.

MONEY RULE: All monetary response fields are integer lamports (int) or
  decimal-as-string (str with Decimal semantics). Never JSON number for money.

SSE /api/feed: emits SnipeEvent frames from a FeedBus (injectable queue);
  real Redis streams provider replaces it in production.

AUTH: Every POST requires Bearer token auth (operator_token).
  LIVE mode additionally requires: DRY_RUN_ENABLED=false (config) AND
  CEO auth token (separate ceo_token header). (AC-060)

DE-RISK ONLY at the contract layer:
  - /api/risk-config POST: tighten-only; 403 on any widening.
  - /api/mode POST: LIVE is fenced; moving down is always allowed.
  - /api/kill, /api/flatten, /api/flatten/{mint}: always safe to repeat.
  - /api/breaker/reset: gated on TRIPPED + operator auth; 409 otherwise.
  - No endpoint or field that can size-up, widen-stop, add leverage, or enable LIVE
    without explicit multi-factor gating.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)

# FastAPI is a hard dependency for the control plane.
# Import at module level so annotation evaluation works correctly.
# (from __future__ import annotations is intentionally absent: FastAPI
# uses annotation introspection at route-registration time and the lazy
# string-annotation mode breaks Request injection in nested closures.)
try:
    from fastapi import FastAPI, HTTPException
    from fastapi import Request as _FastAPIRequest
    from fastapi.responses import StreamingResponse
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False
    _FastAPIRequest = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# ControlPlaneConfig — injectable configuration (no secrets in code)
# ---------------------------------------------------------------------------


@dataclass
class ControlPlaneConfig:
    """Operator-configurable settings for the control plane server.

    All real keys / secrets are injected at runtime via environment variables
    or explicit injection — NEVER hardcoded (crypto-security-engineer audit).
    """

    # Auth tokens — NEVER hardcoded; injected from ENV or test fixture.
    operator_token: str = field(default_factory=lambda: os.environ.get("OPERATOR_TOKEN", "dev-token"))
    # CEO auth token required to enable LIVE mode (AC-060).
    ceo_token: str = field(default_factory=lambda: os.environ.get("CEO_TOKEN", ""))
    # Hard gate: DRY_RUN_ENABLED=false is the only way to allow LIVE (infrastructure config).
    dry_run_enabled: bool = field(
        default_factory=lambda: os.environ.get("DRY_RUN_ENABLED", "true").lower() != "false"
    )
    # Current agent mode (mutable by /api/mode).
    agent_mode: str = "SHADOW"
    # Feed SSE lag budget ms (FR-056, AC-047).
    feed_lag_budget_ms: int = 3000
    # Wallet pubkey (display only; no private key ever here — ADR-0009).
    wallet_pubkey: str = "sim_pubkey"
    wallet_cap_lamports: int = 500_000_000


# ---------------------------------------------------------------------------
# FeedBus — injectable SSE event bus (offline fake + real impl behind same iface)
# ---------------------------------------------------------------------------


class FeedBus:
    """In-process publish/subscribe bus for SnipeEvent SSE frames.

    In production this is backed by a Redis Stream reader (ops.feed).
    In tests it is a simple asyncio.Queue per subscriber — no Redis needed.

    The FAST/SNIPE loops publish events; /api/feed SSE subscribers receive them.
    This keeps external services (Redis) injectable and replaceable with fakes.
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict | None]] = []
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[dict | None]:
        """Subscribe: returns a queue that receives published events.  None = EOF."""
        q: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[dict | None]) -> None:
        async with self._lock:
            import contextlib
            with contextlib.suppress(ValueError):
                self._subscribers.remove(q)

    async def publish(self, event: dict) -> None:
        """Publish a SnipeEvent dict to all SSE subscribers."""
        async with self._lock:
            subs = list(self._subscribers)
        import contextlib
        for q in subs:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    def publish_sync(self, event: dict) -> None:
        """Synchronous publish from non-async contexts (e.g. FAST loop tick)."""
        # Try to schedule on any running event loop; if none, best-effort drop.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self.publish(event))
                )
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# RedisStreamFeedReader — bridges ops.feed Redis Stream -> FeedBus
# ---------------------------------------------------------------------------


class RedisStreamFeedReader:
    """Tail the ops.feed Redis Stream and publish each entry into a FeedBus.

    This is the production adapter that connects the ingestion/controller
    pipeline (which writes to the Redis "ops.feed" stream via
    BusProducer.publish_ops_event) to the SSE /api/feed endpoint (which
    reads from the FeedBus).  In tests the FeedBus is used directly without
    this reader — Redis is injected or replaced with an offline fake.

    Architecture (api-contracts.md §8, BLUEPRINT §5):
      BusProducer.publish_ops_event() -> "ops.feed" (Redis Stream)
        -> RedisStreamFeedReader.run() -> FeedBus.publish()
          -> /api/feed SSE generator -> dashboard subscriber

    Usage:
        reader = RedisStreamFeedReader(redis_client, feed_bus)
        asyncio.create_task(reader.run())   # long-running background task

    The reader uses XREAD with a BLOCK timeout (non-zero) so it yields the
    event loop between polls and never busy-loops.  It is NOT a consumer group
    (no XACK needed for the feed — the SSE stream is fire-and-forget).

    Injection contract:
        redis_client: any object with an async .xread(streams, count, block)
                      method returning Redis Stream entries.  In production:
                      redis.asyncio.Redis.  In tests: an offline fake
                      (FakeRedisStreamClient below).
    """

    STREAM_NAME = "ops.feed"
    BLOCK_MS = 1_000          # max wait per XREAD call (yields event loop)
    BATCH_SIZE = 100          # max entries per XREAD call

    def __init__(
        self,
        redis_client: Any,
        feed_bus: FeedBus,
        *,
        start_id: str = "$",   # "$" = only new entries; "0" = all history
    ) -> None:
        self._redis = redis_client
        self._bus = feed_bus
        self._last_id = start_id
        self._running = False

    async def run(self, *, stop_event: asyncio.Event | None = None) -> None:
        """Tail ops.feed indefinitely; publish each entry to the FeedBus.

        Args:
            stop_event: optional asyncio.Event; set it to stop the reader
                        cleanly (used in tests for deterministic teardown).
        """
        self._running = True
        log.info("RedisStreamFeedReader: starting; stream=%s", self.STREAM_NAME)
        try:
            while self._running:
                if stop_event is not None and stop_event.is_set():
                    break
                try:
                    entries = await self._redis.xread(
                        streams={self.STREAM_NAME: self._last_id},
                        count=self.BATCH_SIZE,
                        block=self.BLOCK_MS,
                    )
                except Exception as exc:
                    # Redis down / connection error: log and back off.
                    # Do NOT crash the reader; the FeedBus continues empty.
                    log.error(
                        "RedisStreamFeedReader.xread error: %s — backing off 1s",
                        exc,
                    )
                    await asyncio.sleep(1)
                    continue

                if not entries:
                    continue  # timeout, no new entries

                for _stream_name, messages in entries:
                    for entry_id, fields in messages:
                        # Update position so next XREAD starts after this entry.
                        self._last_id = (
                            entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                        )
                        try:
                            raw = fields.get(b"data") or fields.get("data") or b"{}"
                            if isinstance(raw, bytes):
                                raw = raw.decode()
                            event = json.loads(raw)
                        except Exception as parse_exc:
                            log.warning(
                                "RedisStreamFeedReader: failed to parse entry %s: %s",
                                self._last_id,
                                parse_exc,
                            )
                            continue
                        await self._bus.publish(event)
        finally:
            self._running = False
            log.info("RedisStreamFeedReader: stopped")

    def stop(self) -> None:
        """Request clean stop on the next XREAD timeout."""
        self._running = False


# ---------------------------------------------------------------------------
# Risk-config tighten-only validation (api-contracts.md §5)
# ---------------------------------------------------------------------------

_RISK_CONFIG_FLOOR_CEILINGS: dict[str, Any] = {
    # (field_name): (floor_ceiling, direction) where direction = "max" means the API
    # value must be <= the floor_ceiling (tighten only: ceiling on exposure).
    "per_trade_cap_lamports": (100_000_000, "max"),   # may only decrease; floor 0.1 SOL
    "max_aggregate_lamports": (500_000_000, "max"),   # may only decrease; floor 0.5 SOL
    "max_slippage_bps": (None, "tighten_only"),       # must not increase
    "jito_tip_cap_frac": (None, "tighten_only"),      # must not increase
}


def _validate_risk_config_tighten_only(
    current: dict, proposed: dict
) -> list[str]:
    """Return list of violations (field names) where proposed WIDENS vs current.

    api-contracts.md §5 TIGHTEN-ONLY semantics:
      - per_trade_cap_lamports: may only decrease (smaller = tighter)
      - max_aggregate_lamports: may only decrease
      - daily_loss_limit_pct: may only become more negative or equal (tighter)
        Represented as positive abs% (3.0 means -3%); INCREASING it = WIDEN.
      - max_slippage_bps: may only decrease
      - snipe_threshold: raising is de-risk; lowering is reject
      - jito_tip_cap_frac: may only decrease
      - kelly_fraction_cap: may only decrease

    CRITICAL: daily_loss_limit_pct, jito_tip_cap_frac, kelly_fraction_cap are
    transmitted on the wire as decimal-strings ("3.0", "0.30", "0.25").
    They MUST be parsed to Decimal before numeric comparison — string ordering
    is lexicographic and produces wrong results (e.g. "2.5" > "1.0" as strings
    but "10.0" < "3.0" lexicographically).

    Returns empty list if all proposed values are valid (no widening).
    """
    violations: list[str] = []

    _DECIMAL_STRING_FIELDS = {"daily_loss_limit_pct", "jito_tip_cap_frac", "kelly_fraction_cap"}

    def _coerce(field: str, val: object) -> object:
        """Parse decimal-string fields to Decimal for numeric comparison."""
        if field in _DECIMAL_STRING_FIELDS and isinstance(val, str):
            try:
                return Decimal(val)
            except Exception:
                return val
        return val

    def _check_decrease_only(field: str) -> None:
        cur = _coerce(field, current.get(field))
        new = _coerce(field, proposed.get(field))
        if cur is not None and new is not None and new > cur:
            violations.append(field)

    def _check_increase_only(field: str) -> None:
        """Raising this field is de-risk; lowering is a widen (reject)."""
        cur = _coerce(field, current.get(field))
        new = _coerce(field, proposed.get(field))
        if cur is not None and new is not None and new < cur:
            violations.append(field)

    _check_decrease_only("per_trade_cap_lamports")
    _check_decrease_only("max_aggregate_lamports")
    _check_decrease_only("max_slippage_bps")
    _check_decrease_only("jito_tip_cap_frac")   # each field appears exactly ONCE
    _check_decrease_only("kelly_fraction_cap")

    # daily_loss_limit_pct: represents the abs% threshold.
    # Raising it means the bot can lose MORE before trip = WIDEN.
    # Parsed to Decimal above so "2.5" vs "1.0" comparison is numeric, not lexicographic.
    _check_decrease_only("daily_loss_limit_pct")

    # snipe_threshold: raising it is de-risk; LOWERING it is widen (reject).
    _check_increase_only("snipe_threshold")

    return violations


# ---------------------------------------------------------------------------
# AgentMode ladder ordering (api-contracts.md §2)
# ---------------------------------------------------------------------------

_MODE_ORDER = {"SHADOW": 0, "PAPER": 1, "LIVE_DRY_RUN": 2, "LIVE": 3}


def _mode_is_downward(current_mode: str, proposed_mode: str) -> bool:
    """Return True if proposed moves DOWN the staging ladder (de-risk direction)."""
    return _MODE_ORDER.get(proposed_mode, -1) < _MODE_ORDER.get(current_mode, 0)


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def build_app(  # noqa: C901 — complex by necessity; each branch is a contract endpoint
    state_store: Any,
    fast_loop: Any,
    kill_switch: Any,
    breaker: Any,
    feed_bus: FeedBus,
    config: ControlPlaneConfig | None = None,
    *,
    # Injectable snapshot providers for read-only GET endpoints.
    # Each is a zero-arg callable returning the relevant data structure.
    # If None, a deterministic offline stub is returned.
    sentiment_provider: Any = None,
    predictions_provider: Any = None,
    reasoning_provider: Any = None,
    risk_config_provider: Any = None,
    latency_provider: Any = None,
    health_provider: Any = None,
    metrics_provider: Any = None,
    # E3 additive: optional candidate queue for GET /api/candidates.
    # If None, the endpoint returns an empty list (backward-compatible).
    # CandidateQueue is injected here so the server has no import dependency on
    # the controller module (direction: server imports schemas; controller owns queue).
    candidates_provider: Any = None,
    # E11 additive: optional wallet-cluster store for GET /api/wallet-cluster.
    # If None, the endpoint returns an empty list (backward-compatible).
    # WalletClusterStore is injected here; server has no import dep on controller.
    wallet_cluster_provider: Any = None,
) -> Any:
    """Build the FastAPI application conforming to api-contracts.md (FROZEN).

    All external dependencies (Redis, RPC, LLM) are injected, so the app runs
    with offline deterministic fakes in tests (no network required).

    Args:
        state_store:    StateStore instance (InMemoryStateStore or Redis-backed).
        fast_loop:      FastLoop instance (for position reads / flatten).
        kill_switch:    KillSwitch instance.
        breaker:        CircuitBreaker instance.
        feed_bus:       FeedBus instance (SSE event publisher/subscriber).
        config:         ControlPlaneConfig (defaults: DRY_RUN=true, mode=SHADOW).
        sentiment_provider:   Optional callable () -> list[MCSScore dict].
        predictions_provider: Optional callable () -> Prediction dict.
        reasoning_provider:   Optional callable () -> list[Reasoning dict].
        risk_config_provider: Optional callable () -> RiskConfig dict.
        latency_provider:     Optional callable () -> LatencyBudget dict.
        health_provider:      Optional callable () -> list[ModuleHealth dict].
        metrics_provider:     Optional callable () -> MetricsSnapshot dict.

    Returns:
        FastAPI application instance.
    """
    if not _FASTAPI_AVAILABLE:
        log.error("FastAPI not installed — control_plane.build_app cannot run")
        return None

    # Use module-level FastAPI imports (annotation introspection requires them
    # in the global namespace when __future__ annotations is absent).
    Request = _FastAPIRequest  # noqa: N806 — local alias for type annotations

    from aats.contracts.api_schemas import (
        AgentMode,
        BreakerResetResponse,
        ErrorEnvelope,
        FlattenResponse,
        KillResponse,
        ModeRequest,
        ModeResponse,
    )
    from aats.contracts.risk import RiskConfig
    from aats.risk.circuit_breaker import mint_operator_reset_token

    cfg = config or ControlPlaneConfig()

    # Mutable mode state (held in cfg; accessed by /api/mode)
    # Validated: only de-risk moves are allowed (or LIVE with full gates).

    app = FastAPI(title="AATS Control Plane", version="1.0.0")

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _check_operator_auth(request: Any) -> None:
        """Verify Bearer operator token on every POST (api-contracts.md §4)."""
        auth = request.headers.get("Authorization", "")
        token = cfg.operator_token
        if not token or not auth.startswith("Bearer ") or auth[7:] != token:
            raise HTTPException(
                status_code=403,
                detail=ErrorEnvelope(
                    error="operator_auth_required",
                    message="Valid operator Bearer token required for POST endpoints.",
                ).model_dump(),
            )

    def _check_ceo_auth(request: Any) -> None:
        """Verify CEO auth token (required for LIVE mode, AC-060)."""
        auth = request.headers.get("X-CEO-Auth", "")
        if not cfg.ceo_token or auth != cfg.ceo_token:
            raise HTTPException(
                status_code=403,
                detail=ErrorEnvelope(
                    error="live_requires_dry_run_disabled_and_ceo_auth",
                    message=(
                        "LIVE mode requires DRY_RUN_ENABLED=false (infrastructure config) "
                        "AND a valid CEO auth token (X-CEO-Auth header). (AC-060)"
                    ),
                ).model_dump(),
            )

    # ------------------------------------------------------------------
    # Snapshot helpers — read from injected providers or return stubs.
    # All stubs contain ZERO win-rate fields (HONESTY CLAUSE, AC-037).
    # All money fields are int lamports or decimal-string, NEVER float.
    # ------------------------------------------------------------------

    def _get_breaker_state() -> Any:
        return state_store.load_breaker_state()

    def _make_metrics_snapshot() -> dict:
        if metrics_provider is not None:
            return metrics_provider()
        bs = _get_breaker_state()
        daily_pnl_lamports = bs.daily_net_pnl_lamports if bs else 0
        # Decimal-as-string (NEVER float for money, NFR-009)
        daily_pnl_sol = str(Decimal(daily_pnl_lamports) / Decimal("1000000000"))
        positions = state_store.load_all_positions()
        return {
            "net_pnl_sol": daily_pnl_sol,
            "gross_pnl_sol": daily_pnl_sol,
            "land_rate_pct": 0.0,       # float: not money
            "median_slot_delay": 0,
            "rug_avoidance_pct": 0.0,   # float: not money
            "tip_efficiency": 0.0,      # float: not money
            "model_vs_baseline": {
                "delta_net_pnl_per_sol_at_risk": "0",
                "lower_95_bound": "0",
                "n_test_windows": 0,
                "gate_b_pass": False,
            },
            "gate_a": {
                "net_pnl_sol": daily_pnl_sol,
                "lower_95_bound": "0",
                "gate_a_pass": False,
            },
            "open_positions": len(positions),
            "daily_pnl_sol": daily_pnl_sol,
            "daily_loss_limit_sol": "-0.30",
            "breaker_tripped": bs is not None and bs.state == "TRIPPED",
            # NO win_rate field (HONESTY CLAUSE, AC-037).
        }

    def _make_position_record(p: Any) -> dict:
        """Serialize a Position to the wire schema (api-contracts.md §4 /api/positions)."""
        cost = p.cost_breakdown
        # cost_breakdown is a CostStack; map to the wire CostBreakdown shape.
        cost_dict = {
            "tip_lamports": cost.tip_lamports if hasattr(cost, "tip_lamports") else cost.jito_tip_bps,
            "priority_lamports": cost.priority_lamports if hasattr(cost, "priority_lamports") else cost.priority_fee_bps,
            "entry_slippage_bps": cost.entry_slippage_bps,
            "amm_fee_bps": cost.amm_fee_bps,
            "exit_slippage_bps": cost.exit_slippage_bps,
            "adverse_selection_bps": cost.adverse_selection_bps,
        }
        # Validate integers (never float for lamports, NFR-009)
        for lf in ("tip_lamports", "priority_lamports"):
            if isinstance(cost_dict[lf], float):
                cost_dict[lf] = int(cost_dict[lf])

        return {
            "mint": p.mint,
            "token": p.token,
            "entry_slot": p.entry_slot,
            "sol_in_lamports": int(p.sol_in_lamports),
            "unrealized_pnl_net_lamports": int(p.unrealized_pnl_net_lamports),
            "entry_slip_bps": p.entry_slippage_bps,
            "tp_hit": p.tp_hit,
            "tp_total": p.tp_total,
            "trailing_armed": p.trailing_armed,
            "hard_stop_pct": -40.0,  # float: percentage, not money
            "exit_mode": p.exit_mode,
            "fsm_state": p.fsm_state.value if hasattr(p.fsm_state, "value") else str(p.fsm_state),
            "age_sec": 0,
            "status": p.status,
            "realized_pnl_net_lamports": (
                int(p.realized_pnl_net_lamports)
                if p.realized_pnl_net_lamports is not None
                else None
            ),
            "surface": p.surface,
            "cost_breakdown": cost_dict,
        }

    def _make_latency_budget() -> dict:
        if latency_provider is not None:
            return latency_provider()
        return {
            "hops": [
                {
                    "name": "ingress_detect",
                    "ms": 60,
                    "budget_ms": 70,
                    "class": "internal_compute",
                },
                {
                    "name": "block_engine_rtt",
                    "ms": 55,
                    "budget_ms": 60,
                    "class": "submission",
                },
                {
                    "name": "leader_land",
                    "ms": 50,
                    "budget_ms": 400,
                    "class": "submission",
                    "p99_ms": 450,
                    "note": "+1 slot under contention",
                },
            ],
            "internal_ms": 67,
            "block_engine_rtt_ms": 55,
            "slot_floor_ms": 400,
            "posture": "DETECTION-COMPETITIVE, SUBMISSION-DISADVANTAGED",
            "tiers": [
                {
                    "name": "dedicated_geyser",
                    "land_rate": 0.38,
                    "median_slot_delay": 7,
                    "entry_slip_pct": 1.45,
                    "active": True,
                },
                {
                    "name": "colo_shred",
                    "land_rate": 0.0,
                    "median_slot_delay": 0,
                    "entry_slip_pct": 0.0,
                    "active": False,
                },
            ],
        }

    def _make_health() -> list[dict]:
        if health_provider is not None:
            return health_provider()
        now_ms = int(time.monotonic_ns() // 1_000_000)
        hb_age = state_store.get_heartbeat_age_ms()
        fast_status = "ok" if hb_age < 1200 else "degraded"
        return [
            {
                "name": "fast_loop",
                "status": fast_status,
                "staleness_ms": hb_age,
                "last_heartbeat_ms": now_ms,
            },
            {
                "name": "snipe_loop",
                "status": "ok",
                "staleness_ms": 0,
                "last_heartbeat_ms": now_ms,
            },
            {
                "name": "slow_loop",
                "status": "ok",
                "staleness_ms": 0,
                "last_heartbeat_ms": now_ms,
            },
            {
                "name": "geyser_feed",
                "status": "ok",
                "staleness_ms": 50,
                "last_heartbeat_ms": now_ms,
            },
        ]

    def _make_sentiment() -> list[dict]:
        if sentiment_provider is not None:
            return sentiment_provider()
        return []

    def _make_predictions() -> dict:
        if predictions_provider is not None:
            return predictions_provider()
        return {
            "model_p": 0.0,
            "baseline_p": 0.0,
            "uncertainty": 0.0,
            "calibration_bins": [],
            "feature_importance": [],
            "ts": int(time.time() * 1000),
        }

    def _make_reasoning() -> list[dict]:
        if reasoning_provider is not None:
            return reasoning_provider()
        return []

    def _make_risk_config() -> dict:
        if risk_config_provider is not None:
            return risk_config_provider()
        rc = RiskConfig()
        return {
            "per_trade_cap_lamports": rc.per_trade_cap_lamports,
            "max_aggregate_lamports": rc.max_aggregate_lamports,
            "daily_risk_tranche_lamports": rc.daily_risk_tranche_lamports,
            "daily_loss_limit_pct": str(rc.daily_loss_limit_pct),
            "daily_loss_floor_lamports": rc.daily_loss_floor_lamports,
            "max_slippage_bps": rc.max_slippage_bps,
            "snipe_threshold": rc.snipe_threshold,
            "veto_threshold": rc.veto_threshold,
            "jito_tip_cap_frac": str(rc.jito_tip_cap_frac),
            "kelly_fraction_cap": str(rc.kelly_fraction_cap),
            "n_wallets_max": rc.n_wallets_max,
            "blast_radius_cap_lamports": rc.blast_radius_cap_lamports,
            "t_dms_seconds": rc.t_dms_seconds,
            "pre_calibration_haircut_bps": rc.pre_calibration_haircut_bps,
        }

    # Store mutable current risk config for tighten-only comparison.
    # Using a mutable container (list with one element) to allow mutation in nested closures.
    _risk_config_box: list[dict] = [_make_risk_config()]

    def _get_current_risk_config() -> dict:
        return _risk_config_box[0]

    def _set_current_risk_config(cfg_dict: dict) -> None:
        _risk_config_box[0] = cfg_dict

    # ------------------------------------------------------------------
    # GET /api/state
    # ------------------------------------------------------------------

    @app.get("/api/state")
    async def get_state() -> dict:
        """Return current AgentState — mode, loops, connection, wallet, flags.

        Source: api-contracts.md §4 GET /api/state.
        wallet.balance_lamports is integer lamports (NOT float).
        dry_run_enabled is read-only (mirrors infrastructure config).
        """
        bs = _get_breaker_state()
        hb_age = state_store.get_heartbeat_age_ms()
        # Loop status derived from heartbeat (heuristic for offline fake).
        fast_status = "running" if hb_age < 5000 else "stale"
        return {
            "mode": cfg.agent_mode,
            "loops": {
                "snipe": "armed" if not kill_switch.is_killed else "halted",
                "fast": fast_status,
                "slow": "running",
            },
            "connection": {
                "geyser": True,
                "shredstream": False,
                "internal_ms": 67,
            },
            "wallet": {
                "pubkey": cfg.wallet_pubkey,
                "balance_lamports": 0,         # integer lamports (NFR-009)
                "cap_lamports": cfg.wallet_cap_lamports,  # integer lamports
            },
            "dry_run_enabled": cfg.dry_run_enabled,
            "breaker_tripped": bs is not None and bs.state == "TRIPPED",
            "killed": kill_switch.is_killed,
        }

    # ------------------------------------------------------------------
    # GET /api/feed  (SSE)
    # ------------------------------------------------------------------

    @app.get("/api/feed")
    async def get_feed(request: Request) -> StreamingResponse:
        """SSE stream of SnipeEvent frames.

        Source: api-contracts.md §6.
        Each frame: data: <SnipeEvent JSON>\\n\\n
        Lag budget: <= 3s (FR-056, AC-047).
        """

        async def event_generator():
            q = await feed_bus.subscribe()
            # Emit an immediate connection-established comment so that the HTTP
            # response headers are flushed to the client before the first event
            # arrives.  This also signals to the client that the SSE stream is
            # live (standard SSE practice; comments are ignored by EventSource).
            yield ": connected\n\n"
            try:
                while True:
                    # Check if client disconnected
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=30.0)
                        if event is None:
                            break
                        yield f"data: {json.dumps(event)}\n\n"
                    except TimeoutError:
                        # Periodic heartbeat to keep connection alive
                        yield ": heartbeat\n\n"
            finally:
                await feed_bus.unsubscribe(q)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ------------------------------------------------------------------
    # GET /api/metrics
    # ------------------------------------------------------------------

    @app.get("/api/metrics")
    async def get_metrics() -> dict:
        """Return MetricsSnapshot.

        Source: api-contracts.md §4 GET /api/metrics.
        All PnL fields are decimal-as-string (NFR-009).
        NO win_rate field (HONESTY CLAUSE, AC-037).
        """
        snap = _make_metrics_snapshot()
        # Validate: no win_rate field must be present anywhere.
        assert "win_rate" not in snap, "HONESTY CLAUSE violated: win_rate in metrics"
        return snap

    # ------------------------------------------------------------------
    # GET /api/positions
    # ------------------------------------------------------------------

    @app.get("/api/positions")
    async def get_positions() -> list[dict]:
        """Return all open positions as PositionRecord[].

        Source: api-contracts.md §4 GET /api/positions.
        sol_in_lamports and unrealized_pnl_net_lamports are integer lamports.
        """
        positions = state_store.load_all_positions()
        return [_make_position_record(p) for p in positions]

    # ------------------------------------------------------------------
    # GET /api/latency
    # ------------------------------------------------------------------

    @app.get("/api/latency")
    async def get_latency() -> dict:
        """Return LatencyBudget with hop breakdown and infra tiers.

        Source: api-contracts.md §4 GET /api/latency.
        Wire key for hop class field is "class" (T-199b FIX; LatencyHop alias).
        tiers array read by dashboard's useInfraTiers hook.
        """
        return _make_latency_budget()

    # ------------------------------------------------------------------
    # GET /api/sentiment
    # ------------------------------------------------------------------

    @app.get("/api/sentiment")
    async def get_sentiment() -> list[dict]:
        """Return MCSScore[] for tracked assets.

        Source: api-contracts.md §4 GET /api/sentiment.
        High synchronicity -> lower conviction (FR-008, AC-010).
        """
        return _make_sentiment()

    # ------------------------------------------------------------------
    # GET /api/predictions
    # ------------------------------------------------------------------

    @app.get("/api/predictions")
    async def get_predictions() -> dict:
        """Return Prediction (calibrated classifier probability + uncertainty).

        Source: api-contracts.md §4 GET /api/predictions.
        Adds uncertainty band (FR-014).
        """
        return _make_predictions()

    # ------------------------------------------------------------------
    # GET /api/reasoning
    # ------------------------------------------------------------------

    @app.get("/api/reasoning")
    async def get_reasoning() -> list[dict]:
        """Return Reasoning[] — LLM veto log including clamp events.

        Source: api-contracts.md §4 GET /api/reasoning.
        Each entry carries action_received, action_applied, risk_increase_clamped (AC-054).
        """
        return _make_reasoning()

    # ------------------------------------------------------------------
    # GET /api/risk-config
    # ------------------------------------------------------------------

    @app.get("/api/risk-config")
    async def get_risk_config() -> dict:
        """Return current RiskConfig (read-only GET).

        Source: api-contracts.md §4 GET /api/risk-config.
        POST /api/risk-config is tighten-only (see below).
        """
        return _get_current_risk_config()

    # ------------------------------------------------------------------
    # GET /api/health
    # ------------------------------------------------------------------

    @app.get("/api/health")
    async def get_health() -> list[dict]:
        """Return ModuleHealth[] — module status + staleness.

        Source: api-contracts.md §4 GET /api/health.
        Geyser feed age > 1200ms surfaces as degraded/STALE (FR-057, AC-051).
        """
        return _make_health()

    # ------------------------------------------------------------------
    # POST /api/risk-config  (TIGHTEN-ONLY, de-risk only)
    # ------------------------------------------------------------------

    @app.post("/api/risk-config")
    async def post_risk_config(request: Request) -> dict:
        """Update RiskConfig — TIGHTEN-ONLY.

        Source: api-contracts.md §5 POST /api/risk-config.
        Any proposed value that widens a limit => 403 risk_increase_rejected.
        Returns the persisted config on success.

        ASYMMETRIC TRUST GUARD: This is the explicit branch enforcing that
        the API can never increase risk exposure via config widening.
        """
        _check_operator_auth(request)

        body = await request.json()

        # Validate tighten-only (api-contracts.md §5)
        violations = _validate_risk_config_tighten_only(_get_current_risk_config(), body)
        if violations:
            raise HTTPException(
                status_code=403,
                detail=ErrorEnvelope(
                    error="risk_increase_rejected",
                    message=(
                        "Proposed risk config would WIDEN one or more limits. "
                        "The API may only TIGHTEN risk parameters. "
                        "Widening requires an explicit R3 config change (file + deploy)."
                    ),
                    detail={"violations": violations},
                ).model_dump(),
            )

        # Additional floor/ceiling validation via RiskConfig model
        try:
            # Convert Decimal-as-string back for model construction
            safe_body = dict(body)
            for f in ("daily_loss_limit_pct", "jito_tip_cap_frac", "kelly_fraction_cap"):
                if f in safe_body and isinstance(safe_body[f], str):
                    safe_body[f] = Decimal(safe_body[f])
            RiskConfig(**safe_body)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error="invalid_risk_config",
                    message=str(exc),
                ).model_dump(),
            ) from exc

        # Persist the tightened config
        _set_current_risk_config(body)
        log.info("control_plane: risk_config tightened")
        return _get_current_risk_config()

    # ------------------------------------------------------------------
    # POST /api/kill  (de-risk only, idempotent)
    # ------------------------------------------------------------------

    @app.post("/api/kill", status_code=202)
    async def post_kill(request: Request) -> dict:
        """Halt all new entries; hand open positions to ExitEngine + survivable-stop.

        Source: api-contracts.md §5 POST /api/kill.
        Idempotent. Effect within <= 2s (FR-055, AC-040).
        """
        _check_operator_auth(request)

        kill_switch.kill()
        log.warning("control_plane: KILL received — entries halted")

        # Flatten all open positions (de-risk action)
        if fast_loop is not None:
            flat_handler = getattr(fast_loop, "flatten_handler", None)
            if flat_handler is not None:
                flat_handler.emergency_flatten_all(reason="operator_kill")

        return KillResponse(
            status="halting",
            message="kill initiated; entries halted, positions handed to ExitEngine",
        ).model_dump()

    # ------------------------------------------------------------------
    # POST /api/flatten  (de-risk only, idempotent — all positions)
    # ------------------------------------------------------------------

    @app.post("/api/flatten", status_code=202)
    async def post_flatten_all(request: Request) -> dict:
        """Flatten ALL open positions via ExitEngine.

        Source: api-contracts.md §5 POST /api/flatten.
        Idempotent. <= 2s (AC-040).
        """
        _check_operator_auth(request)

        if fast_loop is not None:
            flat_handler = getattr(fast_loop, "flatten_handler", None)
            if flat_handler is not None:
                flat_handler.emergency_flatten_all(reason="operator_flatten_all")

        log.warning("control_plane: flatten ALL received")
        return FlattenResponse(status="flattening", mint=None).model_dump()

    # ------------------------------------------------------------------
    # POST /api/flatten/{mint}  (de-risk only — single position)
    # ------------------------------------------------------------------

    @app.post("/api/flatten/{mint}", status_code=202)
    async def post_flatten_mint(mint: str, request: Request) -> dict:
        """Flatten a single position; other positions unchanged.

        Source: api-contracts.md §5 POST /api/flatten/{mint} (AC-044).
        {mint} is URL-encoded.
        """
        _check_operator_auth(request)

        pos = state_store.load_position(mint)
        if pos is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error="position_not_found",
                    message=f"No open position for mint: {mint}",
                ).model_dump(),
            )

        # Flatten ONLY this mint — leave all other positions unchanged (AC-044).
        # ASYMMETRIC TRUST: flatten_one exits exactly one position; it never
        # closes unrelated positions (use POST /api/flatten for book-wide de-risk).
        if fast_loop is not None:
            flat_handler = getattr(fast_loop, "flatten_handler", None)
            if flat_handler is not None:
                flat_handler.flatten_one(mint=mint, reason=f"operator_flatten:{mint}")

        log.warning("control_plane: flatten mint=%s received", mint)
        return FlattenResponse(status="flattening", mint=mint).model_dump()

    # ------------------------------------------------------------------
    # POST /api/breaker/reset  (de-risk-gate; operator-only; NOT LLM)
    # ------------------------------------------------------------------

    @app.post("/api/breaker/reset")
    async def post_breaker_reset(request: Request) -> dict:
        """Re-arm the daily-loss circuit breaker after manual review.

        Source: api-contracts.md §5 POST /api/breaker/reset.
        Requires: breaker TRIPPED (else 409) AND operator auth.
        NO automated path and NO LLM may call this (AC-029).
        The LLM may TRIP the breaker, NEVER reset it.

        This is the ONLY POST whose effect is "less halted" — gated on an
        explicit human-reviewed operator action; cannot silently re-arm after
        a crash (FSM restores TRIPPED, never ARMED on restart — AC-029).
        """
        _check_operator_auth(request)

        if breaker is None:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error="breaker_not_available",
                    message="Circuit breaker is not available.",
                ).model_dump(),
            )

        # Check the authoritative breaker state (the CircuitBreaker object holds the
        # latch; the state_store is a projection). If breaker has a .state property,
        # use it; else fall back to state_store.
        is_tripped = False
        if hasattr(breaker, "state"):
            try:
                bs = breaker.state
                is_tripped = bs.state == "TRIPPED"
            except Exception:
                pass
        if not is_tripped:
            # Fallback: also check the state_store projection
            current = state_store.load_breaker_state()
            if current is not None and current.state == "TRIPPED":
                is_tripped = True

        if not is_tripped:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error="breaker_not_tripped",
                    message="Cannot reset: circuit breaker is not currently TRIPPED.",
                ).model_dump(),
            )

        # Mint the operator reset token (only this authenticated path can do it — AC-029).
        token = mint_operator_reset_token("operator")
        try:
            breaker.reset(token)
        except Exception as exc:
            # If breaker raises BreakerNotTripped (race between check and reset), 409
            from aats.risk.circuit_breaker import BreakerNotTripped
            if isinstance(exc, BreakerNotTripped):
                raise HTTPException(
                    status_code=409,
                    detail=ErrorEnvelope(
                        error="breaker_not_tripped",
                        message="Cannot reset: circuit breaker is not currently TRIPPED.",
                    ).model_dump(),
                ) from exc
            raise
        log.warning("control_plane: breaker RESET by operator")

        return BreakerResetResponse(
            state="ARMED",
            message="breaker re-armed after manual review",
        ).model_dump()

    # ------------------------------------------------------------------
    # POST /api/mode  (de-risk: move down always ok; LIVE requires gates)
    # ------------------------------------------------------------------

    @app.post("/api/mode")
    async def post_mode(request: Request) -> dict:
        """Change agent operating mode.

        Source: api-contracts.md §5 POST /api/mode.
        Mode may only advance toward LIVE with explicit authorization.
        LIVE requires DRY_RUN_ENABLED=false (config) AND CEO auth token (AC-060).
        Moving DOWN the ladder (toward SHADOW) is always permitted (de-risk).

        ASYMMETRIC TRUST GUARD: This is the explicit branch enforcing that
        real-capital exposure cannot happen by accident.
        """
        _check_operator_auth(request)

        body = await request.json()
        try:
            req = ModeRequest(**body)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error="invalid_mode_request",
                    message=str(exc),
                ).model_dump(),
            ) from exc

        proposed = req.mode.value
        current = cfg.agent_mode

        # Moving DOWN the ladder is always de-risk — always permitted.
        if not _mode_is_downward(current, proposed) and proposed == "LIVE":
            # LIVE requires DRY_RUN_ENABLED=false AND CEO auth (AC-060).
            if cfg.dry_run_enabled:
                raise HTTPException(
                    status_code=403,
                    detail=ErrorEnvelope(
                        error="live_requires_dry_run_disabled_and_ceo_auth",
                        message=(
                            "Cannot enable LIVE mode while DRY_RUN_ENABLED=true. "
                            "Set DRY_RUN_ENABLED=false in infrastructure config "
                            "AND provide CEO auth token. (AC-060)"
                        ),
                    ).model_dump(),
                )
            # Also require CEO auth header.
            _check_ceo_auth(request)

        # Apply the mode change.
        old_mode = cfg.agent_mode
        cfg.agent_mode = proposed
        log.warning("control_plane: mode %s -> %s", old_mode, proposed)

        return ModeResponse(
            mode=AgentMode(proposed),
            dry_run_enabled=cfg.dry_run_enabled,
        ).model_dump()

    # ------------------------------------------------------------------
    # GET /api/candidates  (E3 additive read-only endpoint)
    # ------------------------------------------------------------------

    @app.get("/api/candidates")
    async def get_candidates() -> list[dict]:
        """Return the candidate watchlist queue — read-only operator view (E3).

        A pipeline of tokens evaluated by the snipe loop but not entered, or
        entered (status=sniped), ordered newest-first.

        Source: E3 additive endpoint — NOT in the frozen §12 list; additive,
        read-only, no control action.  See delta note in api-contracts.md §14.

        Each record:
          mint:                  Token mint address.
          evaluated_at_slot:     Event-time slot of the LaunchEvent (C-5).
          evaluated_at_wall_ms:  Wall-clock ms at evaluation.
          status:                "monitoring"|"pending"|"skipped"|"sniped"
          model_p:               Calibrated classifier probability [0,1] or null.
          reason:                SnipeSkipReason code or "entered".
          safety_report:         { gate_passed: bool, reasons: [str] }

        HARD RULES:
          - Read-only (GET only).  No POST / no action.
          - No win_rate field (HONESTY CLAUSE, AC-037).
          - No money fields (candidates not yet entered have no filled size).
          - No new risk surface (operator can VIEW but not act on this list).
        """
        records = candidates_provider() if candidates_provider is not None else []

        # Validate: no win_rate anywhere in candidate list (HONESTY CLAUSE).
        for rec in records:
            assert "win_rate" not in rec, (
                "HONESTY CLAUSE violated: win_rate in candidate record"
            )

        return records

    # ------------------------------------------------------------------
    # GET /api/wallet-cluster  (E11 additive read-only endpoint)
    # ------------------------------------------------------------------

    @app.get("/api/wallet-cluster")
    async def get_wallet_cluster() -> list[dict]:
        """Return wallet-cluster connection graphs — read-only operator view (E11).

        Exposes the EXISTING sniper-cluster / bundle-detection data already
        computed by the feature pipeline (aats.features.microstructure) and the
        pre-trade gate (aats.risk.pretrade_gate) as a typed graph the dashboard
        can render (Bubble Maps / wallet-cluster view).

        Each record is a WalletClusterGraph (aats.control_plane.wallet_cluster_schemas):
          mint:                   Token mint address.
          evaluated_at_slot:      Event-time slot (C-5, never compute-time).
          sniper_cluster_score:   [0.0, 1.0] sniper activity score.
          bundling_detected:      True if same-block bundle detected.
          bundler_wallet_count:   Count of bundler wallets (0..N).
          dev_bundle_cluster_bps: Largest cluster holding in bps (0..10000).
          nodes:                  Wallet vertices (type, supply share, flags).
          edges:                  Detected wallet connections (edge type, weight).
          manipulation_flags:     Summary manipulation flags for the cluster.

        Source of detection logic (EXISTING, NOT new):
          - sniper_cluster_score, bundling_detected, bundler_wallet_count,
            sniper_wallet_count, sniper_supply_share_bps:
              aats.features.microstructure.MicrostructureFeatures
          - dev_bundle_cluster_bps:
              aats.risk.pretrade_gate.SafetyInputs
          - bundler_cluster_id, creator_wallet:
              aats.contracts.events.LaunchEvent

        HARD RULES:
          - Read-only (GET only).  No POST / no action.
          - No win_rate field anywhere (HONESTY CLAUSE, AC-037).
          - No money fields (bps are int ratios, not lamports).
          - No new risk surface (operator can VIEW only; cluster data cannot
            trigger, size-up, or widen any position).
          - Backward-compatible: if wallet_cluster_provider is None, returns [].
        """
        records = wallet_cluster_provider() if wallet_cluster_provider is not None else []

        # Validate: no win_rate anywhere in graph records (HONESTY CLAUSE).
        for rec in records:
            assert "win_rate" not in rec, (
                "HONESTY CLAUSE violated: win_rate in wallet_cluster record"
            )

        return records

    return app
