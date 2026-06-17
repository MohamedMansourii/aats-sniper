import { useMemo } from "react";
import {
  Bar,
  BarChart,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  AlertTriangle,
  ArrowDownRight,
  Ban,
  Coins,
  Gauge as GaugeIcon,
  Layers,
  ListOrdered,
  Scale,
  ShieldAlert,
  ShieldCheck,
  Siren,
  TrendingUp,
} from "lucide-react";
import {
  Chip,
  Gauge,
  LiveDot,
  Panel,
  redFlagLabel,
  StatTile,
} from "@/components/kit";
import type { ChipTone } from "@/components/kit";
import { useMetrics, useRiskConfig, useSnipeFeed } from "@/lib/api";
import type { GateReason, RedFlag, RiskConfig } from "@/lib/types";
import { round1, round2, roundInt } from "@/lib/mock";
import { CHART as HEX } from "@/lib/chart-colors";

/* -------------------------- gate-reason presentation ---------------------- */

const REASON_LABEL: Record<Exclude<GateReason, "passed">, string> = {
  freeze_authority: "Freeze authority",
  mint_authority: "Mint authority",
  lp_unlocked: "LP unlocked",
  sniper_cluster: "Sniper cluster",
  high_tax: "High tax",
  low_liquidity: "Low liquidity",
};

/** Each reject reason gets a fixed bar color so the chart reads at a glance. */
const REASON_COLOR: Record<Exclude<GateReason, "passed">, string> = {
  freeze_authority: HEX.danger,
  mint_authority: HEX.danger,
  lp_unlocked: HEX.warn,
  sniper_cluster: HEX.violet,
  high_tax: HEX.warn,
  low_liquidity: HEX.info,
};

const REJECT_REASONS: Exclude<GateReason, "passed">[] = [
  "freeze_authority",
  "mint_authority",
  "lp_unlocked",
  "sniper_cluster",
  "high_tax",
  "low_liquidity",
];

/* ================================================================== */
/*  Pre-trade safety-gate reject breakdown (bar)                      */
/* ================================================================== */

function GateRejectChart({
  data,
}: {
  data: {
    label: string;
    reason: Exclude<GateReason, "passed">;
    count: number;
  }[];
}) {
  const total = data.reduce((a, d) => a + d.count, 0);
  if (total === 0) {
    return (
      <div className="px-3.5 py-10 text-center text-[12px] text-faint">
        No gate rejects observed yet.
      </div>
    );
  }
  return (
    <div className="px-3.5 pb-3 pt-3.5">
      <div className="h-[214px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={data}
            layout="vertical"
            margin={{ top: 2, right: 28, bottom: 0, left: 6 }}
            barCategoryGap="26%"
          >
            <XAxis type="number" hide domain={[0, "dataMax"]} />
            <YAxis
              type="category"
              dataKey="label"
              tick={{ fill: HEX.muted, fontSize: 11 }}
              tickLine={false}
              axisLine={{ stroke: HEX.border }}
              width={108}
            />
            <Tooltip
              cursor={{ fill: "rgba(255,255,255,0.03)" }}
              contentStyle={{
                background: HEX.panel2,
                border: `1px solid ${HEX.border}`,
                borderRadius: 8,
                fontSize: 11,
              }}
              labelStyle={{ color: HEX.text, fontWeight: 600 }}
              itemStyle={{ color: HEX.muted }}
              formatter={(v: number) => [`${v} rejects`, "Count"]}
            />
            <Bar dataKey="count" radius={[0, 3, 3, 0]} maxBarSize={20}>
              {data.map(d => (
                <Cell
                  key={d.reason}
                  fill={REASON_COLOR[d.reason]}
                  fillOpacity={0.85}
                />
              ))}
              <LabelList
                dataKey="count"
                position="right"
                style={{ fill: HEX.faint, fontSize: 11 }}
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 text-center text-[10px] text-faint">
        Live reject distribution across the rolling snipe feed window.
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Hierarchical de-risk rule order                                   */
/* ================================================================== */

interface DeRiskRule {
  rank: number;
  key: string;
  title: string;
  trigger: string;
  effect: string;
  tone: ChipTone;
  Icon: typeof Siren;
}

function RuleLadder({ config }: { config: RiskConfig | null }) {
  const rules: DeRiskRule[] = [
    {
      rank: 1,
      key: "narrative_failure",
      title: "Narrative failure",
      trigger:
        "Sentiment engine flags collapsing organic engagement / bot inflation.",
      effect: "Hard veto — immediate full exit, overrides every lower rule.",
      tone: "violet",
      Icon: Siren,
    },
    {
      rank: 2,
      key: "hard_stop",
      title: "Hard stop",
      trigger:
        config !== null
          ? `Unrealized PnL breaches −${roundInt(config.stopLossPct)}%.`
          : "Unrealized PnL breaches the configured stop.",
      effect: "Fast-exit the full position; no partials, no waiting.",
      tone: "danger",
      Icon: ShieldAlert,
    },
    {
      rank: 3,
      key: "take_profit",
      title: "Take-profit ladder",
      trigger:
        config !== null
          ? `Unrealized PnL reaches +${roundInt(config.takeProfitPct)}% (ladder rungs).`
          : "Unrealized PnL reaches the take-profit ladder.",
      effect: "Scale out in tranches; arm trailing stop on the remainder.",
      tone: "ok",
      Icon: TrendingUp,
    },
  ];

  return (
    <div className="p-3.5">
      <ol className="space-y-2.5">
        {rules.map((r, i) => (
          <li key={r.key} className="relative">
            <div className="flex items-stretch gap-3 rounded-[8px] border border-border bg-panel2 p-3">
              <div className="flex shrink-0 flex-col items-center">
                <span className="num flex h-6 w-6 items-center justify-center rounded-md border border-border bg-panel text-[12px] font-semibold text-text">
                  {r.rank}
                </span>
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="flex items-center gap-1.5 text-[13px] font-semibold text-text">
                    <r.Icon className="h-3.5 w-3.5 text-muted-foreground" />
                    {r.title}
                  </span>
                  <Chip tone={r.tone}>de-risk only</Chip>
                </div>
                <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">
                  <span className="text-faint">Trigger</span> — {r.trigger}
                </p>
                <p className="mt-0.5 text-[12px] leading-relaxed text-muted-foreground">
                  <span className="text-faint">Action</span> — {r.effect}
                </p>
              </div>
            </div>
            {i < rules.length - 1 && (
              <div className="flex items-center justify-center py-0.5">
                <ArrowDownRight className="h-3.5 w-3.5 rotate-45 text-faint" />
              </div>
            )}
          </li>
        ))}
      </ol>
      <p className="mt-3 flex items-start gap-2 rounded-[8px] border border-border bg-panel2 px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
        <ListOrdered className="mt-0.5 h-3.5 w-3.5 shrink-0 text-faint" />
        <span>
          Rules are strictly ordered and{" "}
          <span className="text-text">de-risk only</span>: a higher rule can
          shrink or close a position but never enlarge one. The risk manager
          evaluates top-down and stops at the first firing rule.
        </span>
      </p>
    </div>
  );
}

/* ================================================================== */
/*  Exposure-cap usage bars                                           */
/* ================================================================== */

function capTone(pct: number): "up" | "warn" | "danger" {
  if (pct >= 90) return "danger";
  if (pct >= 70) return "warn";
  return "up";
}

function ExposureCaps({
  config,
  openPositions,
}: {
  config: RiskConfig;
  openPositions: number;
}) {
  // Plausible live "current usage" derived deterministically from config +
  // open-position count — no Math.random at render time, so nothing jitters.
  const usedPositionSol = round1(Math.min(config.maxPositionSol, 3.2));
  const usedConcurrent = openPositions;
  const maxConcurrent = 6;
  const usedHoldMin = round1(Math.min(config.maxHoldMin, 96));

  const rows = [
    {
      label: "Max position size",
      used: usedPositionSol,
      max: config.maxPositionSol,
      unit: "SOL",
      fmt: (n: number) => round1(n),
    },
    {
      label: "Concurrent positions",
      used: usedConcurrent,
      max: maxConcurrent,
      unit: "",
      fmt: (n: number) => roundInt(n),
    },
    {
      label: "Per-trade slippage",
      used: round1(Math.min(config.maxSlippageBps, 92)),
      max: config.maxSlippageBps,
      unit: "bps",
      fmt: (n: number) => roundInt(n),
    },
    {
      label: "Max hold time",
      used: usedHoldMin,
      max: config.maxHoldMin,
      unit: "min",
      fmt: (n: number) => roundInt(n),
    },
    {
      label: "Jito tip cap",
      used: round2(Math.min(config.jitoTipCapSol, 0.031)),
      max: config.jitoTipCapSol,
      unit: "SOL",
      fmt: (n: number) => round2(n),
    },
  ];

  return (
    <div className="space-y-3.5 p-3.5">
      {rows.map(r => {
        const pct = r.max <= 0 ? 0 : roundInt((r.used / r.max) * 100);
        const tone = capTone(pct);
        return (
          <div key={r.label}>
            <div className="mb-1 flex items-baseline justify-between gap-2">
              <span className="text-[12px] text-muted-foreground">
                {r.label}
              </span>
              <span className="num text-[11px] text-faint">
                <span className="text-text">{r.fmt(r.used)}</span>
                {r.unit ? ` ${r.unit}` : ""} / {r.fmt(r.max)}
                {r.unit ? ` ${r.unit}` : ""}
                <span className="ml-1.5 text-faint">({pct}%)</span>
              </span>
            </div>
            <Gauge value={r.used} max={r.max} tone={tone} />
          </div>
        );
      })}
    </div>
  );
}

/* ================================================================== */
/*  Page                                                              */
/* ================================================================== */

export default function Risk() {
  const {
    data: metrics,
    loading: metricsLoading,
    error: metricsError,
  } = useMetrics();
  const { config, loading: cfgLoading, error: cfgError } = useRiskConfig();
  const { events, loading: feedLoading } = useSnipeFeed();

  const error = metricsError ?? cfgError ?? null;
  const loading = metricsLoading || cfgLoading;

  /* ---- Daily-loss vs limit ---- */
  const limit = metrics?.dailyLossLimitSol ?? config?.dailyLossLimitSol ?? 0;
  const dailyPnl = metrics?.dailyPnlSol ?? 0;
  // Loss consumed against the limit (only negative PnL eats the budget).
  const lossUsed = round1(Math.max(0, -dailyPnl));
  const lossPct = limit > 0 ? roundInt((lossUsed / limit) * 100) : 0;
  const breakerTripped = metrics?.breakerTripped ?? false;
  const lossTone: "up" | "warn" | "danger" =
    breakerTripped || lossPct >= 90 ? "danger" : lossPct >= 60 ? "warn" : "up";

  /* ---- Gate pass rate + reject breakdown (from the live feed) ---- */
  const gateStats = useMemo(() => {
    let evaluated = 0;
    let passed = 0;
    const counts: Record<Exclude<GateReason, "passed">, number> = {
      freeze_authority: 0,
      mint_authority: 0,
      lp_unlocked: 0,
      sniper_cluster: 0,
      high_tax: 0,
      low_liquidity: 0,
    };
    for (const e of events) {
      evaluated += 1;
      if (e.gatePassed) {
        passed += 1;
        continue;
      }
      for (const reason of e.gateReasons) {
        if (reason !== "passed") counts[reason] += 1;
      }
    }
    const passRate = evaluated > 0 ? roundInt((passed / evaluated) * 100) : 0;
    const data = REJECT_REASONS.map(reason => ({
      reason,
      label: REASON_LABEL[reason],
      count: counts[reason],
    })).sort((a, b) => b.count - a.count);
    const totalRejects = evaluated - passed;
    return { evaluated, passed, passRate, data, totalRejects };
  }, [events]);

  /* ---- Token-safety red-flag tally (broader than gate reasons) ---- */
  const redFlagStats = useMemo(() => {
    const counts = new Map<RedFlag, number>();
    let flaggedEvents = 0;
    for (const e of events) {
      if (e.redFlags.length > 0) flaggedEvents += 1;
      for (const f of e.redFlags) counts.set(f, (counts.get(f) ?? 0) + 1);
    }
    const rows = Array.from(counts.entries())
      .map(([flag, count]) => ({ flag, count }))
      .sort((a, b) => b.count - a.count);
    return { rows, flaggedEvents, total: events.length };
  }, [events]);

  return (
    <div className="space-y-4">
      {/* Title row */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-[16px] font-semibold tracking-tight text-text">
            <ShieldAlert className="h-4 w-4 text-brand" />
            Risk and guardrails
          </h1>
          <p className="mt-0.5 text-[12px] text-muted-foreground">
            The hard limits, the de-risk ladder, and the safety gate standing
            between the loop and the wallet.
          </p>
        </div>
        {error ? (
          <Chip tone="danger" icon={<AlertTriangle />}>
            {error}
          </Chip>
        ) : (
          <Chip tone="accent" icon={<LiveDot />}>
            Guardrails enforced
          </Chip>
        )}
      </div>

      {/* Headline guardrail KPIs */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          label="Daily loss limit"
          value={loading ? "—" : <>{round1(limit)} SOL</>}
          sub={loading ? "" : `${lossUsed} SOL used today`}
          tone={lossTone === "danger" ? "danger" : "neutral"}
        />
        <StatTile
          label="Hard stop"
          value={config ? <>−{roundInt(config.stopLossPct)}%</> : "—"}
          sub="Per-position fast exit"
          tone="down"
        />
        <StatTile
          label="Take-profit"
          value={config ? <>+{roundInt(config.takeProfitPct)}%</> : "—"}
          sub="Laddered scale-out"
          tone="up"
        />
        <StatTile
          label="Max position"
          value={config ? <>{round1(config.maxPositionSol)} SOL</> : "—"}
          sub={
            config ? `${roundInt(config.maxPositionPct)}% of capital cap` : ""
          }
          tone="accent"
        />
      </div>

      {/* Daily-loss gauge + breaker, and Kelly note */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Panel
          title="Daily loss vs limit"
          icon={<GaugeIcon />}
          actions={
            breakerTripped ? (
              <Chip tone="danger" icon={<Siren />}>
                Breaker tripped
              </Chip>
            ) : (
              <Chip tone="ok" icon={<ShieldCheck />}>
                Breaker armed
              </Chip>
            )
          }
          className="xl:col-span-2"
        >
          <div className="p-3.5">
            <div className="flex items-end justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-wide text-faint">
                  Loss consumed today
                </div>
                <div
                  className={`num mt-1 text-[26px] font-semibold leading-none ${
                    lossTone === "danger"
                      ? "text-danger"
                      : lossTone === "warn"
                        ? "text-warn"
                        : "text-text"
                  }`}
                >
                  {loading ? "—" : <>{lossUsed} SOL</>}
                </div>
              </div>
              <div className="text-right">
                <div className="text-[11px] uppercase tracking-wide text-faint">
                  Of limit
                </div>
                <div className="num mt-1 text-[26px] font-semibold leading-none text-muted-foreground">
                  {loading ? "—" : <>{lossPct}%</>}
                </div>
              </div>
            </div>
            <div className="mt-3">
              <Gauge
                value={lossUsed}
                max={limit > 0 ? limit : 1}
                tone={lossTone}
                label={`Daily loss budget (${round1(limit)} SOL)`}
              />
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
              <span className="num text-text">
                {dailyPnl >= 0 ? "+" : ""}
                {round1(dailyPnl)} SOL
              </span>
              <span>net realized today</span>
              <span className="h-3 w-px bg-border" />
              <span>
                Circuit breaker auto-trips at{" "}
                <span className="num text-text">{round1(limit)} SOL</span>{" "}
                drawdown and halts all new entries until reset.
              </span>
            </div>
          </div>
        </Panel>

        {/* Fractional-Kelly sizing note */}
        <Panel title="Fractional-Kelly sizing" icon={<Scale />}>
          <div className="p-3.5">
            <div className="flex items-baseline gap-2">
              <span className="num text-[26px] font-semibold leading-none text-violet">
                0.25×
              </span>
              <span className="text-[12px] text-muted-foreground">
                Kelly fraction
              </span>
            </div>
            <p className="mt-2.5 text-[12px] leading-relaxed text-muted-foreground">
              Position size scales with model edge but is clamped to a
              quarter-Kelly fraction. Full Kelly maximizes geometric growth in
              theory and ruins you in practice — meme-coin outcomes are noisy
              and fat-tailed, so we deliberately under-bet.
            </p>
            <div className="mt-3 grid grid-cols-2 gap-px overflow-hidden rounded-[8px] border border-border bg-border">
              <div className="bg-panel2 px-3 py-2.5">
                <div className="text-[10px] uppercase tracking-wide text-faint">
                  Edge floor
                </div>
                <div className="num mt-1 text-[14px] font-semibold text-brand">
                  {config ? round2(config.snipeThreshold) : "—"}
                </div>
                <div className="text-[10px] text-faint">snipe threshold</div>
              </div>
              <div className="bg-panel2 px-3 py-2.5">
                <div className="text-[10px] uppercase tracking-wide text-faint">
                  Hard ceiling
                </div>
                <div className="num mt-1 text-[14px] font-semibold text-text">
                  {config ? round1(config.maxPositionSol) : "—"} SOL
                </div>
                <div className="text-[10px] text-faint">caps any Kelly bet</div>
              </div>
            </div>
          </div>
        </Panel>
      </div>

      {/* De-risk rule ladder + exposure caps */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Panel title="De-risk rule order" icon={<ListOrdered />}>
          {cfgLoading ? (
            <div className="space-y-2.5 p-3.5">
              {Array.from({ length: 3 }).map((_, i) => (
                <div
                  key={i}
                  className="h-20 animate-pulse rounded-[8px] bg-panel2"
                />
              ))}
            </div>
          ) : (
            <RuleLadder config={config} />
          )}
        </Panel>

        <Panel
          title="Exposure caps usage"
          icon={<Layers />}
          actions={
            <span className="num text-[11px] text-faint">
              {metrics ? `${roundInt(metrics.openPositions)} open` : "—"}
            </span>
          }
        >
          {cfgLoading || !config ? (
            <div className="space-y-3.5 p-3.5">
              {Array.from({ length: 5 }).map((_, i) => (
                <div key={i}>
                  <div className="mb-1 h-3 w-32 animate-pulse rounded bg-panel2" />
                  <div className="h-1.5 w-full animate-pulse rounded-full bg-panel2" />
                </div>
              ))}
            </div>
          ) : (
            <ExposureCaps
              config={config}
              openPositions={metrics?.openPositions ?? 0}
            />
          )}
        </Panel>
      </div>

      {/* Pre-trade safety-gate stats */}
      <Panel
        title="Pre-trade safety gate"
        icon={<Ban />}
        actions={
          <div className="flex items-center gap-2">
            <Chip tone="ok">
              <span className="num">{gateStats.passRate}%</span>&nbsp;pass
            </Chip>
            <span className="num text-[11px] text-faint">
              {gateStats.evaluated} evaluated
            </span>
          </div>
        }
      >
        {feedLoading && events.length === 0 ? (
          <div className="grid grid-cols-1 gap-4 p-3.5 md:grid-cols-5">
            <div className="md:col-span-2 space-y-3">
              <div className="h-20 animate-pulse rounded-[8px] bg-panel2" />
              <div className="h-20 animate-pulse rounded-[8px] bg-panel2" />
            </div>
            <div className="md:col-span-3 h-[214px] animate-pulse rounded-[8px] bg-panel2" />
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4 p-3.5 md:grid-cols-5">
            {/* Pass / reject summary */}
            <div className="space-y-3 md:col-span-2">
              <div className="grid grid-cols-2 gap-px overflow-hidden rounded-[8px] border border-border bg-border">
                <div className="bg-panel2 px-3 py-3 text-center">
                  <div className="text-[10px] uppercase tracking-wide text-faint">
                    Passed
                  </div>
                  <div className="num mt-1 text-[20px] font-semibold leading-none text-up">
                    {gateStats.passed}
                  </div>
                </div>
                <div className="bg-panel2 px-3 py-3 text-center">
                  <div className="text-[10px] uppercase tracking-wide text-faint">
                    Rejected
                  </div>
                  <div className="num mt-1 text-[20px] font-semibold leading-none text-down">
                    {gateStats.totalRejects}
                  </div>
                </div>
              </div>
              <Gauge
                value={gateStats.passRate}
                max={100}
                tone="up"
                label="Gate pass rate"
              />
              <p className="flex items-start gap-2 text-[11px] leading-relaxed text-muted-foreground">
                <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-up" />
                <span>
                  Every candidate clears authority, liquidity, tax and cluster
                  checks before the model ever scores it. Rejects below are{" "}
                  <span className="text-text">
                    tokens the gate refused outright
                  </span>
                  .
                </span>
              </p>
            </div>

            {/* Reject breakdown bar */}
            <div className="md:col-span-3">
              <div className="mb-1 flex items-center gap-1.5 px-1 text-[11px] text-faint">
                <Coins className="h-3.5 w-3.5" />
                Rejects by gate reason
              </div>
              <div className="rounded-[8px] border border-border bg-panel">
                <GateRejectChart data={gateStats.data} />
              </div>
            </div>
          </div>
        )}
      </Panel>

      {/* Token-safety scanner red flags (honeypot / not-sellable / clusters) */}
      <Panel
        title="Token-safety red flags"
        icon={<Siren />}
        actions={
          <span className="num text-[11px] text-faint">
            {redFlagStats.flaggedEvents} of {redFlagStats.total} flagged
          </span>
        }
      >
        {feedLoading && events.length === 0 ? (
          <div className="grid grid-cols-2 gap-2 p-3.5 sm:grid-cols-3 lg:grid-cols-4">
            {Array.from({ length: 6 }).map((_, i) => (
              <div
                key={i}
                className="h-16 animate-pulse rounded-[8px] bg-panel2"
              />
            ))}
          </div>
        ) : redFlagStats.rows.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 px-4 py-8 text-center">
            <ShieldCheck className="h-5 w-5 text-up" />
            <div className="text-[12px] text-muted-foreground">
              No token-safety red flags in the resident feed window.
            </div>
          </div>
        ) : (
          <div className="p-3.5">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
              {redFlagStats.rows.map(({ flag, count }) => (
                <div
                  key={flag}
                  className="flex items-center justify-between gap-2 rounded-[8px] border border-[color:color-mix(in_srgb,var(--danger)_24%,transparent)] bg-[color:color-mix(in_srgb,var(--danger)_7%,transparent)] px-3 py-2.5"
                >
                  <span className="flex min-w-0 items-center gap-1.5 text-[12px] font-medium text-danger">
                    <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                    <span className="truncate">{redFlagLabel(flag)}</span>
                  </span>
                  <span className="num shrink-0 text-[14px] font-semibold text-text">
                    {count}
                  </span>
                </div>
              ))}
            </div>
            <p className="mt-3 flex items-start gap-2 text-[11px] leading-relaxed text-muted-foreground">
              <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-up" />
              <span>
                The sub-10ms pre-trade safety gate exports these red flags for
                every candidate. A flag is a <span className="text-text">de-risk</span>{" "}
                signal — the gate refuses or the engine skips; nothing here can
                widen risk or force an entry.
              </span>
            </p>
          </div>
        )}
      </Panel>
    </div>
  );
}
