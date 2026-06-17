/**
 * T-351 — Contract-level tests for the DESTRUCTIVE control-plane actions.
 *
 * These assert the typed client hits the EXACT frozen control-plane endpoints
 * (api-contracts.md, T-201) with the right method/body. The dashboard, the
 * control-plane server (D), and Telegram (F) all build to this one contract, so
 * a wrong path here is a real integration break, not a cosmetic nit.
 *
 * USE_MOCK is read once at module load from import.meta.env, so each block that
 * needs a specific mode stubs the env and dynamic-imports a fresh module copy.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** Load a fresh copy of the api module under a given mock-mode env. */
async function loadApi(useMock: boolean) {
  vi.resetModules();
  vi.stubEnv("VITE_USE_MOCK", useMock ? "true" : "false");
  return import("./api");
}

describe("control-plane endpoint map (frozen contract — T-201)", () => {
  it("pins every destructive endpoint to its frozen path", async () => {
    const { ENDPOINTS } = await loadApi(false);
    // These four paths are non-negotiable: server + dashboard + Telegram share them.
    expect(ENDPOINTS.kill).toBe("/api/kill");
    expect(ENDPOINTS.flatten).toBe("/api/flatten");
    expect(ENDPOINTS.breakerReset).toBe("/api/breaker/reset");
    expect(ENDPOINTS.mode).toBe("/api/mode");
  });
});

describe("destructive actions hit the correct endpoint (live transport)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("killSwitch → POST /api/kill", async () => {
    const api = await loadApi(false);
    await api.killSwitch();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/kill");
    expect(init.method).toBe("POST");
  });

  it("flattenAll → POST /api/flatten", async () => {
    const api = await loadApi(false);
    await api.flattenAll();

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/flatten");
    expect(init.method).toBe("POST");
  });

  it("flatten(mint) → POST /api/flatten/{mint} with the mint URL-encoded", async () => {
    const api = await loadApi(false);
    await api.flatten("So111+11/weird");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`/api/flatten/${encodeURIComponent("So111+11/weird")}`);
    expect(init.method).toBe("POST");
  });

  it("resetBreaker → POST /api/breaker/reset", async () => {
    const api = await loadApi(false);
    await api.resetBreaker();

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/breaker/reset");
    expect(init.method).toBe("POST");
  });

  it("setMode → POST /api/mode with the CANONICAL wire mode in the JSON body", async () => {
    // Contract delta (api-contracts.md §2/§9/§13, Lane-E T-352 transcription):
    // the /api/mode POST body carries the canonical 4-value wire enum
    // (SHADOW|PAPER|LIVE_DRY_RUN|LIVE), not the lowercase view-model value. The
    // server (Lane D, T-341) parses ModeRequest(mode: AgentMode StrEnum) and 400s
    // a lowercase body. The view-model `paper` maps to canonical `PAPER`.
    const api = await loadApi(false);
    await api.setMode("paper");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/mode");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ mode: "PAPER" });
  });

  it("setMode('dry-run') → canonical LIVE_DRY_RUN body (de-risk dry-run rung)", async () => {
    const api = await loadApi(false);
    await api.setMode("dry-run");

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init.body as string)).toEqual({ mode: "LIVE_DRY_RUN" });
  });

  it("propagates a non-OK response as an error (kill must not silently succeed)", async () => {
    fetchMock.mockResolvedValueOnce(new Response("nope", { status: 503 }));
    const api = await loadApi(false);
    await expect(api.killSwitch()).rejects.toThrow(/503/);
  });
});

describe("mock mode makes NO network call (offline-safe default)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("USE_MOCK is the default (env unset/true) and the destructive calls touch no network", async () => {
    const api = await loadApi(true);
    expect(api.USE_MOCK).toBe(true);

    await api.killSwitch();
    await api.flattenAll();
    await api.flatten("MintAbc");
    await api.resetBreaker();
    await api.setMode("paper");

    expect(fetchMock).not.toHaveBeenCalled();
  });
});
