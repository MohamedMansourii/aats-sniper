import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowRightLeft,
  Crosshair,
  Gauge as GaugeIcon,
  Radio,
  ShieldCheck,
  Target,
  Users,
  Wallet,
  Zap,
} from "lucide-react";
import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  XAxis,
  YAxis,
} from "recharts";
import {
  useAgentState,
  useLatency,
  useMetrics,
  usePositions,
  useSnipeFeed,
} from "@/lib/api";
import type {
  GateReason,
  Hop,
  LoopState,
  Position,
  SnipeEvent,
  SnipeSource,
} from "@/lib/types";
import {
  Chip,
  LiveDot,
  Panel,
  Spark,
  StatTile,
  type ChipTone,
  type StatTone,
} from "@/components/kit";
import { CHART as HEX } from "@/lib/chart-colors";

/* -------------------------------------------------------------------------- */
/*  Display helpers — every number rounded for display                        */
/* -------------------------------------------------------------------------- */

const fmt1 = (n: number) => (Math.round(n * 10) / 10).toFixed(1);
const fmt2 = (n: number) => (Math.round(n * 100) / 100).toFixed(2);
const fmtInt = (n: number) => `${Math.round(n)}`;
const signed1 = (n: number) => `${n >= 0 ? "+" : ""}${fmt1(n)}`;

function ago(ts: number, now: number): string {
  const s = Math.max(0, Math.round((now - ts) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

function ageLabel(sec: number): string {
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  return `${m}m ${Math.round(sec % 60)}s`;
}

/* -------------------------------------------------------------------------- */
/*  Source / gate / action visual mapping                                     */
/* -------------------------------------------------------------------------- */

const SOURCE_META: Record<SnipeSource, { label: string; tone: ChipTone }> = {
  "pump.fun": { label: "pump.fun", tone: "accent" },
  pumpswap: { label: "pumpswap", tone: "info" },
  raydium_v4: { label: "raydium v4", tone: "violet" },
  raydium_cpmm: { label: "raydium cpmm", tone: "violet" },
  migration: { label: "migration", tone: "warn" },
};

const GATE_LABEL: Record<GateReason, string> = {
  freeze_authority: "freeze auth",
  mint_authority: "mint auth",
  lp_unlocked: "lp unlocked",
  sniper_cluster: "sniper cluster",
  high_tax: "high tax",
  low_liquidity: "low liq",
  passed: "passed",
};

function ActionChip({ action }: { action: SnipeEvent["action"] }) {
  if (action === "sniped") {
    return (
      <Chip tone="accent" icon={<Target />}>
        sniped
      </Chip>
    );
  }
  if (action === "vetoed") {
    return (
      <Chip tone="danger" icon={<ShieldCheck />}>
        vetoed
      </Chip>
    );
  }
  return <Chip tone="neutral">skipped</Chip>;
}

function GateChip({ event }: { event: SnipeEvent }) {
  if (event.gatePassed) {
    return (
      <Chip tone="ok" icon={<ShieldCheck />}>
        gate pass
      </Chip>
    );
  }
  const first = event.gateReasons[0] ?? "low_liquidity";
  const extra = event.gateReasons.length - 1;
  return (
    <Chip tone="danger" icon={<AlertTriangle />}>
      {GATE_LABEL[first]}
      {extra > 0 ? ` +${extra}` : ""}
    </Chip>
  );
}

/* -------------------------------------------------------------------------- */
/*  A small rolling buffer so the KPI sparklines feel alive                   */
/* -------------------------------------------------------------------------- */

function useSeries(value: number | undefined, cap = 24): number[] {
  const [series, setSeries] = useState<number[]>([]);
  const last = useRef<number | null>(null);
  useEffect(() => {
    if (value === undefined || !Number.isFinite(value)) return;
    if (last.current === value) return;
    last.current = value;
    // Intentional: accumulate a rolling history from a live external value as
    // it ticks in. This is stateful-over-time derivation an effect must own.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSeries(cur => [...cur, value].slice(-cap));
  }, [value, cap]);
  return series;
}

/* -------------------------------------------------------------------------- */
/*  Latency budget — per-hop bars                                             */
/* -------------------------------------------------------------------------- */

function hopColor(h: Hop): string {
  const ratio = h.budgetMs <= 0 ? 0 : h.ms / h.budgetMs;
  if (ratio > 1) return HEX.down;
  if (ratio > 0.92) return HEX.warn;
  return HEX.accent;
}

function LatencyBudgetBody() {
  const { data, loading, error } = useLatency();

  if (loading) {
    return <SkeletonRows rows={5} />;
  }
  if (error || !data) {
    return <ErrorNote message={error ?? "No latency data"} />;
  }

  const chartData = data.hops.map(h => ({ ...h }));
  const maxBudget = Math.max(...data.hops.map(h => h.budgetMs), 1);

  return (
    <div className="space-y-3">
      <div style={{ height: 168 }} className="w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={chartData}
            layout="vertical"
            margin={{ top: 0, right: 28, bottom: 0, left: 4 }}
            barCategoryGap={6}
          >
            <XAxis type="number" domain={[0, maxBudget]} hide />
            <YAxis
              type="category"
              dataKey="name"
              width={72}
              tickLine={false}
              axisLine={false}
              tick={{
                fill: HEX.faint,
                fontSize: 11,
                fontFamily:
                  'ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace',
              }}
            />
            <Bar dataKey="ms" radius={[2, 2, 2, 2]} isAnimationActive={false}>
              {chartData.map(h => (
                <Cell key={h.name} fill={hopColor(h)} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      <div className="space-y-1 border-t border-border pt-2.5">
        {data.hops.map(h => {
          const tight = h.ms / h.budgetMs > 0.92;
          return (
            <div
              key={h.name}
              className="flex items-center justify-between text-[11px]"
            >
              <span className="text-muted-foreground">{h.name}</span>
              <span className="num text-faint">
                <span className={tight ? "text-warn" : "text-text"}>
                  {fmt1(h.ms)}
                </span>
                {" / "}
                {fmtInt(h.budgetMs)}ms
              </span>
            </div>
          );
        })}
      </div>

      <div className="flex items-center justify-between rounded-md border border-border bg-panel2 px-3 py-2">
        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <Zap className="h-3.5 w-3.5 text-brand" />
          Internal pipeline
        </div>
        <div className="num text-[15px] font-semibold text-brand">
          {fmtInt(data.internalMs)}
          <span className="ml-0.5 text-[11px] font-normal text-faint">ms</span>
        </div>
      </div>
      <p className="text-[11px] leading-relaxed text-faint">
        Internal budget is{" "}
        <span className="num text-muted-foreground">
          ~{Math.round((data.internalMs / data.slotFloorMs) * 100)}%
        </span>{" "}
        of the{" "}
        <span className="num text-muted-foreground">
          ~{fmtInt(data.slotFloorMs)}ms
        </span>{" "}
        slot floor — comfortable headroom to land in the next block.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Loop status strip                                                         */
/* -------------------------------------------------------------------------- */

const SNIPE_LOOP_META: Record<
  LoopState["snipe"],
  { tone: ChipTone; label: string }
> = {
  idle: { tone: "neutral", label: "Idle" },
  armed: { tone: "info", label: "Armed" },
  firing: { tone: "accent", label: "Firing" },
};

function LoopCell({
  icon,
  name,
  cadence,
  state,
  tone,
  live,
}: {
  icon: ReactNode;
  name: string;
  cadence: string;
  state: string;
  tone: ChipTone;
  live?: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-panel2 px-3 py-2.5">
      <div className="flex min-w-0 items-center gap-2.5">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border bg-panel text-muted-foreground [&_svg]:h-3.5 [&_svg]:w-3.5">
          {icon}
        </span>
        <div className="min-w-0 leading-tight">
          <div className="truncate text-[12px] font-medium text-text">
            {name}
          </div>
          <div className="num text-[11px] text-faint">{cadence}</div>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {live && <LiveDot tone="accent" />}
        <Chip tone={tone}>{state}</Chip>
      </div>
    </div>
  );
}

function LoopStrip() {
  const { data, loading } = useAgentState();

  if (loading && !data) {
    return <SkeletonRows rows={1} />;
  }

  const loops = data?.loops;
  const snipeMeta = SNIPE_LOOP_META[loops?.snipe ?? "idle"];

  return (
    <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
      <LoopCell
        icon={<Crosshair />}
        name="Snipe loop"
        cadence="event-driven"
        state={snipeMeta.label}
        tone={snipeMeta.tone}
        live={loops?.snipe === "firing"}
      />
      <LoopCell
        icon={<Zap />}
        name="Fast loop"
        cadence={loops?.fast ?? "—"}
        state="Online"
        tone="ok"
        live
      />
      <LoopCell
        icon={<Activity />}
        name="Slow loop"
        cadence={loops?.slow ?? "—"}
        state="Online"
        tone="ok"
      />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Open positions summary table                                             */
/* -------------------------------------------------------------------------- */

function PositionsSummary() {
  const { data, loading, error } = usePositions();

  if (loading && !data) {
    return <SkeletonRows rows={3} />;
  }
  if (error) {
    return <ErrorNote message={error} />;
  }

  const open = (data ?? []).filter((p): p is Position => p.status === "open");

  if (open.length === 0) {
    return <EmptyNote message="No open positions" />;
  }

  return (
    <div className="overflow-x-auto scrollbar-thin">
      <table className="w-full border-collapse text-[12px]">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-faint">
            <th className="px-3 py-2 font-medium">Token</th>
            <th className="px-3 py-2 text-right font-medium">Entry</th>
            <th className="px-3 py-2 text-right font-medium">P&amp;L</th>
            <th className="px-3 py-2 text-center font-medium">TP</th>
            <th className="px-3 py-2 text-center font-medium">Trail</th>
            <th className="px-3 py-2 text-right font-medium">Age</th>
          </tr>
        </thead>
        <tbody>
          {open.map(p => {
            const up = p.currentPct >= 0;
            return (
              <tr
                key={p.mint}
                className="border-t border-border transition-colors hover:bg-hover"
              >
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-text">{p.token}</span>
                    <Chip tone={p.exitMode === "secure" ? "info" : "warn"}>
                      {p.exitMode}
                    </Chip>
                  </div>
                </td>
                <td className="num px-3 py-2 text-right text-muted-foreground">
                  {fmt1(p.entrySol)} SOL
                </td>
                <td
                  className={`num px-3 py-2 text-right font-semibold transition-colors ${
                    up ? "text-up" : "text-down"
                  }`}
                >
                  {signed1(p.currentPct)}%
                </td>
                <td className="num px-3 py-2 text-center text-muted-foreground">
                  {p.tpHit}/{p.tpTotal}
                </td>
                <td className="px-3 py-2 text-center">
                  {p.trailingArmed ? (
                    <Chip tone="accent">armed</Chip>
                  ) : (
                    <span className="text-faint">—</span>
                  )}
                </td>
                <td className="num px-3 py-2 text-right text-faint">
                  {ageLabel(p.ageSec)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Live snipe feed (last ~8 events)                                          */
/* -------------------------------------------------------------------------- */

function LiveFeed() {
  const { events, loading, error } = useSnipeFeed();
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  if (loading && events.length === 0) {
    return <SkeletonRows rows={6} />;
  }
  if (error && events.length === 0) {
    return <ErrorNote message={error} />;
  }
  if (events.length === 0) {
    return <EmptyNote message="Waiting for first event…" />;
  }

  const recent = events.slice(0, 8);

  return (
    <ul className="divide-y divide-border">
      {recent.map(e => (
        <li
          key={e.id}
          className="flex items-center gap-2 px-3 py-2 transition-colors hover:bg-hover"
        >
          <div className="flex w-[112px] shrink-0 flex-col leading-tight">
            <span className="text-[12px] font-semibold text-text">
              {e.token}
            </span>
            <span className="num text-[10px] text-faint">
              {ago(e.ts, now)} ago
            </span>
          </div>
          <Chip tone={SOURCE_META[e.source].tone}>
            {SOURCE_META[e.source].label}
          </Chip>
          <GateChip event={e} />
          <div className="ml-auto flex shrink-0 items-center gap-2.5">
            {e.smartWallets > 0 && (
              <span className="num hidden items-center gap-1 text-[11px] text-info md:inline-flex">
                <Users className="h-3 w-3" />
                {fmtInt(e.smartWallets)}
              </span>
            )}
            <span className="num w-[44px] text-right text-[12px] tabular-nums">
              {e.modelP === null ? (
                <span className="text-faint">—</span>
              ) : (
                <span
                  className={
                    e.modelP >= 0.62
                      ? "text-up"
                      : e.modelP < 0.35
                        ? "text-down"
                        : "text-warn"
                  }
                >
                  {fmt2(e.modelP)}
                </span>
              )}
            </span>
            <div className="w-[72px] shrink-0">
              <ActionChip action={e.action} />
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}

/* -------------------------------------------------------------------------- */
/*  Smart-money strip                                                         */
/* -------------------------------------------------------------------------- */

function SmartMoneyStrip() {
  const { events, loading } = useSnipeFeed();

  if (loading && events.length === 0) {
    return <SkeletonRows rows={1} />;
  }

  const recent = events.slice(0, 20);
  const sniped = recent.filter(e => e.action === "sniped");
  const totalWallets = sniped.reduce((acc, e) => acc + e.smartWallets, 0);
  const hottest = [...recent]
    .filter(e => e.smartWallets > 0)
    .sort((a, b) => b.smartWallets - a.smartWallets)
    .slice(0, 4);

  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-2">
        <span className="num text-[28px] font-semibold leading-none text-info">
          {fmtInt(totalWallets)}
        </span>
        <span className="text-[12px] text-muted-foreground">
          proven wallets in across{" "}
          <span className="num text-text">{fmtInt(sniped.length)}</span> live
          snipes
        </span>
      </div>

      {hottest.length === 0 ? (
        <EmptyNote message="No smart-money confluence in recent window" />
      ) : (
        <ul className="space-y-1.5">
          {hottest.map(e => (
            <li
              key={e.id}
              className="flex items-center justify-between gap-2 rounded-md border border-border bg-panel2 px-2.5 py-1.5"
            >
              <span className="text-[12px] font-medium text-text">
                {e.token}
              </span>
              <span className="num inline-flex items-center gap-1 text-[12px] font-semibold text-info">
                <Users className="h-3.5 w-3.5" />
                {fmtInt(e.smartWallets)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Shared state primitives                                                   */
/* -------------------------------------------------------------------------- */

function SkeletonRows({ rows }: { rows: number }) {
  return (
    <div className="space-y-2" aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="h-7 w-full animate-pulse rounded-md bg-panel2"
        />
      ))}
    </div>
  );
}

function ErrorNote({ message }: { message: string }) {
  return (
    <div className="flex items-center gap-2 rounded-md border border-[color:color-mix(in_srgb,var(--danger)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--danger)_8%,transparent)] px-3 py-2 text-[12px] text-danger">
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

function EmptyNote({ message }: { message: string }) {
  return (
    <div className="flex items-center justify-center rounded-md border border-dashed border-border px-3 py-6 text-[12px] text-faint">
      {message}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  KPI row                                                                   */
/* -------------------------------------------------------------------------- */

function KpiRow() {
  const { data, loading, error } = useMetrics();

  const netSeries = useSeries(data?.netPnlSol);
  const landSeries = useSeries(data?.landRatePct);
  const slotSeries = useSeries(data?.medianSlotDelay);
  const rugSeries = useSeries(data?.rugAvoidancePct);
  const tipSeries = useSeries(data?.tipEfficiency);
  const edgeSeries = useSeries(data?.edgeVsBaselinePct);

  if (loading && !data) {
    return (
      <div className="grid grid-cols-2 gap-2.5 md:grid-cols-3 xl:grid-cols-6">
        {Array.from({ length: 6 }).map((_, i) => (
          <div
            key={i}
            className="h-[88px] animate-pulse rounded-[10px] border border-border bg-panel"
          />
        ))}
      </div>
    );
  }
  if (error || !data) {
    return <ErrorNote message={error ?? "No metrics"} />;
  }

  return (
    <div className="grid grid-cols-2 gap-2.5 md:grid-cols-3 xl:grid-cols-6">
      <KpiTile
        label="Net PnL (net of tips)"
        value={`${signed1(data.netPnlSol)}`}
        sub="SOL · session"
        tone={data.netPnlSol >= 0 ? "up" : "down"}
        series={netSeries}
        seriesColor={data.netPnlSol >= 0 ? HEX.up : HEX.down}
      />
      <KpiTile
        label="Land rate"
        value={`${fmtInt(data.landRatePct)}%`}
        sub="Jito bundle inclusion"
        tone="accent"
        series={landSeries}
        seriesColor={HEX.accent}
      />
      <KpiTile
        label="Median slot delay"
        value={`${fmtInt(data.medianSlotDelay)}`}
        sub="slots to land"
        tone={data.medianSlotDelay <= 2 ? "up" : "warn"}
        series={slotSeries}
        seriesColor={data.medianSlotDelay <= 2 ? HEX.up : HEX.warn}
      />
      <KpiTile
        label="Rug avoidance"
        value={`${fmtInt(data.rugAvoidancePct)}%`}
        sub="gate-blocked rugs"
        tone="up"
        series={rugSeries}
        seriesColor={HEX.up}
      />
      <KpiTile
        label="Tip efficiency"
        value={`${fmt2(data.tipEfficiency)}`}
        sub="SOL won / SOL tipped"
        tone={data.tipEfficiency >= 0.6 ? "up" : "warn"}
        series={tipSeries}
        seriesColor={data.tipEfficiency >= 0.6 ? HEX.up : HEX.warn}
      />
      <KpiTile
        label="Edge vs baseline"
        value={`${signed1(data.edgeVsBaselinePct)}%`}
        sub="model over random entry"
        tone={data.edgeVsBaselinePct >= 0 ? "accent" : "down"}
        series={edgeSeries}
        seriesColor={data.edgeVsBaselinePct >= 0 ? HEX.violet : HEX.down}
      />
    </div>
  );
}

function KpiTile({
  label,
  value,
  sub,
  tone,
  series,
  seriesColor,
}: {
  label: string;
  value: string;
  sub: string;
  tone: StatTone;
  series: number[];
  seriesColor: string;
}) {
  return (
    <div className="relative">
      <StatTile label={label} value={value} sub={sub} tone={tone} />
      {series.length > 1 && (
        <div className="pointer-events-none absolute inset-x-3.5 bottom-2.5 h-5 opacity-70">
          <Spark data={series} tone={seriesColor} height={20} />
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                      */
/* -------------------------------------------------------------------------- */

export default function CommandDeck() {
  return (
    <div className="space-y-4">
      {/* Page header */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <h1 className="text-[15px] font-semibold tracking-tight text-text">
            Command deck
          </h1>
          <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <LiveDot tone="accent" />
            Live telemetry
          </span>
        </div>
        <span className="num hidden text-[11px] text-faint sm:block">
          refresh ~1.5s
        </span>
      </div>

      {/* KPI row */}
      <KpiRow />

      {/* Middle: live feed + latency budget */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Panel
          title="Live snipe feed"
          icon={<Crosshair />}
          actions={
            <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <LiveDot tone="accent" />
              streaming
            </span>
          }
          className="xl:col-span-2"
        >
          <LiveFeed />
        </Panel>

        <Panel title="Latency budget" icon={<GaugeIcon />}>
          <LatencyBudgetBody />
        </Panel>
      </div>

      {/* Loop status strip */}
      <Panel
        title="Loop status"
        icon={<ArrowRightLeft />}
        actions={
          <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <Radio className="h-3 w-3 text-up" />
            triple-loop
          </span>
        }
      >
        <LoopStrip />
      </Panel>

      {/* Bottom: positions summary + smart money */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Panel
          title="Open positions"
          icon={<Wallet />}
          className="xl:col-span-2"
        >
          <PositionsSummary />
        </Panel>

        <Panel title="Smart money" icon={<Users />}>
          <SmartMoneyStrip />
        </Panel>
      </div>
    </div>
  );
}
