/**
 * T-353 — Positions CSV / JSON export.
 *
 * Locks: net-of-cost PnL is the PRIMARY column (listed before gross), gross is
 * present but secondary, money is formatted from the view-model's already-
 * display-converted SOL (never re-derived as float-as-money), and NO win-rate
 * column / field / symbol appears anywhere in either format (HONESTY CLAUSE).
 */
import { describe, expect, it } from "vitest";
import {
  exportFilename,
  positionsToCSV,
  positionsToJSON,
  toExportRows,
} from "./export";
import type { Position } from "./types";

function pos(overrides: Partial<Position> = {}): Position {
  return {
    mint: "MintAbc",
    token: "PNUT",
    entrySol: 4,
    currentPct: 12,
    entrySlipPct: 1.45,
    tpHit: 1,
    tpTotal: 3,
    trailingArmed: true,
    hardStopPct: -18,
    exitMode: "secure",
    ageSec: 120,
    status: "closed",
    realizedPnlSol: 1.2, // NET
    realizedGrossPnlSol: 1.65, // GROSS (larger — cost drag)
    unrealizedPnlSol: 0,
    fsmState: "CLOSED",
    surface: "EH-001",
    cost: {
      totalBps: 380,
      tipSol: 0.01,
      prioritySol: 0.002,
      entrySlippageBps: 80,
      ammFeeBps: 50,
      exitSlippageBps: 100,
      adverseSelectionBps: 150,
    },
    ...overrides,
  };
}

describe("export rows — net-of-cost primary, gross secondary", () => {
  it("net column precedes gross in the export shape", () => {
    const [row] = toExportRows([pos()]);
    const keys = Object.keys(row);
    expect(keys.indexOf("realized_pnl_net_sol")).toBeGreaterThanOrEqual(0);
    expect(keys.indexOf("realized_pnl_net_sol")).toBeLessThan(
      keys.indexOf("realized_pnl_gross_sol"),
    );
  });

  it("carries net and gross distinctly", () => {
    const [row] = toExportRows([pos()]);
    expect(row.realized_pnl_net_sol).toBe("1.2000");
    expect(row.realized_pnl_gross_sol).toBe("1.6500");
  });

  it("open positions export null realized and a net unrealized figure", () => {
    const [row] = toExportRows([
      pos({ status: "open", realizedPnlSol: null, realizedGrossPnlSol: null, unrealizedPnlSol: -0.3 }),
    ]);
    expect(row.realized_pnl_net_sol).toBeNull();
    expect(row.unrealized_pnl_net_sol).toBe("-0.3000");
  });
});

describe("CSV serialization", () => {
  it("header lists net before gross and includes cost columns", () => {
    const csv = positionsToCSV([pos()]);
    const header = csv.split("\r\n")[0];
    expect(header).toContain("realized_pnl_net_sol");
    expect(header).toContain("realized_pnl_gross_sol");
    expect(header.indexOf("realized_pnl_net_sol")).toBeLessThan(
      header.indexOf("realized_pnl_gross_sol"),
    );
    expect(header).toContain("total_cost_bps");
    expect(header).toContain("adverse_selection_bps");
  });

  it("escapes cells with commas/quotes safely", () => {
    const csv = positionsToCSV([pos({ token: 'PN,"UT' })]);
    expect(csv).toContain('"PN,""UT"');
  });

  it("never contains a win-rate column", () => {
    const csv = positionsToCSV([pos()]).toLowerCase();
    expect(csv).not.toContain("win_rate");
    expect(csv).not.toContain("winrate");
    expect(csv).not.toContain("win rate");
  });
});

describe("JSON serialization", () => {
  it("declares the net-primary basis and carries no win-rate key", () => {
    const json = positionsToJSON([pos()]);
    const parsed = JSON.parse(json) as {
      pnl_basis: string;
      positions: Record<string, unknown>[];
    };
    expect(parsed.pnl_basis).toBe("net_of_cost_primary_gross_secondary");
    const flat = json.toLowerCase();
    expect(flat).not.toContain("win_rate");
    expect(flat).not.toContain("winrate");
    // The net field is present on each row.
    expect(parsed.positions[0]).toHaveProperty("realized_pnl_net_sol");
  });
});

describe("filename", () => {
  it("produces a timestamped, extension-correct name", () => {
    const name = exportFilename("csv", new Date(2026, 5, 16, 9, 4));
    expect(name).toBe("aats-positions-20260616-0904.csv");
  });
});
