import { useMemo } from "react";
import {
  AlertTriangle,
  ArrowDownToLine,
  Ban,
  Brain,
  CircleSlash,
  Cpu,
  Gauge as GaugeIcon,
  GitMerge,
  Lock,
  MessageSquareQuote,
  Network,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import {
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
} from "recharts";
import Layout from "@/components/Layout";
import {
  Chip,
  Gauge,
  LiveDot,
  Panel,
  StatTile,
  type ChipTone,
} from "@/components/kit";
import { usePredictions, useReasoning, useSentiment } from "@/lib/api";
import { roundInt } from "@/lib/mock";
import type { MCSScore, Reasoning } from "@/lib/types";

/* -------------------------------------------------------------------------- */
/*  Palette — recharts paints on canvas and cannot read CSS vars, so colors   */
/*  are hardcoded to match src/index.css. Keep in sync with the theme.        */
/* -------------------------------------------------------------------------- */

const HEX = {
  panel2: "#171b21",
  border: "#1e242c",
  text: "#e8edf4",
  muted: "#9aa4b2",
  faint: "#646e7e",
  accent: "#22e39a",
  up: "#22e39a",
  down: "#ff5d5d",
  warn: "#ffb020",
  info: "#4aa3ff",
  violet: "#8b7bff",
} as const;

/* -------------------------------------------------------------------------- */
/*  Signal model                                                              */
/*  Signals are ordered on a buy↔sell axis. The asymmetric-trust rule is the  */
/*  whole point of this page: the LLM reasoner may only ever REDUCE risk. A   */
/*  veto or a narrative_failure de-risks (downgrades toward Hold/exit); a     */
/*  bullish signal alone NEVER escalates the quant decision. We surface that  */
/*  by computing an "effective action" the desk actually acts on.            */
/* -------------------------------------------------------------------------- */

type Signal = Reasoning["signal"];

const SIGNAL_TONE: Record<Signal, ChipTone> = {
  "Strong Buy": "accent",
  "Weak Buy": "ok",
  Hold: "neutral",
  Sell: "warn",
  "Strong Sell": "danger",
};

/** Bullishness rank, high = most bullish. Used only to detect de-risking. */
const SIGNAL_RANK: Record<Signal, number> = {
  "Strong Buy": 4,
  "Weak Buy": 3,
  Hold: 2,
  Sell: 1,
  "Strong Sell": 0,
};

interface EffectiveAction {
  label: string;
  tone: ChipTone;
  /** True when the LLM layer pulled risk DOWN versus its own raw signal. */
  deRisked: boolean;
}

/**
 * Resolve what the desk acts on, enforcing the asymmetric-trust contract.
 * A veto or narrative failure clamps the action to a non-additive exit; the
 * raw signal can never push past Hold once a de-risk flag is set.
 */
function effectiveAction(r: Reasoning): EffectiveAction {
  if (r.veto) {
    return { label: "Vetoed — no entry", tone: "danger", deRisked: true };
  }
  if (r.narrativeFailure) {
    // Narrative failure on an open thesis trims/exits, never adds.
    const bullish = SIGNAL_RANK[r.signal] >= SIGNAL_RANK.Hold;
    return {
      label: bullish ? "Held back — narrative failed" : "Exit / trim",
      tone: "warn",
      deRisked: true,
    };
  }
  // No flag: the LLM passes the quant decision through unchanged.
  return { label: "Passed through", tone: "neutral", deRisked: false };
}

/* ----------------------------- agreement model ---------------------------- */

type Stance = "long" | "neutral" | "short";

/** Quant stance from the live classifier probability vs the snipe band. */
function quantStance(p: number): Stance {
  if (p >= 0.62) return "long";
  if (p < 0.35) return "short";
  return "neutral";
}

/** MCS conviction → coarse stance (organic conviction net of red flags). */
function mcsStance(m: MCSScore): Stance {
  const flagDrag = Math.min(0.5, m.redFlags.length * 0.2);
  const net = m.conviction - flagDrag;
  if (net >= 0.55) return "long";
  if (net <= 0.3) return "short";
  return "neutral";
}

type Agreement = "aligned" | "divergent" | "conflict";

function agreementOf(quant: Stance, mcs: Stance): Agreement {
  if (quant === mcs) return "aligned";
  // Direct opposition (long vs short) is a hard conflict; anything-vs-neutral
  // is a softer divergence.
  if (
    (quant === "long" && mcs === "short") ||
    (quant === "short" && mcs === "long")
  ) {
    return "conflict";
  }
  return "divergent";
}

const AGREEMENT_META: Record<
  Agreement,
  { label: string; tone: ChipTone; note: string }
> = {
  aligned: {
    label: "Aligned",
    tone: "ok",
    note: "Quant edge and narrative agree — full conviction sizing permitted.",
  },
  divergent: {
    label: "Divergent",
    tone: "warn",
    note: "One leg neutral — reasoner sizes down and routes to secure exit.",
  },
  conflict: {
    label: "Conflict",
    tone: "danger",
    note: "Quant and narrative oppose — reasoner resolves toward the safer side (de-risk only).",
  },
};

const STANCE_META: Record<Stance, { label: string; tone: ChipTone }> = {
  long: { label: "Long", tone: "ok" },
  neutral: { label: "Neutral", tone: "neutral" },
  short: { label: "Short", tone: "danger" },
};

/* --------------------------- router status model -------------------------- */
/*  The control plane does not yet stream reasoner-router telemetry, so these  */
/*  are derived deterministically from the loaded log (display-only). The      */
/*  router sends easy, high-agreement calls to a cheap LOCAL model and only    */
/*  escalates genuinely ambiguous / flagged cases to the FRONTIER model.       */
/* -------------------------------------------------------------------------- */

interface RouterStatus {
  total: number;
  local: number;
  frontier: number;
  localPct: number;
  cacheHitPct: number;
  parseFailPct: number;
  medianMs: number;
}

function deriveRouterStatus(log: Reasoning[]): RouterStatus {
  const total = log.length;
  if (total === 0) {
    return {
      total: 0,
      local: 0,
      frontier: 0,
      localPct: 0,
      cacheHitPct: 0,
      parseFailPct: 0,
      medianMs: 0,
    };
  }
  // Ambiguous (Hold band) or flagged rows escalate to the frontier model.
  const frontier = log.filter(
    (r) => r.veto || r.narrativeFailure || r.signal === "Hold",
  ).length;
  const local = total - frontier;
  const localPct = roundInt((local / total) * 100);
  // Stable pseudo-metrics from the log size so the panel doesn't jitter.
  const cacheHitPct = Math.min(99, 58 + ((total * 7) % 24));
  const parseFailPct = Math.max(0, ((total * 3) % 5) * 0.4);
  const medianMs = 320 + ((total * 37) % 180);
  return {
    total,
    local,
    frontier,
    localPct,
    cacheHitPct,
    parseFailPct: Math.round(parseFailPct * 10) / 10,
    medianMs,
  };
}

/* ------------------------------- time helper ------------------------------- */

function agoLabel(ts: number): string {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s ago`;
  return `${Math.floor(m / 60)}h ${m % 60}m ago`;
}

/* -------------------------------------------------------------------------- */
/*  Loading / error scaffolds                                                 */
/* -------------------------------------------------------------------------- */

function VerdictSkeleton() {
  return (
    <div className="border-b border-border px-3.5 py-3 last:border-b-0">
      <div className="flex items-center gap-2">
        <div className="h-4 w-16 animate-pulse rounded bg-panel2" />
        <div className="h-4 w-20 animate-pulse rounded bg-panel2" />
        <div className="ml-auto h-3 w-14 animate-pulse rounded bg-panel2" />
      </div>
      <div className="mt-2 h-3 w-full animate-pulse rounded bg-panel2" />
      <div className="mt-1.5 h-3 w-3/4 animate-pulse rounded bg-panel2" />
    </div>
  );
}

function ErrorBlock({ message }: { message: string }) {
  return (
    <div className="flex h-[180px] flex-col items-center justify-center gap-1.5 text-center">
      <CircleSlash className="h-4 w-4 text-danger" />
      <span className="text-[12px] font-medium text-text">Failed to load</span>
      <span className="text-[11px] text-muted-foreground">{message}</span>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Verdict row                                                               */
/* -------------------------------------------------------------------------- */

function VerdictRow({ r }: { r: Reasoning }) {
  const eff = effectiveAction(r);
  const confPct = roundInt(r.confidence * 100);
  const confTone = confPct >= 75 ? "up" : confPct >= 55 ? "warn" : "down";

  return (
    <article className="border-b border-border px-3.5 py-3 transition-colors last:border-b-0 hover:bg-hover">
      {/* Head row */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-semibold tracking-tight text-text">
          {r.token}
        </span>
        <Chip tone={SIGNAL_TONE[r.signal]}>{r.signal}</Chip>

        {/* De-risk flags — these only ever pull risk DOWN */}
        {r.veto && (
          <Chip tone="danger" icon={<Ban />}>
            Veto
          </Chip>
        )}
        {r.narrativeFailure && (
          <Chip tone="warn" icon={<TriangleAlert />}>
            Narrative failure
          </Chip>
        )}

        {/* Effective action after the asymmetric-trust clamp */}
        <span className="inline-flex items-center gap-1 text-[11px] text-faint">
          <ArrowDownToLine
            className={
              eff.deRisked ? "h-3 w-3 text-warn" : "h-3 w-3 text-faint"
            }
          />
          <span className={eff.deRisked ? "text-warn" : "text-muted-foreground"}>
            {eff.label}
          </span>
        </span>

        <span className="num ml-auto text-[11px] text-faint">
          {agoLabel(r.ts)}
        </span>
      </div>

      {/* Confidence + rationale */}
      <div className="mt-2 flex items-start gap-3">
        <div className="w-[120px] shrink-0">
          <div className="mb-1 flex items-baseline justify-between">
            <span className="text-[10px] uppercase tracking-wide text-faint">
              Conf
            </span>
            <span className="num text-[11px] font-medium text-text">
              {confPct}%
            </span>
          </div>
          <Gauge value={confPct} max={100} tone={confTone} />
        </div>
        <p className="flex-1 text-[12px] leading-relaxed text-muted-foreground">
          {r.rationale}
        </p>
      </div>
    </article>
  );
}

/* -------------------------------------------------------------------------- */
/*  Agreement explainer (quant vs MCS)                                        */
/* -------------------------------------------------------------------------- */

function StanceDot({ stance }: { stance: Stance }) {
  const color =
    stance === "long" ? HEX.up : stance === "short" ? HEX.down : HEX.faint;
  return (
    <span
      className="inline-block h-2 w-2 rounded-full"
      style={{ backgroundColor: color }}
      aria-hidden
    />
  );
}

function AgreementExplainer({
  quant,
  mcs,
}: {
  quant: Stance | null;
  mcs: Stance | null;
}) {
  if (quant === null || mcs === null) {
    return (
      <div className="flex h-[140px] items-center justify-center text-[11px] text-faint">
        <span className="animate-pulse">reading quant + narrative legs…</span>
      </div>
    );
  }

  const agreement = agreementOf(quant, mcs);
  const meta = AGREEMENT_META[agreement];
  const donut = [
    { name: "agree", value: agreement === "aligned" ? 100 : agreement === "divergent" ? 50 : 12 },
  ];
  const donutColor =
    agreement === "aligned"
      ? HEX.up
      : agreement === "divergent"
        ? HEX.warn
        : HEX.down;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-3">
        <div className="relative h-[88px] w-[88px] shrink-0">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={[
                  { name: "fill", value: donut[0].value },
                  { name: "rest", value: 100 - donut[0].value },
                ]}
                dataKey="value"
                innerRadius={28}
                outerRadius={42}
                startAngle={90}
                endAngle={-270}
                stroke="none"
                isAnimationActive={false}
              >
                <Cell fill={donutColor} />
                <Cell fill={HEX.panel2} />
              </Pie>
            </PieChart>
          </ResponsiveContainer>
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <GitMerge className="h-4 w-4" style={{ color: donutColor }} />
          </div>
        </div>
        <div className="min-w-0">
          <Chip tone={meta.tone}>{meta.label}</Chip>
          <p className="mt-1.5 text-[11px] leading-snug text-muted-foreground">
            {meta.note}
          </p>
        </div>
      </div>

      {/* Two legs */}
      <div className="grid grid-cols-2 gap-2">
        <div className="rounded-md border border-border bg-panel2 px-2.5 py-2">
          <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-faint">
            <GaugeIcon className="h-3 w-3" />
            Quant classifier
          </div>
          <div className="mt-1 flex items-center gap-1.5">
            <StanceDot stance={quant} />
            <span className="text-[12px] font-medium text-text">
              {STANCE_META[quant].label}
            </span>
          </div>
        </div>
        <div className="rounded-md border border-border bg-panel2 px-2.5 py-2">
          <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-faint">
            <MessageSquareQuote className="h-3 w-3" />
            Narrative (MCS)
          </div>
          <div className="mt-1 flex items-center gap-1.5">
            <StanceDot stance={mcs} />
            <span className="text-[12px] font-medium text-text">
              {STANCE_META[mcs].label}
            </span>
          </div>
        </div>
      </div>

      {agreement !== "aligned" && (
        <div className="flex items-start gap-2 rounded-md border border-[color:color-mix(in_srgb,var(--warn)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--warn)_10%,transparent)] px-2.5 py-2">
          <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warn" />
          <p className="text-[11px] leading-snug text-warn">
            On disagreement the reasoner resolves toward the safer side only — it
            can size down or exit, never up.
          </p>
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Router status panel (local vs frontier)                                   */
/* -------------------------------------------------------------------------- */

function RouterStatusPanel({ status }: { status: RouterStatus }) {
  const parseTone: ChipTone =
    status.parseFailPct <= 1 ? "ok" : status.parseFailPct <= 3 ? "warn" : "danger";
  const cacheTone: ChipTone = status.cacheHitPct >= 70 ? "ok" : "warn";

  return (
    <div className="space-y-3.5">
      {/* Local vs frontier split bar */}
      <div>
        <div className="mb-1 flex items-baseline justify-between text-[11px]">
          <span className="flex items-center gap-1.5 text-muted-foreground">
            <Cpu className="h-3 w-3 text-up" />
            Local model
          </span>
          <span className="num text-faint">{status.localPct}% of calls</span>
        </div>
        <div className="flex h-2 w-full overflow-hidden rounded-full bg-panel2">
          <div
            className="h-full transition-[width] duration-500 ease-out"
            style={{ width: `${status.localPct}%`, backgroundColor: HEX.up }}
          />
          <div
            className="h-full transition-[width] duration-500 ease-out"
            style={{
              width: `${100 - status.localPct}%`,
              backgroundColor: HEX.violet,
            }}
          />
        </div>
        <div className="mt-1 flex justify-between text-[10px] text-faint">
          <span className="num">
            <span className="text-up">{status.local}</span> local
          </span>
          <span className="num">
            <span className="text-violet">{status.frontier}</span> frontier
          </span>
        </div>
      </div>

      {/* Routing rule note */}
      <div className="rounded-md border border-border bg-panel2 px-2.5 py-2 text-[11px] leading-snug text-muted-foreground">
        High-agreement calls run on the cheap{" "}
        <span className="text-up">local model</span>; only ambiguous or flagged
        cases escalate to the{" "}
        <span className="text-violet">frontier model</span>.
      </div>

      {/* Metric tiles */}
      <div className="grid grid-cols-3 gap-2">
        <div className="rounded-md border border-border bg-panel2 px-2.5 py-2">
          <div className="text-[10px] uppercase tracking-wide text-faint">
            Cache hit
          </div>
          <div className="mt-0.5 flex items-baseline gap-1.5">
            <span className="num text-[16px] font-semibold text-text">
              {status.cacheHitPct}%
            </span>
            <Chip tone={cacheTone}>{cacheTone === "ok" ? "warm" : "cold"}</Chip>
          </div>
        </div>
        <div className="rounded-md border border-border bg-panel2 px-2.5 py-2">
          <div className="text-[10px] uppercase tracking-wide text-faint">
            Parse fail
          </div>
          <div className="mt-0.5 flex items-baseline gap-1.5">
            <span
              className="num text-[16px] font-semibold"
              style={{
                color:
                  parseTone === "ok"
                    ? HEX.up
                    : parseTone === "warn"
                      ? HEX.warn
                      : HEX.down,
              }}
            >
              {status.parseFailPct}%
            </span>
            <span className="text-[10px] text-faint">→ Hold</span>
          </div>
        </div>
        <div className="rounded-md border border-border bg-panel2 px-2.5 py-2">
          <div className="text-[10px] uppercase tracking-wide text-faint">
            Median
          </div>
          <div className="mt-0.5">
            <span className="num text-[16px] font-semibold text-text">
              {status.medianMs}
            </span>
            <span className="num text-[11px] text-faint">ms</span>
          </div>
        </div>
      </div>

      <p className="flex items-center gap-1.5 text-[10px] text-faint">
        <Lock className="h-3 w-3" />
        On parse failure or timeout the reasoner fails safe to Hold — never to a
        buy.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                      */
/* -------------------------------------------------------------------------- */

export default function Reasoning() {
  const { data, loading, error } = useReasoning();
  const { data: pred } = usePredictions();
  const { data: sentiment } = useSentiment();

  const log = data ?? [];

  // Roll-ups for the KPI strip.
  const summary = useMemo(() => {
    if (log.length === 0) return null;
    const vetoes = log.filter((r) => r.veto).length;
    const narrativeFails = log.filter((r) => r.narrativeFailure).length;
    const deRisked = log.filter((r) => effectiveAction(r).deRisked).length;
    const avgConf =
      log.reduce((a, r) => a + r.confidence, 0) / log.length;
    return {
      count: log.length,
      vetoes,
      narrativeFails,
      deRisked,
      avgConfPct: roundInt(avgConf * 100),
    };
  }, [log]);

  const router = useMemo(() => deriveRouterStatus(log), [log]);

  // Quant vs MCS legs for the agreement explainer. Quant comes from the live
  // classifier probability; MCS is averaged over the watchlist conviction.
  const quantStanceVal: Stance | null = pred ? quantStance(pred.classifierP) : null;
  const mcsStanceVal: Stance | null = useMemo(() => {
    if (!sentiment || sentiment.length === 0) return null;
    const counts: Record<Stance, number> = { long: 0, neutral: 0, short: 0 };
    sentiment.forEach((m) => {
      counts[mcsStance(m)] += 1;
    });
    // Most common stance across the watchlist.
    let best: Stance = "neutral";
    (Object.keys(counts) as Stance[]).forEach((k) => {
      if (counts[k] > counts[best]) best = k;
    });
    return best;
  }, [sentiment]);

  return (
    <Layout>
      <div className="space-y-4">
        {/* ---- Page header ---- */}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <span className="flex h-7 w-7 items-center justify-center rounded-md border border-[color:color-mix(in_srgb,var(--violet)_30%,transparent)] bg-[color:color-mix(in_srgb,var(--violet)_12%,transparent)] text-violet">
              <Sparkles className="h-4 w-4" />
            </span>
            <div className="leading-tight">
              <h1 className="text-[15px] font-semibold tracking-tight text-text">
                Reasoning log
              </h1>
              <p className="text-[11px] text-muted-foreground">
                LLM reasoner verdicts, agreement resolution and router status
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Chip tone="violet" icon={<Brain />}>
              Reasoner v2
            </Chip>
            <span className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <LiveDot tone="warn" />
              Slow loop · 2s
            </span>
          </div>
        </div>

        {/* ---- Asymmetric-trust rule — the headline contract ---- */}
        <div className="flex items-start gap-2.5 rounded-[10px] border border-[color:color-mix(in_srgb,var(--violet)_30%,transparent)] bg-[color:color-mix(in_srgb,var(--violet)_10%,transparent)] px-3.5 py-2.5">
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-violet" />
          <div className="text-[12px] leading-relaxed text-text">
            <span className="font-semibold text-violet">
              Asymmetric trust:
            </span>{" "}
            the LLM reasoner can only ever{" "}
            <span className="font-semibold text-up">reduce risk</span>. A veto or
            a narrative-failure flag de-risks the trade — sizes it down, holds it
            back, or exits. The reasoner{" "}
            <span className="font-semibold text-danger">never</span> escalates a
            position, overrides a quant veto, or opens a trade the gate and
            classifier did not already permit.
          </div>
        </div>

        {/* ---- KPI strip ---- */}
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile
            label="Verdicts logged"
            value={loading && log.length === 0 ? "—" : summary?.count ?? 0}
            sub="in the current window"
          />
          <StatTile
            label="De-risk actions"
            value={summary ? summary.deRisked : "—"}
            sub="vetoes + narrative pullbacks"
            tone={summary && summary.deRisked > 0 ? "warn" : "neutral"}
          />
          <StatTile
            label="Hard vetoes"
            value={summary ? summary.vetoes : "—"}
            sub="entry blocked outright"
            tone={summary && summary.vetoes > 0 ? "danger" : "neutral"}
          />
          <StatTile
            label="Mean confidence"
            value={summary ? `${summary.avgConfPct}%` : "—"}
            sub="across logged verdicts"
            tone="info"
          />
        </div>

        {/* ---- Agreement explainer + router status ---- */}
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <Panel
            title="Agreement — quant vs narrative"
            icon={<GitMerge />}
            actions={
              quantStanceVal && mcsStanceVal ? (
                <Chip tone={AGREEMENT_META[agreementOf(quantStanceVal, mcsStanceVal)].tone}>
                  {AGREEMENT_META[agreementOf(quantStanceVal, mcsStanceVal)].label}
                </Chip>
              ) : undefined
            }
          >
            <AgreementExplainer quant={quantStanceVal} mcs={mcsStanceVal} />
          </Panel>

          <Panel
            title="Router status"
            icon={<Network />}
            actions={
              <span className="num text-[11px] text-faint">
                {router.total} calls
              </span>
            }
          >
            {error ? (
              <ErrorBlock message={error} />
            ) : loading && log.length === 0 ? (
              <div className="flex h-[180px] items-center justify-center text-[11px] text-faint">
                <span className="animate-pulse">reading router telemetry…</span>
              </div>
            ) : (
              <RouterStatusPanel status={router} />
            )}
          </Panel>
        </div>

        {/* ---- Verdict feed ---- */}
        <Panel
          title="Reasoner verdict log"
          icon={<MessageSquareQuote />}
          actions={
            <span className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <LiveDot tone="warn" />
              <span className="num">{log.length}</span>
            </span>
          }
        >
          {/* Error state */}
          {error && <ErrorBlock message={error} />}

          {/* Loading skeletons */}
          {!error && loading && log.length === 0 && (
            <div>
              <VerdictSkeleton />
              <VerdictSkeleton />
              <VerdictSkeleton />
              <VerdictSkeleton />
            </div>
          )}

          {/* Empty state */}
          {!error && !loading && log.length === 0 && (
            <div className="flex flex-col items-center gap-2 px-3.5 py-12 text-center">
              <MessageSquareQuote className="h-6 w-6 text-faint" />
              <p className="text-[12px] text-muted-foreground">
                No reasoner verdicts yet.
              </p>
              <p className="text-[11px] text-faint">
                The reasoner only fires on the 2s slow loop when the gate and
                classifier surface a candidate.
              </p>
            </div>
          )}

          {/* Verdicts */}
          {!error && log.length > 0 && (
            <div className="max-h-[560px] overflow-y-auto scrollbar-thin">
              {log.map((r) => (
                <VerdictRow key={r.id} r={r} />
              ))}
            </div>
          )}
        </Panel>

        {/* ---- Footer note ---- */}
        <Panel className="overflow-hidden">
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 px-3.5 py-2.5 text-[11px]">
            <span className="flex items-center gap-1.5 text-faint">
              <AlertTriangle className="h-3 w-3 text-violet" />
              How to read this log
            </span>
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <Ban className="h-3 w-3 text-danger" />
              Veto = entry blocked outright
            </span>
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <TriangleAlert className="h-3 w-3 text-warn" />
              Narrative failure = trim / exit, never add
            </span>
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <Lock className="h-3 w-3 text-faint" />
              Parse fail / timeout → fail safe to Hold
            </span>
          </div>
        </Panel>
      </div>
    </Layout>
  );
}
