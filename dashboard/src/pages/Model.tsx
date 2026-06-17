import { useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Activity,
  Brain,
  CircleSlash,
  Gauge as GaugeIcon,
  Layers,
  ShieldCheck,
  Sigma,
  TrendingUp,
} from "lucide-react";
import {
  Chip,
  Gauge,
  LiveDot,
  Panel,
  StatTile,
  type ChipTone,
} from "@/components/kit";
import { usePredictions, useMetrics, useRiskConfig } from "@/lib/api";
import { round1, round2, roundInt } from "@/lib/mock";
import type { Prediction } from "@/lib/types";
import { CHART as HEX } from "@/lib/chart-colors";

const chartTooltip = {
  backgroundColor: HEX.panel2,
  border: `1px solid ${HEX.border}`,
  borderRadius: 8,
  fontSize: 11,
  color: HEX.text,
  fontFamily:
    'ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace',
} as const;

/* -------------------------------------------------------------------------- */
/*  Derived helpers                                                           */
/* -------------------------------------------------------------------------- */

/** Mean absolute calibration error (lower = better calibrated). */
function calibrationError(bins: Prediction["calibrationBins"]): number {
  if (bins.length === 0) return 0;
  const sum = bins.reduce((acc, b) => acc + Math.abs(b.p - b.actual), 0);
  return sum / bins.length;
}

const pct = (p: number) => roundInt(p * 100);

/* -------------------------------------------------------------------------- */
/*  Loading / error scaffolds                                                 */
/* -------------------------------------------------------------------------- */

function LoadingBlock({ label }: { label: string }) {
  return (
    <div className="flex h-[200px] items-center justify-center text-[11px] text-faint">
      <span className="animate-pulse">{label}</span>
    </div>
  );
}

function ErrorBlock({ message }: { message: string }) {
  return (
    <div className="flex h-[200px] flex-col items-center justify-center gap-1.5 text-center">
      <CircleSlash className="h-4 w-4 text-danger" />
      <span className="text-[12px] font-medium text-text">Failed to load</span>
      <span className="text-[11px] text-muted-foreground">{message}</span>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                      */
/* -------------------------------------------------------------------------- */

export default function Model() {
  const {
    data: pred,
    loading: predLoading,
    error: predError,
  } = usePredictions();
  const { data: metrics } = useMetrics();
  const { config } = useRiskConfig();

  const snipeThreshold = config?.snipeThreshold ?? 0.62;
  const vetoThreshold = config?.vetoThreshold ?? 0.35;

  // Edge over the naive baseline. The model is auto-disabled the moment it
  // stops beating baseline (edge <= 0) — a guardrail against model drift.
  const edge = metrics?.edgeVsBaselinePct ?? null;
  const beatingBaseline = edge === null ? true : edge > 0;

  const calData = useMemo(
    () =>
      (pred?.calibrationBins ?? []).map(b => ({
        predicted: pct(b.p),
        actual: pct(b.actual),
      })),
    [pred]
  );
  const calErr = pred
    ? round2(calibrationError(pred.calibrationBins) * 100)
    : 0;

  const featData = useMemo(
    () =>
      (pred?.featureImportance ?? [])
        .slice()
        .sort((a, b) => b.weight - a.weight)
        .map(f => ({ name: f.name, weight: roundInt(f.weight * 100) })),
    [pred?.featureImportance]
  );

  const classifierPct = pred ? pct(pred.classifierP) : null;
  const baselinePct = pred ? pct(pred.baselineP) : null;
  const liftPct =
    pred && pred.baselineP > 0
      ? round1(((pred.classifierP - pred.baselineP) / pred.baselineP) * 100)
      : null;

  // Naive baseline vs calibrated classifier hit-rate comparison.
  const hitRateData =
    classifierPct !== null && baselinePct !== null
      ? [
          { name: "Naive baseline", rate: baselinePct, kind: "baseline" },
          { name: "Snipe classifier", rate: classifierPct, kind: "model" },
        ]
      : [];

  // Where does the current probability land relative to the action thresholds?
  let verdictTone: ChipTone = "neutral";
  let verdictLabel = "Hold band";
  if (pred) {
    if (pred.classifierP >= snipeThreshold) {
      verdictTone = "accent";
      verdictLabel = "Above snipe threshold";
    } else if (pred.classifierP < vetoThreshold) {
      verdictTone = "danger";
      verdictLabel = "Below veto threshold";
    } else {
      verdictTone = "warn";
      verdictLabel = "Hold band";
    }
  }

  return (
    <div className="space-y-4">
      {/* ---- Page header ---- */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <span className="flex h-7 w-7 items-center justify-center rounded-md border border-[color:color-mix(in_srgb,var(--violet)_30%,transparent)] bg-[color:color-mix(in_srgb,var(--violet)_12%,transparent)] text-violet">
            <Brain className="h-4 w-4" />
          </span>
          <div className="leading-tight">
            <h1 className="text-[15px] font-semibold tracking-tight text-text">
              Predictive core
            </h1>
            <p className="text-[11px] text-muted-foreground">
              Snipe classifier, calibration and baseline edge
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <Chip tone="violet" icon={<Sigma />}>
            survivor-TFT v3
          </Chip>
          {beatingBaseline ? (
            <Chip tone="accent" icon={<ShieldCheck />}>
              Active · beating baseline
            </Chip>
          ) : (
            <Chip tone="danger" icon={<CircleSlash />}>
              Auto-disabled · below baseline
            </Chip>
          )}
          <span className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <LiveDot tone={beatingBaseline ? "accent" : "danger"} />
            Live
          </span>
        </div>
      </div>

      {/* ---- Auto-disable guardrail banner ---- */}
      {!beatingBaseline && (
        <div className="flex items-start gap-2.5 rounded-[10px] border border-[color:color-mix(in_srgb,var(--danger)_30%,transparent)] bg-[color:color-mix(in_srgb,var(--danger)_10%,transparent)] px-3.5 py-2.5">
          <CircleSlash className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" />
          <div className="text-[11px] leading-relaxed text-text">
            Model edge fell to or below the naive baseline. The classifier has
            been{" "}
            <span className="font-semibold text-danger">auto-disabled</span> and
            the agent has reverted to gate-only sniping until edge recovers.
          </div>
        </div>
      )}

      {/* ---- KPI row ---- */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          label="Snipe probability"
          value={classifierPct !== null ? `${classifierPct}%` : "—"}
          sub={
            classifierPct !== null
              ? `Snipe ≥ ${pct(snipeThreshold)} · veto < ${pct(vetoThreshold)}`
              : "loading"
          }
          tone={verdictTone === "accent" ? "accent" : "neutral"}
        />
        <StatTile
          label="Naive baseline"
          value={baselinePct !== null ? `${baselinePct}%` : "—"}
          sub="prior-only coin-flip model"
          tone="neutral"
        />
        <StatTile
          label="Edge vs baseline"
          value={edge !== null ? `${edge >= 0 ? "+" : ""}${edge}%` : "—"}
          sub="rolling realized lift"
          tone={beatingBaseline ? "up" : "down"}
          delta={liftPct ?? undefined}
        />
        <StatTile
          label="Calibration error"
          value={pred ? `${calErr}%` : "—"}
          sub="mean |predicted − actual|"
          tone={calErr <= 4 ? "up" : calErr <= 8 ? "warn" : "down"}
        />
      </div>

      {/* ---- Probability + threshold band + baseline comparison ---- */}
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-3">
        {/* Big classifier probability */}
        <Panel
          title="Live classifier probability"
          icon={<GaugeIcon />}
          actions={<Chip tone={verdictTone}>{verdictLabel}</Chip>}
        >
          {predLoading && !pred ? (
            <LoadingBlock label="reading classifier…" />
          ) : predError ? (
            <ErrorBlock message={predError} />
          ) : pred ? (
            <div className="flex flex-col gap-4">
              <div className="flex items-end gap-2">
                <span
                  className="num text-[56px] font-semibold leading-none tracking-tight"
                  style={{
                    color:
                      verdictTone === "accent"
                        ? HEX.accent
                        : verdictTone === "danger"
                          ? HEX.down
                          : verdictTone === "warn"
                            ? HEX.warn
                            : HEX.text,
                  }}
                >
                  {classifierPct}
                </span>
                <span className="num mb-1.5 text-[20px] font-medium text-faint">
                  %
                </span>
                <span className="num mb-2 ml-auto inline-flex items-center gap-1 text-[12px] text-muted-foreground">
                  <TrendingUp className="h-3.5 w-3.5 text-up" />
                  p(land · 2x in {config?.maxHoldMin ?? 240}m)
                </span>
              </div>

              {/* Threshold band visualization */}
              <div>
                <div className="mb-1.5 flex items-center justify-between text-[11px] text-muted-foreground">
                  <span>Action band</span>
                  <span className="num text-faint">
                    veto {pct(vetoThreshold)} · snipe {pct(snipeThreshold)}
                  </span>
                </div>
                <div className="relative h-2.5 w-full overflow-hidden rounded-full bg-panel2">
                  {/* veto / hold / snipe zones */}
                  <div
                    className="absolute inset-y-0 left-0"
                    style={{
                      width: `${pct(vetoThreshold)}%`,
                      background:
                        "color-mix(in srgb, var(--danger) 22%, transparent)",
                    }}
                  />
                  <div
                    className="absolute inset-y-0"
                    style={{
                      left: `${pct(snipeThreshold)}%`,
                      right: 0,
                      background:
                        "color-mix(in srgb, var(--up) 22%, transparent)",
                    }}
                  />
                  {/* current p marker */}
                  <div
                    className="absolute inset-y-0 w-0.5 bg-text"
                    style={{ left: `calc(${classifierPct}% - 1px)` }}
                    aria-hidden
                  />
                </div>
                <div className="mt-1 flex justify-between text-[10px] text-faint">
                  <span>0</span>
                  <span>50</span>
                  <span>100</span>
                </div>
              </div>

              <div className="grid grid-cols-3 gap-2 border-t border-border pt-3 text-center">
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-faint">
                    Veto
                  </div>
                  <div className="num text-[14px] font-semibold text-danger">
                    &lt;{pct(vetoThreshold)}%
                  </div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-faint">
                    Hold
                  </div>
                  <div className="num text-[14px] font-semibold text-warn">
                    {pct(vetoThreshold)}–{pct(snipeThreshold)}%
                  </div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-faint">
                    Snipe
                  </div>
                  <div className="num text-[14px] font-semibold text-up">
                    ≥{pct(snipeThreshold)}%
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <ErrorBlock message="no prediction data" />
          )}
        </Panel>

        {/* Calibration reliability curve */}
        <Panel
          title="Calibration reliability"
          icon={<Activity />}
          actions={
            <Chip tone={calErr <= 4 ? "ok" : calErr <= 8 ? "warn" : "danger"}>
              {calErr}% MAE
            </Chip>
          }
          className="xl:col-span-2"
        >
          {predLoading && !pred ? (
            <LoadingBlock label="building reliability curve…" />
          ) : predError ? (
            <ErrorBlock message={predError} />
          ) : calData.length > 0 ? (
            <>
              <div className="h-[224px]">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart
                    data={calData}
                    margin={{ top: 6, right: 12, bottom: 6, left: -10 }}
                  >
                    <CartesianGrid
                      stroke={HEX.border}
                      strokeDasharray="3 3"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="predicted"
                      type="number"
                      domain={[0, 100]}
                      ticks={[0, 20, 40, 60, 80, 100]}
                      tick={{ fontSize: 10, fill: HEX.faint }}
                      stroke={HEX.border}
                      tickLine={false}
                      unit="%"
                    />
                    <YAxis
                      type="number"
                      domain={[0, 100]}
                      ticks={[0, 20, 40, 60, 80, 100]}
                      tick={{ fontSize: 10, fill: HEX.faint }}
                      stroke={HEX.border}
                      tickLine={false}
                      unit="%"
                    />
                    <Tooltip
                      contentStyle={chartTooltip}
                      labelStyle={{ color: HEX.muted }}
                      labelFormatter={v => `predicted ${v}%`}
                      formatter={(value: number, name: string) => [
                        `${value}%`,
                        name === "actual" ? "observed" : "ideal",
                      ]}
                    />
                    {/* Ideal diagonal (perfect calibration) */}
                    <Line
                      type="linear"
                      dataKey="predicted"
                      name="ideal"
                      stroke={HEX.faint}
                      strokeWidth={1}
                      strokeDasharray="4 4"
                      dot={false}
                      isAnimationActive={false}
                    />
                    {/* Observed reliability */}
                    <Line
                      type="monotone"
                      dataKey="actual"
                      name="actual"
                      stroke={HEX.violet}
                      strokeWidth={2}
                      dot={{ r: 2.5, fill: HEX.violet, strokeWidth: 0 }}
                      activeDot={{ r: 4 }}
                      isAnimationActive={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
                <span className="inline-flex items-center gap-1.5">
                  <span
                    className="inline-block h-0.5 w-4 rounded"
                    style={{ backgroundColor: HEX.violet }}
                  />
                  Observed hit-rate
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <span
                    className="inline-block h-0 w-4 border-t border-dashed"
                    style={{ borderColor: HEX.faint }}
                  />
                  Ideal (y = x)
                </span>
                <span className="ml-auto text-faint">
                  Above line = under-confident · below = over-confident
                </span>
              </div>
            </>
          ) : (
            <ErrorBlock message="no calibration bins" />
          )}
        </Panel>
      </div>

      {/* ---- Baseline hit-rate comparison + feature importance ---- */}
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-3">
        {/* Model vs naive baseline */}
        <Panel title="Model vs naive baseline" icon={<Sigma />}>
          {predLoading && !pred ? (
            <LoadingBlock label="comparing hit-rates…" />
          ) : predError ? (
            <ErrorBlock message={predError} />
          ) : hitRateData.length > 0 ? (
            <>
              <div className="h-[160px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={hitRateData}
                    margin={{ top: 6, right: 12, bottom: 0, left: -16 }}
                  >
                    <CartesianGrid
                      stroke={HEX.border}
                      strokeDasharray="3 3"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="name"
                      tick={{ fontSize: 10, fill: HEX.faint }}
                      stroke={HEX.border}
                      tickLine={false}
                    />
                    <YAxis
                      domain={[0, 100]}
                      ticks={[0, 25, 50, 75, 100]}
                      tick={{ fontSize: 10, fill: HEX.faint }}
                      stroke={HEX.border}
                      tickLine={false}
                      unit="%"
                    />
                    <Tooltip
                      cursor={{ fill: "rgba(255,255,255,0.03)" }}
                      contentStyle={chartTooltip}
                      labelStyle={{ color: HEX.muted }}
                      formatter={(value: number) => [`${value}%`, "hit-rate"]}
                    />
                    <Bar dataKey="rate" radius={[3, 3, 0, 0]} maxBarSize={64}>
                      {hitRateData.map(d => (
                        <Cell
                          key={d.name}
                          fill={d.kind === "model" ? HEX.accent : HEX.faint}
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="mt-2 flex items-center justify-between border-t border-border pt-2.5 text-[11px]">
                <span className="text-muted-foreground">Relative lift</span>
                <span
                  className="num font-semibold"
                  style={{
                    color:
                      liftPct !== null && liftPct >= 0 ? HEX.accent : HEX.down,
                  }}
                >
                  {liftPct !== null
                    ? `${liftPct >= 0 ? "+" : ""}${liftPct}%`
                    : "—"}
                </span>
              </div>
            </>
          ) : (
            <ErrorBlock message="no comparison data" />
          )}
        </Panel>

        {/* Feature importance */}
        <Panel
          title="Feature importance"
          icon={<Layers />}
          className="xl:col-span-2"
          actions={
            <span className="num text-[11px] text-faint">
              {featData.length} features
            </span>
          }
        >
          {predLoading && !pred ? (
            <LoadingBlock label="ranking features…" />
          ) : predError ? (
            <ErrorBlock message={predError} />
          ) : featData.length > 0 ? (
            <div className="space-y-2">
              {featData.map(f => (
                <div key={f.name} className="flex items-center gap-3">
                  <span className="w-44 shrink-0 truncate text-[12px] text-muted-foreground">
                    {f.name}
                  </span>
                  <div className="flex-1">
                    <Gauge
                      value={f.weight}
                      max={featData[0].weight}
                      tone="info"
                    />
                  </div>
                  <span className="num w-10 shrink-0 text-right text-[12px] font-medium text-text">
                    {f.weight}%
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <ErrorBlock message="no feature attributions" />
          )}
        </Panel>
      </div>

      {/* ---- Survivor-TFT architecture note ---- */}
      <Panel title="Architecture note" icon={<Brain />}>
        <div className="flex flex-col gap-3 md:flex-row md:items-start">
          <div className="flex-1 space-y-1.5 text-[12px] leading-relaxed text-muted-foreground">
            <p>
              The snipe classifier is a{" "}
              <span className="font-medium text-violet">
                survivor-corrected Temporal Fusion Transformer
              </span>{" "}
              (survivor-TFT). It models the conditional probability that a
              freshly-launched token both{" "}
              <span className="text-text">lands</span> the entry and reaches the
              first take-profit before the hard stop, conditioned on gate
              features, on-chain flow and social synchronicity.
            </p>
            <p>
              Survivor correction reweights training samples to undo the
              survivorship bias of only observing tokens that did not instantly
              rug — without it, calibration drifts optimistic in the{" "}
              <span className="num text-text">60–90%</span> band.
            </p>
            <p className="text-faint">
              Outputs are isotonic-calibrated, so the reliability curve above is
              the contract: a predicted {pct(snipeThreshold)}% should land{" "}
              {pct(snipeThreshold)}% of the time. The model is{" "}
              <span className="font-medium text-text">
                auto-disabled the moment its rolling edge stops beating the
                naive baseline
              </span>
              , reverting the agent to gate-only sniping.
            </p>
          </div>
          <div className="grid w-full shrink-0 grid-cols-2 gap-2 md:w-64">
            <div className="rounded-md border border-border bg-panel2 px-2.5 py-2">
              <div className="text-[10px] uppercase tracking-wide text-faint">
                Quantile heads
              </div>
              <div className="num text-[13px] font-semibold text-text">
                p10 · p50 · p90
              </div>
            </div>
            <div className="rounded-md border border-border bg-panel2 px-2.5 py-2">
              <div className="text-[10px] uppercase tracking-wide text-faint">
                Calibration
              </div>
              <div className="num text-[13px] font-semibold text-text">
                isotonic
              </div>
            </div>
            <div className="rounded-md border border-border bg-panel2 px-2.5 py-2">
              <div className="text-[10px] uppercase tracking-wide text-faint">
                Inference
              </div>
              <div className="num text-[13px] font-semibold text-up">
                ~0.5 ms
              </div>
            </div>
            <div className="rounded-md border border-border bg-panel2 px-2.5 py-2">
              <div className="text-[10px] uppercase tracking-wide text-faint">
                Guardrail
              </div>
              <div
                className="num text-[13px] font-semibold"
                style={{ color: beatingBaseline ? HEX.accent : HEX.down }}
              >
                {beatingBaseline ? "armed" : "tripped"}
              </div>
            </div>
          </div>
        </div>
      </Panel>
    </div>
  );
}
