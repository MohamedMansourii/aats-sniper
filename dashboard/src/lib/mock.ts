/**
 * AATS Sniper — mock data layer.  *** SYNTHETIC — NOT REAL EDGE DATA ***
 *
 * Every value produced here (snipe launches, fills, MCS/sentiment scores,
 * predictions, latency, positions) is a SYNTHETIC, randomly-generated fake.
 * It exists ONLY so the dashboard runs standalone (npm run dev / mock build)
 * with NO backend. It reflects NO real market, NO real model output, and NO
 * real trading edge. Do not read any signal, performance, or win-rate into it.
 *
 * This generator is the standalone DEFAULT only. The docker image and
 * `docker compose up` build LIVE (VITE_USE_MOCK=false) and bypass this file
 * entirely, fetching real operator telemetry from the control plane instead.
 *
 * All display numbers are rounded at the source via the round() helpers below.
 */

import type {
  AgentState,
  AutoStratPreset,
  Candidate,
  CandidateStatus,
  CostBreakdown,
  GateReason,
  InfraTier,
  LatencyBudget,
  MCSScore,
  MetricsSnapshot,
  ModuleHealth,
  NarrativeSignal,
  Position,
  Prediction,
  Reasoning,
  RedFlag,
  RestingOrder,
  RiskConfig,
  SmartWallet,
  SnipeEvent,
  SnipeSource,
  WalletClusterGraph,
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

/**
 * Extra token-safety scanner fingerprints (T-329 sell-sim / T-323 gate) that
 * can flag a candidate beyond the gate-reason taxonomy. These map to wire
 * `red_flags` and are surfaced verbatim on the feed/risk surfaces.
 */
const EXTRA_RED_FLAGS: readonly RedFlag[] = [
  "honeypot",
  "not_sellable",
  "dev_cluster",
  "bundle_cluster",
  "holder_concentration",
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

  // Red flags: the gate reasons that are token-safety relevant, plus an
  // occasional extra scanner fingerprint (honeypot / not-sellable / clusters).
  // A passed candidate is clean; a rejected one carries its safety red flags.
  const redFlags: RedFlag[] = passed
    ? []
    : Array.from(
        new Set<RedFlag>([
          // Gate reasons (minus "passed") are themselves token-safety red flags.
          ...reasons
            .filter((r) => r !== "passed")
            .map((r): RedFlag => r),
          ...(chance(0.35) ? [pick(EXTRA_RED_FLAGS)] : []),
        ]),
      );

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
    redFlags,
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

const SURFACES = ["EH-001", "EH-002", "EH-003", "EH-005"] as const;

/**
 * Build a plausible cost breakdown and derive the round-trip cost in SOL for an
 * entry of `entrySol`. Net PnL = gross PnL − this cost (net is the headline).
 */
function makeCost(entrySol: number): { cost: CostBreakdown; costSol: number } {
  const tipSol = round3(rand(0.001, 0.02));
  const prioritySol = round3(rand(0.0005, 0.008));
  const entrySlippageBps = roundInt(rand(40, 180));
  const ammFeeBps = roundInt(rand(25, 60));
  const exitSlippageBps = roundInt(rand(30, 160));
  const adverseSelectionBps = 150; // UNCALIBRATED floor (OQ-007, data-models §6.2)
  const slipFeeBps =
    entrySlippageBps + ammFeeBps + exitSlippageBps + adverseSelectionBps;
  // bps cost on notional + the fixed tip/priority lamport costs.
  const costSol = round3(entrySol * (slipFeeBps / 10_000) + tipSol + prioritySol);
  // total bps including the tip/priority expressed as bps-on-notional.
  const fixedBps = entrySol > 0 ? ((tipSol + prioritySol) / entrySol) * 10_000 : 0;
  const totalBps = roundInt(slipFeeBps + fixedBps);
  return {
    cost: {
      totalBps,
      tipSol,
      prioritySol,
      entrySlippageBps,
      ammFeeBps,
      exitSlippageBps,
      adverseSelectionBps,
    },
    costSol,
  };
}

function makeOpenPosition(
  token: string,
  entrySol: number,
  pctRange: [number, number],
  tpHit: number,
  trailingArmed: boolean,
  exitMode: Position["exitMode"],
  ageRange: [number, number],
): Position {
  const currentPct = round1(rand(pctRange[0], pctRange[1]));
  const { cost, costSol } = makeCost(entrySol);
  // Unrealized gross = entry notional * current%. Net subtracts the round-trip
  // cost estimate so the headline is always net-of-cost.
  const grossUnrealized = round2(entrySol * (currentPct / 100));
  const unrealizedPnlSol = round2(grossUnrealized - costSol);
  return {
    mint: randMint(),
    token,
    entrySol,
    currentPct,
    entrySlipPct: round1(cost.entrySlippageBps / 100),
    tpHit,
    tpTotal: 3,
    trailingArmed,
    hardStopPct: -18,
    exitMode,
    ageSec: roundInt(rand(ageRange[0], ageRange[1])),
    status: "open",
    realizedPnlSol: null,
    realizedGrossPnlSol: null,
    unrealizedPnlSol,
    fsmState: "OPEN",
    surface: pick(SURFACES),
    cost,
  };
}

export function makePositions(): Position[] {
  const open: Position[] = [
    makeOpenPosition("PNUT", 4.0, [8, 64], 1, true, "secure", [40, 600]),
    makeOpenPosition("MOODENG", 2.5, [-12, 30], 0, false, "fast", [15, 220]),
    makeOpenPosition("FWOG", 5.0, [80, 210], 2, true, "secure", [120, 900]),
  ];

  const closedTokens = ["WIF", "BONK", "POPCAT", "GIGA", "BRETT", "RETARDIO"];
  const closed: Position[] = closedTokens.map((token) => {
    const profitable = chance(0.62);
    const entry = round1(rand(1.5, 5));
    const { cost, costSol } = makeCost(entry);
    const grossPnl = profitable
      ? round2(entry * rand(0.3, 2.2))
      : round2(-entry * rand(0.1, 0.6));
    // Net is always gross minus the realized round-trip cost (the headline).
    const netPnl = round2(grossPnl - costSol);
    return {
      mint: randMint(),
      token,
      entrySol: entry,
      currentPct: 0,
      entrySlipPct: round1(cost.entrySlippageBps / 100),
      tpHit: profitable ? roundInt(rand(1, 3)) : 0,
      tpTotal: 3,
      trailingArmed: false,
      hardStopPct: -18,
      exitMode: chance(0.5) ? "secure" : "fast",
      ageSec: roundInt(rand(60, 1800)),
      status: "closed",
      realizedPnlSol: netPnl,
      realizedGrossPnlSol: grossPnl,
      unrealizedPnlSol: 0,
      fsmState: "CLOSED",
      surface: pick(SURFACES),
      cost,
    };
  });

  return [...open, ...closed];
}

/* ------------------------- copy-trade / smart money ------------------------ */

const SMART_WALLET_SEED: Array<
  Pick<SmartWallet, "label" | "passesFilter"> & {
    recentEntries: number;
    entryLagSlots: number | null;
    trackedNetPnlSol: number;
    filterReasons: string[];
  }
> = [
  {
    label: "early-lp sniper",
    passesFilter: true,
    recentEntries: 7,
    entryLagSlots: 3,
    trackedNetPnlSol: 41.2,
    filterReasons: ["net-of-cost positive over 30d", "survives group-purge"],
  },
  {
    label: "migration specialist",
    passesFilter: true,
    recentEntries: 5,
    entryLagSlots: 4,
    trackedNetPnlSol: 22.9,
    filterReasons: ["consistent on migrations", "low cluster overlap"],
  },
  {
    label: "high-churn bundler",
    passesFilter: false,
    recentEntries: 19,
    entryLagSlots: 1,
    trackedNetPnlSol: -8.4,
    filterReasons: ["net-of-cost negative", "bundle-cluster overlap"],
  },
  {
    label: "late chaser",
    passesFilter: false,
    recentEntries: 3,
    entryLagSlots: 9,
    trackedNetPnlSol: 1.1,
    filterReasons: ["entry lag too high (we'd be 9 slots behind)"],
  },
  {
    label: "unlabeled wallet",
    passesFilter: false,
    recentEntries: 2,
    entryLagSlots: null,
    trackedNetPnlSol: 0.4,
    filterReasons: ["insufficient tracked history"],
  },
];

const randAddr = () => {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz123456789";
  let s = "";
  for (let i = 0; i < 8; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return `${s}…${chars[Math.floor(Math.random() * chars.length)]}${chars[Math.floor(Math.random() * chars.length)]}`;
};

export function makeSmartWallets(): SmartWallet[] {
  return SMART_WALLET_SEED.map((w, i) => ({
    address: randAddr(),
    label: w.label,
    recentEntries: w.recentEntries,
    entryLagSlots: w.entryLagSlots,
    trackedNetPnlSol: w.trackedNetPnlSol,
    passesFilter: w.passesFilter,
    filterReasons: w.filterReasons,
    lastSeenMs: Date.now() - i * 73_000,
  }));
}

/* ----------------------- candidate / watchlist queue ----------------------- */

/**
 * Reason text per status — honest, de-risk framing. A "skipped" candidate
 * explains WHY we stood down; a "monitoring" one explains what we are waiting
 * on. This is observability copy, not a control prompt.
 */
const CANDIDATE_REASONS: Record<CandidateStatus, readonly string[]> = {
  monitoring: [
    "watching first-K-slot buy pressure before any decision",
    "liquidity still forming; awaiting LP-lock confirmation",
    "social synchronicity rising — observing for confirmation",
  ],
  pending: [
    "passed safety gate; awaiting classifier score",
    "model scoring in progress",
    "smart-wallet inflow detected; awaiting gate verdict",
  ],
  skipped: [
    "model p below snipe threshold — stood down",
    "slot delay too high; we would enter behind the move",
    "safety gate red-flagged the token — de-risked, not entered",
    "cost-to-edge unfavorable after slippage estimate",
  ],
  sniped: [
    "gate clean, model over threshold, smart-wallet inflow — entry taken",
    "high conviction with rising synchronicity — entry taken",
  ],
};

const CANDIDATE_SEED: Array<{
  token: string;
  status: CandidateStatus;
  modelP: number | null;
  safetyPassed: boolean;
  redFlags: RedFlag[];
  smartWallets: number;
}> = [
  {
    token: "FWOG",
    status: "sniped",
    modelP: 0.84,
    safetyPassed: true,
    redFlags: [],
    smartWallets: 6,
  },
  {
    token: "PNUT",
    status: "pending",
    modelP: 0.66,
    safetyPassed: true,
    redFlags: [],
    smartWallets: 4,
  },
  {
    token: "MOODENG",
    status: "monitoring",
    modelP: null,
    safetyPassed: true,
    redFlags: [],
    smartWallets: 2,
  },
  {
    token: "GIGA",
    status: "skipped",
    modelP: 0.31,
    safetyPassed: true,
    redFlags: [],
    smartWallets: 1,
  },
  {
    token: "SLERF",
    status: "skipped",
    modelP: null,
    safetyPassed: false,
    redFlags: ["sniper_cluster", "lp_unlocked"],
    smartWallets: 0,
  },
  {
    token: "BODEN",
    status: "skipped",
    modelP: null,
    safetyPassed: false,
    redFlags: ["honeypot", "not_sellable"],
    smartWallets: 0,
  },
  {
    token: "MICHI",
    status: "monitoring",
    modelP: null,
    safetyPassed: true,
    redFlags: [],
    smartWallets: 3,
  },
  {
    token: "RETARDIO",
    status: "pending",
    modelP: 0.58,
    safetyPassed: true,
    redFlags: ["holder_concentration"],
    smartWallets: 2,
  },
];

/**
 * Deterministic-ish candidate queue for the offline mock. Reasons are picked
 * from the status-appropriate copy; timestamps fan out so the table reads like
 * a live pipeline. Read-only — no field here triggers or implies an action.
 */
export function makeCandidates(): Candidate[] {
  const now = Date.now();
  return CANDIDATE_SEED.map((c, i) => {
    const reasons = CANDIDATE_REASONS[c.status];
    return {
      id: `cand-${i}`,
      firstSeenMs: now - (i + 1) * 47_000 - rand(0, 12_000),
      updatedMs: now - i * 9_000 - rand(0, 5_000),
      token: c.token,
      mint: randMint(),
      source: pick(SOURCES),
      status: c.status,
      modelP: c.modelP,
      redFlags: c.redFlags,
      safetyPassed: c.safetyPassed,
      smartWallets: c.smartWallets,
      reason: pick(reasons),
    };
  });
}

/* --------------------- wallet-cluster graph (Bubble Maps) ------------------- */

/**
 * Deterministic-shape wallet-cluster graphs for the offline mock — the E11
 * "Bubble Maps" view. Each graph mirrors the backend WalletClusterGraph
 * (aats.control_plane.wallet_cluster_schemas): nodes are wallets (creator /
 * bundler / sniper / holder) carrying a supply share in BASIS POINTS (never
 * money), edges are detected connections with a [0,1] correlation weight, and
 * the manipulation flags are de-risk-only signals. Read-only — nothing here
 * triggers or implies an action. The mock supplies one heavily-coordinated mint
 * (the operator's worst case) and one clean mint so both states render.
 */
export function makeWalletClusters(): WalletClusterGraph[] {
  // A heavily coordinated launch: a creator that funded a same-block bundle, a
  // co-timed sniper ring, and a large cluster supply share — the manipulation
  // an operator most needs to SEE.
  const creator = randAddr();
  const bundlers = [randAddr(), randAddr(), randAddr()];
  const snipers = [randAddr(), randAddr()];
  const holder = randAddr();

  const coordinated: WalletClusterGraph = {
    mint: randMint(),
    token: "SCAMINU",
    evaluatedAtSlot: 287_443_120,
    sniperClusterScore: 0.86,
    bundlingDetected: true,
    bundlerWalletCount: bundlers.length,
    devBundleClusterBps: 4200, // 42% of supply in one cluster
    nodes: [
      {
        wallet: creator,
        nodeType: "creator",
        supplyShareBps: 1500,
        buySlot: null,
        manipulationFlags: ["bundling_detected", "freeze_authority_set"],
      },
      ...bundlers.map((w, i) => ({
        wallet: w,
        nodeType: "bundler" as const,
        supplyShareBps: 900 - i * 100,
        buySlot: 287_443_120 + i,
        manipulationFlags: ["bundling_detected" as const],
      })),
      ...snipers.map((w, i) => ({
        wallet: w,
        nodeType: "sniper" as const,
        supplyShareBps: 600 - i * 150,
        buySlot: 287_443_122 + i,
        manipulationFlags: ["high_synchronicity" as const],
      })),
      {
        wallet: holder,
        nodeType: "holder",
        supplyShareBps: 300,
        buySlot: 287_443_140,
        manipulationFlags: [],
      },
    ],
    edges: [
      // creator funded each bundler (same-block bundle)
      ...bundlers.map((w) => ({
        source: creator,
        target: w,
        edgeType: "creator_funded" as const,
        weight: 0.95,
      })),
      // bundlers all bought in the same bundle
      {
        source: bundlers[0],
        target: bundlers[1],
        edgeType: "same_bundle" as const,
        weight: 0.92,
      },
      {
        source: bundlers[1],
        target: bundlers[2],
        edgeType: "same_bundle" as const,
        weight: 0.9,
      },
      // snipers co-timed with each other and share a funder
      {
        source: snipers[0],
        target: snipers[1],
        edgeType: "co_timing" as const,
        weight: 0.78,
      },
      {
        source: creator,
        target: snipers[0],
        edgeType: "shared_funder" as const,
        weight: 0.64,
      },
    ],
    manipulationFlags: [
      "bundling_detected",
      "high_cluster_share",
      "high_synchronicity",
      "freeze_authority_set",
    ],
  };

  // A clean launch: a couple of unrelated holders, no detected coordination.
  const clean: WalletClusterGraph = {
    mint: randMint(),
    token: "WAGMI",
    evaluatedAtSlot: 287_443_980,
    sniperClusterScore: 0.12,
    bundlingDetected: false,
    bundlerWalletCount: 0,
    devBundleClusterBps: 450,
    nodes: [
      {
        wallet: randAddr(),
        nodeType: "creator",
        supplyShareBps: 450,
        buySlot: null,
        manipulationFlags: [],
      },
      {
        wallet: randAddr(),
        nodeType: "holder",
        supplyShareBps: 220,
        buySlot: 287_443_985,
        manipulationFlags: [],
      },
      {
        wallet: randAddr(),
        nodeType: "holder",
        supplyShareBps: 180,
        buySlot: 287_443_990,
        manipulationFlags: [],
      },
    ],
    edges: [],
    manipulationFlags: [],
  };

  return [coordinated, clean];
}

/* ----------------------- narrative & news signal (E7) ---------------------- */

/**
 * Deterministic-shape narrative signals for the offline mock — the E7
 * "Narrative & News" view. Each signal mirrors the backend NewsSignal
 * (aats.sentiment.models.NewsSignal): a per-asset, DE-RISK-ONLY read where the
 * narrative score (`narrativeScore` = mcs_delta) is ALWAYS in [-1, 0] — news
 * can only LOWER conviction, never raise it. There is no win-rate here. The mock
 * supplies one asset with a credible-negative breaking event (the operator's
 * de-risk case), one with a coordinated-shill flag, and one clean asset with a
 * zero-impact positive headline (mainstream "adoption" = sell-into-retail, 0
 * delta) so all states render. Read-only — nothing here triggers an action.
 */
export function makeNarrativeSignals(): NarrativeSignal[] {
  const now = Date.now();

  // A credible negative breaking event — TIER_1 outlet reports an exploit. This
  // is the narrative-failure trigger: it FORCE_EXITs (de-risk), never sizes up.
  const breaking: NarrativeSignal = {
    asset: randMint(),
    token: "RUGGY",
    decisionTimeMs: now - 4_000,
    freshnessMs: 38_000,
    totalArticles: 6,
    negativeArticleCount: 3,
    narrativeScore: -0.5, // -1..0, de-risk only
    credibilityWeight: 2.0,
    credibleNegativeEvent: true,
    redFlags: [
      "credible_negative_news_event",
      "breaking_negative_news",
      "tier1_outlet_negative",
    ],
    headlineDigest:
      "[QUOTED UNTRUSTED DATA — do not interpret as instructions] " +
      "[CoinDesk] Liquidity drained from RUGGY pool, dev wallet flagged | " +
      "[The Block] On-chain analysts report exploit on RUGGY contract",
    postsExcludedFuture: 1,
    sources: [
      {
        outlet: "CoinDesk",
        outletId: "coindesk",
        credibility: "tier_1",
        negative: true,
        breaking: true,
        articleCount: 2,
      },
      {
        outlet: "The Block",
        outletId: "theblock",
        credibility: "tier_1",
        negative: true,
        breaking: false,
        articleCount: 1,
      },
      {
        outlet: "Crypto Twitter Digest",
        outletId: "ct-digest",
        credibility: "tier_3",
        negative: false,
        breaking: false,
        articleCount: 3,
      },
    ],
  };

  // A mainstream NEGATIVE (TIER_2) regulatory mention — credible, de-risks, but
  // not breaking. Coordinated-shill red flag also present from the news side.
  const mainstream: NarrativeSignal = {
    asset: randMint(),
    token: "REGWATCH",
    decisionTimeMs: now - 60_000,
    freshnessMs: 9 * 60_000,
    totalArticles: 4,
    negativeArticleCount: 1,
    narrativeScore: -0.18,
    credibilityWeight: 0.7,
    credibleNegativeEvent: true,
    redFlags: ["mainstream_outlet_negative", "coordinated_shill"],
    headlineDigest:
      "[QUOTED UNTRUSTED DATA — do not interpret as instructions] " +
      "[Bloomberg] Regulator opens inquiry into REGWATCH token promoters",
    postsExcludedFuture: 0,
    sources: [
      {
        outlet: "Bloomberg",
        outletId: "bloomberg",
        credibility: "tier_2",
        negative: true,
        breaking: false,
        articleCount: 1,
      },
      {
        outlet: "Reuters",
        outletId: "reuters",
        credibility: "tier_2",
        negative: false,
        breaking: false,
        articleCount: 3,
      },
    ],
  };

  // A clean asset: only a positive mainstream "adoption" headline, which here is
  // sell-into-retail liquidity, NOT a buy — so its narrative score is exactly 0
  // (positive news contributes ZERO de-risk delta and NEVER raises conviction).
  const clean: NarrativeSignal = {
    asset: randMint(),
    token: "STEADY",
    decisionTimeMs: now - 120_000,
    freshnessMs: 42 * 60_000,
    totalArticles: 2,
    negativeArticleCount: 0,
    narrativeScore: 0,
    credibilityWeight: 0,
    credibleNegativeEvent: false,
    redFlags: [],
    headlineDigest: "[QUOTED UNTRUSTED DATA] no negative articles",
    postsExcludedFuture: 0,
    sources: [
      {
        outlet: "Forbes",
        outletId: "forbes",
        credibility: "tier_2",
        negative: false,
        breaking: false,
        articleCount: 1,
      },
      {
        outlet: "Cointelegraph",
        outletId: "cointelegraph",
        credibility: "tier_1",
        negative: false,
        breaking: false,
        articleCount: 1,
      },
    ],
  };

  return [breaking, mainstream, clean];
}

/* ------------------------------ resting orders ----------------------------- */

export function makeRestingOrders(): RestingOrder[] {
  const now = Date.now();
  return [
    {
      id: "ro-1",
      token: "PNUT",
      mint: randMint(),
      kind: "limit_exit",
      triggerPct: 120,
      fractionBps: 3300,
      status: "armed",
      offlineCapable: true,
      createdMs: now - 600_000,
    },
    {
      id: "ro-2",
      token: "FWOG",
      mint: randMint(),
      kind: "stop_exit",
      triggerPct: -18,
      fractionBps: 10_000,
      status: "armed",
      offlineCapable: true,
      createdMs: now - 1_200_000,
    },
    {
      id: "ro-3",
      token: "MOODENG",
      mint: randMint(),
      kind: "dca_exit",
      triggerPct: 60,
      fractionBps: 2500,
      status: "triggered",
      offlineCapable: true,
      createdMs: now - 2_400_000,
    },
  ];
}

/* ----------------------------- auto-strat presets -------------------------- */

export const autoStratPresets: AutoStratPreset[] = [
  {
    id: "secure-ladder",
    name: "Secure ladder",
    description:
      "Conservative scale-out with an early first rung and a tight trailing give-back. Secure (private) exits. The default.",
    tpLadderPct: [40, 100, 220],
    trailingPct: 14,
    hardStopPct: -18,
    exitMode: "secure",
    maxHoldMin: 240,
  },
  {
    id: "runner",
    name: "Runner",
    description:
      "Lets winners run with a wider trailing give-back and higher rungs. Still de-risk only — trailing tightens, never widens.",
    tpLadderPct: [80, 200, 500],
    trailingPct: 22,
    hardStopPct: -22,
    exitMode: "secure",
    maxHoldMin: 480,
  },
  {
    id: "fast-scalp",
    name: "Fast scalp",
    description:
      "Quick, fast-mode exits for high-contention launches; takes profit early and caps hold time hard.",
    tpLadderPct: [25, 60, 120],
    trailingPct: 10,
    hardStopPct: -14,
    exitMode: "fast",
    maxHoldMin: 60,
  },
];

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
