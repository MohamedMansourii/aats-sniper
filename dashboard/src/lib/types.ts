/**
 * AATS Sniper — shared domain types.
 * Names are the contract. All 10 pages import from here. Do not rename.
 */

export type AgentMode = "paper" | "dry-run" | "live";

export interface LoopState {
  snipe: "idle" | "armed" | "firing";
  fast: string;
  slow: string;
}

export type GateReason =
  | "freeze_authority"
  | "mint_authority"
  | "lp_unlocked"
  | "sniper_cluster"
  | "high_tax"
  | "low_liquidity"
  | "passed";

export type SnipeSource =
  | "pump.fun"
  | "pumpswap"
  | "raydium_v4"
  | "raydium_cpmm"
  | "migration";

export interface SnipeEvent {
  id: string;
  ts: number;
  slot: number;
  token: string;
  source: SnipeSource;
  gatePassed: boolean;
  gateReasons: GateReason[];
  modelP: number | null;
  action: "sniped" | "skipped" | "vetoed";
  slotDelay: number | null;
  smartWallets: number;
  pnlPct: number | null;
}

export interface Hop {
  name: string;
  ms: number;
  budgetMs: number;
}

export interface LatencyBudget {
  hops: Hop[];
  internalMs: number;
  slotFloorMs: number;
}

export interface InfraTier {
  name: string;
  landRate: number;
  medianSlotDelay: number;
  entrySlipPct: number;
}

export interface Position {
  mint: string;
  token: string;
  entrySol: number;
  currentPct: number;
  entrySlipPct: number;
  tpHit: number;
  tpTotal: number;
  trailingArmed: boolean;
  hardStopPct: number;
  exitMode: "secure" | "fast";
  ageSec: number;
  status: "open" | "closed";
  realizedPnlSol: number | null;
}

export interface MCSScore {
  asset: string;
  conviction: number;
  momentum: number;
  novelty: number;
  synchronicity: number;
  redFlags: string[];
  postCount: number;
  reasoning: string;
}

export interface Prediction {
  ts: number;
  classifierP: number;
  baselineP: number;
  calibrationBins: { p: number; actual: number }[];
  featureImportance: { name: string; weight: number }[];
}

export interface Reasoning {
  id: string;
  ts: number;
  token: string;
  signal: "Strong Buy" | "Weak Buy" | "Hold" | "Sell" | "Strong Sell";
  confidence: number;
  veto: boolean;
  narrativeFailure: boolean;
  rationale: string;
}

export interface RiskConfig {
  maxPositionSol: number;
  maxPositionPct: number;
  stopLossPct: number;
  takeProfitPct: number;
  maxSlippageBps: number;
  dailyLossLimitSol: number;
  maxHoldMin: number;
  snipeThreshold: number;
  vetoThreshold: number;
  jitoTipCapSol: number;
}

export interface ModuleHealth {
  name: string;
  status: "online" | "degraded" | "offline";
  latencyMs: number;
  staleMs: number;
}

export interface MetricsSnapshot {
  netPnlSol: number;
  landRatePct: number;
  medianSlotDelay: number;
  rugAvoidancePct: number;
  tipEfficiency: number;
  edgeVsBaselinePct: number;
  openPositions: number;
  dailyPnlSol: number;
  dailyLossLimitSol: number;
  breakerTripped: boolean;
}

export interface AgentState {
  mode: AgentMode;
  loops: LoopState;
  connection: {
    geyser: boolean;
    shredstream: boolean;
    internalMs: number;
  };
  wallet: {
    pubkey: string;
    balanceSol: number;
    capSol: number;
  };
  killed: boolean;
}
