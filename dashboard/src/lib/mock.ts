/**
 * AATS Sniper — mock data layer.
 * A realistic, lightly-evolving stream of operator telemetry so the dashboard
 * runs standalone (npm run dev) with NO backend. All display numbers are
 * rounded at the source via the round() helpers below.
 */

import type {
  AgentState,
  GateReason,
  InfraTier,
  LatencyBudget,
  MCSScore,
  MetricsSnapshot,
  ModuleHealth,
  Position,
  Prediction,
  Reasoning,
  RiskConfig,
  SnipeEvent,
  SnipeSource,
} from "./types";

/* ----------------------------- number helpers ----------------------------- */

export const round1 = (n: number) => Math.round(n * 10) / 10;
export const round2 = (n: number) => Math.round(n * 100) / 100;
export const round3 = (n: number) => Math.round(n * 1000) / 1000;
export const roundInt = (n: number) => Math.round(n);

const rand = (min: number, max: number) => Math.random() * (max - min) + min;
const pick = <T>(arr: readonly T[]): T =>
  arr[Math.floor(Math.random() * arr.length)];
const chance = (p: number) => Math.random() < p;

/* --------------------------------- tokens --------------------------------- */

const TOKEN_NAMES = [
  "GROK",
  "PNUT",
  "WIF",
  "BONK",
  "MOODENG",
  "POPCAT",
  "GIGA",
  "FWOG",
  "RETARDIO",
  "MICHI",
  "BRETT",
  "MUMU",
  "GOAT",
  "ZEREBRO",
  "ai16z",
  "FARTCOIN",
  "PENGU",
  "CHILLGUY",
  "SLERF",
  "BODEN",
] as const;

const SOURCES: readonly SnipeSource[] = [
  "pump.fun",
  "pumpswap",
  "raydium_v4",
  "raydium_cpmm",
  "migration",
];

const FAIL_REASONS: readonly GateReason[] = [
  "freeze_authority",
  "mint_authority",
  "lp_unlocked",
  "sniper_cluster",
  "high_tax",
  "low_liquidity",
];

const randMint = () => {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz123456789";
  let s = "";
  for (let i = 0; i < 6; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return `${s}…pump`;
};

let seq = 0;
const nextId = () => `evt-${Date.now().toString(36)}-${(seq++).toString(36)}`;

let slotCounter = 295_412_880;
const nextSlot = () => (slotCounter += Math.floor(rand(1, 4)));

/* ------------------------------- snipe events ------------------------------ */

export function makeSnipeEvent(): SnipeEvent {
  const passed = chance(0.42);
  const reasons: GateReason[] = passed
    ? ["passed"]
    : Array.from(
        new Set([pick(FAIL_REASONS), ...(chance(0.4) ? [pick(FAIL_REASONS)] : [])]),
      );

  const modelP = passed ? round2(rand(0.3, 0.95)) : chance(0.5) ? round2(rand(0.05, 0.6)) : null;

  let action: SnipeEvent["action"] = "skipped";
  if (passed && modelP !== null) {
    if (modelP >= 0.62) action = "sniped";
    else if (modelP < 0.35) action = "vetoed";
    else action = "skipped";
  } else if (!passed) {
    action = chance(0.15) ? "vetoed" : "skipped";
  }

  const sniped = action === "sniped";

  return {
    id: nextId(),
    ts: Date.now(),
    slot: nextSlot(),
    token: pick(TOKEN_NAMES),
    source: pick(SOURCES),
    gatePassed: passed,
    gateReasons: reasons,
    modelP,
    action,
    slotDelay: sniped ? roundInt(rand(1, 4)) : null,
    smartWallets: roundInt(rand(0, 18)),
    pnlPct: sniped && chance(0.7) ? round1(rand(-32, 140)) : null,
  };
}

/** A seed backlog so the feed isn't empty on first paint. */
export function seedSnipeFeed(n = 14): SnipeEvent[] {
  const out: SnipeEvent[] = [];
  for (let i = 0; i < n; i++) {
    const e = makeSnipeEvent();
    e.ts = Date.now() - (n - i) * 1500;
    out.push(e);
  }
  return out;
}

/* --------------------------------- metrics -------------------------------- */

const metricsBase: MetricsSnapshot = {
  netPnlSol: 42.7,
  landRatePct: 87,
  medianSlotDelay: 2,
  rugAvoidancePct: 96,
  tipEfficiency: 0.71,
  edgeVsBaselinePct: 18.4,
  openPositions: 3,
  dailyPnlSol: 6.3,
  dailyLossLimitSol: 10,
  breakerTripped: false,
};

export function makeMetrics(prev?: MetricsSnapshot): MetricsSnapshot {
  const b = prev ?? metricsBase;
  return {
    netPnlSol: round1(b.netPnlSol + rand(-0.6, 0.9)),
    landRatePct: roundInt(Math.min(99, Math.max(60, b.landRatePct + rand(-1.5, 1.5)))),
    medianSlotDelay: roundInt(Math.min(4, Math.max(1, b.medianSlotDelay + rand(-0.6, 0.6)))),
    rugAvoidancePct: roundInt(Math.min(100, Math.max(88, b.rugAvoidancePct + rand(-0.8, 0.8)))),
    tipEfficiency: round2(Math.min(0.95, Math.max(0.4, b.tipEfficiency + rand(-0.03, 0.03)))),
    edgeVsBaselinePct: round1(Math.min(40, Math.max(-5, b.edgeVsBaselinePct + rand(-0.8, 0.8)))),
    openPositions: b.openPositions,
    dailyPnlSol: round1(b.dailyPnlSol + rand(-0.4, 0.5)),
    dailyLossLimitSol: 10,
    breakerTripped: false,
  };
}

/* -------------------------------- positions ------------------------------- */

export function makePositions(): Position[] {
  const open: Position[] = [
    {
      mint: randMint(),
      token: "PNUT",
      entrySol: 4.0,
      currentPct: round1(rand(8, 64)),
      entrySlipPct: round1(rand(0.4, 2.2)),
      tpHit: 1,
      tpTotal: 3,
      trailingArmed: true,
      hardStopPct: -18,
      exitMode: "secure",
      ageSec: roundInt(rand(40, 600)),
      status: "open",
      realizedPnlSol: null,
    },
    {
      mint: randMint(),
      token: "MOODENG",
      entrySol: 2.5,
      currentPct: round1(rand(-12, 30)),
      entrySlipPct: round1(rand(0.6, 3.1)),
      tpHit: 0,
      tpTotal: 3,
      trailingArmed: false,
      hardStopPct: -18,
      exitMode: "fast",
      ageSec: roundInt(rand(15, 220)),
      status: "open",
      realizedPnlSol: null,
    },
    {
      mint: randMint(),
      token: "FWOG",
      entrySol: 5.0,
      currentPct: round1(rand(80, 210)),
      entrySlipPct: round1(rand(0.5, 1.8)),
      tpHit: 2,
      tpTotal: 3,
      trailingArmed: true,
      hardStopPct: -18,
      exitMode: "secure",
      ageSec: roundInt(rand(120, 900)),
      status: "open",
      realizedPnlSol: null,
    },
  ];

  const closedTokens = ["WIF", "BONK", "POPCAT", "GIGA", "BRETT", "RETARDIO"];
  const closed: Position[] = closedTokens.map((token) => {
    const win = chance(0.62);
    const entry = round1(rand(1.5, 5));
    return {
      mint: randMint(),
      token,
      entrySol: entry,
      currentPct: 0,
      entrySlipPct: round1(rand(0.4, 3)),
      tpHit: win ? roundInt(rand(1, 3)) : 0,
      tpTotal: 3,
      trailingArmed: false,
      hardStopPct: -18,
      exitMode: chance(0.5) ? "secure" : "fast",
      ageSec: roundInt(rand(60, 1800)),
      status: "closed",
      realizedPnlSol: win ? round2(entry * rand(0.3, 2.2)) : round2(-entry * rand(0.1, 0.6)),
    };
  });

  return [...open, ...closed];
}

/* ------------------------------ latency budget ----------------------------- */

export const latencyBudget: LatencyBudget = {
  hops: [
    { name: "ingress", ms: 14, budgetMs: 16 },
    { name: "detect", ms: 1, budgetMs: 2 },
    { name: "decode", ms: 2, budgetMs: 3 },
    { name: "gate", ms: 5, budgetMs: 6 },
    { name: "model", ms: 0.5, budgetMs: 1 },
    { name: "build_sign", ms: 3, budgetMs: 4 },
    { name: "jito_submit", ms: 18, budgetMs: 22 },
  ],
  internalMs: 43,
  slotFloorMs: 400,
};

export const infraTiers: InfraTier[] = [
  { name: "generic", landRate: 71, medianSlotDelay: 3, entrySlipPct: 2.1 },
  { name: "colo", landRate: 92, medianSlotDelay: 1, entrySlipPct: 0.6 },
];

/* -------------------------------- sentiment -------------------------------- */

export const sentimentScores: MCSScore[] = [
  {
    asset: "PNUT",
    conviction: 0.82,
    momentum: 0.74,
    novelty: 0.61,
    synchronicity: 0.69,
    redFlags: [],
    postCount: 1842,
    reasoning:
      "Coordinated organic mention spike across mid-tier KOLs; narrative consistent, low bot signature.",
  },
  {
    asset: "MOODENG",
    conviction: 0.58,
    momentum: 0.88,
    novelty: 0.34,
    synchronicity: 0.41,
    redFlags: ["reused_meme", "low_novelty"],
    postCount: 2210,
    reasoning:
      "High momentum but derivative narrative; synchronicity below threshold suggests fading attention.",
  },
  {
    asset: "FWOG",
    conviction: 0.91,
    momentum: 0.66,
    novelty: 0.79,
    synchronicity: 0.84,
    redFlags: [],
    postCount: 980,
    reasoning:
      "Strong novelty with rising synchronicity; early-stage attention curve, conviction well-supported.",
  },
  {
    asset: "GIGA",
    conviction: 0.39,
    momentum: 0.22,
    novelty: 0.18,
    synchronicity: 0.27,
    redFlags: ["bot_cluster", "declining_volume"],
    postCount: 410,
    reasoning:
      "Bot cluster detected inflating post count; underlying organic engagement collapsing.",
  },
];

/* ------------------------------- predictions ------------------------------- */

export const prediction: Prediction = {
  ts: Date.now(),
  classifierP: 0.71,
  baselineP: 0.5,
  calibrationBins: [
    { p: 0.1, actual: 0.08 },
    { p: 0.2, actual: 0.19 },
    { p: 0.3, actual: 0.27 },
    { p: 0.4, actual: 0.43 },
    { p: 0.5, actual: 0.48 },
    { p: 0.6, actual: 0.64 },
    { p: 0.7, actual: 0.68 },
    { p: 0.8, actual: 0.83 },
    { p: 0.9, actual: 0.88 },
  ],
  featureImportance: [
    { name: "smart_wallet_inflow", weight: 0.27 },
    { name: "lp_lock_ratio", weight: 0.19 },
    { name: "social_synchronicity", weight: 0.16 },
    { name: "slot_delay", weight: 0.13 },
    { name: "deployer_history", weight: 0.11 },
    { name: "holder_concentration", weight: 0.08 },
    { name: "tax_bps", weight: 0.06 },
  ],
};

/* -------------------------------- reasoning -------------------------------- */

export function makeReasoningLog(): Reasoning[] {
  const rows: Omit<Reasoning, "id" | "ts">[] = [
    {
      token: "FWOG",
      signal: "Strong Buy",
      confidence: 0.89,
      veto: false,
      narrativeFailure: false,
      rationale:
        "Gate clean, smart-wallet inflow accelerating, social synchronicity rising; classifier 0.84 over threshold.",
    },
    {
      token: "PNUT",
      signal: "Weak Buy",
      confidence: 0.66,
      veto: false,
      narrativeFailure: false,
      rationale:
        "Edge present but slot delay elevated at 3; sized down and routed to secure exit mode.",
    },
    {
      token: "GIGA",
      signal: "Strong Sell",
      confidence: 0.81,
      veto: true,
      narrativeFailure: true,
      rationale:
        "Bot cluster inflating sentiment, organic engagement collapsing; vetoed despite gate pass.",
    },
    {
      token: "MOODENG",
      signal: "Hold",
      confidence: 0.52,
      veto: false,
      narrativeFailure: false,
      rationale:
        "Momentum high but novelty weak; awaiting synchronicity confirmation before entry.",
    },
    {
      token: "BONK",
      signal: "Sell",
      confidence: 0.7,
      veto: false,
      narrativeFailure: true,
      rationale: "Narrative exhaustion; trailing stop tightened and partial exit triggered.",
    },
  ];
  return rows.map((r, i) => ({
    ...r,
    id: `reason-${i}`,
    ts: Date.now() - i * 42_000,
  }));
}

/* ------------------------------- risk config ------------------------------- */

export const defaultRiskConfig: RiskConfig = {
  maxPositionSol: 5,
  maxPositionPct: 10,
  stopLossPct: 18,
  takeProfitPct: 60,
  maxSlippageBps: 150,
  dailyLossLimitSol: 10,
  maxHoldMin: 240,
  snipeThreshold: 0.62,
  vetoThreshold: 0.35,
  jitoTipCapSol: 0.05,
};

/* ------------------------------ module health ------------------------------ */

export function makeModuleHealth(): ModuleHealth[] {
  const mods: { name: string; status: ModuleHealth["status"] }[] = [
    { name: "Geyser ingest", status: "online" },
    { name: "ShredStream", status: "online" },
    { name: "Gate engine", status: "online" },
    { name: "Classifier", status: "online" },
    { name: "Sentiment (MCS)", status: "degraded" },
    { name: "Jito submitter", status: "online" },
    { name: "Risk manager", status: "online" },
  ];
  return mods.map((m) => ({
    name: m.name,
    status: m.status,
    latencyMs: m.status === "online" ? roundInt(rand(2, 24)) : roundInt(rand(40, 180)),
    staleMs: m.status === "online" ? roundInt(rand(0, 400)) : roundInt(rand(800, 4000)),
  }));
}

/* ------------------------------- agent state ------------------------------- */

let selectedMode: AgentState["mode"] = "dry-run";
export function setMockMode(m: AgentState["mode"]): void {
  selectedMode = m;
}

export function makeAgentState(prev?: AgentState): AgentState {
  const internal = roundInt(prev ? Math.max(36, Math.min(58, prev.connection.internalMs + rand(-2, 2))) : 43);
  const snipe = pick<LoopStateSnipe>(["idle", "armed", "armed", "firing"]);
  return {
    mode: selectedMode,
    loops: {
      snipe,
      fast: "120ms",
      slow: "2s",
    },
    connection: {
      geyser: true,
      shredstream: true,
      internalMs: internal,
    },
    wallet: {
      pubkey: "7Bd…aN4q",
      balanceSol: round2(prev ? prev.wallet.balanceSol + rand(-0.2, 0.3) : 128.42),
      capSol: 200,
    },
    killed: prev?.killed ?? false,
  };
}

type LoopStateSnipe = AgentState["loops"]["snipe"];
