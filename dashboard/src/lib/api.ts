/**
 * AATS Sniper — typed control-plane client + React hooks.
 *
 * Default: mock-backed (import.meta.env.VITE_USE_MOCK !== 'false'), so the
 * dashboard runs standalone with no backend. When VITE_USE_MOCK === 'false',
 * hooks fetch/stream the operator control plane at the URLs stubbed below.
 *
 * No extra deps: plain useState + useEffect + setInterval/EventSource.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  AgentMode,
  AgentState,
  AutoStratPreset,
  Candidate,
  InfraTier,
  LatencyBudget,
  MCSScore,
  MetricsSnapshot,
  ModuleHealth,
  NarrativeSignal,
  Position,
  Prediction,
  Reasoning,
  RestingOrder,
  RiskConfig,
  SmartWallet,
  SnipeEvent,
  WalletClusterGraph,
} from "./types";
import {
  autoStratPresets as mockPresets,
  defaultRiskConfig,
  infraTiers as mockInfraTiers,
  latencyBudget as mockLatencyBudget,
  makeAgentState,
  makeCandidates,
  makeMetrics,
  makeModuleHealth,
  makeNarrativeSignals,
  makePositions,
  makeReasoningLog,
  makeRestingOrders,
  makeSmartWallets,
  makeSnipeEvent,
  makeWalletClusters,
  prediction as mockPrediction,
  seedSnipeFeed,
  sentimentScores as mockSentiment,
  setMockMode,
} from "./mock";
import {
  adaptAgentState,
  adaptCandidates,
  adaptHealth,
  adaptInfraTiers,
  adaptLatencyBudget,
  adaptMetrics,
  adaptNarrative,
  adaptPositions,
  adaptPrediction,
  adaptReasoningLog,
  adaptRestingOrders,
  adaptRiskConfig,
  adaptSentiment,
  adaptSmartWallets,
  adaptSnipeEvent,
  adaptWalletClusters,
  toWireMode,
  toWireRiskConfig,
  type WireAgentState,
  type WireCandidate,
  type WireWalletClusterGraph,
  type WireLatencyBudget,
  type WireMCSScore,
  type WireMetricsSnapshot,
  type WireModuleHealth,
  type WireNarrativeSignal,
  type WirePosition,
  type WirePrediction,
  type WireReasoning,
  type WireRestingOrder,
  type WireRiskConfig,
  type WireSmartWallet,
  type WireSnipeEvent,
} from "./adapters";

/* -------------------------------------------------------------------------- */
/*  Mode + transport                                                          */
/* -------------------------------------------------------------------------- */

export const USE_MOCK =
  (import.meta.env.VITE_USE_MOCK as string | undefined) !== "false";

/**
 * Live control-plane base URL (used only when USE_MOCK === false).
 * `VITE_CONTROL_PLANE_URL` from .env (see .env.example). Empty string means
 * "same origin", so the dev proxy / reverse proxy can front `/api/*`.
 */
export const CONTROL_PLANE_URL = (
  (import.meta.env.VITE_CONTROL_PLANE_URL as string | undefined) ?? ""
).replace(/\/$/, "");

/** Resolve an endpoint path against the configured control-plane base URL. */
function url(path: string): string {
  return CONTROL_PLANE_URL ? `${CONTROL_PLANE_URL}${path}` : path;
}

/** Control-plane endpoints (used only when USE_MOCK === false). */
export const ENDPOINTS = {
  state: "/api/state",
  feed: "/api/feed", // SSE
  metrics: "/api/metrics",
  positions: "/api/positions",
  latency: "/api/latency",
  sentiment: "/api/sentiment",
  predictions: "/api/predictions",
  reasoning: "/api/reasoning",
  riskConfig: "/api/risk-config",
  health: "/api/health",
  kill: "/api/kill",
  flatten: "/api/flatten",
  breakerReset: "/api/breaker/reset",
  mode: "/api/mode",
} as const;

/**
 * OPTIONAL read-only projections for the new feature surfaces (T-353).
 *
 * The FROZEN control-plane contract (api-contracts.md §12) does NOT yet expose
 * smart-money / resting-orders / preset endpoints — surfacing them on the wire
 * is a future contract delta owned by the `solana-systems-architect`, not this
 * lane. Until then these are **GET-only, de-risk-only** projections the live
 * branch *probes*; if the control plane returns a non-2xx (endpoint absent),
 * the hook falls back to the deterministic offline fake. No POST, no
 * risk-increasing write, and no new command endpoint is introduced here.
 */
export const OPTIONAL_ENDPOINTS = {
  smartWallets: "/api/smart-wallets", // read-only selectivity VIEW projection
  restingOrders: "/api/resting-orders", // read-only resting-order ledger
  presets: "/api/presets", // read-only auto-strat preset catalog
  candidates: "/api/candidates", // read-only candidate / watchlist queue (E3)
  walletCluster: "/api/wallet-cluster", // read-only wallet-cluster graph (E11)
  narrative: "/api/narrative", // read-only narrative & news signal (E7)
} as const;

/** Poll/stream cadence for the live mock streams. */
export const TICK_MS = 1500;

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(url(path), { headers: { accept: "application/json" } });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return (await res.json()) as T;
}

async function postJSON<T = unknown>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(url(path), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return (await res.json().catch(() => ({}))) as T;
}

/* -------------------------------------------------------------------------- */
/*  Generic async-resource hook shape                                         */
/* -------------------------------------------------------------------------- */

export interface Resource<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

/* -------------------------------------------------------------------------- */
/*  useAgentState                                                             */
/* -------------------------------------------------------------------------- */

export function useAgentState(): Resource<AgentState> {
  const [data, setData] = useState<AgentState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const prev = useRef<AgentState | undefined>(undefined);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      const next = makeAgentState(prev.current);
      prev.current = next;
      setData(next);
      setLoading(false);
      return;
    }
    try {
      const s = await getJSON<WireAgentState>(ENDPOINTS.state);
      setData(adaptAgentState(s));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load state");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, TICK_MS);
    return () => clearInterval(id);
  }, [load]);

  return { data, loading, error, refetch: load };
}

/* -------------------------------------------------------------------------- */
/*  useSnipeFeed — growing array, streams every ~1.5s                          */
/* -------------------------------------------------------------------------- */

const FEED_CAP = 200;

export function useSnipeFeed(): {
  events: SnipeEvent[];
  loading: boolean;
  error: string | null;
} {
  const [events, setEvents] = useState<SnipeEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (USE_MOCK) {
      // Intentional: seed the stream once, then subscribe to the mock ticker.
      // This is subscription setup (external stream → React state), the
      // documented legitimate use of an effect.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setEvents(seedSnipeFeed());
      setLoading(false);
      const id = setInterval(() => {
        setEvents((cur) => [makeSnipeEvent(), ...cur].slice(0, FEED_CAP));
      }, TICK_MS);
      return () => clearInterval(id);
    }

    // Real transport: Server-Sent Events stream of SnipeEvent JSON (snake_case
    // wire frames; adapted to the camelCase view-model per frame, §6).
    setLoading(true);
    const src = new EventSource(url(ENDPOINTS.feed));
    src.onopen = () => setLoading(false);
    src.onmessage = (msg) => {
      try {
        const wire = JSON.parse(msg.data) as WireSnipeEvent;
        const evt = adaptSnipeEvent(wire);
        setEvents((cur) => [evt, ...cur].slice(0, FEED_CAP));
      } catch {
        /* ignore malformed frame */
      }
    };
    src.onerror = () => {
      setError("feed stream interrupted");
      setLoading(false);
    };
    return () => src.close();
  }, []);

  return { events, loading, error };
}

/* -------------------------------------------------------------------------- */
/*  useMetrics                                                                */
/* -------------------------------------------------------------------------- */

export function useMetrics(): Resource<MetricsSnapshot> {
  const [data, setData] = useState<MetricsSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const prev = useRef<MetricsSnapshot | undefined>(undefined);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      const next = makeMetrics(prev.current);
      prev.current = next;
      setData(next);
      setLoading(false);
      return;
    }
    try {
      const m = await getJSON<WireMetricsSnapshot>(ENDPOINTS.metrics);
      setData(adaptMetrics(m));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load metrics");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, TICK_MS);
    return () => clearInterval(id);
  }, [load]);

  return { data, loading, error, refetch: load };
}

/* -------------------------------------------------------------------------- */
/*  usePositions                                                              */
/* -------------------------------------------------------------------------- */

export function usePositions(): Resource<Position[]> {
  const [data, setData] = useState<Position[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      setData(makePositions());
      setLoading(false);
      return;
    }
    try {
      const wire = await getJSON<WirePosition[]>(ENDPOINTS.positions);
      setData(adaptPositions(wire));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load positions");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    if (USE_MOCK) {
      const id = setInterval(load, TICK_MS * 2);
      return () => clearInterval(id);
    }
  }, [load]);

  return { data, loading, error, refetch: load };
}

/* -------------------------------------------------------------------------- */
/*  useLatency / useInfraTiers                                                */
/* -------------------------------------------------------------------------- */

export function useLatency(): Resource<LatencyBudget> {
  const [data, setData] = useState<LatencyBudget | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      setData(mockLatencyBudget);
      setLoading(false);
      return;
    }
    try {
      const wire = await getJSON<WireLatencyBudget>(ENDPOINTS.latency);
      setData(adaptLatencyBudget(wire));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load latency");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { data, loading, error, refetch: load };
}

export function useInfraTiers(): Resource<InfraTier[]> {
  const [data, setData] = useState<InfraTier[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      setData(mockInfraTiers);
      setLoading(false);
      return;
    }
    try {
      // Infra tiers are part of the latency report on the control plane (§4).
      const budget = await getJSON<WireLatencyBudget>(ENDPOINTS.latency);
      setData(adaptInfraTiers(budget));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load infra tiers");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { data, loading, error, refetch: load };
}

/* -------------------------------------------------------------------------- */
/*  useSentiment / usePredictions / useReasoning                              */
/* -------------------------------------------------------------------------- */

export function useSentiment(): Resource<MCSScore[]> {
  const [data, setData] = useState<MCSScore[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      setData(mockSentiment);
      setLoading(false);
      return;
    }
    try {
      const wire = await getJSON<WireMCSScore[]>(ENDPOINTS.sentiment);
      setData(adaptSentiment(wire));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load sentiment");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { data, loading, error, refetch: load };
}

export function usePredictions(): Resource<Prediction> {
  const [data, setData] = useState<Prediction | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      setData({ ...mockPrediction, ts: Date.now() });
      setLoading(false);
      return;
    }
    try {
      const wire = await getJSON<WirePrediction>(ENDPOINTS.predictions);
      setData(adaptPrediction(wire));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load predictions");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { data, loading, error, refetch: load };
}

export function useReasoning(): Resource<Reasoning[]> {
  const [data, setData] = useState<Reasoning[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      setData(makeReasoningLog());
      setLoading(false);
      return;
    }
    try {
      const wire = await getJSON<WireReasoning[]>(ENDPOINTS.reasoning);
      setData(adaptReasoningLog(wire));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load reasoning");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { data, loading, error, refetch: load };
}

/* -------------------------------------------------------------------------- */
/*  useRiskConfig — returns { config, save }                                  */
/* -------------------------------------------------------------------------- */

export function useRiskConfig(): {
  config: RiskConfig | null;
  loading: boolean;
  error: string | null;
  saving: boolean;
  save: (next: RiskConfig) => Promise<void>;
} {
  const [config, setConfig] = useState<RiskConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      if (USE_MOCK) {
        if (alive) {
          setConfig(defaultRiskConfig);
          setLoading(false);
        }
        return;
      }
      try {
        const wire = await getJSON<WireRiskConfig>(ENDPOINTS.riskConfig);
        if (alive) {
          setConfig(adaptRiskConfig(wire));
          setError(null);
        }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : "failed to load risk config");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const save = useCallback(async (next: RiskConfig) => {
    setSaving(true);
    setError(null);
    try {
      if (USE_MOCK) {
        // Simulate a round-trip; persist optimistically in memory.
        await new Promise((r) => setTimeout(r, 250));
        setConfig(next);
        return;
      }
      const saved = await postJSON<WireRiskConfig>(
        ENDPOINTS.riskConfig,
        toWireRiskConfig(next),
      );
      // Server echoes the persisted (tightened) config in wire shape; adapt it.
      setConfig(saved ? adaptRiskConfig(saved) : next);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to save risk config");
      throw e;
    } finally {
      setSaving(false);
    }
  }, []);

  return { config, loading, error, saving, save };
}

/* -------------------------------------------------------------------------- */
/*  useModuleHealth                                                           */
/* -------------------------------------------------------------------------- */

export function useModuleHealth(): Resource<ModuleHealth[]> {
  const [data, setData] = useState<ModuleHealth[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      setData(makeModuleHealth());
      setLoading(false);
      return;
    }
    try {
      const wire = await getJSON<WireModuleHealth[]>(ENDPOINTS.health);
      setData(adaptHealth(wire));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load health");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, TICK_MS * 2);
    return () => clearInterval(id);
  }, [load]);

  return { data, loading, error, refetch: load };
}

/* -------------------------------------------------------------------------- */
/*  useSmartWallets / useRestingOrders / useAutoStratPresets (T-353)           */
/*                                                                            */
/*  These surface NEW competitive features. They are READ-ONLY views/config:  */
/*  the copy-trade page is a selectivity FILTER VIEW (no buy/mirror control),  */
/*  resting orders are de-risk-direction-only, and presets configure EXIT      */
/*  behavior. In live mode the hooks probe an OPTIONAL read-only projection    */
/*  and fall back to the deterministic offline fake if the control plane does  */
/*  not expose it (the frozen contract §12 does not yet — that is a future     */
/*  architect-owned delta). No POST and no risk-increasing write here.         */
/* -------------------------------------------------------------------------- */

/** GET that resolves to `null` (not throw) when the optional endpoint is absent. */
async function getOptionalJSON<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(url(path), { headers: { accept: "application/json" } });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export function useSmartWallets(): Resource<SmartWallet[]> {
  const [data, setData] = useState<SmartWallet[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      setData(makeSmartWallets());
      setLoading(false);
      return;
    }
    try {
      const wire = await getOptionalJSON<WireSmartWallet[]>(
        OPTIONAL_ENDPOINTS.smartWallets,
      );
      // Endpoint absent on the frozen contract → empty (offline-safe), not error.
      setData(wire ? adaptSmartWallets(wire) : []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load smart wallets");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    if (USE_MOCK) {
      const id = setInterval(load, TICK_MS * 4);
      return () => clearInterval(id);
    }
  }, [load]);

  return { data, loading, error, refetch: load };
}

export function useRestingOrders(): Resource<RestingOrder[]> {
  const [data, setData] = useState<RestingOrder[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      setData(makeRestingOrders());
      setLoading(false);
      return;
    }
    try {
      const wire = await getOptionalJSON<WireRestingOrder[]>(
        OPTIONAL_ENDPOINTS.restingOrders,
      );
      setData(wire ? adaptRestingOrders(wire) : []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load resting orders");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { data, loading, error, refetch: load };
}

export function useAutoStratPresets(): Resource<AutoStratPreset[]> {
  const [data, setData] = useState<AutoStratPreset[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      setData(mockPresets);
      setLoading(false);
      return;
    }
    try {
      const wire = await getOptionalJSON<AutoStratPreset[]>(
        OPTIONAL_ENDPOINTS.presets,
      );
      // Presets are a static catalog; fall back to the built-in set if absent.
      setData(wire && wire.length > 0 ? wire : mockPresets);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load presets");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { data, loading, error, refetch: load };
}

/* -------------------------------------------------------------------------- */
/*  useCandidates (E3) — READ-ONLY candidate / watchlist queue.                */
/*                                                                            */
/*  The pipeline of tokens evaluated-but-not-necessarily-acted-on (monitoring  */
/*  / pending / skipped / sniped). Pure observability: a GET poll, no POST and */
/*  no command endpoint. `/api/candidates` is NOT in the frozen control-plane  */
/*  contract (§12) — surfacing it is a future architect-owned delta — so the   */
/*  live branch PROBES the optional projection and falls back to the offline   */
/*  fake if absent, exactly like smart-wallets / resting-orders. The dashboard */
/*  therefore still builds and runs GREEN on the mock with no backend.         */
/* -------------------------------------------------------------------------- */

export function useCandidates(): Resource<Candidate[]> {
  const [data, setData] = useState<Candidate[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      setData(makeCandidates());
      setLoading(false);
      return;
    }
    try {
      const wire = await getOptionalJSON<WireCandidate[]>(
        OPTIONAL_ENDPOINTS.candidates,
      );
      // Endpoint absent on the frozen contract → empty (offline-safe), not error.
      setData(wire ? adaptCandidates(wire) : []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load candidates");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    if (USE_MOCK) {
      const id = setInterval(load, TICK_MS * 2);
      return () => clearInterval(id);
    }
  }, [load]);

  return { data, loading, error, refetch: load };
}

/* -------------------------------------------------------------------------- */
/*  useWalletClusters (E11) — READ-ONLY wallet-cluster ("Bubble Maps") graphs.  */
/*                                                                            */
/*  Per-mint wallet-connection graphs exposing the EXISTING sniper-cluster /    */
/*  bundle-detection data so the operator can SEE coordinated manipulation.     */
/*  Pure observability: a GET poll, no POST and no command endpoint.            */
/*  `/api/wallet-cluster` is an E11-additive read-only endpoint, NOT part of    */
/*  the frozen control-plane contract (§12) — so the live branch PROBES the     */
/*  optional projection and falls back to an empty list if the endpoint is      */
/*  absent, exactly like candidates / smart-wallets. The dashboard therefore    */
/*  still builds and runs GREEN on the mock with no backend.                    */
/* -------------------------------------------------------------------------- */

export function useWalletClusters(): Resource<WalletClusterGraph[]> {
  const [data, setData] = useState<WalletClusterGraph[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      setData(makeWalletClusters());
      setLoading(false);
      return;
    }
    try {
      const wire = await getOptionalJSON<WireWalletClusterGraph[]>(
        OPTIONAL_ENDPOINTS.walletCluster,
      );
      // Endpoint absent on the frozen contract → empty (offline-safe), not error.
      setData(wire ? adaptWalletClusters(wire) : []);
      setError(null);
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "failed to load wallet clusters",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    if (USE_MOCK) {
      const id = setInterval(load, TICK_MS * 2);
      return () => clearInterval(id);
    }
  }, [load]);

  return { data, loading, error, refetch: load };
}

/* -------------------------------------------------------------------------- */
/*  useNarrative (E7) — READ-ONLY narrative & news signal (de-risk VIEW).      */
/*                                                                            */
/*  Per-asset news narrative: sources, freshness, the de-risk narrative score  */
/*  (mcs_delta ∈ [-1,0], never a win-rate), and coordinated-shill / breaking    */
/*  red flags. Pure observability: a GET poll, no POST and no command endpoint. */
/*  `/api/narrative` is an E7-additive read-only endpoint, NOT part of the      */
/*  frozen control-plane contract (§12) — so the live branch PROBES the         */
/*  optional projection and falls back to an empty list if absent, exactly      */
/*  like candidates / wallet-cluster. The dashboard therefore still builds and  */
/*  runs GREEN on the mock with no backend (VITE_USE_MOCK=true).                 */
/* -------------------------------------------------------------------------- */

export function useNarrative(): Resource<NarrativeSignal[]> {
  const [data, setData] = useState<NarrativeSignal[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (USE_MOCK) {
      setData(makeNarrativeSignals());
      setLoading(false);
      return;
    }
    try {
      const wire = await getOptionalJSON<WireNarrativeSignal[]>(
        OPTIONAL_ENDPOINTS.narrative,
      );
      // Endpoint absent on the frozen contract → empty (offline-safe), not error.
      setData(wire ? adaptNarrative(wire) : []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load narrative");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    if (USE_MOCK) {
      // Slow-loop narrative cadence — refreshed slower than the snipe loop.
      const id = setInterval(load, TICK_MS * 4);
      return () => clearInterval(id);
    }
  }, [load]);

  return { data, loading, error, refetch: load };
}

/* -------------------------------------------------------------------------- */
/*  Imperative actions                                                        */
/* -------------------------------------------------------------------------- */

export async function killSwitch(): Promise<void> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 150));
    return;
  }
  await postJSON(ENDPOINTS.kill);
}

export async function flattenAll(): Promise<void> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 150));
    return;
  }
  await postJSON(ENDPOINTS.flatten);
}

export async function flatten(mint: string): Promise<void> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 150));
    return;
  }
  await postJSON(`${ENDPOINTS.flatten}/${encodeURIComponent(mint)}`);
}

export async function resetBreaker(): Promise<void> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 150));
    return;
  }
  await postJSON(ENDPOINTS.breakerReset);
}

/**
 * Change agent operating mode (DE-RISK ONLY at the operator surface; the server
 * fences `LIVE` behind DRY_RUN=false + CEO auth, AC-060 — the dashboard cannot
 * enable live capital on its own).
 *
 * The `/api/mode` POST body carries the **canonical wire enum**
 * (`SHADOW|PAPER|LIVE_DRY_RUN|LIVE`, api-contracts.md §2/§9/§13 — the Lane-E
 * transcription the frozen contract assigns to T-352). The dashboard view-model
 * `AgentMode` (`paper|dry-run|live`) is translated at the single adapter seam:
 * `toWireMode` on this write path (mirroring `fromWireMode` on the /api/state
 * read path), so the body the control plane (Lane D, T-341) parses as
 * `ModeRequest(mode: AgentMode StrEnum)` is string-equal uppercase and passes
 * pydantic validation. `paper→PAPER`, `dry-run→LIVE_DRY_RUN`, `live→LIVE`.
 *
 * This translation only NAMES the target ladder rung — it cannot itself enable
 * `LIVE`; the server rejects a `LIVE` request without DRY_RUN=false + CEO auth
 * with 403 (de-risk-only operator surface).
 */
export async function setMode(m: AgentMode): Promise<void> {
  if (USE_MOCK) {
    setMockMode(m);
    await new Promise((r) => setTimeout(r, 150));
    return;
  }
  await postJSON(ENDPOINTS.mode, { mode: toWireMode(m) });
}
