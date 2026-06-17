"""M4 Guardrails — risk engine, cost gate, circuit breaker, survivable stop.

Implemented by: risk-guardrails-engineer (T-320..T-326, T-329).

SAFETY-FIRST ordering (per TASKBOARD.md §3):
  T-320 (circuit breaker) → T-321 (survivable stop) → T-322 (DMS)
  MUST be proven before any live-capable execution path is enabled.

Key invariants (enforced by tests):
  - Money is integer lamports or Decimal.  NEVER float.
  - Circuit breaker: trip at -X% / -0.30 SOL; manual re-arm only; LLM may
    trip but NEVER reset (api-contracts.md §5, AC-029).
  - Cost gate: expected_edge_bps > total_cost_bps or NO TRADE (FR-027).
  - Sizing: monotonic non-increasing in all secondary signals (AC-021/031).
"""

# E13 — Anti-FOMO / already-pumped EXCLUSION filter (SLOW-loop, point-in-time;
# REJECT / DOWN-WEIGHT only; a mainstream/CEX/Forbes-tier mention is an EXCLUSION
# signal, never a buy trigger; the conviction multiplier is clamped to <= 1 so it
# can only SHRINK conviction).  (isort sorts `anti_fomo` ahead of `blacklist`.)
from aats.risk.anti_fomo import (
    AntiFomoFilter,
    AntiFomoInputs,
    AntiFomoTelemetry,
    AntiFomoThresholds,
    FomoDecision,
    FomoReason,
    FomoReasonHit,
    FomoVerdict,
    MentionTier,
    parse_mention_tier,
)

# E2 — Creator/token denylist + trusted allowlist pre-filter (file-backed,
# hot-reloadable; runs BEFORE the safety gate; REJECT-only; allowlist never skips
# the safety gate).  (isort sorts `blacklist` ahead of `circuit_breaker`.)
from aats.risk.blacklist import (
    DEFAULT_DENYLIST_PATH,
    DenyAllowSnapshot,
    DenyAllowWatcher,
    DenylistTelemetry,
    PreGateResult,
    evaluate_with_denylist,
    parse_snapshot,
)

# T-320 — Daily-loss circuit breaker (HARD HALT) — BUILT + PROVEN FIRST.
from aats.risk.circuit_breaker import (
    DEFAULT_SOFT_RATIO,
    BreakerNotTripped,
    BreakerStore,
    BreakerTelemetry,
    CircuitBreaker,
    FlattenHandler,
    InMemoryBreakerStore,
    LLMDeRiskSignal,
    OperatorResetToken,
    PnLEvent,
    mint_operator_reset_token,
)

# T-324 — Hierarchical risk rule engine + fractional-Kelly sizing + cost-aware
# entry gate.  Deterministic, de-risk-only; lives in the FAST loop (<100ms),
# NEVER awaits an LLM (the LLM's catastrophic-exit arrives as a PRE-SET flag).
from aats.risk.cost_model import (
    REASON_EDGE_BELOW_COST,
    REASON_HAIRCUT_BELOW_FLOOR,
    REASON_NEGATIVE_EDGE,
    REASON_NEGATIVE_LINE,
    REASON_TOTAL_MISMATCH,
    CostAwareEntryGate,
    CostGateDecision,
)

# T-321 — Survivable stop (3 independent layers; holds if the bot process dies).
# T-322 — the aats-dms SEPARATE-FAILURE-DOMAIN service backing the breaker.
# NOTE: T-321 + T-322 modules form one first-party `aats.risk.*` import group;
# ruff/isort sorts them alphabetically as a unit, so the two task families are
# interleaved here by module name rather than grouped by task id.
from aats.risk.deadman import (
    DeadMansSwitch,
    DmsStandDownToken,
    DmsTelemetry,
    EmergencyFlattenVenue,
    mint_dms_standdown_token,
)
from aats.risk.dms_main import (
    DmsService,
    DmsServiceConfig,
    DryRunBlockEngineSubmitter,
    RedisHeartbeatStore,
)
from aats.risk.dms_service import (
    BreakerView,
    DmsServiceTelemetry,
    DmsWatchdog,
    Heartbeat,
    HeartbeatStore,
    InMemoryHeartbeatStore,
    build_heartbeat,
)
from aats.risk.exit_engine import (
    AUTO_STRAT_PRESETS,
    DEFAULT_PRESET_LABEL,
    FAST_SLIPPAGE,
    SECURE_SLIPPAGE,
    ExitConfig,
    ExitDecision,
    ExitEngine,
    ExitReason,
    ExitState,
    MevExitMode,
    MevSlippageModel,
    TpRung,
    preset,
    slippage_model_for,
)
from aats.risk.exit_sim import (
    ABResult,
    ScenarioLaunch,
    WalkResult,
    gate_and_fill,
    generate_path,
    generate_scenario_launches,
    naive_walk_position,
    run_ab,
    walk_position,
)

# AUDIT-liquidity — pre-trade LIQUIDITY-SANITY gate (depth + slippage simulation):
# 24h volume >= ~10x intended position AND simulated entry slippage within a
# threshold.  REJECT-only (selectivity / de-risk); point-in-time; FAST-path-safe
# (0 RPC, no LLM, no social/screener input); never a standalone buy trigger.
from aats.risk.liquidity_sanity import (
    LiquidityDecision,
    LiquidityInputs,
    LiquidityReject,
    LiquidityRejectHit,
    LiquiditySanityGate,
    LiquidityTelemetry,
    LiquidityThresholds,
    LiquidityVerdict,
    simulate_entry_slippage_bps,
)

# AUDIT-micropreset — the EARLY-ENTRY MICRO preset (a NAMED composition of the
# ALREADY-BUILT safety gate + sizing + exit primitives; de-risk / selectivity
# only, tighten-only, never a standalone buy trigger).
from aats.risk.presets import (
    ENTRY_PRESETS,
    MICRO_EXIT_CONFIG,
    MICRO_HARD_STOP_R,
    MICRO_MIN_LP_LOCK_DAYS,
    MICRO_MIN_LP_LOCKED_BPS,
    MICRO_PRESET,
    MICRO_PRESET_LABEL,
    MICRO_SAFETY_THRESHOLDS,
    MICRO_SIZE_FRACTION_BPS,
    MICRO_TOP5_CONCENTRATION_MAX_BPS,
    MicroPreset,
    MicroRedFlag,
    MicroSafetyFacts,
    MicroSizingResult,
    entry_preset,
    micro_red_flag_codes,
    micro_size,
)
from aats.risk.presigned_flatten import (
    BlockEngineSubmitter,
    PreSignedFlatten,
    PreSignedFlattenHolder,
)

# T-323 — Sub-10ms pre-trade safety gate / token-safety scanner (local hot-path
# checks, 0 RPC; reject-only; sell-sim seam stubbed for T-329).
from aats.risk.pretrade_gate import (
    BPS_DENOM,
    HOT_CHECK_ORDER,
    GateDecision,
    GateTelemetry,
    GateVerdict,
    PreTradeSafetyGate,
    RedFlag,
    RedFlagHit,
    SafetyInputs,
    SafetyThresholds,
    SellSimProbe,
)
from aats.risk.resting_orders import (
    BuyFireContext,
    BuyFireRejection,
    BuyFireResult,
    DcaPlan,
    EntryGateChain,
    GatedEntryBuilder,
    GatedEntryFireChain,
    InMemoryRestingOrderStore,
    OffBoxSellKeeper,
    RestingBuyOrder,
    RestingOrder,
    RestingOrderBook,
    RestingOrderStore,
    RestingSellOrder,
    RestingSide,
    TickFire,
    TriggerDirection,
)
from aats.risk.rule_engine import (
    HierarchicalRiskRuleEngine,
    RuleInputs,
    RuleOutcome,
    RuleOutcomeKind,
    RulePriority,
    TakeProfitLevel,
)

# E8 — Tunable discovery / SCREENER filter (late-entry / survivor niche).
# SLOW-LOOP ONLY; REJECT-or-PASS only (selectivity / de-risk); never a buy trigger.
from aats.risk.screener import (
    DiscoveryScreener,
    ScreenDecision,
    ScreenerTelemetry,
    ScreenerThresholds,
    ScreenInputs,
    ScreenReason,
    ScreenReasonHit,
    ScreenVerdict,
)
from aats.risk.secondary_enforcer import (
    MarkTick,
    SecondaryStopEnforcer,
    StopFireTelemetry,
)
from aats.risk.sizing import (
    DeRiskSignals,
    FractionalKellySizer,
    SizingDecision,
    full_kelly_fraction,
)
from aats.risk.survivable_stop import (
    StopState,
    StopThresholds,
    derive_stop_trigger_price,
)
from aats.risk.survivable_stop_coordinator import (
    ArmResult,
    SurvivableStopCoordinator,
)
from aats.risk.venue_native_stop import (
    RestingStopOrder,
    RestingStopVenue,
    SimulationRestingStopVenue,
    VenueNativeStopPlacer,
    resting_order_from_stop,
)

__all__ = [
    # T-320
    "DEFAULT_SOFT_RATIO",
    "BreakerNotTripped",
    "BreakerStore",
    "BreakerTelemetry",
    "CircuitBreaker",
    "FlattenHandler",
    "InMemoryBreakerStore",
    "LLMDeRiskSignal",
    "OperatorResetToken",
    "PnLEvent",
    "mint_operator_reset_token",
    # T-321 — rule + thresholds
    "StopState",
    "StopThresholds",
    "derive_stop_trigger_price",
    # T-321 — Layer 2 in-process enforcer
    "MarkTick",
    "SecondaryStopEnforcer",
    "StopFireTelemetry",
    # T-321 — Layer 1 venue-native resting order
    "RestingStopOrder",
    "RestingStopVenue",
    "SimulationRestingStopVenue",
    "VenueNativeStopPlacer",
    "resting_order_from_stop",
    # T-321 — Layer 3 dead-man's switch
    "DeadMansSwitch",
    "DmsStandDownToken",
    "DmsTelemetry",
    "EmergencyFlattenVenue",
    "mint_dms_standdown_token",
    # T-321 — coordinator
    "ArmResult",
    "SurvivableStopCoordinator",
    # T-322 — aats-dms separate-failure-domain service
    "BreakerView",
    "DmsServiceTelemetry",
    "DmsWatchdog",
    "Heartbeat",
    "HeartbeatStore",
    "InMemoryHeartbeatStore",
    "build_heartbeat",
    "BlockEngineSubmitter",
    "PreSignedFlatten",
    "PreSignedFlattenHolder",
    "DmsService",
    "DmsServiceConfig",
    "DryRunBlockEngineSubmitter",
    "RedisHeartbeatStore",
    # E13 — anti-FOMO / already-pumped exclusion filter (SLOW-loop, de-risk only)
    "AntiFomoFilter",
    "AntiFomoInputs",
    "AntiFomoTelemetry",
    "AntiFomoThresholds",
    "FomoDecision",
    "FomoReason",
    "FomoReasonHit",
    "FomoVerdict",
    "MentionTier",
    "parse_mention_tier",
    # E2 — creator/token denylist + trusted allowlist pre-filter
    "DEFAULT_DENYLIST_PATH",
    "DenyAllowSnapshot",
    "DenyAllowWatcher",
    "DenylistTelemetry",
    "PreGateResult",
    "evaluate_with_denylist",
    "parse_snapshot",
    # T-323 — pre-trade safety gate / token-safety scanner
    "BPS_DENOM",
    "HOT_CHECK_ORDER",
    "GateDecision",
    "GateTelemetry",
    "GateVerdict",
    "PreTradeSafetyGate",
    "RedFlag",
    "RedFlagHit",
    "SafetyInputs",
    "SafetyThresholds",
    "SellSimProbe",
    # T-324 — hierarchical rule engine
    "HierarchicalRiskRuleEngine",
    "RuleInputs",
    "RuleOutcome",
    "RuleOutcomeKind",
    "RulePriority",
    "TakeProfitLevel",
    # E8 — tunable discovery / screener filter (SLOW-loop, selectivity-only)
    "DiscoveryScreener",
    "ScreenDecision",
    "ScreenInputs",
    "ScreenReason",
    "ScreenReasonHit",
    "ScreenVerdict",
    "ScreenerTelemetry",
    "ScreenerThresholds",
    # T-326 — durable resting LIMIT + DCA orders (fire offline; de-risk-direction)
    "BuyFireContext",
    "BuyFireRejection",
    "BuyFireResult",
    "DcaPlan",
    "EntryGateChain",
    "GatedEntryBuilder",
    "GatedEntryFireChain",
    "InMemoryRestingOrderStore",
    "OffBoxSellKeeper",
    "RestingBuyOrder",
    "RestingOrder",
    "RestingOrderBook",
    "RestingOrderStore",
    "RestingSellOrder",
    "RestingSide",
    "TickFire",
    "TriggerDirection",
    # T-325 — productionized ExitEngine (TP-ladder + trailing + hard-stop + timeout)
    "AUTO_STRAT_PRESETS",
    "DEFAULT_PRESET_LABEL",
    "FAST_SLIPPAGE",
    "SECURE_SLIPPAGE",
    "ExitConfig",
    "ExitDecision",
    "ExitEngine",
    "ExitReason",
    "ExitState",
    "MevExitMode",
    "MevSlippageModel",
    "TpRung",
    "preset",
    "slippage_model_for",
    # T-325 — paper-walk + A/B-vs-naive harness
    "ABResult",
    "ScenarioLaunch",
    "WalkResult",
    "gate_and_fill",
    "generate_path",
    "generate_scenario_launches",
    "naive_walk_position",
    "run_ab",
    "walk_position",
    # T-324 — fractional-Kelly sizing + caps
    "DeRiskSignals",
    "FractionalKellySizer",
    "SizingDecision",
    "full_kelly_fraction",
    # T-324 — cost-aware entry gate
    "CostAwareEntryGate",
    "CostGateDecision",
    "REASON_EDGE_BELOW_COST",
    "REASON_HAIRCUT_BELOW_FLOOR",
    "REASON_NEGATIVE_EDGE",
    "REASON_NEGATIVE_LINE",
    "REASON_TOTAL_MISMATCH",
    # AUDIT-micropreset — the EARLY-ENTRY MICRO preset (named composition)
    "ENTRY_PRESETS",
    "MICRO_EXIT_CONFIG",
    "MICRO_HARD_STOP_R",
    "MICRO_MIN_LP_LOCK_DAYS",
    "MICRO_MIN_LP_LOCKED_BPS",
    "MICRO_PRESET",
    "MICRO_PRESET_LABEL",
    "MICRO_SAFETY_THRESHOLDS",
    "MICRO_SIZE_FRACTION_BPS",
    "MICRO_TOP5_CONCENTRATION_MAX_BPS",
    "MicroPreset",
    "MicroRedFlag",
    "MicroSafetyFacts",
    "MicroSizingResult",
    "entry_preset",
    "micro_red_flag_codes",
    "micro_size",
    # AUDIT-liquidity — pre-trade liquidity-sanity gate (depth + slippage sim)
    "LiquidityDecision",
    "LiquidityInputs",
    "LiquidityReject",
    "LiquidityRejectHit",
    "LiquiditySanityGate",
    "LiquidityTelemetry",
    "LiquidityThresholds",
    "LiquidityVerdict",
    "simulate_entry_slippage_bps",
]
