/**
 * AATS Sniper — client-side CSV / JSON export (T-353 P&L export).
 *
 * Pure, dependency-free serialization + a small browser download trigger. Money
 * values are formatted from the already-display-converted SOL numbers the view
 * model holds (themselves derived from integer base units / Decimal strings on
 * the wire — never float-as-money for a decision). Export is a read-only,
 * de-risk-neutral operator convenience; it initiates nothing on the bot.
 *
 * NO win-rate column, field, or symbol appears in any export (HONESTY CLAUSE).
 */

import type { Position } from "./types";

/** Round to a fixed number of decimals for stable, diff-friendly export cells. */
function fixed(n: number, dp = 4): string {
  if (!Number.isFinite(n)) return "0";
  return (Math.round(n * 10 ** dp) / 10 ** dp).toFixed(dp);
}

/** RFC-4180-ish CSV cell escaping. */
function csvCell(value: string | number | boolean | null): string {
  const s = value === null ? "" : String(value);
  if (/[",\r\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

/**
 * The export row shape. Net-of-cost PnL is the PRIMARY column and is listed
 * first among the PnL fields; gross is explicitly suffixed and secondary.
 */
export interface PositionExportRow {
  mint: string;
  token: string;
  surface: string;
  status: Position["status"];
  fsm_state: Position["fsmState"];
  exit_mode: Position["exitMode"];
  entry_sol: string;
  // NET first (primary), gross after (secondary, clearly labeled).
  realized_pnl_net_sol: string | null;
  realized_pnl_gross_sol: string | null;
  unrealized_pnl_net_sol: string;
  total_cost_bps: number;
  entry_slippage_bps: number;
  amm_fee_bps: number;
  exit_slippage_bps: number;
  adverse_selection_bps: number;
  tp_hit: number;
  tp_total: number;
  trailing_armed: boolean;
  hard_stop_pct: number;
  age_sec: number;
}

export function toExportRows(positions: Position[]): PositionExportRow[] {
  return positions.map((p) => ({
    mint: p.mint,
    token: p.token,
    surface: p.surface,
    status: p.status,
    fsm_state: p.fsmState,
    exit_mode: p.exitMode,
    entry_sol: fixed(p.entrySol),
    realized_pnl_net_sol: p.realizedPnlSol === null ? null : fixed(p.realizedPnlSol),
    realized_pnl_gross_sol:
      p.realizedGrossPnlSol === null ? null : fixed(p.realizedGrossPnlSol),
    unrealized_pnl_net_sol: fixed(p.unrealizedPnlSol),
    total_cost_bps: p.cost.totalBps,
    entry_slippage_bps: p.cost.entrySlippageBps,
    amm_fee_bps: p.cost.ammFeeBps,
    exit_slippage_bps: p.cost.exitSlippageBps,
    adverse_selection_bps: p.cost.adverseSelectionBps,
    tp_hit: p.tpHit,
    tp_total: p.tpTotal,
    trailing_armed: p.trailingArmed,
    hard_stop_pct: p.hardStopPct,
    age_sec: p.ageSec,
  }));
}

/** Serialize export rows to a CSV string (header from the first row's keys). */
export function positionsToCSV(positions: Position[]): string {
  const rows = toExportRows(positions);
  const headers: (keyof PositionExportRow)[] = [
    "mint",
    "token",
    "surface",
    "status",
    "fsm_state",
    "exit_mode",
    "entry_sol",
    "realized_pnl_net_sol",
    "realized_pnl_gross_sol",
    "unrealized_pnl_net_sol",
    "total_cost_bps",
    "entry_slippage_bps",
    "amm_fee_bps",
    "exit_slippage_bps",
    "adverse_selection_bps",
    "tp_hit",
    "tp_total",
    "trailing_armed",
    "hard_stop_pct",
    "age_sec",
  ];
  const lines = [headers.join(",")];
  for (const row of rows) {
    lines.push(headers.map((h) => csvCell(row[h])).join(","));
  }
  return lines.join("\r\n");
}

/** Serialize export rows to a stable, pretty JSON string. */
export function positionsToJSON(positions: Position[]): string {
  return JSON.stringify(
    {
      exported_at_ms: Date.now(),
      // Honest framing baked into the payload metadata.
      pnl_basis: "net_of_cost_primary_gross_secondary",
      count: positions.length,
      positions: toExportRows(positions),
    },
    null,
    2,
  );
}

/**
 * Trigger a browser download of `content` as a file. No-op outside the browser
 * (so it is safe to import in a non-DOM test environment without side effects).
 */
export function downloadFile(
  filename: string,
  content: string,
  mime: string,
): void {
  if (typeof document === "undefined" || typeof URL?.createObjectURL !== "function") {
    return;
  }
  const blob = new Blob([content], { type: mime });
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(href);
}

/** Build a timestamped export filename, e.g. `aats-positions-20260616-1244.csv`. */
export function exportFilename(ext: "csv" | "json", now = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}`;
  return `aats-positions-${stamp}.${ext}`;
}
