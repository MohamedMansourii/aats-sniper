/**
 * AATS Sniper — LIVE control-plane wire adapters (T-352).
 *
 * The control plane (Lane D, T-341) speaks the FROZEN wire contract
 * (api-contracts.md, T-201): snake_case keys, money as **integer base units
 * (lamports)** or **decimal strings**, the canonical 4-value `AgentMode` enum,
 * and the `/api/latency` hop discriminator on the wire key `"class"`.
 *
 * The dashboard's 10 pages were built against a camelCase view-model
 * (src/lib/types.ts). This module is the single, pure, side-effect-free seam
 * that maps wire → view-model for the live (VITE_USE_MOCK=false) transport.
 * The mock branch never touches this file.
 *
 * MONEY DISCIPLINE (NFR-009, AC-036 — non-waivable):
 *   - Money arrives as integer lamports or a Decimal-as-string. It is NEVER a
 *     JSON float and is NEVER parsed into a float for arithmetic that drives a
 *     decision. The only float that appears here is a *display* SOL value
 *     derived deterministically from the integer/string source, used purely to
 *     feed the existing presentational components (which format-and-round).
 *   - `lamportsToSolDisplay` / `decimalStringToSolDisplay` are the only two
 *     conversion points, both clearly display-only.
 *
 * No win-rate field, target, or symbol exists anywhere in this module
 * (HONESTY CLAUSE, AC-037).
 */

import type {
  AgentMode,
  AgentState,
  Candidate,
  CandidateStatus,
  CostBreakdown,
  Hop,
  InfraTier,
  LatencyBudget,
  MCSScore,
  MetricsSnapshot,
  ModuleHealth,
  NarrativeSignal,
  NarrativeSource,
  NewsCredibilityTier,
  Position,
  Prediction,
  Reasoning,
  RedFlag,
  RestingOrder,
  RiskConfig,
  SmartWallet,
  SnipeEvent,
  SnipeSource,
  GateReason,
  ManipulationFlag,
  WalletClusterGraph,
  WalletEdge,
  WalletEdgeType,
  WalletNode,
  WalletNodeType,
} from "./types";

/* -------------------------------------------------------------------------- */
/*  Money — integer lamports / decimal-string → display SOL (NEVER float math) */
/* -------------------------------------------------------------------------- */

const LAMPORTS_PER_SOL = 1_000_000_000;

/**
 * Convert integer lamports to a display SOL number.
 *
 * Display-only: the source of truth on the wire is the integer lamport count.
 * We divide for presentation; no monetary decision is ever made on this number.
 * Non-integer / non-finite input is treated as 0 (defensive; the contract
 * guarantees integers, but a malformed frame must not crash the deck).
 */
export function lamportsToSolDisplay(lamports: number | null | undefined): number {
  if (typeof lamports !== "number" || !Number.isFinite(lamports)) return 0;
  return lamports / LAMPORTS_PER_SOL;
}

/**
 * Convert a Decimal-as-string money value (e.g. "-0.0123") to a display number.
 *
 * Display-only. The wire keeps the exact decimal string; we only call Number()
 * to feed the presentational formatters. A non-numeric string yields 0.
 */
export function decimalStringToSolDisplay(
  value: string | number | null | undefined,
): number {
  if (typeof value === "number") return Number.isFinite(value) ? value : 0;
  if (typeof value !== "string") return 0;
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

/* -------------------------------------------------------------------------- */
/*  AgentMode — canonical 4-value wire enum  ↔  dashboard view-model           */
/* -------------------------------------------------------------------------- */

/**
 * Canonical wire enum (api-contracts.md §2, FROZEN):
 *   SHADOW | PAPER | LIVE_DRY_RUN | LIVE
 */
export type WireAgentMode = "SHADOW" | "PAPER" | "LIVE_DRY_RUN" | "LIVE";

/**
 * Wire mode → dashboard view-model mode.
 *
 * The dashboard view-model (`AgentMode`) predates the frozen 4-value ladder and
 * is pinned by the T-351 destructive tests to `paper | dry-run | live`. We map:
 *   SHADOW       → paper    (no broadcast, zero capital — the safest tile)
 *   PAPER        → paper
 *   LIVE_DRY_RUN → dry-run  (builds+signs, never submits — FR-039 dry-run)
 *   LIVE         → live
 * Unknown wire values fall back to the safest view-model mode (`paper`).
 */
export function fromWireMode(wire: string | null | undefined): AgentMode {
  switch (wire) {
    case "SHADOW":
    case "PAPER":
      return "paper";
    case "LIVE_DRY_RUN":
      return "dry-run";
    case "LIVE":
      return "live";
    default:
      return "paper";
  }
}

/**
 * Dashboard view-model mode → canonical wire mode.
 *
 * `dry-run` maps to the contract's `LIVE_DRY_RUN` (real path builds+signs, never
 * submits). `paper` maps to `PAPER`. `live` maps to `LIVE` — but note the
 * control plane independently fences `LIVE` behind DRY_RUN=false + CEO auth
 * (AC-060): this mapping only names the target; it cannot itself enable LIVE.
 *
 * DE-RISK ONLY: this function performs no risk increase. The server is the sole
 * authority on whether a mode change is accepted; a `LIVE` request without the
 * gates is rejected server-side with 403.
 */
export function toWireMode(mode: AgentMode): WireAgentMode {
  switch (mode) {
    case "paper":
      return "PAPER";
    case "dry-run":
      return "LIVE_DRY_RUN";
    case "live":
      return "LIVE";
  }
}

/* -------------------------------------------------------------------------- */
/*  Wire shapes (snake_case) — typed; no `any` shortcuts                       */
/* -------------------------------------------------------------------------- */

export interface WireAgentState {
  mode?: string;
  loops?: { snipe?: string; fast?: string; slow?: string };
  connection?: { geyser?: boolean; shredstream?: boolean; internal_ms?: number };
  wallet?: { pubkey?: string; balance_lamports?: number; cap_lamports?: number };
  dry_run_enabled?: boolean;
  breaker_tripped?: boolean;
  killed?: boolean;
}

export interface WireSnipeEvent {
  id?: string;
  ts?: number;
  slot?: number;
  token?: string;
  source?: string;
  gate_passed?: boolean;
  gate_reasons?: string[];
  model_p?: number | null;
  action?: string;
  veto_source?: string | null;
  slot_delay?: number | null;
  smart_wallets?: number;
  pnl_pct?: number | null;
  red_flags?: string[];
}

export interface WireHop {
  name?: string;
  ms?: number;
  budget_ms?: number;
  /** FROZEN wire discriminator key is "class" (T-199b LatencyHop alias). */
  class?: string;
  p99_ms?: number;
  note?: string;
}

export interface WireInfraTier {
  name?: string;
  land_rate?: number;
  median_slot_delay?: number;
  entry_slip_pct?: number;
  active?: boolean;
}

export interface WireLatencyBudget {
  hops?: WireHop[];
  internal_ms?: number;
  block_engine_rtt_ms?: number;
  slot_floor_ms?: number;
  posture?: string;
  tiers?: WireInfraTier[];
}

export interface WireCostBreakdown {
  tip_lamports?: number;
  priority_lamports?: number;
  entry_slippage_bps?: number;
  amm_fee_bps?: number;
  exit_slippage_bps?: number;
  adverse_selection_bps?: number;
  total_cost_bps?: number;
}

export interface WirePosition {
  mint?: string;
  token?: string;
  entry_slot?: number;
  sol_in_lamports?: number;
  unrealized_pnl_net_lamports?: number;
  unrealized_pnl_gross_lamports?: number;
  entry_slip_bps?: number;
  tp_hit?: number;
  tp_total?: number;
  trailing_armed?: boolean;
  hard_stop_pct?: number;
  exit_mode?: string;
  fsm_state?: string;
  age_sec?: number;
  status?: string;
  realized_pnl_net_lamports?: number | null;
  realized_pnl_gross_lamports?: number | null;
  surface?: string;
  cost_breakdown?: WireCostBreakdown;
}

export interface WireMCSScore {
  asset?: string;
  conviction?: number;
  momentum?: number;
  novelty?: number;
  synchronicity?: number;
  red_flags?: string[];
  post_count?: number;
  reasoning?: string;
}

export interface WirePrediction {
  ts?: number;
  model_p?: number;
  baseline_p?: number;
  uncertainty?: number;
  calibration_bins?: { p?: number; actual?: number }[];
  feature_importance?: { name?: string; weight?: number }[];
}

export interface WireReasoning {
  id?: string;
  ts?: number;
  token?: string;
  signal?: string;
  confidence?: number;
  veto?: boolean;
  narrative_failure?: boolean;
  rationale?: string;
  action_received?: string;
  action_applied?: string;
  risk_increase_clamped?: boolean;
}

export interface WireRiskConfig {
  per_trade_cap_lamports?: number;
  max_aggregate_lamports?: number;
  daily_loss_limit_pct?: string;
  daily_loss_floor_lamports?: number;
  max_slippage_bps?: number;
  snipe_threshold?: number;
  veto_threshold?: number;
  jito_tip_cap_frac?: string;
}

export interface WireModuleHealth {
  name?: string;
  status?: string;
  staleness_ms?: number;
  latency_ms?: number;
}

export interface WireModelVsBaseline {
  delta_net_pnl_per_sol_at_risk?: string;
}

export interface WireMetricsSnapshot {
  net_pnl_sol?: string;
  land_rate_pct?: number;
  median_slot_delay?: number;
  rug_avoidance_pct?: number;
  tip_efficiency?: number;
  model_vs_baseline?: WireModelVsBaseline;
  open_positions?: number;
  daily_pnl_sol?: string;
  daily_loss_limit_sol?: string;
  breaker_tripped?: boolean;
}

/* -------------------------------------------------------------------------- */
/*  Small coercion helpers                                                    */
/* -------------------------------------------------------------------------- */

const num = (v: unknown, fallback = 0): number =>
  typeof v === "number" && Number.isFinite(v) ? v : fallback;
const str = (v: unknown, fallback = ""): string =>
  typeof v === "string" ? v : fallback;
const bool = (v: unknown, fallback = false): boolean =>
  typeof v === "boolean" ? v : fallback;

const GATE_REASONS: ReadonlySet<string> = new Set<GateReason>([
  "freeze_authority",
  "mint_authority",
  "lp_unlocked",
  "sniper_cluster",
  "high_tax",
  "low_liquidity",
  "passed",
]);

const SNIPE_SOURCES: ReadonlySet<string> = new Set<SnipeSource>([
  "pump.fun",
  "pumpswap",
  "raydium_v4",
  "raydium_cpmm",
  "migration",
]);

/* -------------------------------------------------------------------------- */
/*  Adapters: wire → view-model                                               */
/* -------------------------------------------------------------------------- */

export function adaptAgentState(w: WireAgentState): AgentState {
  const snipeRaw = str(w.loops?.snipe, "idle");
  const snipe: AgentState["loops"]["snipe"] =
    snipeRaw === "armed" || snipeRaw === "firing" ? snipeRaw : "idle";
  return {
    mode: fromWireMode(w.mode),
    loops: {
      snipe,
      fast: str(w.loops?.fast, "—"),
      slow: str(w.loops?.slow, "—"),
    },
    connection: {
      geyser: bool(w.connection?.geyser),
      shredstream: bool(w.connection?.shredstream),
      internalMs: num(w.connection?.internal_ms),
    },
    wallet: {
      pubkey: str(w.wallet?.pubkey),
      // balance_lamports is the integer source of truth; display-only conversion.
      balanceSol: lamportsToSolDisplay(w.wallet?.balance_lamports),
      capSol: lamportsToSolDisplay(w.wallet?.cap_lamports),
    },
    killed: bool(w.killed),
  };
}

export function adaptSnipeEvent(w: WireSnipeEvent): SnipeEvent {
  const action = str(w.action);
  const reasons = (w.gate_reasons ?? []).filter((r): r is GateReason =>
    GATE_REASONS.has(r),
  );
  const source = SNIPE_SOURCES.has(str(w.source))
    ? (w.source as SnipeSource)
    : "pump.fun";
  return {
    id: str(w.id),
    ts: num(w.ts, Date.now()),
    slot: num(w.slot),
    token: str(w.token),
    source,
    gatePassed: bool(w.gate_passed),
    gateReasons: reasons,
    modelP: typeof w.model_p === "number" ? w.model_p : null,
    action:
      action === "sniped" || action === "skipped" || action === "vetoed"
        ? action
        : "skipped",
    slotDelay: typeof w.slot_delay === "number" ? w.slot_delay : null,
    smartWallets: num(w.smart_wallets),
    // pnl_pct is a percentage, not a monetary amount (no money-as-float concern).
    pnlPct: typeof w.pnl_pct === "number" ? w.pnl_pct : null,
    // Token-safety scanner red flags (FR-037, AC-015). Unknown codes are kept
    // verbatim — an operator must never miss a safety warning to a typo.
    redFlags: (w.red_flags ?? []).filter(
      (r): r is RedFlag => typeof r === "string",
    ),
  };
}

export function adaptHop(w: WireHop): Hop {
  return {
    name: str(w.name),
    ms: num(w.ms),
    budgetMs: num(w.budget_ms),
  };
}

export function adaptInfraTier(w: WireInfraTier): InfraTier {
  return {
    name: str(w.name),
    // Wire land_rate is a fraction (0.38); the view-model renders a percentage.
    landRate: num(w.land_rate) * 100,
    medianSlotDelay: num(w.median_slot_delay),
    entrySlipPct: num(w.entry_slip_pct),
  };
}

export function adaptLatencyBudget(w: WireLatencyBudget): LatencyBudget {
  return {
    hops: (w.hops ?? []).map(adaptHop),
    internalMs: num(w.internal_ms),
    slotFloorMs: num(w.slot_floor_ms),
  };
}

export function adaptInfraTiers(w: WireLatencyBudget): InfraTier[] {
  return (w.tiers ?? []).map(adaptInfraTier);
}

const FSM_STATES: ReadonlySet<string> = new Set<Position["fsmState"]>([
  "IDLE",
  "ENTERING",
  "OPEN",
  "CLOSING",
  "CLOSED",
  "VETOED",
]);

/** Wire CostStack → view-model CostBreakdown (integer base units → display SOL). */
export function adaptCostBreakdown(w: WireCostBreakdown | undefined): CostBreakdown {
  const c = w ?? {};
  return {
    totalBps: num(c.total_cost_bps),
    // tip/priority are integer lamports on the wire; display-only conversion.
    tipSol: lamportsToSolDisplay(c.tip_lamports),
    prioritySol: lamportsToSolDisplay(c.priority_lamports),
    entrySlippageBps: num(c.entry_slippage_bps),
    ammFeeBps: num(c.amm_fee_bps),
    exitSlippageBps: num(c.exit_slippage_bps),
    adverseSelectionBps: num(c.adverse_selection_bps),
  };
}

export function adaptPosition(w: WirePosition): Position {
  const entrySol = lamportsToSolDisplay(w.sol_in_lamports);
  const unrealizedSol = lamportsToSolDisplay(w.unrealized_pnl_net_lamports);
  // currentPct is unrealized PnL as a percentage of entry notional. Derived from
  // the integer lamport fields; this is a percentage, not a monetary quantity.
  const currentPct = entrySol > 0 ? (unrealizedSol / entrySol) * 100 : 0;
  const exitRaw = str(w.exit_mode, "secure");
  const status = str(w.status, "open") === "closed" ? "closed" : "open";
  const fsmRaw = str(w.fsm_state, status === "closed" ? "CLOSED" : "OPEN");
  return {
    mint: str(w.mint),
    token: str(w.token),
    entrySol,
    currentPct,
    // entry_slip_bps (basis points) → percent for the view-model.
    entrySlipPct: num(w.entry_slip_bps) / 100,
    tpHit: num(w.tp_hit),
    tpTotal: num(w.tp_total),
    trailingArmed: bool(w.trailing_armed),
    hardStopPct: num(w.hard_stop_pct),
    exitMode: exitRaw === "fast" ? "fast" : "secure",
    ageSec: num(w.age_sec),
    status,
    // PnL is NET-of-cost first (the headline); gross is secondary.
    realizedPnlSol:
      typeof w.realized_pnl_net_lamports === "number"
        ? lamportsToSolDisplay(w.realized_pnl_net_lamports)
        : null,
    realizedGrossPnlSol:
      typeof w.realized_pnl_gross_lamports === "number"
        ? lamportsToSolDisplay(w.realized_pnl_gross_lamports)
        : null,
    unrealizedPnlSol: unrealizedSol,
    fsmState: FSM_STATES.has(fsmRaw)
      ? (fsmRaw as Position["fsmState"])
      : "OPEN",
    surface: str(w.surface),
    cost: adaptCostBreakdown(w.cost_breakdown),
  };
}

export function adaptPositions(w: WirePosition[]): Position[] {
  return (w ?? []).map(adaptPosition);
}

export function adaptMCSScore(w: WireMCSScore): MCSScore {
  return {
    asset: str(w.asset),
    conviction: num(w.conviction),
    momentum: num(w.momentum),
    novelty: num(w.novelty),
    synchronicity: num(w.synchronicity),
    redFlags: (w.red_flags ?? []).filter((r): r is string => typeof r === "string"),
    postCount: num(w.post_count),
    reasoning: str(w.reasoning),
  };
}

export function adaptSentiment(w: WireMCSScore[]): MCSScore[] {
  return (w ?? []).map(adaptMCSScore);
}

export function adaptPrediction(w: WirePrediction): Prediction {
  return {
    ts: num(w.ts, Date.now()),
    classifierP: num(w.model_p),
    baselineP: num(w.baseline_p),
    calibrationBins: (w.calibration_bins ?? []).map((b) => ({
      p: num(b.p),
      actual: num(b.actual),
    })),
    featureImportance: (w.feature_importance ?? []).map((f) => ({
      name: str(f.name),
      weight: num(f.weight),
    })),
  };
}

const SIGNALS: ReadonlySet<string> = new Set<Reasoning["signal"]>([
  "Strong Buy",
  "Weak Buy",
  "Hold",
  "Sell",
  "Strong Sell",
]);

export function adaptReasoning(w: WireReasoning): Reasoning {
  const signal = str(w.signal);
  return {
    id: str(w.id),
    ts: num(w.ts, Date.now()),
    token: str(w.token),
    signal: SIGNALS.has(signal) ? (signal as Reasoning["signal"]) : "Hold",
    confidence: num(w.confidence),
    veto: bool(w.veto),
    narrativeFailure: bool(w.narrative_failure),
    rationale: str(w.rationale),
  };
}

export function adaptReasoningLog(w: WireReasoning[]): Reasoning[] {
  return (w ?? []).map(adaptReasoning);
}

export function adaptRiskConfig(w: WireRiskConfig): RiskConfig {
  return {
    // Caps arrive as integer lamports; the view-model slider edits SOL.
    maxPositionSol: lamportsToSolDisplay(w.per_trade_cap_lamports),
    // No portfolio-% field on the wire; the slider is informational here.
    maxPositionPct: 10,
    // The wire carries no separate stop/take-profit; surface contract values
    // the dashboard already understands, leaving the wire fields it does have.
    stopLossPct: 18,
    takeProfitPct: 60,
    maxSlippageBps: num(w.max_slippage_bps, 150),
    // daily_loss_floor_lamports is the absolute SOL floor (integer → display).
    dailyLossLimitSol: lamportsToSolDisplay(w.daily_loss_floor_lamports),
    maxHoldMin: 240,
    snipeThreshold: num(w.snipe_threshold, 0.62),
    vetoThreshold: num(w.veto_threshold, 0.35),
    // jito_tip_cap_frac is a Decimal-string fraction; display-only conversion.
    jitoTipCapSol: decimalStringToSolDisplay(w.jito_tip_cap_frac),
  };
}

/**
 * View-model RiskConfig → wire RiskConfig (for the tighten-only POST).
 *
 * Emits only the contract-recognized wire fields, converted back to base units.
 * This NEVER widens a limit on its own — the control plane independently
 * validates field-by-field and rejects any widening with 403
 * (api-contracts.md §5). Converting back to integer lamports keeps money out of
 * float on the wire; the view-model SOL editor is display/edit-only.
 */
export function toWireRiskConfig(v: RiskConfig): WireRiskConfig {
  return {
    per_trade_cap_lamports: Math.round(v.maxPositionSol * LAMPORTS_PER_SOL),
    daily_loss_floor_lamports: Math.round(v.dailyLossLimitSol * LAMPORTS_PER_SOL),
    max_slippage_bps: Math.round(v.maxSlippageBps),
    snipe_threshold: v.snipeThreshold,
    veto_threshold: v.vetoThreshold,
    // Decimal-string on the wire (never float for money).
    jito_tip_cap_frac: String(v.jitoTipCapSol),
  };
}

const HEALTH_STATUS: ReadonlySet<string> = new Set<ModuleHealth["status"]>([
  "online",
  "degraded",
  "offline",
]);

/**
 * The wire uses {ok, degraded} (plus staleness); the view-model uses
 * {online, degraded, offline}. Map "ok"→"online"; honor explicit view values.
 */
export function adaptModuleHealth(w: WireModuleHealth): ModuleHealth {
  const raw = str(w.status, "online");
  const status: ModuleHealth["status"] = HEALTH_STATUS.has(raw)
    ? (raw as ModuleHealth["status"])
    : raw === "ok"
      ? "online"
      : raw === "stale"
        ? "degraded"
        : "offline";
  return {
    name: str(w.name),
    status,
    latencyMs: num(w.latency_ms),
    staleMs: num(w.staleness_ms),
  };
}

export function adaptHealth(w: WireModuleHealth[]): ModuleHealth[] {
  return (w ?? []).map(adaptModuleHealth);
}

export function adaptMetrics(w: WireMetricsSnapshot): MetricsSnapshot {
  // edgeVsBaselinePct is derived from model_vs_baseline.delta_net_pnl_per_sol_at_risk
  // (a Decimal-string edge metric, NOT a win-rate). Surfaced as a percentage.
  const edgeDelta = decimalStringToSolDisplay(
    w.model_vs_baseline?.delta_net_pnl_per_sol_at_risk,
  );
  return {
    // net_pnl_sol is a Decimal-string; display-only conversion (never float math).
    netPnlSol: decimalStringToSolDisplay(w.net_pnl_sol),
    landRatePct: num(w.land_rate_pct),
    medianSlotDelay: num(w.median_slot_delay),
    rugAvoidancePct: num(w.rug_avoidance_pct),
    tipEfficiency: num(w.tip_efficiency),
    edgeVsBaselinePct: edgeDelta * 100,
    openPositions: num(w.open_positions),
    dailyPnlSol: decimalStringToSolDisplay(w.daily_pnl_sol),
    dailyLossLimitSol: Math.abs(decimalStringToSolDisplay(w.daily_loss_limit_sol)),
    breakerTripped: bool(w.breaker_tripped),
  };
}

/* -------------------------------------------------------------------------- */
/*  Smart-money / copy-trade selectivity (T-302 stream projection — VIEW only) */
/* -------------------------------------------------------------------------- */

export interface WireSmartWallet {
  address?: string;
  label?: string;
  recent_entries?: number;
  entry_lag_slots?: number | null;
  /** Net-of-cost tracked PnL — Decimal-string SOL (never float-as-money). */
  tracked_net_pnl_sol?: string;
  passes_filter?: boolean;
  filter_reasons?: string[];
  last_seen_ms?: number;
}

/**
 * Wire smart-wallet → view-model. This is a SELECTIVITY VIEW: it carries no
 * action and the page renders no buy/mirror control. `tracked_net_pnl_sol` is a
 * Decimal-string (money discipline); `entry_lag_slots` honestly shows we follow
 * (positive = we are behind their fill).
 */
export function adaptSmartWallet(w: WireSmartWallet): SmartWallet {
  return {
    address: str(w.address),
    label: str(w.label),
    recentEntries: num(w.recent_entries),
    entryLagSlots:
      typeof w.entry_lag_slots === "number" ? w.entry_lag_slots : null,
    trackedNetPnlSol: decimalStringToSolDisplay(w.tracked_net_pnl_sol),
    passesFilter: bool(w.passes_filter),
    filterReasons: (w.filter_reasons ?? []).filter(
      (r): r is string => typeof r === "string",
    ),
    lastSeenMs: num(w.last_seen_ms, Date.now()),
  };
}

export function adaptSmartWallets(w: WireSmartWallet[]): SmartWallet[] {
  return (w ?? []).map(adaptSmartWallet);
}

/* -------------------------------------------------------------------------- */
/*  Resting orders (limit / DCA / stop) — de-risk-direction-only ledger VIEW   */
/* -------------------------------------------------------------------------- */

export interface WireRestingOrder {
  id?: string;
  token?: string;
  mint?: string;
  kind?: string;
  trigger_pct?: number;
  fraction_bps?: number;
  status?: string;
  offline_capable?: boolean;
  created_ms?: number;
}

const RESTING_KINDS: ReadonlySet<string> = new Set<RestingOrder["kind"]>([
  "limit_exit",
  "dca_exit",
  "stop_exit",
]);

const RESTING_STATUS: ReadonlySet<string> = new Set<RestingOrder["status"]>([
  "armed",
  "triggered",
  "cancelled",
]);

export function adaptRestingOrder(w: WireRestingOrder): RestingOrder {
  const kind = str(w.kind);
  const status = str(w.status);
  return {
    id: str(w.id),
    token: str(w.token),
    mint: str(w.mint),
    kind: RESTING_KINDS.has(kind) ? (kind as RestingOrder["kind"]) : "limit_exit",
    triggerPct: num(w.trigger_pct),
    // bounded 0..10000 bps (a reduction fraction; never an increase).
    fractionBps: Math.max(0, Math.min(10_000, num(w.fraction_bps))),
    status: RESTING_STATUS.has(status)
      ? (status as RestingOrder["status"])
      : "armed",
    offlineCapable: bool(w.offline_capable),
    createdMs: num(w.created_ms, Date.now()),
  };
}

export function adaptRestingOrders(w: WireRestingOrder[]): RestingOrder[] {
  return (w ?? []).map(adaptRestingOrder);
}

/* -------------------------------------------------------------------------- */
/*  Candidate / watchlist queue (E3) — READ-ONLY pipeline VIEW projection.     */
/*                                                                            */
/*  Wire `GET /api/candidates` → view-model Candidate[]. Typed to the real      */
/*  CandidateRecord (api-contracts.md §14 / candidate_schemas.py): mint,        */
/*  evaluated_at_slot, evaluated_at_wall_ms, status, model_p, reason, and a     */
/*  nested safety_report { gate_passed, reasons }. Pure, side-effect-free, no   */
/*  money fields (modelP is a probability) so no float-as-money concern arises. */
/*                                                                            */
/*  Safety is load-bearing: safetyPassed and redFlags are derived ONLY from     */
/*  safety_report (gate_passed / reasons) — a vetoed candidate must NEVER show   */
/*  a blank safety cell. The status set is closed; an unknown wire status falls  */
/*  back to the safest VIEW state ("monitoring", a no-action observation) rather */
/*  than implying an entry was taken. The wire carries no token name, launch     */
/*  source, or smart-wallet count for a candidate — those are marked absent      */
/*  (token := mint, source/smartWallets := null) rather than fabricated.         */
/* -------------------------------------------------------------------------- */

/** Nested pre-trade safety verdict (CandidateRecord.safety_report). */
export interface WireSafetyReport {
  gate_passed?: boolean;
  reasons?: string[];
}

/**
 * Exact wire frame for one row of `GET /api/candidates`
 * (aats.control_plane.candidate_schemas.CandidateRecord). Fields are optional
 * here only for defensive parsing of a partial/garbled frame — the backend
 * always populates them.
 */
export interface WireCandidate {
  mint?: string;
  evaluated_at_slot?: number;
  evaluated_at_wall_ms?: number;
  status?: string;
  model_p?: number | null;
  reason?: string;
  safety_report?: WireSafetyReport;
}

const CANDIDATE_STATUS: ReadonlySet<string> = new Set<CandidateStatus>([
  "monitoring",
  "pending",
  "skipped",
  "sniped",
]);

export function adaptCandidate(w: WireCandidate): Candidate {
  const status = str(w.status);
  const mint = str(w.mint);
  const wallMs = num(w.evaluated_at_wall_ms, Date.now());
  const safety = w.safety_report ?? {};
  return {
    // No surrogate id on the wire — mint + event-time slot is unique per row.
    id: mint
      ? `${mint}:${num(w.evaluated_at_slot)}`
      : `cand:${num(w.evaluated_at_slot)}`,
    // Single evaluation timestamp on the wire — drives both first-seen & updated.
    firstSeenMs: wallMs,
    updatedMs: wallMs,
    // The wire carries no separate token name — display the mint, never a faked one.
    token: mint,
    mint,
    // The wire carries no launch source for a candidate — mark absent, don't invent.
    source: null,
    // Unknown status → "monitoring" (a no-action observation), never "sniped".
    status: CANDIDATE_STATUS.has(status)
      ? (status as CandidateStatus)
      : "monitoring",
    modelP: typeof w.model_p === "number" ? w.model_p : null,
    // Safety derived from safety_report ONLY. gate_passed missing → treat as NOT
    // clean (fail-closed: never claim a candidate is safe on a malformed frame).
    safetyPassed: bool(safety.gate_passed),
    // Token-safety reasons surfaced verbatim (never dropped) — an operator must
    // never miss a safety warning. Drop the trivial "passed" affirmation so a
    // clean row reads as clean instead of listing a non-flag.
    redFlags: (safety.reasons ?? [])
      .filter((r): r is RedFlag => typeof r === "string")
      .filter((r) => r !== "passed"),
    // The wire carries no smart-wallet count for a candidate — mark absent.
    smartWallets: null,
    reason: str(w.reason),
  };
}

export function adaptCandidates(w: WireCandidate[]): Candidate[] {
  return (w ?? []).map(adaptCandidate);
}

/* -------------------------------------------------------------------------- */
/*  Wallet-cluster graph (E11) — READ-ONLY "Bubble Maps" VIEW projection.       */
/*                                                                            */
/*  Wire `GET /api/wallet-cluster` → view-model WalletClusterGraph[]. Typed to  */
/*  the AUTHORITATIVE backend schema                                            */
/*  (aats.control_plane.wallet_cluster_schemas.WalletClusterGraph): mint,        */
/*  evaluated_at_slot, sniper_cluster_score, bundling_detected,                 */
/*  bundler_wallet_count, dev_bundle_cluster_bps, and nested nodes[] / edges[]  */
/*  / manipulation_flags[]. Pure, side-effect-free. No money field exists       */
/*  (all share quantities are bps int ratios, weights/scores are statistical    */
/*  floats in [0,1]) so no float-as-money concern arises.                       */
/*                                                                            */
/*  Safety is load-bearing: an edge is dropped if either endpoint pubkey is     */
/*  missing (a dangling edge must never render against a phantom node), bps and  */
/*  scores are clamped to their valid ranges defensively, and the closed type    */
/*  sets fall back to the safest neutral VIEW value ("unknown"/"co_timing")      */
/*  rather than inventing a manipulation category. Manipulation flags are        */
/*  surfaced verbatim (never dropped) — an operator must never miss a warning.   */
/* -------------------------------------------------------------------------- */

/** One node of `GET /api/wallet-cluster` (WalletNode). All keys optional for
 *  defensive parsing of a partial frame — the backend always populates them. */
export interface WireWalletNode {
  wallet?: string;
  node_type?: string;
  supply_share_bps?: number;
  buy_slot?: number | null;
  manipulation_flags?: string[];
}

/** One edge of `GET /api/wallet-cluster` (WalletEdge). */
export interface WireWalletEdge {
  source?: string;
  target?: string;
  edge_type?: string;
  weight?: number;
}

/** Exact wire frame for one graph of `GET /api/wallet-cluster`
 *  (WalletClusterGraph). */
export interface WireWalletClusterGraph {
  mint?: string;
  evaluated_at_slot?: number;
  sniper_cluster_score?: number;
  bundling_detected?: boolean;
  bundler_wallet_count?: number;
  dev_bundle_cluster_bps?: number;
  nodes?: WireWalletNode[];
  edges?: WireWalletEdge[];
  manipulation_flags?: string[];
}

const WALLET_NODE_TYPE: ReadonlySet<string> = new Set<WalletNodeType>([
  "creator",
  "bundler",
  "sniper",
  "holder",
  "unknown",
]);

const WALLET_EDGE_TYPE: ReadonlySet<string> = new Set<WalletEdgeType>([
  "same_bundle",
  "shared_funder",
  "co_timing",
  "creator_funded",
]);

/** Clamp an int bps value into [0, 10000] (the contract's valid range). */
function clampBps(v: unknown): number {
  const n = num(v);
  return Math.max(0, Math.min(10_000, n));
}

/** Clamp a statistical score/weight into [0, 1]. */
function clamp01(v: unknown): number {
  const n = num(v);
  return Math.max(0, Math.min(1, n));
}

function adaptWalletNode(w: WireWalletNode): WalletNode {
  const t = str(w.node_type);
  return {
    wallet: str(w.wallet),
    // Unknown role → "unknown" (a neutral, non-manipulation classification).
    nodeType: WALLET_NODE_TYPE.has(t) ? (t as WalletNodeType) : "unknown",
    supplyShareBps: clampBps(w.supply_share_bps),
    buySlot: typeof w.buy_slot === "number" ? w.buy_slot : null,
    // Manipulation flags surfaced verbatim (never dropped) — open set.
    manipulationFlags: (w.manipulation_flags ?? []).filter(
      (f): f is ManipulationFlag => typeof f === "string",
    ),
  };
}

export function adaptWalletClusterGraph(
  w: WireWalletClusterGraph,
): WalletClusterGraph {
  const nodes = (w.nodes ?? []).map(adaptWalletNode);
  // A node set to validate edges against — a dangling edge (an endpoint with no
  // node) must NEVER render against a phantom wallet, so drop it.
  const present = new Set(nodes.map((n) => n.wallet).filter(Boolean));
  const edges = (w.edges ?? [])
    .map((e): WalletEdge => {
      const et = str(e.edge_type);
      return {
        source: str(e.source),
        target: str(e.target),
        // Unknown relation → the weakest connection category ("co_timing"),
        // never an invented one; the type set is closed.
        edgeType: WALLET_EDGE_TYPE.has(et)
          ? (et as WalletEdgeType)
          : "co_timing",
        weight: clamp01(e.weight),
      };
    })
    .filter((e) => e.source && e.target && present.has(e.source) && present.has(e.target));
  const mint = str(w.mint);
  return {
    mint,
    // The wire carries no separate token name — display the mint, never a faked one.
    token: mint,
    evaluatedAtSlot: num(w.evaluated_at_slot),
    sniperClusterScore: clamp01(w.sniper_cluster_score),
    bundlingDetected: bool(w.bundling_detected),
    bundlerWalletCount: Math.max(0, num(w.bundler_wallet_count)),
    devBundleClusterBps: clampBps(w.dev_bundle_cluster_bps),
    nodes,
    edges,
    manipulationFlags: (w.manipulation_flags ?? []).filter(
      (f): f is ManipulationFlag => typeof f === "string",
    ),
  };
}

export function adaptWalletClusters(
  w: WireWalletClusterGraph[],
): WalletClusterGraph[] {
  return (w ?? []).map(adaptWalletClusterGraph);
}

/* -------------------------------------------------------------------------- */
/*  Narrative & News signal (E7) — READ-ONLY de-risk VIEW projection.           */
/*                                                                            */
/*  Wire `GET /api/narrative` → view-model NarrativeSignal[]. Typed to the      */
/*  AUTHORITATIVE backend schema (aats.sentiment.models.NewsSignal): asset,     */
/*  decision_time_ms, total_articles, negative_article_count,                   */
/*  credible_negative_event, mcs_delta, credibility_weight, source_article_ids, */
/*  headline_digest, red_flags, posts_excluded_future — plus an optional        */
/*  `sources` roll-up the projection MAY attach for the sources/freshness view. */
/*  Pure, side-effect-free. No money field exists (mcs_delta is a [-1,0]         */
/*  conviction delta, credibility_weight is a statistical weight) so no          */
/*  float-as-money concern arises.                                              */
/*                                                                            */
/*  DE-RISK FRAMING is load-bearing and ENFORCED here, not merely trusted:      */
/*  `mcs_delta` is re-clamped to [-1.0, 0.0] on the way in. The backend formula  */
/*  guarantees mcs_delta <= 0, but the adapter REFUSES to surface a positive     */
/*  conviction delta even if a malformed/hostile frame sent one — a positive     */
/*  value is clamped to 0 (no-impact), never shown as a buy signal. There is no  */
/*  win-rate field anywhere. Red flags are surfaced verbatim (never dropped) —   */
/*  an operator must never miss a manipulation/news warning.                     */
/* -------------------------------------------------------------------------- */

const CREDIBILITY_TIERS: ReadonlySet<string> = new Set<NewsCredibilityTier>([
  "tier_1",
  "tier_2",
  "tier_3",
]);

/** One outlet roll-up the optional projection may attach (display only). */
export interface WireNarrativeSource {
  outlet?: string;
  outlet_id?: string;
  credibility?: string;
  negative?: boolean;
  breaking?: boolean;
  article_count?: number;
}

/** Exact wire frame for one asset's narrative signal (NewsSignal). All keys
 *  optional for defensive parsing — the backend always populates the core. */
export interface WireNarrativeSignal {
  asset?: string;
  token?: string;
  decision_time_ms?: number;
  freshness_ms?: number | null;
  total_articles?: number;
  negative_article_count?: number;
  /** Always <= 0 on the wire (de-risk). Re-clamped to [-1,0] here regardless. */
  mcs_delta?: number;
  credibility_weight?: number;
  credible_negative_event?: boolean;
  red_flags?: string[];
  headline_digest?: string;
  posts_excluded_future?: number;
  sources?: WireNarrativeSource[];
}

/**
 * Clamp the news narrative delta into [-1.0, 0.0]. The single load-bearing
 * de-risk invariant of this surface: news can ONLY lower conviction. A positive
 * value (a malformed or hostile frame) is forced to 0 — it is never surfaced as
 * a buy signal. NaN/non-finite → 0 (no impact).
 */
function clampDeRiskDelta(v: unknown): number {
  const n = num(v);
  return Math.max(-1, Math.min(0, n));
}

function adaptNarrativeSource(w: WireNarrativeSource): NarrativeSource {
  const cred = str(w.credibility);
  return {
    outlet: str(w.outlet),
    outletId: str(w.outlet_id),
    // Unknown tier → the LOWEST-trust tier, never a higher one (de-risk bias).
    credibility: CREDIBILITY_TIERS.has(cred)
      ? (cred as NewsCredibilityTier)
      : "tier_3",
    negative: bool(w.negative),
    breaking: bool(w.breaking),
    articleCount: Math.max(0, num(w.article_count)),
  };
}

export function adaptNarrativeSignal(w: WireNarrativeSignal): NarrativeSignal {
  const asset = str(w.asset);
  return {
    asset,
    // The wire carries no separate token name — display the asset, never a faked one.
    token: str(w.token) || asset,
    decisionTimeMs: num(w.decision_time_ms, Date.now()),
    freshnessMs:
      typeof w.freshness_ms === "number" && Number.isFinite(w.freshness_ms)
        ? Math.max(0, w.freshness_ms)
        : null,
    totalArticles: Math.max(0, num(w.total_articles)),
    negativeArticleCount: Math.max(0, num(w.negative_article_count)),
    // DE-RISK INVARIANT: re-clamped to [-1,0]; a positive frame is forced to 0.
    narrativeScore: clampDeRiskDelta(w.mcs_delta),
    credibilityWeight: Math.max(0, num(w.credibility_weight)),
    credibleNegativeEvent: bool(w.credible_negative_event),
    // Red flags surfaced verbatim (never dropped) — open set, never miss a warning.
    redFlags: (w.red_flags ?? []).filter(
      (f): f is RedFlag => typeof f === "string",
    ),
    headlineDigest: str(w.headline_digest),
    postsExcludedFuture: Math.max(0, num(w.posts_excluded_future)),
    sources: (w.sources ?? []).map(adaptNarrativeSource),
  };
}

export function adaptNarrative(w: WireNarrativeSignal[]): NarrativeSignal[] {
  return (w ?? []).map(adaptNarrativeSignal);
}
