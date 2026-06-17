import { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  Ban,
  Crosshair,
  Filter,
  SkipForward,
  Target,
  Users,
  XOctagon,
} from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Chip, LiveDot, Panel, RedFlags, Spark, StatTile } from "@/components/kit";
import type { ChipTone } from "@/components/kit";
import { useSnipeFeed } from "@/lib/api";
import type { GateReason, SnipeEvent, SnipeSource } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CHART } from "@/lib/chart-colors";

/* -------------------------------------------------------------------------- */
/*  Display maps                                                              */
/* -------------------------------------------------------------------------- */

type ActionFilter = "all" | "sniped" | "skipped" | "vetoed";
const ACTION_FILTERS: ActionFilter[] = ["all", "sniped", "skipped", "vetoed"];

const SOURCES: SnipeSource[] = [
  "pump.fun",
  "pumpswap",
  "raydium_v4",
  "raydium_cpmm",
  "migration",
];

const SOURCE_LABEL: Record<SnipeSource, string> = {
  "pump.fun": "pump.fun",
  pumpswap: "pumpswap",
  raydium_v4: "raydium v4",
  raydium_cpmm: "raydium cpmm",
  migration: "migration",
};

const SOURCE_TONE: Record<SnipeSource, ChipTone> = {
  "pump.fun": "accent",
  pumpswap: "violet",
  raydium_v4: "info",
  raydium_cpmm: "info",
  migration: "warn",
};

const ACTION_TONE: Record<SnipeEvent["action"], ChipTone> = {
  sniped: "ok",
  skipped: "neutral",
  vetoed: "danger",
};

const ACTION_ICON: Record<SnipeEvent["action"], typeof Target> = {
  sniped: Target,
  skipped: SkipForward,
  vetoed: Ban,
};

const GATE_LABEL: Record<GateReason, string> = {
  passed: "passed",
  freeze_authority: "freeze auth",
  mint_authority: "mint auth",
  lp_unlocked: "lp unlocked",
  sniper_cluster: "sniper cluster",
  high_tax: "high tax",
  low_liquidity: "low liquidity",
};

/* -------------------------------------------------------------------------- */
/*  Formatters (all numbers mono + rounded)                                   */
/* -------------------------------------------------------------------------- */

const fmtTime = (ts: number) =>
  new Date(ts).toLocaleTimeString("en-US", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

const fmtP = (p: number | null) => (p === null ? "—" : p.toFixed(2));

const fmtPnl = (pnl: number | null) =>
  pnl === null ? null : `${pnl >= 0 ? "+" : ""}${Math.round(pnl)}%`;

/* -------------------------------------------------------------------------- */
/*  New-row highlight tracking                                                */
/* -------------------------------------------------------------------------- */

const HIGHLIGHT_MS = 1400;

function useFreshIds(events: SnipeEvent[]): Set<string> {
  const [fresh, setFresh] = useState<Set<string>>(() => new Set());
  const seen = useRef<Set<string>>(new Set());
  const timers = useRef<number[]>([]);

  useEffect(() => {
    const incoming: string[] = [];
    for (const e of events) {
      if (!seen.current.has(e.id)) {
        seen.current.add(e.id);
        incoming.push(e.id);
      }
    }
    // On first mount the whole seed is "seen" but should not flash.
    if (incoming.length === 0 || seen.current.size === incoming.length) return;

    // Intentional: flag the just-arrived rows as fresh, then schedule a timer
    // to clear the flash. Timer-driven highlight is a legitimate effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setFresh(cur => {
      const next = new Set(cur);
      for (const id of incoming) next.add(id);
      return next;
    });

    const t = window.setTimeout(() => {
      setFresh(cur => {
        const next = new Set(cur);
        for (const id of incoming) next.delete(id);
        return next;
      });
    }, HIGHLIGHT_MS);
    timers.current.push(t);
  }, [events]);

  useEffect(() => {
    const list = timers.current;
    return () => {
      for (const t of list) window.clearTimeout(t);
    };
  }, []);

  return fresh;
}

/* -------------------------------------------------------------------------- */
/*  Wall clock                                                                 */
/*  A ticking "now" so time-windowed metrics (events/min over a rolling 60s)   */
/*  re-evaluate as time passes — not only when a new event arrives. Reading    */
/*  the clock from state keeps render pure (no Date.now() during render).      */
/* -------------------------------------------------------------------------- */

function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                      */
/* -------------------------------------------------------------------------- */

export default function SnipeFeed() {
  const { events, loading, error } = useSnipeFeed();
  const fresh = useFreshIds(events);
  // Ticking clock: re-runs the rolling-window math every second so the
  // events/min counter decays to 0 when the stream goes quiet instead of
  // freezing at its last value (the stale-counter bug).
  const now = useNow(1000);

  const [action, setAction] = useState<ActionFilter>("all");
  const [source, setSource] = useState<SnipeSource | "all">("all");

  /* ---- derived header metrics (over the resident window) ---- */
  const metrics = useMemo(() => {
    if (events.length === 0) {
      return { perMin: 0, acceptPct: 0, sniped: 0, vetoed: 0, total: 0 };
    }
    // Count only events inside the rolling trailing-60s window. As `now`
    // advances each second, old events fall out and the rate updates live.
    const lastMin = events.filter(e => now - e.ts <= 60_000).length;
    const sniped = events.filter(e => e.action === "sniped").length;
    const vetoed = events.filter(e => e.action === "vetoed").length;
    return {
      perMin: lastMin,
      acceptPct: Math.round((sniped / events.length) * 100),
      sniped,
      vetoed,
      total: events.length,
    };
  }, [events, now]);

  /* ---- throughput sparkline: events bucketed into ~5s windows ---- */
  const throughput = useMemo(() => {
    if (events.length === 0) return [] as number[];
    const bucketMs = 5_000;
    const buckets = new Map<number, number>();
    for (const e of events) {
      const k = Math.floor(e.ts / bucketMs);
      buckets.set(k, (buckets.get(k) ?? 0) + 1);
    }
    return Array.from(buckets.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([, v]) => v)
      .slice(-24);
  }, [events]);

  /* ---- filtered rows ---- */
  const rows = useMemo(
    () =>
      events.filter(
        e =>
          (action === "all" || e.action === action) &&
          (source === "all" || e.source === source)
      ),
    [events, action, source]
  );

  const counts = useMemo(() => {
    const c: Record<ActionFilter, number> = {
      all: events.length,
      sniped: 0,
      skipped: 0,
      vetoed: 0,
    };
    for (const e of events) c[e.action] += 1;
    return c;
  }, [events]);

  return (
    <div className="mx-auto flex max-w-[1400px] flex-col gap-3">
      {/* Page heading */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <h1 className="text-[15px] font-semibold tracking-tight text-text">
            Snipe feed
          </h1>
          <Chip tone="accent" icon={<LiveDot />}>
            Live
          </Chip>
        </div>
        <div className="num text-[11px] text-faint">
          {metrics.total} events resident · newest on top
        </div>
      </div>

      {/* Header KPIs */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatTile
          label="Throughput"
          value={
            <span>
              {metrics.perMin}
              <span className="ml-1 text-[12px] text-faint">/min</span>
            </span>
          }
          sub="events ingested"
          tone="accent"
        />
        <StatTile
          label="Accept rate"
          value={`${metrics.acceptPct}%`}
          sub={`${metrics.sniped} sniped of ${metrics.total}`}
          tone={metrics.acceptPct >= 25 ? "up" : "neutral"}
        />
        <StatTile
          label="Vetoed"
          value={metrics.vetoed}
          sub="model / narrative blocks"
          tone={metrics.vetoed > 0 ? "danger" : "neutral"}
        />
        <div className="rounded-[10px] border border-border bg-panel px-3.5 py-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-faint">
            Flow
          </div>
          <div className="mt-2">
            <Spark data={throughput} tone={CHART.accent} height={34} />
          </div>
        </div>
      </div>

      {/* Feed panel */}
      <Panel
        title="Event stream"
        icon={<Crosshair />}
        actions={
          <div className="flex items-center gap-2">
            {/* Action filter chips */}
            <div
              role="tablist"
              aria-label="Filter by action"
              className="flex items-center gap-0.5 rounded-md border border-border bg-panel2 p-0.5"
            >
              {ACTION_FILTERS.map(f => {
                const selected = action === f;
                return (
                  <button
                    key={f}
                    type="button"
                    role="tab"
                    aria-selected={selected}
                    onClick={() => setAction(f)}
                    className={cn(
                      "num inline-flex items-center gap-1 rounded px-2 py-1 text-[11px] font-medium capitalize transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40",
                      selected
                        ? "bg-hover text-text"
                        : "text-muted-foreground hover:text-text"
                    )}
                  >
                    {f}
                    <span
                      className={cn(
                        "text-[10px]",
                        selected ? "text-brand" : "text-faint"
                      )}
                    >
                      {counts[f]}
                    </span>
                  </button>
                );
              })}
            </div>

            {/* Source filter */}
            <Select
              value={source}
              onValueChange={v => setSource(v as SnipeSource | "all")}
            >
              <SelectTrigger
                aria-label="Filter by source"
                className="h-7 w-[140px] gap-1.5 border-border bg-panel2 px-2 text-[11px] text-muted-foreground"
              >
                <Filter className="h-3 w-3 shrink-0 text-faint" />
                <SelectValue placeholder="All sources" />
              </SelectTrigger>
              <SelectContent className="text-[12px]">
                <SelectItem value="all">All sources</SelectItem>
                {SOURCES.map(s => (
                  <SelectItem key={s} value={s}>
                    {SOURCE_LABEL[s]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        }
      >
        <FeedTable
          rows={rows}
          fresh={fresh}
          loading={loading}
          error={error}
          filtered={action !== "all" || source !== "all"}
          totalResident={events.length}
        />
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Table                                                                     */
/* -------------------------------------------------------------------------- */

interface FeedTableProps {
  rows: SnipeEvent[];
  fresh: Set<string>;
  loading: boolean;
  error: string | null;
  filtered: boolean;
  totalResident: number;
}

const COL_HEAD =
  "sticky top-0 z-10 border-b border-border bg-panel2 px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-wide text-faint";

function FeedTable({
  rows,
  fresh,
  loading,
  error,
  filtered,
  totalResident,
}: FeedTableProps) {
  /* Error state */
  if (error) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 px-4 py-16 text-center">
        <XOctagon className="h-5 w-5 text-danger" />
        <div className="text-[13px] font-medium text-text">
          Feed interrupted
        </div>
        <div className="num text-[11px] text-muted-foreground">{error}</div>
      </div>
    );
  }

  /* Loading state (first paint, no seed yet) */
  if (loading && rows.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 px-4 py-16 text-center">
        <Activity className="h-5 w-5 animate-pulse text-brand" />
        <div className="text-[13px] font-medium text-text">
          Connecting to stream…
        </div>
        <div className="text-[11px] text-muted-foreground">
          Awaiting first snipe events
        </div>
      </div>
    );
  }

  /* Empty state */
  if (rows.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 px-4 py-16 text-center">
        <Filter className="h-5 w-5 text-faint" />
        <div className="text-[13px] font-medium text-text">
          {filtered ? "No events match this filter" : "No events yet"}
        </div>
        <div className="num text-[11px] text-muted-foreground">
          {filtered
            ? `${totalResident} events resident — adjust action or source`
            : "Waiting for the next detection"}
        </div>
      </div>
    );
  }

  return (
    <div className="max-h-[calc(100vh-300px)] overflow-auto scrollbar-thin">
      <table className="w-full border-collapse text-[12px]">
        <thead>
          <tr>
            <th className={cn(COL_HEAD, "w-[78px]")}>Time</th>
            <th className={cn(COL_HEAD, "w-[64px]")}>Slot</th>
            <th className={COL_HEAD}>Token</th>
            <th className={cn(COL_HEAD, "w-[120px]")}>Source</th>
            <th className={COL_HEAD}>Gate verdict</th>
            <th className={COL_HEAD}>Safety red flags</th>
            <th className={cn(COL_HEAD, "w-[68px] text-right")}>Model p</th>
            <th className={cn(COL_HEAD, "w-[64px] text-right")}>
              <span className="inline-flex items-center gap-1">
                <Users className="h-3 w-3" />
                SW
              </span>
            </th>
            <th className={cn(COL_HEAD, "w-[88px]")}>Action</th>
            <th className={cn(COL_HEAD, "w-[72px] text-right")}>PnL</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(e => (
            <FeedRow key={e.id} e={e} fresh={fresh.has(e.id)} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Row                                                                       */
/* -------------------------------------------------------------------------- */

function FeedRow({ e, fresh }: { e: SnipeEvent; fresh: boolean }) {
  const ActionIcon = ACTION_ICON[e.action];
  const pnl = fmtPnl(e.pnlPct);
  const overThreshold = e.modelP !== null && e.modelP >= 0.62;

  return (
    <tr
      className={cn(
        "border-b border-border/60 transition-colors hover:bg-hover",
        fresh &&
          "bg-[color:color-mix(in_srgb,var(--accent)_10%,transparent)] motion-reduce:bg-[color:color-mix(in_srgb,var(--accent)_6%,transparent)]"
      )}
    >
      {/* Time */}
      <td className="num whitespace-nowrap px-3 py-1.5 text-muted-foreground tabular-nums">
        {fmtTime(e.ts)}
      </td>

      {/* Slot delay as N+x */}
      <td className="num whitespace-nowrap px-3 py-1.5 text-faint">
        {e.slotDelay === null ? (
          <span className="text-faint">—</span>
        ) : (
          <span
            className={cn(
              e.slotDelay <= 2
                ? "text-up"
                : e.slotDelay === 3
                  ? "text-warn"
                  : "text-down"
            )}
          >
            N+{e.slotDelay}
          </span>
        )}
      </td>

      {/* Token */}
      <td className="px-3 py-1.5">
        <span className="num font-semibold text-text">{e.token}</span>
      </td>

      {/* Source */}
      <td className="px-3 py-1.5">
        <Chip tone={SOURCE_TONE[e.source]}>
          <span className="num">{SOURCE_LABEL[e.source]}</span>
        </Chip>
      </td>

      {/* Gate verdict */}
      <td className="px-3 py-1.5">
        {e.gatePassed ? (
          <Chip tone="ok">passed</Chip>
        ) : (
          <div className="flex flex-wrap items-center gap-1">
            {e.gateReasons.map(r => (
              <Chip key={r} tone="danger">
                {GATE_LABEL[r]}
              </Chip>
            ))}
          </div>
        )}
      </td>

      {/* Token-safety scanner red flags (honeypot / not-sellable / clusters …) */}
      <td className="px-3 py-1.5">
        <RedFlags flags={e.redFlags} hideWhenClean />
        {e.redFlags.length === 0 && (
          <span className="text-[11px] text-faint">—</span>
        )}
      </td>

      {/* Model p */}
      <td className="num px-3 py-1.5 text-right tabular-nums">
        <span
          className={cn(
            e.modelP === null
              ? "text-faint"
              : overThreshold
                ? "text-up"
                : "text-muted-foreground"
          )}
        >
          {fmtP(e.modelP)}
        </span>
      </td>

      {/* Smart wallets */}
      <td className="num px-3 py-1.5 text-right tabular-nums">
        <span
          className={cn(
            e.smartWallets >= 8 ? "text-brand" : "text-muted-foreground"
          )}
        >
          {e.smartWallets}
        </span>
      </td>

      {/* Action */}
      <td className="px-3 py-1.5">
        <Chip tone={ACTION_TONE[e.action]} icon={<ActionIcon />}>
          {e.action}
        </Chip>
      </td>

      {/* PnL */}
      <td className="num px-3 py-1.5 text-right tabular-nums">
        {pnl === null ? (
          <span className="text-faint">—</span>
        ) : (
          <span
            className={cn(
              e.pnlPct !== null && e.pnlPct >= 0 ? "text-up" : "text-down"
            )}
          >
            {pnl}
          </span>
        )}
      </td>
    </tr>
  );
}
