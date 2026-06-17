import { useMemo, type ReactNode } from "react";
import {
  AlertTriangle,
  BadgeCheck,
  Clock,
  Info,
  Newspaper,
  Quote,
  Radio,
  ShieldAlert,
  ShieldCheck,
  TrendingDown,
} from "lucide-react";
import { Chip, LiveDot, Panel, StatTile } from "@/components/kit";
import type { ChipTone } from "@/components/kit";
import { useNarrative } from "@/lib/api";
import type {
  NarrativeSignal,
  NarrativeSource,
  NewsCredibilityTier,
  RedFlag,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/* -------------------------------------------------------------------------- */
/*  Formatting                                                                */
/* -------------------------------------------------------------------------- */

/** ms-since → compact freshness string. Null window → em-dash. */
function fmtFreshness(ms: number | null): string {
  if (ms === null) return "—";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m ago`;
}

/** Pretty-print a red-flag slug: breaking_negative_news → Breaking negative news. */
function flagLabel(slug: RedFlag): string {
  const s = slug.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

const TIER_META: Record<
  NewsCredibilityTier,
  { label: string; tone: ChipTone }
> = {
  tier_1: { label: "Tier 1", tone: "info" },
  tier_2: { label: "Tier 2", tone: "neutral" },
  tier_3: { label: "Tier 3", tone: "neutral" },
};

/* -------------------------------------------------------------------------- */
/*  Narrative-score model — DE-RISK ONLY ([-1, 0], never a win-rate).          */
/*  Score is the news contribution to conviction. It can only LOWER it.        */
/* -------------------------------------------------------------------------- */

function scoreVerdict(score: number): { label: string; tone: ChipTone } {
  // score is in [-1, 0]; 0 = no impact, more negative = stronger de-risk.
  if (score <= -0.5) return { label: "Narrative collapse", tone: "danger" };
  if (score <= -0.15) return { label: "Conviction lowered", tone: "warn" };
  if (score < 0) return { label: "Slight drag", tone: "neutral" };
  return { label: "No news impact", tone: "ok" };
}

/* -------------------------------------------------------------------------- */
/*  De-risk bar — fills LEFT from zero (a negative-only meter).                 */
/* -------------------------------------------------------------------------- */

function DeRiskBar({ value }: { value: number }) {
  // value in [-1, 0]. Fill width is |value| as a % of the full track; the bar
  // grows from the right edge (0) toward the left (-1). Red, because it only
  // ever de-risks — there is no "up" direction on this surface.
  const clamped = Math.max(-1, Math.min(0, value));
  const width = Math.abs(clamped) * 100;
  return (
    <div className="w-full">
      <div className="relative h-2 w-full overflow-hidden rounded-full bg-panel2">
        <div
          className="absolute right-0 top-0 h-full rounded-full bg-down transition-[width] duration-500 ease-out"
          style={{ width: `${width}%` }}
        />
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-faint">
        <span className="num">-1.0</span>
        <span className="num">de-risk only</span>
        <span className="num">0</span>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Sources / freshness row                                                   */
/* -------------------------------------------------------------------------- */

function SourceRow({ source }: { source: NarrativeSource }) {
  const tier = TIER_META[source.credibility];
  return (
    <div className="flex items-center justify-between gap-2 py-1.5">
      <div className="flex min-w-0 items-center gap-2">
        <span className="truncate text-[12px] text-text">{source.outlet}</span>
        <Chip tone={tier.tone}>{tier.label}</Chip>
        {source.breaking && (
          <Chip tone="danger" icon={<Radio />}>
            breaking
          </Chip>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2 text-[11px]">
        {source.negative ? (
          <span className="inline-flex items-center gap-1 text-down">
            <TrendingDown className="h-3 w-3" />
            negative
          </span>
        ) : (
          <span className="text-faint">neutral / positive</span>
        )}
        <span className="num text-faint">
          {source.articleCount} article{source.articleCount === 1 ? "" : "s"}
        </span>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Per-asset narrative card                                                  */
/* -------------------------------------------------------------------------- */

function NarrativeCard({ signal }: { signal: NarrativeSignal }) {
  const verdict = scoreVerdict(signal.narrativeScore);
  const hasFlags = signal.redFlags.length > 0;

  return (
    <Panel className="overflow-hidden">
      {/* Header row */}
      <div className="flex items-start justify-between gap-3 border-b border-border px-3.5 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate text-[14px] font-semibold tracking-tight text-text">
              {signal.token}
            </span>
            <Chip tone={verdict.tone}>{verdict.label}</Chip>
          </div>
          <div className="mt-1 flex items-center gap-3 text-[11px] text-faint">
            <span className="inline-flex items-center gap-1">
              <Newspaper className="h-3 w-3" />
              <span className="num text-muted-foreground">
                {signal.totalArticles}
              </span>{" "}
              scored
            </span>
            <span className="inline-flex items-center gap-1">
              <Clock className="h-3 w-3" />
              <span className="text-muted-foreground">
                {fmtFreshness(signal.freshnessMs)}
              </span>
            </span>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="text-[10px] uppercase tracking-wide text-faint">
            Narrative score
          </div>
          <div
            className={cn(
              "num text-[26px] font-semibold leading-none",
              signal.narrativeScore < 0 ? "text-down" : "text-faint",
            )}
          >
            {signal.narrativeScore.toFixed(2)}
          </div>
          <div className="text-[9px] uppercase tracking-wide text-faint">
            de-risk only
          </div>
        </div>
      </div>

      <div className="space-y-3.5 p-3.5">
        {/* The de-risk bar */}
        <DeRiskBar value={signal.narrativeScore} />

        {/* Credible-negative breaking banner — the headline de-risk message */}
        {signal.credibleNegativeEvent && (
          <div className="flex items-start gap-2 rounded-md border border-[color:color-mix(in_srgb,var(--danger)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--danger)_10%,transparent)] px-2.5 py-2">
            <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" />
            <p className="text-[11px] leading-snug text-down">
              A credible negative event from a tier-1/tier-2 outlet — the
              narrative-failure trigger. It can only force a de-risk exit; it
              never sizes up, widens a stop, or initiates a buy.
            </p>
          </div>
        )}

        {/* Red flags (de-risk-only) */}
        <div>
          <div className="mb-1.5 flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-faint">
            <AlertTriangle className="h-3 w-3" />
            Coordinated-shill / news flags
          </div>
          {hasFlags ? (
            <div className="flex flex-wrap gap-1.5">
              {signal.redFlags.map((flag) => (
                <Chip key={flag} tone="danger" icon={<AlertTriangle />}>
                  {flagLabel(flag)}
                </Chip>
              ))}
            </div>
          ) : (
            <Chip tone="ok" icon={<ShieldCheck />}>
              None detected
            </Chip>
          )}
        </div>

        {/* Sources + freshness */}
        <div className="rounded-md border border-border bg-panel2 px-3 py-2">
          <div className="mb-0.5 flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-faint">
            <BadgeCheck className="h-3 w-3" />
            Sources
          </div>
          {signal.sources.length > 0 ? (
            <div className="divide-y divide-border">
              {signal.sources.map((s) => (
                <SourceRow key={s.outletId || s.outlet} source={s} />
              ))}
            </div>
          ) : (
            <p className="py-1.5 text-[11px] text-faint">
              No outlets in the decision window.
            </p>
          )}
          {signal.postsExcludedFuture > 0 && (
            <p className="mt-1 text-[10px] text-faint">
              <span className="num">{signal.postsExcludedFuture}</span>{" "}
              future-dated article(s) excluded by the point-in-time filter.
            </p>
          )}
        </div>

        {/* Headline digest (QUOTED UNTRUSTED DATA) */}
        <div className="rounded-md border border-border bg-panel2 px-3 py-2.5">
          <div className="mb-1 flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-faint">
            <Quote className="h-3 w-3" />
            Headline digest
          </div>
          <p className="text-[12px] leading-relaxed text-muted-foreground">
            {signal.headlineDigest}
          </p>
        </div>
      </div>
    </Panel>
  );
}

/* -------------------------------------------------------------------------- */
/*  States                                                                    */
/* -------------------------------------------------------------------------- */

function CardSkeleton() {
  return (
    <Panel className="overflow-hidden">
      <div className="border-b border-border px-3.5 py-3">
        <div className="h-4 w-28 animate-pulse rounded bg-panel2 motion-reduce:animate-none" />
      </div>
      <div className="space-y-3 p-3.5" aria-busy="true">
        <div className="h-2 w-full animate-pulse rounded-full bg-panel2 motion-reduce:animate-none" />
        <div className="h-12 w-full animate-pulse rounded-md bg-panel2 motion-reduce:animate-none" />
        <div className="h-16 w-full animate-pulse rounded-md bg-panel2 motion-reduce:animate-none" />
      </div>
    </Panel>
  );
}

function EmptyState({ icon, label }: { icon: ReactNode; label: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-16 text-center">
      <span className="flex h-9 w-9 items-center justify-center rounded-full border border-border bg-panel2 text-faint [&_svg]:h-4 [&_svg]:w-4">
        {icon}
      </span>
      <span className="text-[12px] text-muted-foreground">{label}</span>
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <div className="flex items-center justify-center gap-2 px-4 py-16 text-center text-[12px] text-danger">
      <AlertTriangle className="h-4 w-4 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                      */
/* -------------------------------------------------------------------------- */

export default function Narrative() {
  const { data, loading, error } = useNarrative();
  const signals = useMemo(() => data ?? [], [data]);

  const isLoading = loading && data === null;
  const hasError = !!error && data === null;

  const totals = useMemo(() => {
    let credibleNegative = 0;
    let flagged = 0;
    let mostNegative = 0; // closest to -1 across the board
    let articles = 0;
    for (const s of signals) {
      if (s.credibleNegativeEvent) credibleNegative += 1;
      if (s.redFlags.length > 0) flagged += 1;
      if (s.narrativeScore < mostNegative) mostNegative = s.narrativeScore;
      articles += s.totalArticles;
    }
    return {
      assets: signals.length,
      credibleNegative,
      flagged,
      mostNegative,
      articles,
    };
  }, [signals]);

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-[16px] font-semibold tracking-tight text-text">
            <Newspaper className="h-4 w-4 text-brand" />
            Narrative &amp; news
          </h1>
          <p className="mt-0.5 max-w-2xl text-[12px] text-muted-foreground">
            A <span className="text-text">read-only de-risk view</span> of the
            news narrative per asset: which outlets are reporting, how fresh the
            story is, the per-asset narrative score, and any coordinated-shill
            flags. This is observability only; nothing here snipes, sizes,
            vetoes, or otherwise acts on an asset.
          </p>
        </div>
        <Chip tone="violet" icon={<LiveDot tone="warn" />}>
          Slow-loop signal
        </Chip>
      </div>

      {/* Explainer banner — load-bearing: de-risk-only, no win-rate, no action */}
      <div className="flex items-start gap-2 rounded-[10px] border border-[color:color-mix(in_srgb,var(--info)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--info)_8%,transparent)] px-3.5 py-2.5 text-[12px] leading-relaxed text-muted-foreground">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-info" />
        <span>
          The narrative score is a{" "}
          <span className="num text-text">[−1 … 0] de-risk delta</span>, not a
          win-rate: news can only <span className="text-text">lower</span>{" "}
          conviction, never raise it. A positive mainstream headline is
          sell-into-retail liquidity — it scores{" "}
          <span className="num text-text">0</span>, never a buy. A credible
          negative event can force a de-risk exit; it can never size up or widen
          a stop.
        </span>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          label="Assets tracked"
          value={<span className="num">{totals.assets}</span>}
          sub="on the narrative board"
          tone="info"
        />
        <StatTile
          label="Credible negatives"
          value={<span className="num">{totals.credibleNegative}</span>}
          sub="narrative-failure candidates"
          tone={totals.credibleNegative > 0 ? "danger" : "neutral"}
        />
        <StatTile
          label="Coordinated-shill flags"
          value={<span className="num">{totals.flagged}</span>}
          sub="asset(s) with news red flags"
          tone={totals.flagged > 0 ? "warn" : "neutral"}
        />
        <StatTile
          label="Strongest de-risk"
          value={
            <span className="num">
              {totals.mostNegative < 0
                ? totals.mostNegative.toFixed(2)
                : "0.00"}
            </span>
          }
          sub="most-negative narrative score"
          tone={totals.mostNegative < 0 ? "down" : "neutral"}
        />
      </div>

      {/* Cards */}
      {isLoading ? (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <CardSkeleton />
          <CardSkeleton />
        </div>
      ) : hasError ? (
        <Panel title="Narrative & news" icon={<Newspaper />}>
          <ErrorState message={error ?? "Failed to load narrative"} />
        </Panel>
      ) : signals.length === 0 ? (
        <Panel title="Narrative & news" icon={<Newspaper />}>
          <EmptyState
            icon={<Newspaper />}
            label="No assets on the narrative board yet."
          />
        </Panel>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {signals.map((s) => (
            <NarrativeCard key={s.asset} signal={s} />
          ))}
        </div>
      )}
    </div>
  );
}
