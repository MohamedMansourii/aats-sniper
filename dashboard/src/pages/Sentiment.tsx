import { useMemo } from "react";
import {
  AlertTriangle,
  Bot,
  Flame,
  Megaphone,
  MessageSquareText,
  Quote,
  ShieldAlert,
  Sparkles,
  TrendingUp,
} from "lucide-react";
import {
  Cell,
  Pie,
  PieChart,
  PolarAngleAxis,
  RadialBar,
  RadialBarChart,
  ResponsiveContainer,
} from "recharts";
import Layout from "@/components/Layout";
import { Chip, LiveDot, Panel, StatTile } from "@/components/kit";
import { useSentiment } from "@/lib/api";
import type { MCSScore } from "@/lib/types";

/* -------------------------------------------------------------------------- */
/*  Palette — recharts paints on canvas and cannot read CSS vars, so colors    */
/*  are hardcoded to match src/index.css. Keep in sync with the theme.         */
/* -------------------------------------------------------------------------- */

const HEX = {
  up: "#22e39a",
  down: "#ff5d5d",
  warn: "#ffb020",
  info: "#4aa3ff",
  violet: "#8b7bff",
  faint: "#646e7e",
  border: "#1e242c",
  panel2: "#171b21",
} as const;

/* -------------------------------------------------------------------------- */
/*  MCS model                                                                  */
/*  Raw components are 0..1. The diverging Market Conviction Score is −1..1.   */
/*  Synchronicity is treated as a CONTRARIAN risk above a threshold: a tightly */
/*  coordinated shill is manufactured hype, not organic conviction, so it      */
/*  PENALISES the score. Red flags subtract further. This makes engineered     */
/*  hype read red, never bullish.                                              */
/* -------------------------------------------------------------------------- */

/** Synchronicity above this reads as coordinated manufacturing, not organic. */
const SYNC_DANGER = 0.6;
const round2 = (n: number) => Math.round(n * 100) / 100;
const pct0 = (n: number) => Math.round(n * 100);

interface DerivedMCS {
  /** Composite signed conviction, −1..1, rounded for display. */
  mcs: number;
  /** How much coordinated-shill synchronicity dragged the score down (0..1). */
  syncPenalty: number;
  /** True once synchronicity crosses into manufactured-hype territory. */
  manufactured: boolean;
}

function deriveMCS(s: MCSScore): DerivedMCS {
  // Organic bullish drivers (conviction-weighted, momentum + novelty support).
  const organic = 0.5 * s.conviction + 0.3 * s.momentum + 0.2 * s.novelty;

  // Synchronicity only counts against us once it crosses the danger line:
  // coordinated posting = manufactured attention, a contrarian short signal.
  const excessSync = Math.max(0, s.synchronicity - SYNC_DANGER);
  const syncPenalty = (excessSync / (1 - SYNC_DANGER)) * 0.7;

  // Each red flag chips away conviction (capped so flags can flip the sign).
  const flagPenalty = Math.min(0.6, s.redFlags.length * 0.22);

  // Map organic 0..1 onto −1..1, then subtract the manufactured-hype penalties.
  const raw = (organic * 2 - 1) - syncPenalty - flagPenalty;
  const mcs = Math.max(-1, Math.min(1, raw));

  return {
    mcs: round2(mcs),
    syncPenalty: round2(syncPenalty),
    manufactured: s.synchronicity >= SYNC_DANGER,
  };
}

function mcsVerdict(mcs: number): { label: string; tone: "ok" | "warn" | "danger" | "neutral" } {
  if (mcs >= 0.4) return { label: "Conviction", tone: "ok" };
  if (mcs >= 0.1) return { label: "Lean long", tone: "neutral" };
  if (mcs > -0.1) return { label: "Neutral", tone: "neutral" };
  if (mcs > -0.4) return { label: "Caution", tone: "warn" };
  return { label: "Contrarian", tone: "danger" };
}

/** Pretty-print a red-flag slug: bot_cluster → Bot cluster. */
function flagLabel(slug: string): string {
  const s = slug.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/* -------------------------------------------------------------------------- */
/*  Diverging MCS gauge (−1..1) — custom thin bar centred at zero.             */
/* -------------------------------------------------------------------------- */

function DivergingGauge({ value }: { value: number }) {
  const clamped = Math.max(-1, Math.min(1, value));
  // Half-width of the fill, as a % of the full track.
  const half = Math.abs(clamped) * 50;
  const positive = clamped >= 0;
  const color = positive ? HEX.up : HEX.down;

  return (
    <div className="w-full">
      <div className="relative h-2 w-full overflow-hidden rounded-full bg-panel2">
        {/* zero baseline */}
        <span className="absolute left-1/2 top-0 h-full w-px -translate-x-1/2 bg-[color:var(--faint)]" />
        <div
          className="absolute top-0 h-full transition-[width,left] duration-500 ease-out"
          style={{
            backgroundColor: color,
            left: positive ? "50%" : `${50 - half}%`,
            width: `${half}%`,
          }}
        />
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-faint">
        <span className="num">-1.0</span>
        <span className="num">0</span>
        <span className="num">+1.0</span>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Sub-bar (conviction / momentum / novelty) — labelled 0..1 bar.             */
/* -------------------------------------------------------------------------- */

function SubBar({
  label,
  value,
  color,
}: {
  label: string;
  value: number;
  color: string;
}) {
  const pct = Math.max(0, Math.min(100, value * 100));
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-[11px] text-muted-foreground">{label}</span>
        <span className="num text-[11px] text-text">{value.toFixed(2)}</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-panel2">
        <div
          className="h-full rounded-full transition-[width] duration-500 ease-out"
          style={{ width: `${pct}%`, backgroundColor: color }}
        />
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Synchronicity meter — radial gauge. HIGH is a RED contrarian warning.      */
/* -------------------------------------------------------------------------- */

function SyncMeter({ value }: { value: number }) {
  const pct = pct0(value);
  const danger = value >= SYNC_DANGER;
  const color = danger ? HEX.down : value >= 0.4 ? HEX.warn : HEX.faint;
  const data = [{ name: "sync", value: pct }];

  return (
    <div className="relative h-[112px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <RadialBarChart
          data={data}
          innerRadius="74%"
          outerRadius="100%"
          startAngle={90}
          endAngle={-270}
          barSize={9}
        >
          <PolarAngleAxis type="number" domain={[0, 100]} tick={false} />
          <RadialBar
            dataKey="value"
            cornerRadius={6}
            fill={color}
            background={{ fill: HEX.panel2 }}
            isAnimationActive={false}
          />
        </RadialBarChart>
      </ResponsiveContainer>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <span
          className="num text-[20px] font-semibold leading-none"
          style={{ color }}
        >
          {pct}
          <span className="text-[11px] text-faint">%</span>
        </span>
        <span className="mt-1 text-[10px] uppercase tracking-wide text-faint">
          sync
        </span>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Bot-vs-organic split donut — derived from synchronicity + red flags.       */
/* -------------------------------------------------------------------------- */

function OriginSplit({ score }: { score: MCSScore }) {
  // Estimate the inorganic share: synchronicity carries it, a detected
  // bot_cluster flag floors it higher. Display-only heuristic.
  const botFlag = score.redFlags.includes("bot_cluster");
  const inorganic = Math.min(
    0.92,
    Math.max(0.04, score.synchronicity * 0.7 + (botFlag ? 0.25 : 0)),
  );
  const organic = 1 - inorganic;
  const data = [
    { name: "Organic", value: pct0(organic), color: HEX.info },
    { name: "Coordinated", value: pct0(inorganic), color: HEX.down },
  ];

  return (
    <div className="flex items-center gap-3">
      <div className="relative h-[88px] w-[88px] shrink-0">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={data}
              dataKey="value"
              innerRadius={28}
              outerRadius={42}
              startAngle={90}
              endAngle={-270}
              stroke="none"
              isAnimationActive={false}
            >
              {data.map((d) => (
                <Cell key={d.name} fill={d.color} />
              ))}
            </Pie>
          </PieChart>
        </ResponsiveContainer>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <span className="num text-[14px] font-semibold leading-none text-down">
            {pct0(inorganic)}%
          </span>
          <span className="text-[9px] uppercase tracking-wide text-faint">coord</span>
        </div>
      </div>
      <div className="space-y-1.5">
        {data.map((d) => (
          <div key={d.name} className="flex items-center gap-2">
            <span
              className="h-2 w-2 rounded-[2px]"
              style={{ backgroundColor: d.color }}
            />
            <span className="text-[11px] text-muted-foreground">{d.name}</span>
            <span className="num text-[11px] text-text">{d.value}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Per-asset card                                                             */
/* -------------------------------------------------------------------------- */

function AssetCard({ score }: { score: MCSScore }) {
  const { mcs, manufactured } = deriveMCS(score);
  const verdict = mcsVerdict(mcs);
  const hasFlags = score.redFlags.length > 0;

  return (
    <Panel className="overflow-hidden">
      {/* Header row */}
      <div className="flex items-start justify-between gap-3 border-b border-border px-3.5 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate text-[14px] font-semibold tracking-tight text-text">
              {score.asset}
            </span>
            <Chip tone={verdict.tone}>{verdict.label}</Chip>
          </div>
          <div className="mt-1 flex items-center gap-1.5 text-[11px] text-faint">
            <MessageSquareText className="h-3 w-3" />
            <span className="num text-muted-foreground">
              {score.postCount.toLocaleString("en-US")}
            </span>
            <span>posts analysed</span>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="text-[10px] uppercase tracking-wide text-faint">MCS</div>
          <div
            className="num text-[26px] font-semibold leading-none"
            style={{ color: mcs >= 0.1 ? HEX.up : mcs <= -0.1 ? HEX.down : HEX.faint }}
          >
            {mcs > 0 ? "+" : ""}
            {mcs.toFixed(2)}
          </div>
        </div>
      </div>

      <div className="space-y-3.5 p-3.5">
        {/* Diverging MCS gauge */}
        <DivergingGauge value={mcs} />

        {/* Manufactured-hype banner — the headline risk message */}
        {manufactured && (
          <div className="flex items-start gap-2 rounded-md border border-[color:color-mix(in_srgb,var(--danger)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--danger)_10%,transparent)] px-2.5 py-2">
            <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" />
            <p className="text-[11px] leading-snug text-down">
              High synchronicity ({pct0(score.synchronicity)}%) reads as a coordinated
              shill — manufactured hype, treated as a contrarian risk, not a buy.
            </p>
          </div>
        )}

        {/* Sub-bars + synchronicity meter */}
        <div className="grid grid-cols-[1fr_auto] gap-4">
          <div className="space-y-2.5">
            <SubBar label="Conviction" value={score.conviction} color={HEX.up} />
            <SubBar label="Momentum" value={score.momentum} color={HEX.info} />
            <SubBar label="Novelty" value={score.novelty} color={HEX.violet} />
          </div>
          <div className="flex w-[120px] flex-col items-center">
            <SyncMeter value={score.synchronicity} />
            <span
              className={
                manufactured
                  ? "mt-0.5 text-center text-[10px] font-medium text-down"
                  : "mt-0.5 text-center text-[10px] text-faint"
              }
            >
              {manufactured ? "Coordinated — contrarian" : "Organic spread"}
            </span>
          </div>
        </div>

        {/* Origin split */}
        <div className="rounded-md border border-border bg-panel2 p-3">
          <OriginSplit score={score} />
        </div>

        {/* Red flags */}
        <div>
          <div className="mb-1.5 flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-faint">
            <AlertTriangle className="h-3 w-3" />
            Red flags
          </div>
          {hasFlags ? (
            <div className="flex flex-wrap gap-1.5">
              {score.redFlags.map((flag) => (
                <Chip key={flag} tone="danger" icon={<AlertTriangle />}>
                  {flagLabel(flag)}
                </Chip>
              ))}
            </div>
          ) : (
            <Chip tone="ok">None detected</Chip>
          )}
        </div>

        {/* Reasoning */}
        <div className="rounded-md border border-border bg-panel2 px-3 py-2.5">
          <div className="mb-1 flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-faint">
            <Quote className="h-3 w-3" />
            Narrative read
          </div>
          <p className="text-[12px] leading-relaxed text-muted-foreground">
            {score.reasoning}
          </p>
        </div>
      </div>
    </Panel>
  );
}

/* -------------------------------------------------------------------------- */
/*  Loading / error / empty states                                             */
/* -------------------------------------------------------------------------- */

function CardSkeleton() {
  return (
    <Panel className="overflow-hidden">
      <div className="border-b border-border px-3.5 py-3">
        <div className="h-4 w-24 animate-pulse rounded bg-panel2" />
      </div>
      <div className="space-y-3 p-3.5">
        <div className="h-2 w-full animate-pulse rounded-full bg-panel2" />
        <div className="h-2 w-full animate-pulse rounded-full bg-panel2" />
        <div className="h-2 w-3/4 animate-pulse rounded-full bg-panel2" />
        <div className="h-16 w-full animate-pulse rounded-md bg-panel2" />
      </div>
    </Panel>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                       */
/* -------------------------------------------------------------------------- */

export default function Sentiment() {
  const { data, loading, error } = useSentiment();
  const scores = data ?? [];

  // Roll up the watchlist for the header strip.
  const summary = useMemo(() => {
    if (scores.length === 0) return null;
    const derived = scores.map(deriveMCS);
    const avg = derived.reduce((a, d) => a + d.mcs, 0) / derived.length;
    const manufactured = derived.filter((d) => d.manufactured).length;
    const flagged = scores.filter((s) => s.redFlags.length > 0).length;
    const posts = scores.reduce((a, s) => a + s.postCount, 0);
    return {
      avg: round2(avg),
      manufactured,
      flagged,
      posts,
    };
  }, [scores]);

  return (
    <Layout>
      <div className="space-y-4">
        {/* Page heading */}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-[16px] font-semibold tracking-tight text-text">
              <Megaphone className="h-4 w-4 text-brand" />
              Market conviction score
            </h1>
            <p className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <LiveDot tone="warn" />
              Slow-loop signal — refreshed on the 2s narrative cadence, not the snipe loop.
            </p>
          </div>
          <Chip tone="violet" icon={<Sparkles />}>
            Narrative engine
          </Chip>
        </div>

        {/* Summary strip */}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatTile
            label="Watched assets"
            value={loading && scores.length === 0 ? "—" : scores.length}
            sub="on the conviction board"
          />
          <StatTile
            label="Mean MCS"
            value={
              summary ? `${summary.avg > 0 ? "+" : ""}${summary.avg.toFixed(2)}` : "—"
            }
            sub="−1.0 contrarian · +1.0 conviction"
            tone={summary ? (summary.avg >= 0.1 ? "up" : summary.avg <= -0.1 ? "down" : "neutral") : "neutral"}
          />
          <StatTile
            label="Manufactured hype"
            value={summary ? summary.manufactured : "—"}
            sub="coordinated-shill flags"
            tone={summary && summary.manufactured > 0 ? "danger" : "neutral"}
          />
          <StatTile
            label="Posts analysed"
            value={summary ? summary.posts.toLocaleString("en-US") : "—"}
            sub={summary ? `${summary.flagged} asset(s) with red flags` : "across the watchlist"}
            tone="info"
          />
        </div>

        {/* How synchronicity is read — make the contrarian framing explicit */}
        <Panel className="overflow-hidden">
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 px-3.5 py-2.5 text-[11px]">
            <span className="flex items-center gap-1.5 text-faint">
              <Flame className="h-3 w-3 text-brand" />
              How MCS reads the room
            </span>
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <TrendingUp className="h-3 w-3 text-up" />
              Conviction · momentum · novelty drive the score up
            </span>
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <Bot className="h-3 w-3 text-down" />
              High synchronicity = coordinated shill → drives it{" "}
              <span className="font-medium text-down">down</span> (contrarian)
            </span>
          </div>
        </Panel>

        {/* Error state */}
        {error && (
          <Panel className="overflow-hidden">
            <div className="flex items-center gap-2 px-3.5 py-6 text-[12px] text-down">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              Failed to load sentiment: {error}
            </div>
          </Panel>
        )}

        {/* Loading skeletons */}
        {loading && scores.length === 0 && !error && (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            <CardSkeleton />
            <CardSkeleton />
            <CardSkeleton />
            <CardSkeleton />
          </div>
        )}

        {/* Empty state */}
        {!loading && !error && scores.length === 0 && (
          <Panel className="overflow-hidden">
            <div className="flex flex-col items-center gap-2 px-3.5 py-12 text-center">
              <MessageSquareText className="h-6 w-6 text-faint" />
              <p className="text-[12px] text-muted-foreground">
                No assets on the conviction board.
              </p>
              <p className="text-[11px] text-faint">
                The narrative engine has nothing to score yet.
              </p>
            </div>
          </Panel>
        )}

        {/* Asset cards */}
        {scores.length > 0 && (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {scores.map((score) => (
              <AssetCard key={score.asset} score={score} />
            ))}
          </div>
        )}
      </div>
    </Layout>
  );
}
