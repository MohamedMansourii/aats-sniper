/**
 * T-352 — LIVE wire adapter tests.
 *
 * These pin the snake_case→camelCase mapping, the money discipline (integer
 * lamports / Decimal-string → display SOL, never float-as-money on the wire),
 * the canonical AgentMode ladder mapping, the /api/latency `"class"` + `tiers`
 * handling, and the HONESTY CLAUSE (no win-rate field reaches the view-model).
 *
 * They run in the dashboard (jsdom) vitest project alongside — but independent
 * of — the T-351 destructive-control tests, which they must not perturb.
 */
import { describe, expect, it } from "vitest";
import {
  adaptAgentState,
  adaptHealth,
  adaptInfraTiers,
  adaptLatencyBudget,
  adaptMetrics,
  adaptPosition,
  adaptPrediction,
  adaptReasoning,
  adaptRiskConfig,
  adaptSentiment,
  adaptSnipeEvent,
  decimalStringToSolDisplay,
  fromWireMode,
  lamportsToSolDisplay,
  toWireMode,
  toWireRiskConfig,
  type WireLatencyBudget,
} from "./adapters";

describe("money — integer lamports / Decimal-string → display SOL (NFR-009)", () => {
  it("lamports → SOL is an integer-base-unit division, not float-as-money", () => {
    expect(lamportsToSolDisplay(1_000_000_000)).toBe(1);
    expect(lamportsToSolDisplay(500_000_000)).toBe(0.5);
    expect(lamportsToSolDisplay(-120_000)).toBeCloseTo(-0.00012, 10);
  });

  it("malformed lamports never throw (defensive — bad frame must not crash deck)", () => {
    expect(lamportsToSolDisplay(undefined)).toBe(0);
    expect(lamportsToSolDisplay(null)).toBe(0);
    expect(lamportsToSolDisplay(Number.NaN)).toBe(0);
  });

  it("Decimal-string money parses for display only", () => {
    expect(decimalStringToSolDisplay("-0.0123")).toBeCloseTo(-0.0123, 10);
    expect(decimalStringToSolDisplay("0.0044")).toBeCloseTo(0.0044, 10);
    expect(decimalStringToSolDisplay("not-a-number")).toBe(0);
    expect(decimalStringToSolDisplay(undefined)).toBe(0);
  });
});

describe("AgentMode — canonical wire ladder ↔ view-model", () => {
  it("maps every canonical wire value to a view-model mode", () => {
    expect(fromWireMode("SHADOW")).toBe("paper");
    expect(fromWireMode("PAPER")).toBe("paper");
    expect(fromWireMode("LIVE_DRY_RUN")).toBe("dry-run");
    expect(fromWireMode("LIVE")).toBe("live");
  });

  it("unknown wire mode falls back to the safest view-model mode", () => {
    expect(fromWireMode("???")).toBe("paper");
    expect(fromWireMode(undefined)).toBe("paper");
  });

  it("view-model → canonical wire (dry-run is the contract's LIVE_DRY_RUN)", () => {
    expect(toWireMode("paper")).toBe("PAPER");
    expect(toWireMode("dry-run")).toBe("LIVE_DRY_RUN");
    expect(toWireMode("live")).toBe("LIVE");
  });
});

describe("AgentState — snake_case → camelCase, lamports → display SOL", () => {
  it("adapts the frozen /api/state shape", () => {
    const s = adaptAgentState({
      mode: "LIVE_DRY_RUN",
      loops: { snipe: "armed", fast: "running", slow: "running" },
      connection: { geyser: true, shredstream: false, internal_ms: 67 },
      wallet: { pubkey: "7Bd…aN4q", balance_lamports: 128_420_000_000, cap_lamports: 500_000_000 },
      dry_run_enabled: true,
      breaker_tripped: false,
      killed: false,
    });
    expect(s.mode).toBe("dry-run");
    expect(s.loops.snipe).toBe("armed");
    expect(s.connection.internalMs).toBe(67);
    expect(s.wallet.balanceSol).toBeCloseTo(128.42, 6);
    expect(s.wallet.capSol).toBe(0.5);
    expect(s.killed).toBe(false);
  });

  it("clamps an unknown snipe-loop value to idle", () => {
    const s = adaptAgentState({ loops: { snipe: "weird" } });
    expect(s.loops.snipe).toBe("idle");
  });
});

describe("SnipeEvent — §6 snake_case wire → camelCase view-model", () => {
  it("maps gate/model/slot fields and filters invalid gate reasons", () => {
    const e = adaptSnipeEvent({
      id: "evt-1",
      ts: 1_718_500_000_000,
      slot: 12345,
      token: "PNUT",
      source: "pump.fun",
      gate_passed: true,
      gate_reasons: ["passed", "garbage_reason"],
      model_p: 0.31,
      action: "sniped",
      slot_delay: 7,
      smart_wallets: 3,
      pnl_pct: null,
    });
    expect(e.gatePassed).toBe(true);
    expect(e.gateReasons).toEqual(["passed"]);
    expect(e.modelP).toBe(0.31);
    expect(e.action).toBe("sniped");
    expect(e.slotDelay).toBe(7);
    expect(e.smartWallets).toBe(3);
    expect(e.pnlPct).toBeNull();
  });

  it("unknown source/action degrade to safe defaults", () => {
    const e = adaptSnipeEvent({ source: "??", action: "??" });
    expect(e.source).toBe("pump.fun");
    expect(e.action).toBe("skipped");
  });
});

describe("Latency — /api/latency `class` discriminator + tiers (T-199b)", () => {
  const wire: WireLatencyBudget = {
    hops: [
      { name: "ingress_detect", ms: 60, budget_ms: 70, class: "internal_compute" },
      { name: "block_engine_rtt", ms: 55, budget_ms: 60, class: "submission" },
      {
        name: "leader_land",
        ms: 50,
        budget_ms: 400,
        class: "submission",
        p99_ms: 450,
        note: "+1 slot under contention",
      },
    ],
    internal_ms: 67,
    block_engine_rtt_ms: 55,
    slot_floor_ms: 400,
    posture: "DETECTION-COMPETITIVE, SUBMISSION-DISADVANTAGED",
    tiers: [
      { name: "dedicated_geyser", land_rate: 0.38, median_slot_delay: 7, entry_slip_pct: 1.45, active: true },
      { name: "colo_shred", land_rate: 0.0, median_slot_delay: 0, entry_slip_pct: 0.0, active: false },
    ],
  };

  it("maps hops (budget_ms → budgetMs) and slot floor without choking on `class`", () => {
    const b = adaptLatencyBudget(wire);
    expect(b.hops).toHaveLength(3);
    expect(b.hops[0]).toEqual({ name: "ingress_detect", ms: 60, budgetMs: 70 });
    expect(b.internalMs).toBe(67);
    expect(b.slotFloorMs).toBe(400);
  });

  it("reads tiers from the latency report; land_rate fraction → percent", () => {
    const tiers = adaptInfraTiers(wire);
    expect(tiers).toHaveLength(2);
    expect(tiers[0].name).toBe("dedicated_geyser");
    expect(tiers[0].landRate).toBeCloseTo(38, 6);
    expect(tiers[0].medianSlotDelay).toBe(7);
    expect(tiers[0].entrySlipPct).toBeCloseTo(1.45, 6);
  });
});

describe("Position — integer lamports / bps → view-model", () => {
  it("derives entrySol, currentPct and entrySlipPct from integer fields", () => {
    const p = adaptPosition({
      mint: "MintAbc",
      token: "PNUT",
      sol_in_lamports: 100_000_000, // 0.1 SOL
      unrealized_pnl_net_lamports: -10_000_000, // -0.01 SOL = -10%
      entry_slip_bps: 145,
      tp_hit: 1,
      tp_total: 3,
      trailing_armed: true,
      hard_stop_pct: -40,
      exit_mode: "secure",
      fsm_state: "OPEN",
      age_sec: 42,
      status: "open",
      realized_pnl_net_lamports: null,
    });
    expect(p.entrySol).toBeCloseTo(0.1, 9);
    expect(p.currentPct).toBeCloseTo(-10, 6);
    expect(p.entrySlipPct).toBeCloseTo(1.45, 6);
    expect(p.exitMode).toBe("secure");
    expect(p.status).toBe("open");
    expect(p.realizedPnlSol).toBeNull();
  });

  it("realized PnL lamports → display SOL when present", () => {
    const p = adaptPosition({
      sol_in_lamports: 1_000_000_000,
      unrealized_pnl_net_lamports: 0,
      status: "closed",
      realized_pnl_net_lamports: 250_000_000,
    });
    expect(p.realizedPnlSol).toBeCloseTo(0.25, 9);
    expect(p.status).toBe("closed");
  });
});

describe("Metrics — HONESTY CLAUSE (no win-rate ever)", () => {
  it("adapts the frozen metrics shape and exposes no win-rate field", () => {
    const m = adaptMetrics({
      net_pnl_sol: "-0.0123",
      land_rate_pct: 38.2,
      median_slot_delay: 7,
      rug_avoidance_pct: 61.0,
      tip_efficiency: 0.42,
      model_vs_baseline: { delta_net_pnl_per_sol_at_risk: "0.013" },
      open_positions: 2,
      daily_pnl_sol: "-0.0123",
      daily_loss_limit_sol: "-0.30",
      breaker_tripped: false,
    });
    expect(m.netPnlSol).toBeCloseTo(-0.0123, 9);
    expect(m.edgeVsBaselinePct).toBeCloseTo(1.3, 6);
    expect(m.dailyLossLimitSol).toBeCloseTo(0.3, 9);
    expect(m.breakerTripped).toBe(false);
    // The view-model type has no win-rate field, and none is injected at runtime.
    expect(Object.keys(m)).not.toContain("winRate");
    expect(Object.keys(m)).not.toContain("win_rate");
  });
});

describe("Sentiment / Prediction / Reasoning / Health — snake → camel", () => {
  it("sentiment maps red_flags/post_count", () => {
    const [s] = adaptSentiment([
      { asset: "PNUT", conviction: 0.82, momentum: 0.74, novelty: 0.61, synchronicity: 0.69, red_flags: ["x"], post_count: 1842, reasoning: "r" },
    ]);
    expect(s.redFlags).toEqual(["x"]);
    expect(s.postCount).toBe(1842);
  });

  it("prediction maps model_p/baseline_p → classifierP/baselineP", () => {
    const p = adaptPrediction({
      model_p: 0.71,
      baseline_p: 0.5,
      uncertainty: 0.12,
      calibration_bins: [{ p: 0.1, actual: 0.08 }],
      feature_importance: [{ name: "smart_wallet_inflow", weight: 0.27 }],
    });
    expect(p.classifierP).toBe(0.71);
    expect(p.baselineP).toBe(0.5);
    expect(p.calibrationBins[0]).toEqual({ p: 0.1, actual: 0.08 });
    expect(p.featureImportance[0]).toEqual({ name: "smart_wallet_inflow", weight: 0.27 });
  });

  it("reasoning maps narrative_failure and clamps unknown signal to Hold", () => {
    const r = adaptReasoning({
      id: "rsn-1",
      token: "GIGA",
      signal: "Strong Sell",
      confidence: 0.81,
      veto: true,
      narrative_failure: true,
      rationale: "bot cluster",
    });
    expect(r.signal).toBe("Strong Sell");
    expect(r.narrativeFailure).toBe(true);
    expect(adaptReasoning({ signal: "???" }).signal).toBe("Hold");
  });

  it("health maps ok→online, stale→degraded, staleness_ms→staleMs", () => {
    const h = adaptHealth([
      { name: "fast_loop", status: "ok", staleness_ms: 50, latency_ms: 3 },
      { name: "geyser_feed", status: "stale", staleness_ms: 1500 },
    ]);
    expect(h[0]).toEqual({ name: "fast_loop", status: "online", latencyMs: 3, staleMs: 50 });
    expect(h[1].status).toBe("degraded");
    expect(h[1].staleMs).toBe(1500);
  });
});

describe("RiskConfig — wire ↔ view-model; tighten-only POST stays in base units", () => {
  it("wire (lamports / Decimal-string) → view-model SOL", () => {
    const rc = adaptRiskConfig({
      per_trade_cap_lamports: 100_000_000,
      daily_loss_floor_lamports: 300_000_000,
      max_slippage_bps: 150,
      snipe_threshold: 0.62,
      veto_threshold: 0.35,
      jito_tip_cap_frac: "0.05",
    });
    expect(rc.maxPositionSol).toBeCloseTo(0.1, 9);
    expect(rc.dailyLossLimitSol).toBeCloseTo(0.3, 9);
    expect(rc.maxSlippageBps).toBe(150);
    expect(rc.snipeThreshold).toBe(0.62);
    expect(rc.jitoTipCapSol).toBeCloseTo(0.05, 9);
  });

  it("view-model → wire emits integer lamports + Decimal-string tip (no money float)", () => {
    const wire = toWireRiskConfig({
      maxPositionSol: 0.1,
      maxPositionPct: 10,
      stopLossPct: 18,
      takeProfitPct: 60,
      maxSlippageBps: 150,
      dailyLossLimitSol: 0.3,
      maxHoldMin: 240,
      snipeThreshold: 0.62,
      vetoThreshold: 0.35,
      jitoTipCapSol: 0.05,
    });
    expect(wire.per_trade_cap_lamports).toBe(100_000_000);
    expect(wire.daily_loss_floor_lamports).toBe(300_000_000);
    expect(Number.isInteger(wire.per_trade_cap_lamports)).toBe(true);
    expect(Number.isInteger(wire.daily_loss_floor_lamports)).toBe(true);
    expect(typeof wire.jito_tip_cap_frac).toBe("string");
    expect(wire.jito_tip_cap_frac).toBe("0.05");
  });
});
