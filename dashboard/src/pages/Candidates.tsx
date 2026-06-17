import { useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  Eye,
  Filter,
  Hourglass,
  Info,
  ListChecks,
  ShieldCheck,
  SkipForward,
  Target,
} from "lucide-react";
import { Chip, LiveDot, Panel, RedFlags, StatTile } from "@/components/kit";
import type { ChipTone } from "@/components/kit";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useCandidates } from "@/lib/api";
import type { Candidate, CandidateStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

/* -------------------------------------------------------------------------- */
/*  Formatting                                                                */
/* -------------------------------------------------------------------------- */

function fmtSince(ms: number): string {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m ago`;
}

/** Probability 0..1 → percent string, or an em-dash when not yet scored. */
function fmtP(p: number | null): string {
  if (p === null) return "—";
  return `${Math.round(p * 100)}%`;
}

/* -------------------------------------------------------------------------- */
/*  Status presentation                                                       */
/* -------------------------------------------------------------------------- */

const STATUS_META: Record<
  CandidateStatus,
  { label: string; tone: ChipTone; icon: ReactNode }
> = {
  monitoring: { label: "monitoring", tone: "info", icon: <Eye /> },
  pending: { label: "pending", tone: "accent", icon: <Hourglass /> },
  skipped: { label: "skipped", tone: "neutral", icon: <SkipForward /> },
  sniped: { label: "sniped", tone: "ok", icon: <Target /> },
};

function StatusChip({ status }: { status: CandidateStatus }) {
  const m = STATUS_META[status];
  return (
    <Chip tone={m.tone} icon={m.icon}>
      {m.label}
    </Chip>
  );
}

/* -------------------------------------------------------------------------- */
/*  States                                                                    */
/* -------------------------------------------------------------------------- */

function TableSkeleton({ rows, cols }: { rows: number; cols: number }) {
  return (
    <div className="space-y-px p-3.5" aria-busy="true" aria-label="Loading candidates">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex items-center gap-3 py-1.5">
          {Array.from({ length: cols }).map((__, c) => (
            <div
              key={c}
              className="h-3 flex-1 animate-pulse rounded bg-panel2 motion-reduce:animate-none"
              style={{ maxWidth: c === 0 ? 160 : 80 }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

function EmptyState({ icon, label }: { icon: ReactNode; label: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-12 text-center">
      <span className="flex h-9 w-9 items-center justify-center rounded-full border border-border bg-panel2 text-faint [&_svg]:h-4 [&_svg]:w-4">
        {icon}
      </span>
      <span className="text-[12px] text-muted-foreground">{label}</span>
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <div className="flex items-center justify-center gap-2 px-4 py-12 text-center text-[12px] text-danger">
      <AlertTriangle className="h-4 w-4 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Candidate row                                                             */
/* -------------------------------------------------------------------------- */

const TH =
  "h-8 px-2.5 text-left align-middle text-[10px] font-semibold uppercase tracking-wide text-faint";
const TH_R = cn(TH, "text-right");
const ROW = "border-b border-border transition-colors hover:bg-hover";
const TD = "px-2.5 py-2 align-middle text-[12px]";

function CandidateRow({ c }: { c: Candidate }) {
  return (
    <TableRow className={ROW}>
      <TableCell className={TD}>
        <div className="flex min-w-0 flex-col leading-tight">
          <span className="font-semibold tracking-tight text-text">
            {c.token || "unknown"}
          </span>
          <span className="num truncate text-[10px] text-faint">{c.mint}</span>
        </div>
      </TableCell>
      <TableCell className={cn(TD, "text-muted-foreground")}>
        {c.source === null ? (
          <span className="text-[10px] text-faint" title="Not reported by the backend">
            n/a
          </span>
        ) : (
          <span className="rounded border border-border bg-panel2 px-1.5 py-0.5 text-[10px] text-faint">
            {c.source}
          </span>
        )}
      </TableCell>
      <TableCell className={TD}>
        <StatusChip status={c.status} />
      </TableCell>
      <TableCell className={cn(TD, "num text-right")}>
        {c.modelP === null ? (
          <span className="text-faint" title="Not yet scored by the classifier">
            —
          </span>
        ) : (
          <span
            className={cn(
              "font-semibold",
              c.modelP >= 0.62
                ? "text-up"
                : c.modelP >= 0.35
                  ? "text-warn"
                  : "text-down",
            )}
          >
            {fmtP(c.modelP)}
          </span>
        )}
      </TableCell>
      <TableCell className={cn(TD, "num text-right text-text")}>
        {c.smartWallets === null ? (
          <span className="text-faint" title="Not reported by the backend">
            n/a
          </span>
        ) : (
          c.smartWallets
        )}
      </TableCell>
      <TableCell className={TD}>
        {c.safetyPassed && c.redFlags.length === 0 ? (
          <Chip tone="ok" icon={<ShieldCheck />}>
            clean
          </Chip>
        ) : (
          <RedFlags flags={c.redFlags} hideWhenClean />
        )}
      </TableCell>
      <TableCell className={cn(TD, "max-w-[280px] text-muted-foreground")}>
        <span className="line-clamp-2">{c.reason || "—"}</span>
      </TableCell>
      <TableCell className={cn(TD, "num text-right text-faint")}>
        {fmtSince(c.updatedMs)}
      </TableCell>
    </TableRow>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                      */
/* -------------------------------------------------------------------------- */

type View = "all" | CandidateStatus;

export default function Candidates() {
  const { data, loading, error } = useCandidates();
  const [view, setView] = useState<View>("all");

  const candidates = useMemo(() => data ?? [], [data]);

  const counts = useMemo(() => {
    const c: Record<CandidateStatus, number> = {
      monitoring: 0,
      pending: 0,
      skipped: 0,
      sniped: 0,
    };
    for (const cand of candidates) c[cand.status] += 1;
    return c;
  }, [candidates]);

  const rows = useMemo(() => {
    if (view === "all") return candidates;
    return candidates.filter((c) => c.status === view);
  }, [candidates, view]);

  const isLoading = loading && data === null;
  const hasError = !!error && data === null;

  const VIEW_TABS: { key: View; label: string; count: number }[] = [
    { key: "all", label: "All", count: candidates.length },
    { key: "monitoring", label: "Monitoring", count: counts.monitoring },
    { key: "pending", label: "Pending", count: counts.pending },
    { key: "skipped", label: "Skipped", count: counts.skipped },
    { key: "sniped", label: "Sniped", count: counts.sniped },
  ];

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-[16px] font-semibold tracking-tight text-text">
            <ListChecks className="h-4 w-4 text-brand" />
            Candidate queue
          </h1>
          <p className="mt-0.5 max-w-2xl text-[12px] text-muted-foreground">
            A <span className="text-text">read-only pipeline view</span> of
            tokens the engine evaluated: what is being monitored, pending a
            decision, skipped, or sniped — with the model probability, safety
            report, and the reason for each. This is observability only; nothing
            here snipes, sizes, vetoes, or re-queues a candidate.
          </p>
        </div>
        <Chip tone="accent" icon={<LiveDot />}>
          {candidates.length} in queue
        </Chip>
      </div>

      {/* Explainer banner — load-bearing: this surface initiates NO action */}
      <div className="flex items-start gap-2 rounded-[10px] border border-[color:color-mix(in_srgb,var(--info)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--info)_8%,transparent)] px-3.5 py-2.5 text-[12px] leading-relaxed text-muted-foreground">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-info" />
        <span>
          The queue mirrors the engine's own evaluation. A{" "}
          <span className="text-text">skipped</span> row is a de-risk decision —
          we stood down — and shows why. The model probability is a{" "}
          <span className="num text-text">classifier p</span>, not a win-rate;
          smart-wallet counts are a selectivity feature, never a buy signal.
        </span>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          label="Monitoring"
          value={<span className="num">{counts.monitoring}</span>}
          sub="observed, no decision yet"
          tone="info"
        />
        <StatTile
          label="Pending"
          value={<span className="num">{counts.pending}</span>}
          sub="awaiting model / gate verdict"
          tone="accent"
        />
        <StatTile
          label="Skipped"
          value={<span className="num">{counts.skipped}</span>}
          sub="de-risked, stood down"
          tone="neutral"
        />
        <StatTile
          label="Sniped"
          value={<span className="num">{counts.sniped}</span>}
          sub="entry taken (informational)"
          tone="up"
        />
      </div>

      {/* Candidate table */}
      <Panel
        title="Candidates"
        icon={<Filter />}
        actions={
          <div
            role="tablist"
            aria-label="Filter candidates by status"
            className="flex flex-wrap items-center gap-0.5 rounded-md border border-border bg-panel2 p-0.5"
          >
            {VIEW_TABS.map((t) => {
              const selected = view === t.key;
              return (
                <button
                  key={t.key}
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  onClick={() => setView(t.key)}
                  className={cn(
                    "inline-flex items-center gap-1 rounded px-2 py-1 text-[11px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40",
                    selected
                      ? "bg-hover text-text"
                      : "text-muted-foreground hover:text-text",
                  )}
                >
                  {t.label}
                  <span
                    className={cn(
                      "num text-[10px]",
                      selected ? "text-brand" : "text-faint",
                    )}
                  >
                    {t.count}
                  </span>
                </button>
              );
            })}
          </div>
        }
      >
        {isLoading ? (
          <TableSkeleton rows={6} cols={6} />
        ) : hasError ? (
          <ErrorState message={error ?? "Failed to load candidates"} />
        ) : rows.length === 0 ? (
          <EmptyState
            icon={<ListChecks />}
            label={
              candidates.length === 0
                ? "No candidates in the queue yet."
                : "No candidates match this filter."
            }
          />
        ) : (
          <div className="overflow-auto scrollbar-thin">
            <Table className="text-[12px]">
              <TableHeader className="sticky top-0 z-10 bg-panel">
                <TableRow className="border-b border-border hover:bg-transparent">
                  <TableHead className={TH}>Token</TableHead>
                  <TableHead className={TH}>Source</TableHead>
                  <TableHead className={TH}>Status</TableHead>
                  <TableHead className={TH_R}>Model p</TableHead>
                  <TableHead className={TH_R}>Smart wallets</TableHead>
                  <TableHead className={TH}>Safety report</TableHead>
                  <TableHead className={TH}>Reason</TableHead>
                  <TableHead className={TH_R}>Updated</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((c) => (
                  <CandidateRow key={c.id} c={c} />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </Panel>
    </div>
  );
}
