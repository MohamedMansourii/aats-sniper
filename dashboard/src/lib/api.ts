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
} from "./types";
import {
  defaultRiskConfig,
  infraTiers as mockInfraTiers,
  latencyBudget as mockLatencyBudget,
  makeAgentState,
  makeMetrics,
  makeModuleHealth,
  makePositions,
  makeReasoningLog,
  makeSnipeEvent,
  prediction as mockPrediction,
  seedSnipeFeed,
  sentimentScores as mockSentiment,
  setMockMode,
} from "./mock";

/* -------------------------------------------------------------------------- */
/*  Mode + transport                                                          */
/* -------------------------------------------------------------------------- */

export const USE_MOCK =
  (import.meta.env.VITE_USE_MOCK as string | undefined) !== "false";

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

/** Poll/stream cadence for the live mock streams. */
export const TICK_MS = 1500;

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: { accept: "application/json" } });
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return (await res.json()) as T;
}

async function postJSON<T = unknown>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
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
      const s = await getJSON<AgentState>(ENDPOINTS.state);
      setData(s);
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
      setEvents(seedSnipeFeed());
      setLoading(false);
      const id = setInterval(() => {
        setEvents((cur) => [makeSnipeEvent(), ...cur].slice(0, FEED_CAP));
      }, TICK_MS);
      return () => clearInterval(id);
    }

    // Real transport: Server-Sent Events stream of SnipeEvent JSON.
    setLoading(true);
    const src = new EventSource(ENDPOINTS.feed);
    src.onopen = () => setLoading(false);
    src.onmessage = (msg) => {
      try {
        const evt = JSON.parse(msg.data) as SnipeEvent;
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
      const m = await getJSON<MetricsSnapshot>(ENDPOINTS.metrics);
      setData(m);
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
      setData(await getJSON<Position[]>(ENDPOINTS.positions));
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
      setData(await getJSON<LatencyBudget>(ENDPOINTS.latency));
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
      // Infra tiers are part of the latency report on the control plane.
      const budget = await getJSON<LatencyBudget & { tiers?: InfraTier[] }>(
        ENDPOINTS.latency,
      );
      setData(budget.tiers ?? []);
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
      setData(await getJSON<MCSScore[]>(ENDPOINTS.sentiment));
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
      setData(await getJSON<Prediction>(ENDPOINTS.predictions));
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
      setData(await getJSON<Reasoning[]>(ENDPOINTS.reasoning));
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
        const c = await getJSON<RiskConfig>(ENDPOINTS.riskConfig);
        if (alive) {
          setConfig(c);
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
      const saved = await postJSON<RiskConfig>(ENDPOINTS.riskConfig, next);
      setConfig(saved ?? next);
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
      setData(await getJSON<ModuleHealth[]>(ENDPOINTS.health));
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

export async function setMode(m: AgentMode): Promise<void> {
  if (USE_MOCK) {
    setMockMode(m);
    await new Promise((r) => setTimeout(r, 150));
    return;
  }
  await postJSON(ENDPOINTS.mode, { mode: m });
}
