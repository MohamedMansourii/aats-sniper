import { useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Copy,
  Filter,
  Info,
  Loader2,
  ShieldCheck,
  Users,
  XCircle,
} from "lucide-react";
import { Chip, LiveDot, Panel, StatTile } from "@/components/kit";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useSmartWallets } from "@/lib/api";
import type { SmartWallet } from "@/lib/types";
import { round1 } from "@/lib/mock";
import { cn } from "@/lib/utils";
import { toast } from "sonner";

/* -------------------------------------------------------------------------- */
/*  Formatting                                                                */
/* -------------------------------------------------------------------------- */

const fmtSolSigned = (n: number) =>
  `${n >= 0 ? "+" : ""}${round1(n).toFixed(1)}`;
const pnlClass = (n: number) => (n >= 0 ? "text-up" : "text-down");

function fmtSince(ms: number): string {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m ago`;
}

/* -------------------------------------------------------------------------- */
/*  States                                                                    */
/* -------------------------------------------------------------------------- */

function TableSkeleton({ rows, cols }: { rows: number; cols: number }) {
  return (
    <div className="space-y-px p-3.5" aria-busy="true" aria-label="Loading wallets">
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
/*  Wallet row                                                                */
/* -------------------------------------------------------------------------- */

const TH =
  "h-8 px-2.5 text-left align-middle text-[10px] font-semibold uppercase tracking-wide text-faint";
const TH_R = cn(TH, "text-right");
const ROW = "border-b border-border transition-colors hover:bg-hover";
const TD = "px-2.5 py-2 align-middle text-[12px]";

function CopyAddr({ address }: { address: string }) {
  const [busy, setBusy] = useState(false);
  function onCopy() {
    setBusy(true);
    void navigator.clipboard
      ?.writeText(address)
      .then(() => toast.message("Address copied"))
      .catch(() => toast.error("Copy failed"))
      .finally(() => setBusy(false));
  }
  return (
    <button
      type="button"
      onClick={onCopy}
      aria-label={`Copy address ${address}`}
      className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-border bg-panel text-faint transition-colors hover:bg-hover hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40"
    >
      {busy ? (
        <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" />
      ) : (
        <Copy className="h-3 w-3" />
      )}
    </button>
  );
}

function WalletRow({ w }: { w: SmartWallet }) {
  return (
    <TableRow className={ROW}>
      <TableCell className={TD}>
        <div className="flex min-w-0 items-center gap-2">
          {w.passesFilter ? (
            <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-up" aria-hidden />
          ) : (
            <XCircle className="h-3.5 w-3.5 shrink-0 text-faint" aria-hidden />
          )}
          <div className="flex min-w-0 flex-col leading-tight">
            <span className="font-semibold tracking-tight text-text">
              {w.label || "unlabeled"}
            </span>
            <span className="num truncate text-[10px] text-faint">
              {w.address}
            </span>
          </div>
          <CopyAddr address={w.address} />
        </div>
      </TableCell>
      <TableCell className={cn(TD, "num text-right text-text")}>
        {w.recentEntries}
      </TableCell>
      <TableCell className={cn(TD, "num text-right")}>
        {w.entryLagSlots === null ? (
          <span className="text-faint">—</span>
        ) : (
          <span
            className={cn(
              w.entryLagSlots <= 3
                ? "text-muted-foreground"
                : w.entryLagSlots <= 6
                  ? "text-warn"
                  : "text-down",
            )}
            title="Slots we trail this wallet's fill (we are a follower, not a front-runner)"
          >
            +{w.entryLagSlots}
          </span>
        )}
      </TableCell>
      <TableCell
        className={cn(TD, "num text-right font-semibold", pnlClass(w.trackedNetPnlSol))}
      >
        {fmtSolSigned(w.trackedNetPnlSol)}
      </TableCell>
      <TableCell className={TD}>
        {w.passesFilter ? (
          <Chip tone="ok" icon={<ShieldCheck />}>
            considered
          </Chip>
        ) : (
          <Chip tone="neutral">filtered out</Chip>
        )}
      </TableCell>
      <TableCell className={cn(TD, "text-muted-foreground")}>
        <div className="flex flex-wrap items-center gap-1">
          {w.filterReasons.map((r) => (
            <span
              key={r}
              className="rounded border border-border bg-panel2 px-1.5 py-0.5 text-[10px] text-faint"
            >
              {r}
            </span>
          ))}
        </div>
      </TableCell>
      <TableCell className={cn(TD, "num text-right text-faint")}>
        {fmtSince(w.lastSeenMs)}
      </TableCell>
    </TableRow>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                      */
/* -------------------------------------------------------------------------- */

type View = "all" | "considered" | "filtered";

export default function CopyTrade() {
  const { data, loading, error } = useSmartWallets();
  const [view, setView] = useState<View>("all");

  const wallets = useMemo(() => data ?? [], [data]);
  const considered = useMemo(
    () => wallets.filter((w) => w.passesFilter),
    [wallets],
  );

  const rows = useMemo(() => {
    if (view === "considered") return considered;
    if (view === "filtered") return wallets.filter((w) => !w.passesFilter);
    return wallets;
  }, [wallets, considered, view]);

  // Net-of-cost tracked PnL across wallets that clear the selectivity bar.
  const consideredNetPnl = round1(
    considered.reduce((s, w) => s + w.trackedNetPnlSol, 0),
  );

  const isLoading = loading && data === null;
  const hasError = !!error && data === null;

  const VIEW_TABS: { key: View; label: string; count: number }[] = [
    { key: "all", label: "All tracked", count: wallets.length },
    { key: "considered", label: "Considered", count: considered.length },
    {
      key: "filtered",
      label: "Filtered out",
      count: wallets.length - considered.length,
    },
  ];

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-[16px] font-semibold tracking-tight text-text">
            <Users className="h-4 w-4 text-brand" />
            Copy-trade & smart money
          </h1>
          <p className="mt-0.5 max-w-2xl text-[12px] text-muted-foreground">
            A <span className="text-text">selectivity filter view</span>: which
            tracked wallets the engine would consider as a feature input. This is
            not a buy trigger — nothing here places, sizes, or mirrors an order.
          </p>
        </div>
        <Chip tone="accent" icon={<LiveDot />}>
          tracking {wallets.length}
        </Chip>
      </div>

      {/* Explainer banner — load-bearing: this surface NEVER initiates a trade */}
      <div className="flex items-start gap-2 rounded-[10px] border border-[color:color-mix(in_srgb,var(--info)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--info)_8%,transparent)] px-3.5 py-2.5 text-[12px] leading-relaxed text-muted-foreground">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-info" />
        <span>
          Smart-money is used for <span className="text-text">selectivity</span>,
          never blind mirroring. A wallet "in" only contributes the{" "}
          <span className="num text-text">smart_wallets_in</span> count to the
          model; entries still pass the full cost-gated safety pipeline. Entry-lag
          is shown honestly — a positive lag means we would enter{" "}
          <span className="text-text">behind</span> that wallet.
        </span>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          label="Tracked wallets"
          value={<span className="num">{wallets.length}</span>}
          sub="smart-money universe"
          tone="neutral"
        />
        <StatTile
          label="Pass selectivity"
          value={<span className="num">{considered.length}</span>}
          sub="clear the filter bar"
          tone="accent"
        />
        <StatTile
          label="Considered net PnL"
          value={
            <span className="num">{fmtSolSigned(consideredNetPnl)}</span>
          }
          sub="net-of-cost · tracked SOL"
          tone={consideredNetPnl >= 0 ? "up" : "down"}
        />
        <StatTile
          label="Filtered out"
          value={
            <span className="num">{wallets.length - considered.length}</span>
          }
          sub="below the bar"
          tone="neutral"
        />
      </div>

      {/* Wallet table */}
      <Panel
        title="Tracked wallets"
        icon={<Filter />}
        actions={
          <div
            role="tablist"
            aria-label="Filter wallets"
            className="flex items-center gap-0.5 rounded-md border border-border bg-panel2 p-0.5"
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
          <TableSkeleton rows={5} cols={5} />
        ) : hasError ? (
          <ErrorState message={error ?? "Failed to load smart wallets"} />
        ) : rows.length === 0 ? (
          <EmptyState
            icon={<Users />}
            label={
              wallets.length === 0
                ? "No tracked wallets yet."
                : "No wallets match this filter."
            }
          />
        ) : (
          <div className="overflow-auto scrollbar-thin">
            <Table className="text-[12px]">
              <TableHeader className="sticky top-0 z-10 bg-panel">
                <TableRow className="border-b border-border hover:bg-transparent">
                  <TableHead className={TH}>Wallet</TableHead>
                  <TableHead className={TH_R}>Recent entries</TableHead>
                  <TableHead className={TH_R}>
                    <span className="inline-flex items-center justify-end gap-1">
                      <Clock className="h-3 w-3" />
                      Entry lag
                    </span>
                  </TableHead>
                  <TableHead className={TH_R}>Tracked net PnL</TableHead>
                  <TableHead className={TH}>Selectivity</TableHead>
                  <TableHead className={TH}>Reasons</TableHead>
                  <TableHead className={TH_R}>Last seen</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((w) => (
                  <WalletRow key={w.address} w={w} />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </Panel>
    </div>
  );
}
