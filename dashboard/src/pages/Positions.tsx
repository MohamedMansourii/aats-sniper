import { useMemo, useState, type ReactNode } from "react";
import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Activity,
  AlertTriangle,
  Archive,
  Coins,
  Crosshair,
  Download,
  FileJson,
  Layers,
  Loader2,
  Receipt,
  ShieldCheck,
  Wallet,
  Zap,
} from "lucide-react";
import {
  Chip,
  Gauge,
  LiveDot,
  Panel,
  StatTile,
  type StatTone,
} from "@/components/kit";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { flatten, usePositions } from "@/lib/api";
import type { Position } from "@/lib/types";
import { round1, round2, roundInt } from "@/lib/mock";
import {
  downloadFile,
  exportFilename,
  positionsToCSV,
  positionsToJSON,
} from "@/lib/export";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { CHART as HEX } from "@/lib/chart-colors";

/* ------------------------------- formatting ------------------------------- */

const fmtSol = (n: number) => round2(n).toFixed(2);
const fmtPctSigned = (n: number) =>
  `${n >= 0 ? "+" : ""}${round1(n).toFixed(1)}%`;

function fmtAge(sec: number): string {
  const s = roundInt(sec);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

const pctTone = (n: number): StatTone => (n >= 0 ? "up" : "down");
const pctClass = (n: number) => (n >= 0 ? "text-up" : "text-down");

/* ------------------------------- TP ladder -------------------------------- */

function TpLadder({ hit, total }: { hit: number; total: number }) {
  const pips = Array.from({ length: Math.max(total, 0) }, (_, i) => i < hit);
  return (
    <div className="flex items-center gap-1.5">
      <div className="flex items-center gap-0.5">
        {pips.map((on, i) => (
          <span
            key={i}
            aria-hidden
            className={cn(
              "h-1.5 w-3.5 rounded-[2px]",
              on ? "bg-up" : "bg-panel2 ring-1 ring-inset ring-border"
            )}
          />
        ))}
      </div>
      <span className="num text-[11px] text-muted-foreground">
        {hit}/{total}
      </span>
    </div>
  );
}

/* ----------------------------- exit-mode chip ----------------------------- */

function ExitModeChip({ mode }: { mode: Position["exitMode"] }) {
  return mode === "secure" ? (
    <Chip tone="info" icon={<ShieldCheck />}>
      Secure
    </Chip>
  ) : (
    <Chip tone="warn" icon={<Zap />}>
      Fast
    </Chip>
  );
}

/* -------------------------- per-position flatten -------------------------- */

function FlattenCell({ mint, token }: { mint: string; token: string }) {
  const [busy, setBusy] = useState(false);
  async function onFlatten() {
    setBusy(true);
    try {
      await flatten(mint);
      toast.warning(`Flatten ${token} sent`, {
        description: "Market-exiting the position.",
      });
    } catch (e) {
      toast.error(`Flatten ${token} failed`, {
        description: e instanceof Error ? e.message : "Unknown error",
      });
    } finally {
      setBusy(false);
    }
  }
  return (
    <button
      type="button"
      onClick={onFlatten}
      disabled={busy}
      aria-label={`Flatten ${token}`}
      className="inline-flex items-center gap-1 rounded-md border border-[color:color-mix(in_srgb,var(--danger)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--danger)_10%,transparent)] px-2 py-1 text-[11px] font-medium text-danger transition-colors hover:bg-[color:color-mix(in_srgb,var(--danger)_18%,transparent)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger/40 disabled:cursor-not-allowed disabled:opacity-60"
    >
      {busy ? (
        <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" />
      ) : (
        <Crosshair className="h-3 w-3" />
      )}
      Flatten
    </button>
  );
}

/* ------------------------------ shared cells ------------------------------ */

const TH =
  "h-8 px-2.5 text-left align-middle text-[10px] font-semibold uppercase tracking-wide text-faint";
const TH_R = cn(TH, "text-right");
const ROW = "border-b border-border transition-colors hover:bg-hover";
const TD = "px-2.5 py-2 align-middle text-[12px]";

function TokenCell({ token, mint }: { token: string; mint: string }) {
  return (
    <div className="flex min-w-0 flex-col leading-tight">
      <span className="font-semibold tracking-tight text-text">{token}</span>
      <span className="num truncate text-[10px] text-faint">{mint}</span>
    </div>
  );
}

/* --------------------------- placeholder states --------------------------- */

function TableSkeleton({ rows, cols }: { rows: number; cols: number }) {
  return (
    <div
      className="space-y-px p-3.5"
      aria-busy="true"
      aria-label="Loading positions"
    >
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex items-center gap-3 py-1.5">
          {Array.from({ length: cols }).map((__, c) => (
            <div
              key={c}
              className="h-3 flex-1 animate-pulse rounded bg-panel2 motion-reduce:animate-none"
              style={{ maxWidth: c === 0 ? 120 : 80 }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

function EmptyState({ icon, label }: { icon: ReactNode; label: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-10 text-center">
      <span className="flex h-9 w-9 items-center justify-center rounded-full border border-border bg-panel2 text-faint [&_svg]:h-4 [&_svg]:w-4">
        {icon}
      </span>
      <span className="text-[12px] text-muted-foreground">{label}</span>
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <div className="flex items-center justify-center gap-2 px-4 py-10 text-center text-[12px] text-danger">
      <AlertTriangle className="h-4 w-4 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

/* ================================== page ================================== */

export default function Positions() {
  const { data, loading, error } = usePositions();

  const open = useMemo<Position[]>(
    () => (data ?? []).filter(p => p.status === "open"),
    [data]
  );
  const closed = useMemo<Position[]>(
    () => (data ?? []).filter(p => p.status === "closed"),
    [data]
  );

  /* ------------------------------ aggregates ------------------------------ */
  const openExposureSol = round2(open.reduce((s, p) => s + p.entrySol, 0));
  // Unrealized PnL is the NET-of-cost figure carried on each position (PRIMARY).
  const unrealizedSol = round2(
    open.reduce((s, p) => s + p.unrealizedPnlSol, 0)
  );
  const unrealizedPctOnExposure =
    openExposureSol > 0 ? round1((unrealizedSol / openExposureSol) * 100) : 0;

  // Realized PnL — NET (primary) and GROSS (secondary), strictly separated.
  const realizedNetSol = round2(
    closed.reduce((s, p) => s + (p.realizedPnlSol ?? 0), 0)
  );
  const realizedGrossSol = round2(
    closed.reduce((s, p) => s + (p.realizedGrossPnlSol ?? 0), 0)
  );
  // Cost drag = how much cost separated gross from net across closed trades.
  const realizedCostSol = round2(realizedGrossSol - realizedNetSol);
  // Keep the old name pointing at NET so existing tiles stay net-of-cost.
  const realizedSol = realizedNetSol;
  const closedCount = closed.length;

  /* ------------------------------- export -------------------------------- */
  const allPositions = data ?? [];
  function onExportCSV() {
    if (allPositions.length === 0) {
      toast.message("Nothing to export", { description: "No positions yet." });
      return;
    }
    downloadFile(
      exportFilename("csv"),
      positionsToCSV(allPositions),
      "text/csv;charset=utf-8",
    );
    toast.success("Positions exported (CSV)", {
      description: `${allPositions.length} rows · net-of-cost PnL primary.`,
    });
  }
  function onExportJSON() {
    if (allPositions.length === 0) {
      toast.message("Nothing to export", { description: "No positions yet." });
      return;
    }
    downloadFile(
      exportFilename("json"),
      positionsToJSON(allPositions),
      "application/json;charset=utf-8",
    );
    toast.success("Positions exported (JSON)", {
      description: `${allPositions.length} rows · net-of-cost PnL primary.`,
    });
  }

  // Wallet cap is fixed at 200 SOL in the mock agent state; exposure gauge ref.
  const EXPOSURE_CAP = 200;

  const exposureBars = useMemo(
    () =>
      open.map(p => ({
        token: p.token,
        sol: round2(p.entrySol),
        up: p.currentPct >= 0,
      })),
    [open]
  );

  const isLoading = loading && data === null;
  const hasError = !!error && data === null;

  return (
    <div className="space-y-4">
      {/* Page header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <Wallet className="h-4 w-4 text-brand" />
          <h1 className="text-[15px] font-semibold tracking-tight text-text">
            Positions
          </h1>
          <span className="hidden h-4 w-px bg-border sm:block" />
          <div className="hidden items-center gap-1.5 text-[11px] text-muted-foreground sm:flex">
            <LiveDot tone="accent" />
            <span className="num">{open.length}</span>
            <span>open</span>
            <span className="text-faint">·</span>
            <span className="num">{closedCount}</span>
            <span>closed</span>
          </div>
        </div>

        {/* Export controls (read-only operator convenience) */}
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={onExportCSV}
            disabled={allPositions.length === 0}
            aria-label="Export positions as CSV"
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-panel2 px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-hover hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Download className="h-3.5 w-3.5" />
            CSV
          </button>
          <button
            type="button"
            onClick={onExportJSON}
            disabled={allPositions.length === 0}
            aria-label="Export positions as JSON"
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-panel2 px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-hover hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <FileJson className="h-3.5 w-3.5" />
            JSON
          </button>
        </div>
      </div>

      {/* P&L cards — NET-of-cost PRIMARY, gross secondary, cost drag explicit */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        {/* Realized net (primary headline) */}
        <Panel title="Realized P&L (net of cost)" icon={<Coins />}>
          <div className="space-y-3">
            <div className="flex items-end justify-between gap-3">
              <div>
                <div className="text-[10px] uppercase tracking-wide text-faint">
                  Net realized · primary
                </div>
                <div
                  className={cn(
                    "num mt-1 text-[28px] font-semibold leading-none",
                    pctClass(realizedNetSol)
                  )}
                >
                  {realizedNetSol >= 0 ? "+" : ""}
                  {fmtSol(realizedNetSol)}
                  <span className="ml-1 text-[12px] font-normal text-faint">
                    SOL
                  </span>
                </div>
              </div>
              <Chip tone={realizedNetSol >= 0 ? "ok" : "danger"}>
                {closedCount} closed
              </Chip>
            </div>
            <div className="flex items-baseline justify-between border-t border-border pt-2 text-[11px]">
              <span className="text-faint">Gross (secondary)</span>
              <span className={cn("num", pctClass(realizedGrossSol))}>
                {realizedGrossSol >= 0 ? "+" : ""}
                {fmtSol(realizedGrossSol)} SOL
              </span>
            </div>
          </div>
        </Panel>

        {/* Cost drag */}
        <Panel title="Cost drag" icon={<Receipt />}>
          <div className="space-y-3">
            <div>
              <div className="text-[10px] uppercase tracking-wide text-faint">
                Net minus gross · realized
              </div>
              <div className="num mt-1 text-[28px] font-semibold leading-none text-down">
                −{fmtSol(Math.abs(realizedCostSol))}
                <span className="ml-1 text-[12px] font-normal text-faint">
                  SOL
                </span>
              </div>
            </div>
            <p className="text-[11px] leading-snug text-muted-foreground">
              Tips, priority fees, slippage, AMM fees and the uncalibrated
              adverse-selection floor. The headline above is{" "}
              <span className="text-text">already net</span> of this.
            </p>
          </div>
        </Panel>

        {/* Unrealized net (open) */}
        <Panel title="Unrealized P&L (net, open)" icon={<Activity />}>
          <div className="space-y-3">
            <div className="flex items-end justify-between gap-3">
              <div>
                <div className="text-[10px] uppercase tracking-wide text-faint">
                  Open · mark-to-market net
                </div>
                <div
                  className={cn(
                    "num mt-1 text-[28px] font-semibold leading-none",
                    pctClass(unrealizedSol)
                  )}
                >
                  {unrealizedSol >= 0 ? "+" : ""}
                  {fmtSol(unrealizedSol)}
                  <span className="ml-1 text-[12px] font-normal text-faint">
                    SOL
                  </span>
                </div>
              </div>
              <Chip tone="accent">{open.length} open</Chip>
            </div>
            <div className="flex items-baseline justify-between border-t border-border pt-2 text-[11px]">
              <span className="text-faint">On exposure</span>
              <span className={cn("num", pctClass(unrealizedSol))}>
                {fmtPctSigned(unrealizedPctOnExposure)}
              </span>
            </div>
          </div>
        </Panel>
      </div>

      {/* Summary KPI tiles */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          label="Open positions"
          value={<span className="num">{open.length}</span>}
          sub={`Exposure ${fmtSol(openExposureSol)} SOL`}
          tone="neutral"
        />
        <StatTile
          label="Open exposure"
          value={<span className="num">{fmtSol(openExposureSol)}</span>}
          sub={`${roundInt((openExposureSol / EXPOSURE_CAP) * 100)}% of ${EXPOSURE_CAP} SOL cap`}
          tone="accent"
        />
        <StatTile
          label="Unrealized PnL"
          value={
            <span className="num">
              {unrealizedSol >= 0 ? "+" : ""}
              {fmtSol(unrealizedSol)}
            </span>
          }
          sub={`${fmtPctSigned(unrealizedPctOnExposure)} on exposure`}
          delta={unrealizedPctOnExposure}
          tone={pctTone(unrealizedSol)}
        />
        <StatTile
          label="Realized PnL (closed)"
          value={
            <span className="num">
              {realizedSol >= 0 ? "+" : ""}
              {fmtSol(realizedSol)}
            </span>
          }
          sub={`${closedCount} closed · net SOL`}
          tone={pctTone(realizedSol)}
        />
      </div>

      {/* Open positions */}
      <Panel
        title="Open positions"
        icon={<Activity />}
        actions={
          <span className="num text-[11px] text-faint">{open.length} live</span>
        }
      >
        {isLoading ? (
          <TableSkeleton rows={3} cols={6} />
        ) : hasError ? (
          <ErrorState message={error ?? "Failed to load positions"} />
        ) : open.length === 0 ? (
          <EmptyState
            icon={<Crosshair />}
            label="No open positions — the sniper is flat."
          />
        ) : (
          <Table className="text-[12px]">
            <TableHeader className="sticky top-0 z-10 bg-panel">
              <TableRow className="border-b border-border hover:bg-transparent">
                <TableHead className={TH}>Token</TableHead>
                <TableHead className={TH_R}>Entry</TableHead>
                <TableHead className={TH_R}>Current</TableHead>
                <TableHead className={TH_R}>Entry slip</TableHead>
                <TableHead className={TH}>TP ladder</TableHead>
                <TableHead className={TH}>Trailing</TableHead>
                <TableHead className={TH_R}>Hard stop</TableHead>
                <TableHead className={TH}>Exit mode</TableHead>
                <TableHead className={TH_R}>Age</TableHead>
                <TableHead className={TH_R}>Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {open.map(p => (
                <TableRow key={p.mint} className={ROW}>
                  <TableCell className={TD}>
                    <TokenCell token={p.token} mint={p.mint} />
                  </TableCell>
                  <TableCell className={cn(TD, "num text-right text-text")}>
                    {fmtSol(p.entrySol)}
                  </TableCell>
                  <TableCell
                    className={cn(
                      TD,
                      "num text-right font-semibold transition-colors",
                      pctClass(p.currentPct)
                    )}
                  >
                    {fmtPctSigned(p.currentPct)}
                  </TableCell>
                  <TableCell
                    className={cn(TD, "num text-right text-muted-foreground")}
                  >
                    {round1(p.entrySlipPct).toFixed(1)}%
                  </TableCell>
                  <TableCell className={TD}>
                    <TpLadder hit={p.tpHit} total={p.tpTotal} />
                  </TableCell>
                  <TableCell className={TD}>
                    {p.trailingArmed ? (
                      <Chip tone="accent">Armed</Chip>
                    ) : (
                      <Chip tone="neutral">Idle</Chip>
                    )}
                  </TableCell>
                  <TableCell className={cn(TD, "num text-right text-down")}>
                    {round1(p.hardStopPct).toFixed(0)}%
                  </TableCell>
                  <TableCell className={TD}>
                    <ExitModeChip mode={p.exitMode} />
                  </TableCell>
                  <TableCell
                    className={cn(TD, "num text-right text-muted-foreground")}
                  >
                    {fmtAge(p.ageSec)}
                  </TableCell>
                  <TableCell className={cn(TD, "text-right")}>
                    <div className="flex justify-end">
                      <FlattenCell mint={p.mint} token={p.token} />
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>

      {/* Aggregate exposure + closed positions */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        {/* Aggregate exposure */}
        <Panel
          title="Aggregate exposure"
          icon={<Layers />}
          className="xl:col-span-1"
        >
          <div className="space-y-4">
            <Gauge
              value={openExposureSol}
              max={EXPOSURE_CAP}
              tone="accent"
              label="Open exposure vs wallet cap (SOL)"
            />
            <div className="grid grid-cols-2 gap-2 text-[11px]">
              <div className="rounded-md border border-border bg-panel2 px-2.5 py-2">
                <div className="text-faint">Open size</div>
                <div className="num mt-0.5 text-[14px] font-semibold text-text">
                  {fmtSol(openExposureSol)}
                </div>
              </div>
              <div className="rounded-md border border-border bg-panel2 px-2.5 py-2">
                <div className="text-faint">Unrealized</div>
                <div
                  className={cn(
                    "num mt-0.5 text-[14px] font-semibold",
                    pctClass(unrealizedSol)
                  )}
                >
                  {unrealizedSol >= 0 ? "+" : ""}
                  {fmtSol(unrealizedSol)}
                </div>
              </div>
            </div>

            {/* Per-position SOL exposure */}
            <div>
              <div className="mb-1.5 text-[11px] text-muted-foreground">
                Size by position (SOL)
              </div>
              {isLoading ? (
                <div
                  className="h-[120px] animate-pulse rounded-md bg-panel2 motion-reduce:animate-none"
                  aria-hidden
                />
              ) : exposureBars.length === 0 ? (
                <div className="flex h-[120px] items-center justify-center rounded-md border border-border bg-panel2 text-[11px] text-faint">
                  No open exposure
                </div>
              ) : (
                <div style={{ height: 120 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart
                      data={exposureBars}
                      margin={{ top: 4, right: 4, bottom: 0, left: -18 }}
                    >
                      <XAxis
                        dataKey="token"
                        tick={{ fontSize: 10, fill: HEX.faint }}
                        axisLine={{ stroke: HEX.border }}
                        tickLine={false}
                      />
                      <YAxis
                        tick={{ fontSize: 10, fill: HEX.faint }}
                        axisLine={false}
                        tickLine={false}
                      />
                      <Tooltip
                        cursor={{ fill: "rgba(255,255,255,0.04)" }}
                        contentStyle={{
                          backgroundColor: HEX.panel2,
                          border: `1px solid ${HEX.border}`,
                          borderRadius: 8,
                          fontSize: 11,
                          color: HEX.text,
                        }}
                        labelStyle={{ color: HEX.faint }}
                        formatter={(v: number) => [`${fmtSol(v)} SOL`, "Size"]}
                      />
                      <Bar dataKey="sol" radius={[3, 3, 0, 0]} maxBarSize={36}>
                        {exposureBars.map((b, i) => (
                          <Cell
                            key={i}
                            fill={b.up ? HEX.up : HEX.down}
                            fillOpacity={0.85}
                          />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>
          </div>
        </Panel>

        {/* Closed positions */}
        <Panel
          title="Closed positions"
          icon={<Archive />}
          actions={
            <span
              className={cn(
                "num text-[11px] font-semibold",
                pctClass(realizedSol)
              )}
            >
              {realizedSol >= 0 ? "+" : ""}
              {fmtSol(realizedSol)} SOL
            </span>
          }
          className="xl:col-span-2"
        >
          {isLoading ? (
            <TableSkeleton rows={6} cols={5} />
          ) : hasError ? (
            <ErrorState message={error ?? "Failed to load positions"} />
          ) : closed.length === 0 ? (
            <EmptyState icon={<Archive />} label="No closed positions yet." />
          ) : (
            <div className="max-h-[320px] overflow-auto scrollbar-thin">
              <Table className="text-[12px]">
                <TableHeader className="sticky top-0 z-10 bg-panel">
                  <TableRow className="border-b border-border hover:bg-transparent">
                    <TableHead className={TH}>Token</TableHead>
                    <TableHead className={TH_R}>Entry</TableHead>
                    <TableHead className={TH}>TP ladder</TableHead>
                    <TableHead className={TH}>Exit mode</TableHead>
                    <TableHead className={TH_R}>Hold</TableHead>
                    <TableHead className={TH_R}>Net PnL</TableHead>
                    <TableHead className={TH_R}>Gross</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {closed.map(p => {
                    const net = p.realizedPnlSol ?? 0;
                    const gross = p.realizedGrossPnlSol ?? net;
                    const roiPct =
                      p.entrySol > 0 ? round1((net / p.entrySol) * 100) : 0;
                    return (
                      <TableRow key={p.mint} className={ROW}>
                        <TableCell className={TD}>
                          <TokenCell token={p.token} mint={p.mint} />
                        </TableCell>
                        <TableCell
                          className={cn(TD, "num text-right text-text")}
                        >
                          {fmtSol(p.entrySol)}
                        </TableCell>
                        <TableCell className={TD}>
                          <TpLadder hit={p.tpHit} total={p.tpTotal} />
                        </TableCell>
                        <TableCell className={TD}>
                          <ExitModeChip mode={p.exitMode} />
                        </TableCell>
                        <TableCell
                          className={cn(
                            TD,
                            "num text-right text-muted-foreground"
                          )}
                        >
                          {fmtAge(p.ageSec)}
                        </TableCell>
                        {/* Net PnL — PRIMARY (bold, with ROI%) */}
                        <TableCell className={cn(TD, "text-right")}>
                          <div className="flex flex-col items-end leading-tight">
                            <span
                              className={cn("num font-semibold", pctClass(net))}
                            >
                              {net >= 0 ? "+" : ""}
                              {fmtSol(net)}
                            </span>
                            <span
                              className={cn("num text-[10px]", pctClass(net))}
                            >
                              {fmtPctSigned(roiPct)}
                            </span>
                          </div>
                        </TableCell>
                        {/* Gross — SECONDARY (muted, smaller) */}
                        <TableCell
                          className={cn(
                            TD,
                            "num text-right text-[11px] text-faint"
                          )}
                        >
                          {gross >= 0 ? "+" : ""}
                          {fmtSol(gross)}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
