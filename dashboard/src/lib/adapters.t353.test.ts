/**
 * T-353 — wire adapters for the NEW feature surfaces.
 *
 * Pins: SnipeEvent carries token-safety `red_flags`; Position carries the cost
 * breakdown + net/gross PnL (money from integer base units, never float-as-
 * money); smart-money tracked PnL is a Decimal-string (selectivity VIEW); a
 * resting order's fraction is bounded to a reduction (de-risk-direction only).
 */
import { describe, expect, it } from "vitest";
import {
  adaptCostBreakdown,
  adaptPosition,
  adaptRestingOrder,
  adaptSmartWallet,
  adaptSnipeEvent,
} from "./adapters";

describe("SnipeEvent — token-safety red flags (FR-037, AC-015)", () => {
  it("surfaces red_flags verbatim, including unknown scanner codes", () => {
    const e = adaptSnipeEvent({
      action: "skipped",
      gate_passed: false,
      red_flags: ["honeypot", "not_sellable", "some_new_fingerprint"],
    });
    expect(e.redFlags).toEqual(["honeypot", "not_sellable", "some_new_fingerprint"]);
  });

  it("defaults to an empty red-flag list when absent", () => {
    const e = adaptSnipeEvent({ action: "sniped", gate_passed: true });
    expect(e.redFlags).toEqual([]);
  });
});

describe("Position — cost breakdown + net/gross PnL (money discipline)", () => {
  it("builds the cost breakdown from integer base units / bps", () => {
    const c = adaptCostBreakdown({
      tip_lamports: 10_000_000, // 0.01 SOL
      priority_lamports: 2_000_000, // 0.002 SOL
      entry_slippage_bps: 80,
      amm_fee_bps: 50,
      exit_slippage_bps: 100,
      adverse_selection_bps: 150,
      total_cost_bps: 380,
    });
    expect(c.tipSol).toBeCloseTo(0.01, 9);
    expect(c.prioritySol).toBeCloseTo(0.002, 9);
    expect(c.totalBps).toBe(380);
    expect(c.adverseSelectionBps).toBe(150);
  });

  it("maps net and gross realized PnL from integer lamports", () => {
    const p = adaptPosition({
      sol_in_lamports: 4_000_000_000,
      unrealized_pnl_net_lamports: 0,
      status: "closed",
      realized_pnl_net_lamports: 1_200_000_000, // 1.2 SOL net
      realized_pnl_gross_lamports: 1_650_000_000, // 1.65 SOL gross
      cost_breakdown: { total_cost_bps: 380 },
      surface: "EH-003",
    });
    expect(p.realizedPnlSol).toBeCloseTo(1.2, 9);
    expect(p.realizedGrossPnlSol).toBeCloseTo(1.65, 9);
    expect(p.surface).toBe("EH-003");
    expect(p.cost.totalBps).toBe(380);
    // Gross >= net (cost drag) — sanity on the wire mapping.
    expect(p.realizedGrossPnlSol!).toBeGreaterThanOrEqual(p.realizedPnlSol!);
  });

  it("clamps an unknown fsm_state to a safe default", () => {
    const p = adaptPosition({ fsm_state: "WEIRD", status: "open" });
    expect(p.fsmState).toBe("OPEN");
  });
});

describe("SmartWallet — selectivity VIEW (Decimal-string money, honest lag)", () => {
  it("maps tracked net PnL from a Decimal-string and keeps entry lag", () => {
    const w = adaptSmartWallet({
      address: "Wxyz…12",
      label: "early-lp sniper",
      recent_entries: 7,
      entry_lag_slots: 3,
      tracked_net_pnl_sol: "41.2",
      passes_filter: true,
      filter_reasons: ["net-of-cost positive"],
      last_seen_ms: 1_718_500_000_000,
    });
    expect(w.trackedNetPnlSol).toBeCloseTo(41.2, 6);
    expect(w.entryLagSlots).toBe(3); // positive lag = we are BEHIND them
    expect(w.passesFilter).toBe(true);
    expect(w.filterReasons).toEqual(["net-of-cost positive"]);
  });

  it("tolerates a null entry lag (no follow observed)", () => {
    const w = adaptSmartWallet({ entry_lag_slots: null });
    expect(w.entryLagSlots).toBeNull();
  });
});

describe("RestingOrder — de-risk direction only", () => {
  it("bounds fraction_bps to a reduction (0..10000) and defaults safely", () => {
    const over = adaptRestingOrder({ fraction_bps: 50_000, kind: "limit_exit", status: "armed" });
    expect(over.fractionBps).toBe(10_000); // capped at full-position reduction
    const neg = adaptRestingOrder({ fraction_bps: -5 });
    expect(neg.fractionBps).toBe(0);
  });

  it("clamps unknown kind/status to safe defaults", () => {
    const o = adaptRestingOrder({ kind: "buy_more", status: "??" });
    expect(o.kind).toBe("limit_exit"); // never an entry/increase variant
    expect(o.status).toBe("armed");
  });
});
