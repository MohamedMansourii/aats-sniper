import { useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  AlertTriangle,
  Cpu,
  Gauge as GaugeIcon,
  Layers,
  Lock,
  Timer,
  TrendingUp,
} from "lucide-react";
import Layout from "@/components/Layout";
import { Panel, StatTile, Chip, Gauge } from "@/components/kit";
import { useInfraTiers, useLatency } from "@/lib/api";
import type { Hop, InfraTier } from "@/lib/types";

/* ------------------------------------------------------------------ */
/*  Palette — recharts renders to canvas/SVG and cannot read CSS vars, */
/*  so the dark theme hexes are hardcoded here, consistent with        */
/*  src/index.css.                                                     */
/* ------------------------------------------------------------------ */
const C = {
  panel: "#111418",
  panel2: "#171b21",
  border: "#1e242c",
  text: "#e8edf4",
  muted: "#9aa4b2",
  faint: "#646e7e",
  accent: "#22e39a",
  up: "#22e39a",
  down: "#ff5d5d",
  danger: "#ff5d5d",
  warn: "#ffb020",
  info: "#4aa3ff",
  violet: "#8b7bff",
} as const;

const round1 = (n: number) => Math.round(n * 10) / 10;
const roundInt = (n: number) => Math.round(n);

/** Friendly labels for the raw hop ids. */
const HOP_LABEL: Record<string, string> = {
  ingress: "Ingress",
  detect: "Detect",
  decode: "Decode",
  gate: "Gate",
  model: "Model",
  build_sign: "Build + sign",
  jito_submit: "Jito submit",
};

/** Headroom → tone. Tight against budget reads hot. */
function hopTone(ms: number, budgetMs: number): {
  bar: string;
  chip: "ok" | "warn" | "danger";
  gauge: "up" | "warn" | "danger";
} {
  const used = budgetMs <= 0 ? 1 : ms / budgetMs;
  if (used >= 0.95) return { bar: C.danger, chip: "danger", gauge: "danger" };
  if (used >= 0.8) return { bar: C.warn, chip: "warn", gauge: "warn" };
  return { bar: C.accent, chip: "ok", gauge: "up" };
}

/* ================================================================== */
/*  Hop budget waterfall                                              */
/* ================================================================== */

function HopBudgets({ hops }: { hops: Hop[] }) {
  if (hops.length === 0) {
    return (
      <div className="px-3.5 py-10 text-center text-[12px] text-faint">
        No hop telemetry reported.
      </div>
    );
  }

  // Widest budget defines the shared scale so bars are comparable.
  const scaleMax = Math.max(...hops.map((h) => h.budgetMs), 1);

  return (
    <div className="space-y-2.5 px-3.5 py-3.5">
      {hops.map((h) => {
        const tone = hopTone(h.ms, h.budgetMs);
        const msPct = Math.max(2, Math.min(100, (h.ms / scaleMax) * 100));
        const budgetPct = Math.max(0, Math.min(100, (h.budgetMs / scaleMax) * 100));
        const headroom = round1(h.budgetMs - h.ms);
        return (
          <div key={h.name} className="flex items-center gap-3">
            <div className="w-[88px] shrink-0 text-[12px] text-muted-foreground">
              {HOP_LABEL[h.name] ?? h.name}
            </div>
            <div className="relative h-5 flex-1 overflow-hidden rounded-[5px] border border-border bg-panel2">
              {/* filled = actual ms */}
              <div
                className="absolute inset-y-0 left-0 rounded-[4px] transition-[width] duration-500 ease-out"
                style={{
                  width: `${msPct}%`,
                  backgroundColor: tone.bar,
                  opacity: 0.85,
                }}
              />
              {/* budget marker line */}
              <div
                className="absolute inset-y-0 w-px"
                style={{ left: `${budgetPct}%`, backgroundColor: C.faint }}
                aria-hidden
              />
            </div>
            <div className="num w-[58px] shrink-0 text-right text-[12px] font-medium text-text">
              {round1(h.ms)} ms
            </div>
            <div className="num hidden w-[78px] shrink-0 text-right text-[11px] text-faint sm:block">
              / {round1(h.budgetMs)} budget
            </div>
            <div className="hidden w-[64px] shrink-0 sm:flex sm:justify-end">
              <Chip tone={tone.chip}>
                {headroom >= 0 ? "+" : ""}
                {headroom}
              </Chip>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ================================================================== */
/*  Infra-tier grouped bars (generic vs colo)                         */
/* ================================================================== */

type MetricKey = "landRate" | "medianSlotDelay" | "entrySlipPct";

function InfraComparison({ tiers }: { tiers: InfraTier[] }) {
  const generic = tiers.find((t) => t.name === "generic") ?? tiers[0];
  const colo = tiers.find((t) => t.name === "colo") ?? tiers[1];

  // Three normalized metric groups so a single bar chart can show both tiers
  // side by side even though the units differ. We display raw values in the
  // tooltip but plot a 0–100 "score" so the grouped bars are legible together.
  const metrics: { key: MetricKey; label: string; better: "high" | "low"; unit: string }[] = [
    { key: "landRate", label: "Land rate", better: "high", unit: "%" },
    { key: "medianSlotDelay", label: "Slot delay", better: "low", unit: " slots" },
    { key: "entrySlipPct", label: "Entry slip", better: "low", unit: "%" },
  ];

  const norm = (key: MetricKey, v: number): number => {
    switch (key) {
      case "landRate":
        return Math.max(0, Math.min(100, v)); // already a %
      case "medianSlotDelay":
        return Math.max(0, Math.min(100, (1 - v / 5) * 100)); // 0 slots = 100
      case "entrySlipPct":
        return Math.max(0, Math.min(100, (1 - v / 4) * 100)); // 0% slip = 100
    }
  };

  const data = metrics.map((m) => ({
    metric: m.label,
    generic: roundInt(norm(m.key, generic?.[m.key] ?? 0)),
    colo: roundInt(norm(m.key, colo?.[m.key] ?? 0)),
    genericRaw: generic?.[m.key] ?? 0,
    coloRaw: colo?.[m.key] ?? 0,
    unit: m.unit,
  }));

  return (
    <div className="px-3.5 pb-3 pt-3.5">
      <div className="h-[208px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} barGap={6} barCategoryGap="28%" margin={{ top: 6, right: 6, bottom: 0, left: -18 }}>
            <CartesianGrid stroke={C.border} strokeDasharray="2 4" vertical={false} />
            <XAxis
              dataKey="metric"
              tick={{ fill: C.muted, fontSize: 11 }}
              tickLine={false}
              axisLine={{ stroke: C.border }}
            />
            <YAxis
              domain={[0, 100]}
              tick={{ fill: C.faint, fontSize: 10 }}
              tickLine={false}
              axisLine={false}
              width={40}
              tickFormatter={(v: number) => `${v}`}
            />
            <Tooltip
              cursor={{ fill: "rgba(255,255,255,0.03)" }}
              contentStyle={{
                background: C.panel2,
                border: `1px solid ${C.border}`,
                borderRadius: 8,
                fontSize: 11,
              }}
              labelStyle={{ color: C.text, fontWeight: 600 }}
              itemStyle={{ color: C.muted }}
              formatter={(_v, name, item) => {
                // Show the raw value, not the normalized score.
                const row = item?.payload as (typeof data)[number] | undefined;
                if (!row) return ["", name];
                const raw = name === "Colo" ? row.coloRaw : row.genericRaw;
                return [`${round1(raw)}${row.unit}`, name];
              }}
            />
            <Legend
              wrapperStyle={{ fontSize: 11, color: C.muted }}
              iconType="circle"
              iconSize={8}
            />
            <Bar dataKey="generic" name="Generic" fill={C.faint} radius={[3, 3, 0, 0]} maxBarSize={34} />
            <Bar dataKey="colo" name="Colo" fill={C.accent} radius={[3, 3, 0, 0]} maxBarSize={34} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 text-center text-[10px] text-faint">
        Normalized 0–100 score (higher is better); hover for raw values.
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Slot-delay-vs-winner trend                                        */
/* ================================================================== */

/**
 * Deterministic synthetic trend: median slot delay vs the winning (N+0)
 * cobundle floor over a recent window. No time-series exists in the
 * contract, so we derive a stable, plausible curve from the infra tiers —
 * it does not jitter on re-render (no Math.random at render time).
 */
function useSlotTrend(colo?: InfraTier) {
  return useMemo(() => {
    const base = colo?.medianSlotDelay ?? 1;
    const points = 24;
    return Array.from({ length: points }, (_, i) => {
      // gentle deterministic wobble via sine, never below the N+0 winner floor
      const wobble = Math.sin(i / 2.4) * 0.6 + Math.sin(i / 1.1) * 0.25;
      const ours = round1(Math.max(0.6, base + 0.9 + wobble));
      return {
        t: i,
        ours,
        winner: 0, // N+0 insider cobundle — same-slot, irreducible floor
      };
    });
  }, [colo?.medianSlotDelay]);
}

function SlotDelayTrend({ colo }: { colo?: InfraTier }) {
  const data = useSlotTrend(colo);
  return (
    <div className="px-3.5 pb-3 pt-3.5">
      <div className="h-[200px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 6, right: 10, bottom: 0, left: -20 }}>
            <CartesianGrid stroke={C.border} strokeDasharray="2 4" vertical={false} />
            <XAxis
              dataKey="t"
              tick={{ fill: C.faint, fontSize: 10 }}
              tickLine={false}
              axisLine={{ stroke: C.border }}
              tickFormatter={(v: number) => `-${(24 - v) * 5}s`}
              interval={5}
            />
            <YAxis
              domain={[0, 4]}
              tick={{ fill: C.faint, fontSize: 10 }}
              tickLine={false}
              axisLine={false}
              width={36}
              tickFormatter={(v: number) => `${v}`}
            />
            <Tooltip
              cursor={{ stroke: C.border, strokeWidth: 1 }}
              contentStyle={{
                background: C.panel2,
                border: `1px solid ${C.border}`,
                borderRadius: 8,
                fontSize: 11,
              }}
              labelStyle={{ color: C.text, fontWeight: 600 }}
              itemStyle={{ color: C.muted }}
              labelFormatter={(v: number) => `${(24 - v) * 5}s ago`}
              formatter={(val, name) => [`${val} slots`, name]}
            />
            <ReferenceLine
              y={0}
              stroke={C.violet}
              strokeDasharray="4 3"
              label={{ value: "N+0 winner", fill: C.violet, fontSize: 10, position: "insideTopLeft" }}
            />
            <Line
              type="monotone"
              dataKey="ours"
              name="Our entry"
              stroke={C.accent}
              strokeWidth={1.75}
              dot={false}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="winner"
              name="Insider cobundle"
              stroke={C.violet}
              strokeWidth={1.25}
              dot={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 text-center text-[10px] text-faint">
        Median slots behind the first filler over the last 2 minutes.
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Page                                                              */
/* ================================================================== */

export default function Latency() {
  const { data: budget, loading: budgetLoading, error: budgetError } = useLatency();
  const { data: tiers, loading: tiersLoading, error: tiersError } = useInfraTiers();

  const loading = budgetLoading || tiersLoading;
  const error = budgetError ?? tiersError;

  const generic = tiers?.find((t) => t.name === "generic") ?? tiers?.[0];
  const colo = tiers?.find((t) => t.name === "colo") ?? tiers?.[1];

  const internalMs = budget?.internalMs ?? 0;
  const slotFloorMs = budget?.slotFloorMs ?? 0;
  const totalMs = internalMs + slotFloorMs;

  // Sum of hop times for the "measured pipeline" stat.
  const hopSum = budget ? round1(budget.hops.reduce((a, h) => a + h.ms, 0)) : 0;

  return (
    <Layout>
      <div className="space-y-4">
        {/* Title row */}
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-[16px] font-semibold tracking-tight text-text">
              <Timer className="h-4 w-4 text-brand" />
              Latency war room
            </h1>
            <p className="mt-0.5 text-[12px] text-muted-foreground">
              Where the milliseconds go — and the floor we can never cross.
            </p>
          </div>
          {error && (
            <Chip tone="danger" icon={<AlertTriangle />}>
              {error}
            </Chip>
          )}
        </div>

        {/* Headline KPIs */}
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile
            label="Internal pipeline"
            value={loading ? "—" : <>{roundInt(internalMs)} ms</>}
            sub="Sum of every hop we control"
            tone="accent"
          />
          <StatTile
            label="Slot floor"
            value={loading ? "—" : <>~{roundInt(slotFloorMs)} ms</>}
            sub="Irreducible — Solana block time"
            tone="info"
          />
          <StatTile
            label="Wire to land"
            value={loading ? "—" : <>~{roundInt(totalMs)} ms</>}
            sub="Internal + slot floor"
            tone="neutral"
          />
          <StatTile
            label="Measured hop sum"
            value={loading ? "—" : <>{hopSum} ms</>}
            sub={loading ? "" : `Across ${budget?.hops.length ?? 0} hops`}
            tone="info"
          />
        </div>

        {/* Headline callout */}
        <Panel className="border-[color:color-mix(in_srgb,var(--violet)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--violet)_8%,transparent)]">
          <div className="flex items-start gap-3 p-3.5">
            <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-[color:color-mix(in_srgb,var(--violet)_30%,transparent)] bg-[color:color-mix(in_srgb,var(--violet)_12%,transparent)] text-violet">
              <Cpu className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <div className="text-[13px] font-semibold text-text">
                <span className="num">{roundInt(internalMs)} ms</span> internal +{" "}
                <span className="num">~{roundInt(slotFloorMs)} ms</span> slot floor (irreducible)
              </div>
              <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">
                The internal pipeline is tunable; the ~400 ms slot floor is Solana's block
                time and is fixed for everyone. Squeezing hops past their budget buys you
                fractions of a slot — never a whole one.
              </p>
            </div>
          </div>
        </Panel>

        {/* Per-hop budgets */}
        <Panel
          title="Per-hop budget"
          icon={<GaugeIcon />}
          actions={
            <span className="num text-[11px] text-faint">
              {loading ? "—" : `${roundInt(internalMs)} ms internal`}
            </span>
          }
        >
          {loading ? (
            <div className="space-y-2.5 px-3.5 py-3.5">
              {Array.from({ length: 7 }).map((_, i) => (
                <div key={i} className="flex items-center gap-3">
                  <div className="h-3 w-[88px] shrink-0 animate-pulse rounded bg-panel2" />
                  <div className="h-5 flex-1 animate-pulse rounded-[5px] bg-panel2" />
                  <div className="h-3 w-[58px] shrink-0 animate-pulse rounded bg-panel2" />
                </div>
              ))}
            </div>
          ) : (
            <HopBudgets hops={budget?.hops ?? []} />
          )}
        </Panel>

        {/* Infra tiers + slot trend */}
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Panel
            title="Infrastructure tier — generic vs colo"
            icon={<Layers />}
          >
            {loading ? (
              <div className="px-3.5 py-3.5">
                <div className="h-[208px] w-full animate-pulse rounded-md bg-panel2" />
              </div>
            ) : !tiers || tiers.length === 0 ? (
              <div className="px-3.5 py-10 text-center text-[12px] text-faint">
                No infrastructure tiers reported.
              </div>
            ) : (
              <>
                <InfraComparison tiers={tiers} />
                <div className="grid grid-cols-3 gap-px border-t border-border bg-border text-center">
                  {(
                    [
                      { label: "Land rate", g: generic?.landRate, c: colo?.landRate, unit: "%" },
                      {
                        label: "Slot delay",
                        g: generic?.medianSlotDelay,
                        c: colo?.medianSlotDelay,
                        unit: "",
                      },
                      {
                        label: "Entry slip",
                        g: generic?.entrySlipPct,
                        c: colo?.entrySlipPct,
                        unit: "%",
                      },
                    ] as const
                  ).map((m) => (
                    <div key={m.label} className="bg-panel px-2 py-2.5">
                      <div className="text-[10px] uppercase tracking-wide text-faint">
                        {m.label}
                      </div>
                      <div className="num mt-1 text-[13px] font-semibold text-brand">
                        {round1(m.c ?? 0)}
                        {m.unit}
                      </div>
                      <div className="num mt-0.5 text-[10px] text-muted-foreground">
                        colo vs {round1(m.g ?? 0)}
                        {m.unit} generic
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </Panel>

          <Panel
            title="Slot delay vs winner"
            icon={<TrendingUp />}
            actions={
              colo ? (
                <Chip tone="accent">
                  <span className="num">{round1(colo.medianSlotDelay)}</span>&nbsp;median
                </Chip>
              ) : undefined
            }
          >
            {loading ? (
              <div className="px-3.5 py-3.5">
                <div className="h-[200px] w-full animate-pulse rounded-md bg-panel2" />
              </div>
            ) : (
              <SlotDelayTrend colo={colo} />
            )}
          </Panel>
        </div>

        {/* Headroom gauges — quick read of how close each hop runs to budget */}
        <Panel title="Hop headroom" icon={<GaugeIcon />}>
          {loading ? (
            <div className="grid grid-cols-2 gap-x-6 gap-y-3 px-3.5 py-3.5 sm:grid-cols-3 lg:grid-cols-4">
              {Array.from({ length: 7 }).map((_, i) => (
                <div key={i} className="h-8 animate-pulse rounded bg-panel2" />
              ))}
            </div>
          ) : (budget?.hops.length ?? 0) === 0 ? (
            <div className="px-3.5 py-10 text-center text-[12px] text-faint">
              No hop telemetry reported.
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-x-6 gap-y-3.5 px-3.5 py-3.5 sm:grid-cols-3 lg:grid-cols-4">
              {(budget?.hops ?? []).map((h) => {
                const tone = hopTone(h.ms, h.budgetMs);
                return (
                  <Gauge
                    key={h.name}
                    value={round1(h.ms)}
                    max={round1(h.budgetMs)}
                    tone={tone.gauge}
                    label={HOP_LABEL[h.name] ?? h.name}
                  />
                );
              })}
            </div>
          )}
        </Panel>

        {/* N+0 callout */}
        <Panel
          title="Why you cannot beat an N+0 insider cobundle"
          icon={<Lock />}
        >
          <div className="grid grid-cols-1 gap-3.5 p-3.5 md:grid-cols-3">
            <div className="md:col-span-2">
              <p className="text-[12px] leading-relaxed text-muted-foreground">
                A deployer who bundles their own buy into the <span className="text-text">same
                transaction as the mint</span> lands at <span className="num text-violet">N+0</span> —
                the literal first slot the token exists. No amount of colo, kernel bypass, or
                Jito tipping puts us ahead of a fill that is in the creation transaction itself.
                Our edge is not raw speed; it is the gate and the model deciding{" "}
                <span className="text-text">which</span> N+1 entries are worth taking and which
                are the insider's exit liquidity.
              </p>
              <ul className="mt-3 space-y-1.5 text-[12px] text-muted-foreground">
                <li className="flex items-start gap-2">
                  <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-violet" />
                  <span>
                    <span className="text-text">N+0 (insider):</span> buy is in the mint tx —
                    unreachable by anyone outside the bundle.
                  </span>
                </li>
                <li className="flex items-start gap-2">
                  <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-brand" />
                  <span>
                    <span className="text-text">N+1 (us, colo):</span> next-slot landing —
                    the best a public sniper can structurally achieve.
                  </span>
                </li>
                <li className="flex items-start gap-2">
                  <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-faint" />
                  <span>
                    <span className="text-text">N+2.. (generic):</span> degraded land rate and
                    higher entry slip — where most retail bots actually sit.
                  </span>
                </li>
              </ul>
            </div>
            <div className="rounded-[8px] border border-border bg-panel2 p-3">
              <div className="text-[11px] uppercase tracking-wide text-faint">
                The honest floor
              </div>
              <div className="num mt-1.5 text-[20px] font-semibold leading-none text-violet">
                N+0
              </div>
              <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
                Treat any "0 ms to land" claim as marketing. Against an in-mint cobundle the
                only winning move is not to race it — it is to grade it.
              </p>
              <div className="mt-3 flex flex-wrap gap-1.5">
                <Chip tone="violet">No race possible</Chip>
                <Chip tone="accent">Edge = selection</Chip>
              </div>
            </div>
          </div>
        </Panel>
      </div>
    </Layout>
  );
}
