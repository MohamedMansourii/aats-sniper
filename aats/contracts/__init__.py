"""aats.contracts — Shared typed message contracts for the AATS ultra-sniper.

This package is the single source of truth for EVERY type that crosses a loop
boundary or is persisted to Redis / Parquet.  All other packages import from
here; none define their own wire shapes.

Law:
  - data-models.md (version 1.1.0, FROZEN post-G1-red-team) defines every
    schema; this package transcribes it verbatim.
  - api-contracts.md (version 1.0.0, FROZEN) defines the control-plane wire;
    the Pydantic models here match those shapes exactly.
  - execution-venue.md (version 1.1.0, FROZEN) defines the ExecutionVenue ABC;
    it is promoted here.
  - ADR-0006 (asymmetric trust) and ADR-0010 (provenance/lineage guards) are
    enforced structurally by the types in this package.

Hard rules (non-waivable; CI refuses a build that breaks them):
  - Money is int lamports or Decimal — NEVER float.
  - No win_rate field on any model.
  - ReasoningAction has NO risk-increase member.
  - Intent union has NO risk-increase variant.
  - No truth_* field on any production/validation model.
  - LLM never on the FAST/SNIPE path (structural, not comment).
  - DRY_RUN_ENABLED=true is the boot default; real capital is disabled.
  - event_time (slot-based) is the ONLY join anchor; wall_clock_ms is
    monitoring-only and MUST NOT be used for joins.
"""

from aats.contracts.api_schemas import (
    AgentMode,
    AgentState,
    BreakerResetResponse,
    ConnectionState,
    CostGate,
    ErrorEnvelope,
    FlattenResponse,
    InfraTier,
    KillResponse,
    LatencyBudget,
    LatencyHop,
    MetricsSnapshot,
    ModelVsBaseline,
    ModeRequest,
    ModeResponse,
    ModuleHealth,
    SnipeEvent,
    WalletState,
)
from aats.contracts.events import (
    DetectionTransport,
    EventTime,
    LaunchEvent,
    LaunchOutcome,
    LaunchOutcomeLabel,
    LaunchSource,
)
from aats.contracts.features import (
    FeatureFrame,
    FeatureProvenance,
    FeatureSourceWindow,
)
from aats.contracts.intents import (
    CostStack,
    EntryIntent,
    ExitIntent,
    Intent,
    IntentKind,
    ReduceIntent,
    VetoIntent,
)
from aats.contracts.models import (
    DecisionSignal,
    MCSScore,
    ReasoningAction,
    ReasoningVerdict,
    RegimeDeRiskDirective,
    RegimeSignal,
    RegimeState,
    regime_derisk_directive,
)
from aats.contracts.positions import (
    FSMState,
    Position,
)
from aats.contracts.provenance import (
    ALLOWED_LINEAGE_DATASETS,
    ProvenanceTaintError,
    assert_feature_frame_column_disjointness,
    assert_recorded_at_honesty,
    check_feature_window_cutoff,
    check_lineage_taint,
)
from aats.contracts.risk import (
    BreakerState,
    RiskConfig,
)
from aats.contracts.venue import (
    ExecutionVenue,
    FillResult,
    LandResult,
    Quote,
    Side,
    SignedTx,
    SimResult,
    SimulationVenue,
    SubmitMode,
    UnsignedTx,
    VenueRegistryEntry,
)

__all__ = [
    # events
    "DetectionTransport",
    "EventTime",
    "LaunchEvent",
    "LaunchOutcome",
    "LaunchOutcomeLabel",
    "LaunchSource",
    # features
    "FeatureFrame",
    "FeatureProvenance",
    "FeatureSourceWindow",
    # models / signals
    "DecisionSignal",
    "MCSScore",
    "ReasoningAction",
    "ReasoningVerdict",
    "RegimeDeRiskDirective",
    "RegimeSignal",
    "RegimeState",
    "regime_derisk_directive",
    # intents
    "CostStack",
    "EntryIntent",
    "ExitIntent",
    "Intent",
    "IntentKind",
    "ReduceIntent",
    "VetoIntent",
    # positions / FSM
    "FSMState",
    "Position",
    # risk
    "BreakerState",
    "RiskConfig",
    # venue
    "ExecutionVenue",
    "FillResult",
    "LandResult",
    "Quote",
    "Side",
    "SimResult",
    "SignedTx",
    "SimulationVenue",
    "SubmitMode",
    "UnsignedTx",
    "VenueRegistryEntry",
    # control-plane API schemas
    "AgentMode",
    "AgentState",
    "BreakerResetResponse",
    "ConnectionState",
    "CostGate",
    "ErrorEnvelope",
    "FlattenResponse",
    "InfraTier",
    "KillResponse",
    "LatencyBudget",
    "LatencyHop",
    "MetricsSnapshot",
    "ModelVsBaseline",
    "ModeRequest",
    "ModeResponse",
    "ModuleHealth",
    "SnipeEvent",
    "WalletState",
    # provenance / lineage guards (ADR-0010)
    "ALLOWED_LINEAGE_DATASETS",
    "ProvenanceTaintError",
    "assert_feature_frame_column_disjointness",
    "assert_recorded_at_honesty",
    "check_feature_window_cutoff",
    "check_lineage_taint",
]
