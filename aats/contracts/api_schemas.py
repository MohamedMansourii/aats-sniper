"""Control-plane API Pydantic schemas — FROZEN contract (api-contracts.md v1.0.0).

These schemas are shared by:
  - The control-plane server (Lane D, agent-orchestration-engineer, T-341)
  - The operator dashboard (Lane E, frontend-engineer, T-352)
  - The Telegram bot (Lane F, backend-engineer, T-360/361)

FROZEN: After G1, only the solana-systems-architect may change these (ADR + delta notice).

ALL schemas conform to api-contracts.md EXACTLY:
  - Money fields: integer lamports OR decimal-as-string, NEVER float
  - AgentMode: SHADOW | PAPER | LIVE_DRY_RUN | LIVE (canonical 4-value enum)
  - SnipeEvent: snake_case wire format
  - GET = read-only; POST = de-risk ONLY; risk-increase → 403

Honesty clause AC-037 is enforced: no outcome-rate field on any schema.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# §2 — Canonical AgentMode enum (api-contracts.md §2)
# ---------------------------------------------------------------------------


class AgentMode(StrEnum):
    """The 4-value staging ladder for agent mode.

    Source: api-contracts.md §2 (FROZEN).
    LIVE requires DRY_RUN_ENABLED=false + CEO auth (AC-060).
    Moving down the ladder (toward SHADOW) is always permitted (de-risk).
    """

    SHADOW = "SHADOW"  # record only, submit nothing (default startup, FR-004)
    PAPER = "PAPER"  # full triple loop vs SimulationVenue; no real submit
    LIVE_DRY_RUN = "LIVE_DRY_RUN"  # real venue builds/signs but DOES NOT submit
    LIVE = "LIVE"  # real submit — ONLY with DRY_RUN_ENABLED=false + CEO auth


# ---------------------------------------------------------------------------
# §4 GET endpoint response schemas
# ---------------------------------------------------------------------------


class ConnectionState(BaseModel):
    """Connection health in AgentState."""

    geyser: bool
    shredstream: bool
    internal_ms: int  # integer ms, not float

    model_config = {"frozen": True}


class WalletState(BaseModel):
    """Wallet info in AgentState — balances are integer lamports (api-contracts §4)."""

    pubkey: str
    balance_lamports: int  # integer lamports, NOT float
    cap_lamports: int  # integer lamports, NOT float

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _reject_float_lamports(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        for field in ("balance_lamports", "cap_lamports"):
            val = data.get(field)
            if isinstance(val, float):
                raise ValueError(
                    f"WalletState.{field} must be int (lamports), got float {val!r}."
                )
        return data


class LoopStates(BaseModel):
    snipe: str
    fast: str
    slow: str

    model_config = {"frozen": True}


class AgentState(BaseModel):
    """GET /api/state response — current mode, loop states, connection, wallet, flags.

    Source: api-contracts.md §4 GET /api/state.
    dry_run_enabled: read-only mirror of the hard DRY-RUN flag.
    """

    mode: AgentMode
    loops: LoopStates
    connection: ConnectionState
    wallet: WalletState
    dry_run_enabled: bool  # read-only (infrastructure config, NOT settable via API)
    breaker_tripped: bool
    killed: bool

    model_config = {"frozen": True}


class ModelVsBaseline(BaseModel):
    """GATE-B headline metric (api-contracts.md §4 GET /api/metrics).

    All monetary quantities are decimal-as-string.
    Honesty clause AC-037: outcome-rate fields are excluded by design.
    """

    delta_net_pnl_per_sol_at_risk: str  # decimal-as-string
    lower_95_bound: str  # decimal-as-string
    n_test_windows: int
    gate_b_pass: bool

    model_config = {"frozen": True}


class GateAResult(BaseModel):
    """GATE-A result — aggregate net-of-cost PnL (api-contracts.md §4 GET /api/metrics)."""

    net_pnl_sol: str  # decimal-as-string
    lower_95_bound: str  # decimal-as-string
    gate_a_pass: bool

    model_config = {"frozen": True}


class MetricsSnapshot(BaseModel):
    """GET /api/metrics response.

    Source: api-contracts.md §4 GET /api/metrics.
    All PnL fields are decimal-as-string (NFR-009).
    Honesty clause AC-037: outcome-rate fields are excluded by design.
    """

    net_pnl_sol: str  # decimal-as-string; PRIMARY (AC-036)
    gross_pnl_sol: str  # decimal-as-string; secondary, labeled gross
    # AC-037 honesty clause: no outcome-rate field here
    land_rate_pct: float  # not money — float is correct
    median_slot_delay: int  # integer slots
    rug_avoidance_pct: float  # not money — float is correct
    tip_efficiency: float  # not money — float is correct
    model_vs_baseline: ModelVsBaseline  # GATE-B headline
    gate_a: GateAResult
    open_positions: int
    daily_pnl_sol: str  # decimal-as-string
    daily_loss_limit_sol: str  # decimal-as-string, e.g. "-0.30" (OQ-001/005)
    breaker_tripped: bool

    model_config = {"frozen": True}


class CostBreakdown(BaseModel):
    """Cost breakdown in a position record (api-contracts.md §4 GET /api/positions)."""

    tip_lamports: int  # integer lamports
    priority_lamports: int  # integer lamports
    entry_slippage_bps: int
    amm_fee_bps: int
    exit_slippage_bps: int
    adverse_selection_bps: int

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _reject_float_lamports(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        for field in ("tip_lamports", "priority_lamports"):
            val = data.get(field)
            if isinstance(val, float):
                raise ValueError(
                    f"CostBreakdown.{field} must be int (lamports), got float {val!r}."
                )
        return data


class PositionRecord(BaseModel):
    """A position record as returned by GET /api/positions.

    Source: api-contracts.md §4 GET /api/positions.
    sol_in_lamports and unrealized_pnl_net_lamports are integer lamports.
    Honesty clause AC-037: outcome-rate fields are excluded by design.
    """

    mint: str
    token: str
    entry_slot: int
    sol_in_lamports: int  # integer lamports (NOT float)
    unrealized_pnl_net_lamports: int  # integer lamports, labeled net (AC-039)
    entry_slip_bps: int
    tp_hit: int
    tp_total: int
    trailing_armed: bool
    hard_stop_pct: float  # fractional percentage, not money — float is correct
    exit_mode: Literal["secure", "fast"]
    fsm_state: Literal["IDLE", "ENTERING", "OPEN", "CLOSING", "CLOSED", "VETOED"]
    age_sec: int
    status: Literal["open", "closed"]
    realized_pnl_net_lamports: int | None  # integer lamports or None while open
    surface: str
    cost_breakdown: CostBreakdown

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _reject_float_money(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        for field in ("sol_in_lamports", "unrealized_pnl_net_lamports"):
            val = data.get(field)
            if isinstance(val, float):
                raise ValueError(
                    f"PositionRecord.{field} must be int (lamports), got float {val!r}."
                )
        realized = data.get("realized_pnl_net_lamports")
        if realized is not None and isinstance(realized, float):
            raise ValueError(
                f"PositionRecord.realized_pnl_net_lamports must be int (lamports), "
                f"got float {realized!r}."
            )
        return data


class LatencyHop(BaseModel):
    """A single hop in the latency budget (api-contracts.md §4 GET /api/latency).

    T-199b FIX: The wire key is "class" per the FROZEN api-contracts.md §4 /api/latency
    schema.  Python cannot use 'class' as an identifier, so the field is named 'cls'
    and the alias "class" is applied for both input parsing and serialisation.
    populate_by_name=True allows construction with either 'cls=' (Python) or 'class='
    (e.g. from parsed JSON), ensuring sibling-lane compatibility without a rename.
    """

    name: str
    ms: int
    budget_ms: int
    # FROZEN wire key is "class" (api-contracts.md §4).  Field named 'cls' in Python
    # to avoid the keyword conflict; alias + serialization_alias ensure round-trip fidelity.
    cls: str = Field(default="", alias="class", serialization_alias="class")
    p99_ms: int | None = None
    note: str | None = None

    model_config = {"frozen": True, "populate_by_name": True}


class InfraTier(BaseModel):
    """An infra tier for the latency page (api-contracts.md §4 GET /api/latency).

    Used by the dashboard's useInfraTiers hook reading from budget.tiers.
    """

    name: str
    land_rate: float
    median_slot_delay: int
    entry_slip_pct: float
    active: bool

    model_config = {"frozen": True}


class LatencyBudget(BaseModel):
    """GET /api/latency response (api-contracts.md §4).

    Splits internal compute from block-engine RTT (C-1; AC-050).
    All ms values are integer (not float money — but milliseconds are also integer here
    for determinism in latency accounting).
    """

    hops: list[LatencyHop]
    internal_ms: int
    block_engine_rtt_ms: int
    slot_floor_ms: int
    posture: str
    tiers: list[InfraTier]

    model_config = {"frozen": True}


class CostGate(BaseModel):
    """Cost gate result in a SnipeEvent (api-contracts.md §6)."""

    expected_edge_bps: int
    total_cost_bps: int
    passed: bool

    model_config = {"frozen": True}


class SnipeEvent(BaseModel):
    """SSE /api/feed event schema — FROZEN (api-contracts.md §6).

    Wire format is snake_case.  The dashboard adapter in api.ts maps
    snake_case → camelCase for the view model (api-contracts §6, Lane-E task).

    event_time carries both slot and wall_clock_ms (C-5).
    model_p and model_uncertainty are float (probabilities, not money).
    Honesty clause AC-037: outcome-rate fields are excluded by design.
    """

    id: str
    event_time: dict  # {"slot": int, "wall_clock_ms": int} — C-5 event-time
    observation_slot: int
    confirmation_slot: int
    detection_transport: Literal["geyser", "shredstream"]
    ts: int  # compute/emit time — distinct from event_time
    slot: int
    token: str
    mint: str
    source: Literal["pump.fun", "pumpswap", "raydium_v4", "raydium_cpmm", "migration"]
    gate_passed: bool
    gate_reasons: list[str]
    red_flags: list[str]
    model_p: float | None  # calibrated probability (null if no score)
    model_uncertainty: float | None
    action: Literal["sniped", "skipped", "vetoed"]
    veto_source: Literal["gate", "llm", "cost_gate", "null"] | None
    slot_delay: int
    smart_wallets: int  # adversarial selectivity feature (NEVER a buy trigger)
    tip_contention_bucket: Literal["low", "medium", "high"]
    cost_gate: CostGate | None
    pnl_pct: float | None  # None until closed; percentage, not money

    model_config = {"frozen": True}


class ModuleHealth(BaseModel):
    """GET /api/health entry — module status + staleness (api-contracts.md §4)."""

    name: str
    status: Literal["ok", "degraded", "down"]
    staleness_ms: int  # integer ms
    last_heartbeat_ms: int  # integer ms

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# §5 POST endpoint request/response schemas
# ---------------------------------------------------------------------------


class ModeRequest(BaseModel):
    """POST /api/mode body — may only step toward de-risk or require explicit CEO auth.

    Source: api-contracts.md §5 POST /api/mode.
    LIVE requires DRY_RUN_ENABLED=false + CEO auth (AC-060).
    """

    mode: AgentMode

    model_config = {"frozen": True}


class ModeResponse(BaseModel):
    """POST /api/mode response."""

    mode: AgentMode
    dry_run_enabled: bool

    model_config = {"frozen": True}


class KillResponse(BaseModel):
    """POST /api/kill response (202 Accepted)."""

    status: Literal["halting"]
    message: str = "kill initiated; entries halted, positions handed to ExitEngine"

    model_config = {"frozen": True}


class FlattenResponse(BaseModel):
    """POST /api/flatten or /api/flatten/{mint} response (202 Accepted)."""

    status: Literal["flattening"]
    mint: str | None = None  # None means "all positions"
    message: str = "flatten initiated"

    model_config = {"frozen": True}


class BreakerResetResponse(BaseModel):
    """POST /api/breaker/reset response (200 OK or 409 Conflict)."""

    state: Literal["ARMED"]  # only returned on 200; 409 returns ErrorEnvelope
    message: str = "breaker re-armed after manual review"

    model_config = {"frozen": True}


class ErrorEnvelope(BaseModel):
    """Standard non-2xx error envelope (api-contracts.md §3)."""

    error: str  # machine code, e.g. "risk_increase_rejected"
    message: str  # human-readable
    detail: dict = {}

    model_config = {"frozen": True}
