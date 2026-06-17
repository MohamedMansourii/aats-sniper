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

/**
 * Token-safety scanner red flags (T-323/T-329 pre-trade gate → wire `red_flags`).
 * These are reasons the safety gate would DE-RISK (skip/veto) a candidate. The
 * set is open on the wire (the scanner may add fingerprints); unknown codes are
 * surfaced verbatim rather than dropped, so an operator never misses a warning.
 */
export type RedFlag =
  | "freeze_authority"
  | "mint_authority"
  | "lp_unlocked"
  | "lp_not_locked"
  | "sniper_cluster"
  | "dev_cluster"
  | "bundle_cluster"
  | "high_tax"
  | "honeypot"
  | "not_sellable"
  | "holder_concentration"
  | "low_liquidity"
  | (string & {});

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
  /** Token-safety scanner red flags carried on the event (FR-037, AC-015). */
  redFlags: RedFlag[];
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

/**
 * Per-position cost breakdown (wire `cost_breakdown`, data-models §6.2 CostStack).
 * All values are basis points or display-SOL derived from integer base units on
 * the wire — never float-as-money. Used to make net-of-cost PnL the headline and
 * keep gross strictly secondary (BUILD-DIRECTIVE: net-of-cost PRIMARY).
 */
export interface CostBreakdown {
  /** Total round-trip cost in bps (tip + priority + slippage + amm + adverse). */
  totalBps: number;
  tipSol: number;
  prioritySol: number;
  entrySlippageBps: number;
  ammFeeBps: number;
  exitSlippageBps: number;
  adverseSelectionBps: number;
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
  /** Realized PnL NET of all costs (PRIMARY). Null while open. */
  realizedPnlSol: number | null;
  /** Realized PnL GROSS of costs (SECONDARY — shown smaller, labeled gross). */
  realizedGrossPnlSol: number | null;
  /** Unrealized PnL NET of estimated round-trip cost (PRIMARY). */
  unrealizedPnlSol: number;
  /** FSM state for the position lifecycle (data-models §7). */
  fsmState: "IDLE" | "ENTERING" | "OPEN" | "CLOSING" | "CLOSED" | "VETOED";
  /** Thesis surface that fired (EH-001 | EH-003 | …). */
  surface: string;
  cost: CostBreakdown;
}

/* -------------------------------------------------------------------------- */
/*  Copy-trade / smart-money SELECTIVITY (view only — NEVER a buy trigger).    */
/*  T-302 stream + T-304 `smart_wallets_in` feature + T-324 filter wiring.     */
/*  This surface DISPLAYS which tracked wallets the selectivity filter would    */
/*  consider; it has no control that places, sizes, or mirrors an order.        */
/* -------------------------------------------------------------------------- */

export interface SmartWallet {
  /** Wallet address (truncated for display by the page). */
  address: string;
  /** Operator-facing label/tag for the wallet, if any. */
  label: string;
  /**
   * Count of recent tracked entries this wallet made in first-K slots — the
   * `smart_wallets_in` selectivity feature (a count, never a buy signal).
   */
  recentEntries: number;
  /**
   * Our entry slot minus their fill slot when we followed: positive = we were
   * BEHIND them (data-models §3 `smart_wallet_entry_lag_slots`). Null if N/A.
   * Surfacing the lag is honest: it shows we are a follower, not a front-runner.
   */
  entryLagSlots: number | null;
  /** Net-of-cost realized PnL across their tracked closed trades (display SOL). */
  trackedNetPnlSol: number;
  /** Whether this wallet currently clears the selectivity bar (filter VIEW). */
  passesFilter: boolean;
  /** Why it does/does not pass — selectivity reasons (de-risk framing). */
  filterReasons: string[];
  /** Last time we observed this wallet act (ms epoch). */
  lastSeenMs: number;
}

/* -------------------------------------------------------------------------- */
/*  Resting orders (limit / DCA) — fire when the operator is OFFLINE.          */
/*  T-326. De-risk-direction only: a resting order may exit/reduce; the entry  */
/*  side is the cost-gated SNIPE path, never widened here.                     */
/* -------------------------------------------------------------------------- */

export type RestingOrderKind = "limit_exit" | "dca_exit" | "stop_exit";

export interface RestingOrder {
  id: string;
  token: string;
  mint: string;
  kind: RestingOrderKind;
  /** Trigger expressed as a signed % move from entry (e.g. +60 TP, -18 stop). */
  triggerPct: number;
  /** Fraction of the remaining position this order exits, in bps (0..10000). */
  fractionBps: number;
  status: "armed" | "triggered" | "cancelled";
  /** True when this order can fire with no operator/process online (DMS-backed). */
  offlineCapable: boolean;
  createdMs: number;
}

/* -------------------------------------------------------------------------- */
/*  Auto-strat presets (TP-ladder + trailing + exit mode) — T-325.            */
/*  A preset only configures EXIT behavior (de-risk). It never sizes entries.  */
/* -------------------------------------------------------------------------- */

export interface AutoStratPreset {
  id: string;
  name: string;
  description: string;
  /** TP ladder rungs as signed % gains (ascending). */
  tpLadderPct: number[];
  /** Trailing-stop give-back %, applied after the last rung arms. */
  trailingPct: number;
  /** Hard-stop % (negative). */
  hardStopPct: number;
  /** Default exit mode for positions opened under this preset. */
  exitMode: "secure" | "fast";
  /** Max hold before timeout-exit (minutes). */
  maxHoldMin: number;
}

/* -------------------------------------------------------------------------- */
/*  Candidate / watchlist queue (E3) — READ-ONLY pipeline VIEW.                */
/*                                                                            */
/*  The queue of tokens the engine EVALUATED but has not necessarily acted on: */
/*  what is being monitored, what is pending a decision, what was skipped, and */
/*  what was sniped. This surface is pure observability — it carries no action */
/*  and the page renders NO control that snipes, sizes, vetoes, or re-queues a */
/*  candidate. `GET /candidates` only; never a write. (ENHANCEMENT E3.)         */
/* -------------------------------------------------------------------------- */

/**
 * Where a candidate sits in the evaluation pipeline.
 *   monitoring — observed, still being watched (no decision yet)
 *   pending    — passed initial screens, awaiting the model/gate verdict
 *   skipped    — evaluated and NOT acted on (de-risk: we stood down)
 *   sniped     — an entry was taken (terminal, informational here)
 */
export type CandidateStatus = "monitoring" | "pending" | "skipped" | "sniped";

/**
 * One row in the candidate queue. `modelP` is the classifier probability
 * (0..1) or null when the model has not scored it yet. `redFlags` is the
 * token-safety report (same scanner taxonomy as SnipeEvent — open set, unknown
 * codes surfaced verbatim). `reason` is the human-readable why-this-status.
 */
export interface Candidate {
  id: string;
  /** First seen in the pipeline (ms epoch). */
  firstSeenMs: number;
  /** Last evaluated / status-updated (ms epoch). */
  updatedMs: number;
  /**
   * Display name. The wire (CandidateRecord, api-contracts.md §14) carries only
   * the `mint` — there is no separate token name. The live adapter sets this to
   * the mint; the offline mock supplies a friendlier symbol for the demo.
   */
  token: string;
  mint: string;
  /**
   * Launch venue. NOT on the candidate wire (CandidateRecord has no `source`) —
   * `null` means "not reported by the backend", rendered as n/a. The mock fills
   * a venue for the offline demo only.
   */
  source: SnipeSource | null;
  status: CandidateStatus;
  /** Classifier probability 0..1, or null if not yet scored. */
  modelP: number | null;
  /** Token-safety scanner report (de-risk reasons). Empty = clean. */
  redFlags: RedFlag[];
  /** Whether the pre-trade safety gate passed (clean) for this candidate. */
  safetyPassed: boolean;
  /**
   * Count of tracked smart wallets in — a selectivity feature, never a buy
   * signal. NOT on the candidate wire (CandidateRecord has no smart-wallet
   * count); `null` means "not reported", rendered as n/a. Mock-only otherwise.
   */
  smartWallets: number | null;
  /** Human-readable reason for the current status (why monitoring/skipped/etc). */
  reason: string;
}

/* -------------------------------------------------------------------------- */
/*  Wallet-cluster graph (E11) — READ-ONLY "Bubble Maps" VIEW.                  */
/*                                                                            */
/*  A wallet-connection graph that renders the EXISTING sniper-cluster /        */
/*  bundle-detection data (microstructure features + the pre-trade gate) so an  */
/*  operator can SEE coordinated manipulation. Pure observability — the page    */
/*  carries no action and no field here can trigger, size-up, or widen a stop.  */
/*  `GET /wallet-cluster` only; never a write. All share quantities are basis   */
/*  points (int, 0..10000), never money. (ENHANCEMENT E11; mirrors             */
/*  aats.control_plane.wallet_cluster_schemas.WalletClusterGraph.)             */
/* -------------------------------------------------------------------------- */

/** Role a wallet plays in the cluster graph. Closed set; unknown → "unknown". */
export type WalletNodeType =
  | "creator" // the deployer / developer wallet
  | "bundler" // same-bundle buyer (same-block funded from creator)
  | "sniper" // early buyer in the first-K-slots window
  | "holder" // top-10 holder, not a bundler/sniper
  | "unknown"; // observed but not classified

/** Nature of a detected connection between two wallets. Closed set. */
export type WalletEdgeType =
  | "same_bundle" // bought in the same block / bundle (correlated timing)
  | "shared_funder" // common ancestor funding source detected
  | "co_timing" // statistically co-timed buys (high synchronicity)
  | "creator_funded"; // the creator wallet directly funded the buyer

/**
 * De-risk-only manipulation flags. A present flag LOWERS safety / adds a
 * de-risk signal — it can NEVER increase risk tolerance or trigger an entry.
 * The wire set is closed; an unknown code is surfaced verbatim (a `string`)
 * rather than dropped — an operator must never miss a manipulation warning.
 */
export type ManipulationFlag =
  | "bundling_detected" // 3+ wallets in same-block coordinated buys
  | "high_cluster_share" // cluster holds > threshold% of supply
  | "freeze_authority_set" // freeze authority not renounced
  | "high_synchronicity" // sniper_cluster_score over the coordination threshold
  | (string & {});

/** One wallet vertex in the cluster graph. */
export interface WalletNode {
  /** Public key (display only; never a signing key). */
  wallet: string;
  /** Role in the cluster. */
  nodeType: WalletNodeType;
  /** Token supply share held, in basis points (int, 0..10000). NOT money. */
  supplyShareBps: number;
  /** Slot of the buy event, or null if this wallet is not a buyer. */
  buySlot: number | null;
  /** De-risk-only manipulation flags present on this wallet. */
  manipulationFlags: ManipulationFlag[];
}

/** One detected connection between two wallet nodes. */
export interface WalletEdge {
  /** Source wallet pubkey (must match a WalletNode.wallet). */
  source: string;
  /** Target wallet pubkey (must match a WalletNode.wallet). */
  target: string;
  /** Nature of the detected connection. */
  edgeType: WalletEdgeType;
  /** Correlation strength in [0, 1] — a statistical quantity, NOT money. */
  weight: number;
}

/**
 * Per-mint wallet-connection graph the operator views. Mirrors the backend
 * WalletClusterGraph (api wire / wallet_cluster_schemas.py). `sniperClusterScore`
 * is a [0, 1] statistical score (0 = no snipers, 1 = fully coordinated), not a
 * win-rate. All bps quantities are int ratios, never lamports.
 */
export interface WalletClusterGraph {
  /** Token mint this graph describes. */
  mint: string;
  /** Display name. The wire carries only the mint; the mock supplies a symbol. */
  token: string;
  /** Event-time slot (C-5) the cluster was detected at — never compute-time. */
  evaluatedAtSlot: number;
  /** [0, 1] sniper coordination score (statistical, not money, not a win-rate). */
  sniperClusterScore: number;
  /** True if a same-block bundle of >= 3 wallets was detected. */
  bundlingDetected: boolean;
  /** Count of bundler wallets (0..N). */
  bundlerWalletCount: number;
  /** Largest single cluster holding in basis points (int, 0..10000). NOT money. */
  devBundleClusterBps: number;
  /** Wallet vertices. */
  nodes: WalletNode[];
  /** Detected connections between wallet pairs. */
  edges: WalletEdge[];
  /** Union of all manipulation flags across the graph (de-risk-only). */
  manipulationFlags: ManipulationFlag[];
}

/* -------------------------------------------------------------------------- */
/*  Narrative & News signal (E7) — READ-ONLY de-risk VIEW.                      */
/*                                                                            */
/*  Per-asset news narrative read, mirroring the backend NewsSignal            */
/*  (aats.sentiment.models.NewsSignal). The page that renders this is pure      */
/*  observability — it carries NO action and no field here can trigger,        */
/*  size-up, widen a stop, or add leverage.                                     */
/*                                                                            */
/*  DE-RISK FRAMING (non-waivable, mirrors the backend's asymmetric formula):   */
/*    `narrativeScore` (= NewsSignal.mcs_delta) is ALWAYS in [-1.0, 0.0]. News  */
/*    can only LOWER conviction, never raise it. 0 = no impact; -1 = total      */
/*    narrative collapse. Positive/neutral news contributes ZERO — a mainstream */
/*    "adoption" headline is sell-into-retail liquidity here, not a buy.        */
/*    There is NO win-rate, target, or probability-of-profit field anywhere.    */
/*  `GET /narrative` only; never a write. (ENHANCEMENT E7.)                      */
/* -------------------------------------------------------------------------- */

/**
 * Credibility tier of the outlet behind a negative signal. Closed set; an
 * unknown wire value falls back to the lowest-trust tier ("tier_3"), never a
 * higher one (de-risk bias — we never over-trust a source we cannot classify).
 */
export type NewsCredibilityTier = "tier_1" | "tier_2" | "tier_3";

/**
 * One source outlet that contributed an article in the decision window. Display
 * only — the URL is an audit reference, never fetched again by the dashboard.
 */
export interface NarrativeSource {
  /** Human-readable outlet name (e.g. "CoinDesk"). QUOTED UNTRUSTED DATA. */
  outlet: string;
  /** Machine-stable outlet id (e.g. "coindesk"). */
  outletId: string;
  /** Credibility tier of the outlet. */
  credibility: NewsCredibilityTier;
  /** True if any article from this outlet was a NEGATIVE signal. */
  negative: boolean;
  /** True if any article from this outlet was flagged breaking. */
  breaking: boolean;
  /** Count of articles from this outlet in the window. */
  articleCount: number;
}

/**
 * Aggregated news narrative for one asset at a point in time. Mirrors the
 * backend NewsSignal — the structured, de-risk-only contribution to conviction.
 */
export interface NarrativeSignal {
  /** Token mint / asset this signal describes. */
  asset: string;
  /** Display name. The wire carries only the asset; the mock supplies a symbol. */
  token: string;
  /** Point-in-time decision anchor (ms epoch) the window closed at (C-5). */
  decisionTimeMs: number;
  /** Freshness: ms since the most recent article in the window. Null if none. */
  freshnessMs: number | null;
  /** Total articles scored in the window. */
  totalArticles: number;
  /** Negative articles in the window. */
  negativeArticleCount: number;
  /**
   * Per-asset narrative score (= NewsSignal.mcs_delta). ALWAYS in [-1.0, 0.0]:
   * a DE-RISK delta. 0 = no impact, negative = conviction lowered. NEVER a
   * win-rate and NEVER positive.
   */
  narrativeScore: number;
  /** Aggregate credibility weight of the negative articles (>= 0). */
  credibilityWeight: number;
  /**
   * True iff a credible (TIER_1/TIER_2) NEGATIVE article exists — the
   * narrative-failure trigger that may FORCE_EXIT (de-risk only).
   */
  credibleNegativeEvent: boolean;
  /**
   * Coordinated-shill / manipulation red flags (de-risk-only). A present flag
   * LOWERS conviction; it can NEVER raise risk or trigger an entry. Open set —
   * an unknown code is surfaced verbatim, never dropped.
   */
  redFlags: RedFlag[];
  /** Sanitised digest of the most-credible negative headlines (UNTRUSTED). */
  headlineDigest: string;
  /** Articles dropped by the point-in-time filter (future-dated). */
  postsExcludedFuture: number;
  /** Per-outlet source roll-up for the sources/freshness view. */
  sources: NarrativeSource[];
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
