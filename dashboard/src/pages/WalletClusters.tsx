import { useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  Crown,
  Eye,
  Info,
  Layers,
  Network,
  ShieldCheck,
  Target,
  Users,
  Zap,
} from "lucide-react";
import { Chip, LiveDot, Panel, StatTile } from "@/components/kit";
import type { ChipTone } from "@/components/kit";
import { useWalletClusters } from "@/lib/api";
import { CHART } from "@/lib/chart-colors";
import type {
  ManipulationFlag,
  WalletClusterGraph,
  WalletEdge,
  WalletEdgeType,
  WalletNode,
  WalletNodeType,
} from "@/lib/types";

/* -------------------------------------------------------------------------- */
/*  Formatting                                                                */
/* -------------------------------------------------------------------------- */

/** Basis points → percent string (e.g. 4200 → "42.0%"). Never a money value. */
function fmtBps(bps: number): string {
  return `${(bps / 100).toFixed(1)}%`;
}

/** Truncate a wallet pubkey for compact display (it is never a signing key). */
function shortWallet(w: string): string {
  if (w.length <= 12) return w;
  return `${w.slice(0, 4)}…${w.slice(-4)}`;
}

const MANIP_LABEL: Record<string, string> = {
  bundling_detected: "bundling detected",
  high_cluster_share: "high cluster share",
  freeze_authority_set: "freeze authority set",
  high_synchronicity: "high synchronicity",
};

/** Human label for a manipulation flag; unknown codes surfaced verbatim. */
function manipLabel(f: ManipulationFlag): string {
  return MANIP_LABEL[f] ?? f.replace(/_/g, " ");
}

const EDGE_LABEL: Record<WalletEdgeType, string> = {
  same_bundle: "same bundle",
  shared_funder: "shared funder",
  co_timing: "co-timed buys",
  creator_funded: "creator-funded",
};

/* -------------------------------------------------------------------------- */
/*  Node presentation                                                         */
/* -------------------------------------------------------------------------- */

const NODE_META: Record<
  WalletNodeType,
  { label: string; color: string; icon: ReactNode }
> = {
  creator: { label: "creator", color: CHART.danger, icon: <Crown /> },
  bundler: { label: "bundler", color: CHART.warn, icon: <Layers /> },
  sniper: { label: "sniper", color: CHART.violet, icon: <Target /> },
  holder: { label: "holder", color: CHART.info, icon: <Users /> },
  unknown: { label: "unknown", color: CHART.faint, icon: <Eye /> },
};

/* -------------------------------------------------------------------------- */
/*  States                                                                    */
/* -------------------------------------------------------------------------- */

function GraphSkeleton() {
  return (
    <div
      className="flex h-[360px] items-center justify-center"
      aria-busy="true"
      aria-label="Loading wallet-cluster graph"
    >
      <div className="h-40 w-40 animate-pulse rounded-full bg-panel2 motion-reduce:animate-none" />
    </div>
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
/*  Graph layout — deterministic radial placement (no force sim, no new deps)  */
/*                                                                            */
/*  The highest-degree / creator node anchors the centre; every other node    */
/*  sits on a ring around it, ordered so same-type wallets cluster together.  */
/*  Bubble radius encodes supply share (bps). Pure geometry — no animation     */
/*  loop, so there is nothing to dispose and nothing that can jank a phone.    */
/* -------------------------------------------------------------------------- */

interface Placed extends WalletNode {
  x: number;
  y: number;
  r: number;
}

const VIEW_W = 720;
const VIEW_H = 420;

function layout(nodes: WalletNode[]): Placed[] {
  if (nodes.length === 0) return [];

  // Bubble radius scales with supply share (sqrt so area ∝ share), clamped to a
  // readable band. A 0-bps node still renders as a small dot.
  const radiusFor = (bps: number) => {
    const frac = Math.max(0, Math.min(1, bps / 10_000));
    return 10 + Math.sqrt(frac) * 34;
  };

  // Degree per wallet — the most-connected node centres the graph; if no edges,
  // the largest supply share centres it. Creator wins ties (it is the root).
  const score = (n: WalletNode) =>
    (n.nodeType === "creator" ? 1_000_000 : 0) + n.supplyShareBps;
  const ordered = [...nodes].sort((a, b) => score(b) - score(a));

  const cx = VIEW_W / 2;
  const cy = VIEW_H / 2;
  const placed: Placed[] = [];

  // Centre node.
  const centre = ordered[0];
  placed.push({ ...centre, x: cx, y: cy, r: radiusFor(centre.supplyShareBps) });

  // Remaining nodes on a ring, evenly spaced, ordered by type then share so the
  // ring reads as grouped clusters rather than noise.
  const rest = ordered.slice(1);
  const ringR = Math.min(VIEW_W, VIEW_H) / 2 - 56;
  rest.forEach((n, i) => {
    const angle = (i / Math.max(1, rest.length)) * Math.PI * 2 - Math.PI / 2;
    placed.push({
      ...n,
      x: cx + Math.cos(angle) * ringR,
      y: cy + Math.sin(angle) * ringR,
      r: radiusFor(n.supplyShareBps),
    });
  });

  return placed;
}

const EDGE_COLOR: Record<WalletEdgeType, string> = {
  same_bundle: CHART.danger,
  shared_funder: CHART.warn,
  co_timing: CHART.violet,
  creator_funded: CHART.warn,
};

/* -------------------------------------------------------------------------- */
/*  Cluster graph SVG                                                         */
/* -------------------------------------------------------------------------- */

function ClusterGraph({
  graph,
  onSelect,
  selected,
}: {
  graph: WalletClusterGraph;
  onSelect: (wallet: string | null) => void;
  selected: string | null;
}) {
  const placed = useMemo(() => layout(graph.nodes), [graph.nodes]);
  const byWallet = useMemo(() => {
    const m = new Map<string, Placed>();
    for (const p of placed) m.set(p.wallet, p);
    return m;
  }, [placed]);

  if (placed.length === 0) {
    return (
      <EmptyState
        icon={<Network />}
        label="No wallet nodes detected for this mint."
      />
    );
  }

  return (
    <svg
      viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
      className="h-auto w-full select-none"
      role="img"
      aria-label={`Wallet-connection graph for ${graph.token}: ${graph.nodes.length} wallets, ${graph.edges.length} detected connections`}
    >
      {/* Edges first so nodes paint on top. */}
      <g>
        {graph.edges.map((e: WalletEdge, i) => {
          const a = byWallet.get(e.source);
          const b = byWallet.get(e.target);
          if (!a || !b) return null;
          const dim =
            selected !== null && e.source !== selected && e.target !== selected;
          return (
            <line
              key={`${e.source}-${e.target}-${i}`}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke={EDGE_COLOR[e.edgeType]}
              strokeWidth={0.75 + e.weight * 2.25}
              strokeOpacity={dim ? 0.08 : 0.18 + e.weight * 0.5}
              strokeLinecap="round"
            />
          );
        })}
      </g>

      {/* Nodes. */}
      <g>
        {placed.map((n) => {
          const meta = NODE_META[n.nodeType];
          const isSel = selected === n.wallet;
          const dim = selected !== null && !isSel;
          const flagged = n.manipulationFlags.length > 0;
          return (
            <g
              key={n.wallet}
              transform={`translate(${n.x} ${n.y})`}
              tabIndex={0}
              role="button"
              aria-pressed={isSel}
              aria-label={`${meta.label} wallet ${shortWallet(n.wallet)}, ${fmtBps(
                n.supplyShareBps,
              )} of supply${flagged ? `, ${n.manipulationFlags.length} manipulation flag(s)` : ""}`}
              className="cursor-pointer outline-none focus-visible:[&>circle]:stroke-text"
              onClick={() => onSelect(isSel ? null : n.wallet)}
              onKeyDown={(ev) => {
                if (ev.key === "Enter" || ev.key === " ") {
                  ev.preventDefault();
                  onSelect(isSel ? null : n.wallet);
                }
              }}
              opacity={dim ? 0.4 : 1}
            >
              <circle
                r={n.r}
                fill={meta.color}
                fillOpacity={0.16}
                stroke={meta.color}
                strokeWidth={isSel ? 2.5 : 1.5}
              />
              {flagged && (
                <circle
                  r={n.r + 3}
                  fill="none"
                  stroke={CHART.danger}
                  strokeWidth={1}
                  strokeDasharray="3 3"
                  opacity={0.7}
                />
              )}
              <text
                textAnchor="middle"
                dy="0.32em"
                fontSize={10}
                fontFamily="ui-monospace, monospace"
                fill={CHART.text}
                pointerEvents="none"
              >
                {fmtBps(n.supplyShareBps)}
              </text>
            </g>
          );
        })}
      </g>
    </svg>
  );
}

/* -------------------------------------------------------------------------- */
/*  Graph card — one mint                                                     */
/* -------------------------------------------------------------------------- */

function scoreTone(score: number): { tone: ChipTone; label: string } {
  if (score >= 0.7) return { tone: "danger", label: "highly coordinated" };
  if (score >= 0.4) return { tone: "warn", label: "some coordination" };
  return { tone: "ok", label: "low coordination" };
}

function GraphCard({ graph }: { graph: WalletClusterGraph }) {
  const [selected, setSelected] = useState<string | null>(null);
  const st = scoreTone(graph.sniperClusterScore);
  const selectedNode = useMemo(
    () => graph.nodes.find((n) => n.wallet === selected) ?? null,
    [graph.nodes, selected],
  );
  // No reset effect needed: each GraphCard is keyed by mint in the parent, so a
  // mint swap remounts the card and clears the selection for free.

  return (
    <Panel
      title={graph.token}
      icon={<Network />}
      actions={
        <div className="flex items-center gap-1.5">
          <Chip tone={st.tone}>{st.label}</Chip>
          <span
            className="num text-[11px] text-faint"
            title="Event-time slot the cluster was detected at"
          >
            slot {graph.evaluatedAtSlot.toLocaleString()}
          </span>
        </div>
      }
    >
      <div className="space-y-3">
        {/* Mint + headline metrics */}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="num truncate text-[10px] text-faint">{graph.mint}</span>
          <div className="flex flex-wrap items-center gap-1.5">
            {graph.bundlingDetected ? (
              <Chip tone="danger" icon={<Layers />}>
                bundling detected
              </Chip>
            ) : (
              <Chip tone="ok" icon={<ShieldCheck />}>
                no bundling
              </Chip>
            )}
            <Chip tone="neutral" icon={<Zap />}>
              cluster score{" "}
              <span className="num ml-0.5">
                {graph.sniperClusterScore.toFixed(2)}
              </span>
            </Chip>
          </div>
        </div>

        {/* Manipulation flags (de-risk-only) */}
        {graph.manipulationFlags.length > 0 && (
          <div className="flex flex-wrap items-center gap-1">
            {graph.manipulationFlags.map((f) => (
              <Chip key={f} tone="danger" icon={<AlertTriangle />}>
                {manipLabel(f)}
              </Chip>
            ))}
          </div>
        )}

        {/* The graph */}
        <div className="rounded-[10px] border border-border bg-bg p-2">
          <ClusterGraph
            graph={graph}
            selected={selected}
            onSelect={setSelected}
          />
        </div>

        {/* Selected-wallet detail (read-only) */}
        {selectedNode ? (
          <div className="rounded-md border border-border bg-panel2 px-3 py-2 text-[12px]">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span
                  className="inline-flex items-center gap-1 [&_svg]:h-3.5 [&_svg]:w-3.5"
                  style={{ color: NODE_META[selectedNode.nodeType].color }}
                >
                  {NODE_META[selectedNode.nodeType].icon}
                  <span className="font-semibold">
                    {NODE_META[selectedNode.nodeType].label}
                  </span>
                </span>
                <span className="num text-[11px] text-faint">
                  {shortWallet(selectedNode.wallet)}
                </span>
              </div>
              <span className="num text-text">
                {fmtBps(selectedNode.supplyShareBps)} of supply
                {selectedNode.buySlot !== null && (
                  <span className="ml-2 text-faint">
                    bought @ slot {selectedNode.buySlot.toLocaleString()}
                  </span>
                )}
              </span>
            </div>
            {selectedNode.manipulationFlags.length > 0 && (
              <div className="mt-1.5 flex flex-wrap items-center gap-1">
                {selectedNode.manipulationFlags.map((f) => (
                  <Chip key={f} tone="danger">
                    {manipLabel(f)}
                  </Chip>
                ))}
              </div>
            )}
          </div>
        ) : (
          <p className="text-[11px] text-faint">
            Bubble size = supply share. Dashed ring = a manipulation flag is
            present. Select a wallet to inspect its share, buy slot, and flags —
            this is a read-only inspection, it takes no action.
          </p>
        )}
      </div>
    </Panel>
  );
}

/* -------------------------------------------------------------------------- */
/*  Legend                                                                    */
/* -------------------------------------------------------------------------- */

function Legend() {
  const nodeTypes: WalletNodeType[] = [
    "creator",
    "bundler",
    "sniper",
    "holder",
    "unknown",
  ];
  const edgeTypes: WalletEdgeType[] = [
    "creator_funded",
    "same_bundle",
    "shared_funder",
    "co_timing",
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-[11px] text-muted-foreground">
      <span className="text-faint">Wallets:</span>
      {nodeTypes.map((t) => (
        <span key={t} className="inline-flex items-center gap-1.5">
          <span
            className="h-2.5 w-2.5 rounded-full"
            style={{
              backgroundColor: NODE_META[t].color,
              opacity: 0.85,
            }}
          />
          {NODE_META[t].label}
        </span>
      ))}
      <span className="ml-2 text-faint">Edges:</span>
      {edgeTypes.map((t) => (
        <span key={t} className="inline-flex items-center gap-1.5">
          <span
            className="h-px w-4"
            style={{ backgroundColor: EDGE_COLOR[t] }}
          />
          {EDGE_LABEL[t]}
        </span>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                      */
/* -------------------------------------------------------------------------- */

export default function WalletClusters() {
  const { data, loading, error } = useWalletClusters();
  const graphs = useMemo(() => data ?? [], [data]);

  const isLoading = loading && data === null;
  const hasError = !!error && data === null;

  const totals = useMemo(() => {
    let coordinated = 0;
    let bundling = 0;
    let wallets = 0;
    for (const g of graphs) {
      if (g.sniperClusterScore >= 0.7) coordinated += 1;
      if (g.bundlingDetected) bundling += 1;
      wallets += g.nodes.length;
    }
    return { coordinated, bundling, wallets, mints: graphs.length };
  }, [graphs]);

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-[16px] font-semibold tracking-tight text-text">
            <Network className="h-4 w-4 text-brand" />
            Wallet clusters
          </h1>
          <p className="mt-0.5 max-w-2xl text-[12px] text-muted-foreground">
            A <span className="text-text">read-only Bubble Maps view</span> of
            the wallet-connection graph the engine already computes — creators,
            bundlers, snipers and holders, and the detected links between them —
            so the operator can <span className="text-text">see coordinated
            manipulation</span> at a glance. This is observability only; nothing
            here snipes, blocks, denylists, or otherwise acts on a wallet.
          </p>
        </div>
        <Chip tone="accent" icon={<LiveDot />}>
          {totals.mints} mint{totals.mints === 1 ? "" : "s"}
        </Chip>
      </div>

      {/* Explainer banner — load-bearing: this surface initiates NO action */}
      <div className="flex items-start gap-2 rounded-[10px] border border-[color:color-mix(in_srgb,var(--info)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--info)_8%,transparent)] px-3.5 py-2.5 text-[12px] leading-relaxed text-muted-foreground">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-info" />
        <span>
          The graph mirrors the engine's existing sniper-cluster and bundle
          detection. The cluster score is a{" "}
          <span className="num text-text">[0–1] coordination measure</span>, not
          a win-rate; supply shares are{" "}
          <span className="text-text">percentages of token supply</span>, not
          money. Every manipulation flag <span className="text-text">lowers</span>{" "}
          safety — none of them can ever trigger or size up an entry.
        </span>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          label="Mints mapped"
          value={<span className="num">{totals.mints}</span>}
          sub="clusters under observation"
          tone="info"
        />
        <StatTile
          label="Highly coordinated"
          value={<span className="num">{totals.coordinated}</span>}
          sub="cluster score ≥ 0.70"
          tone={totals.coordinated > 0 ? "danger" : "neutral"}
        />
        <StatTile
          label="Bundling detected"
          value={<span className="num">{totals.bundling}</span>}
          sub="same-block coordinated buys"
          tone={totals.bundling > 0 ? "warn" : "neutral"}
        />
        <StatTile
          label="Wallets graphed"
          value={<span className="num">{totals.wallets}</span>}
          sub="across all mapped mints"
          tone="neutral"
        />
      </div>

      {/* Legend */}
      <Panel>
        <div className="px-3.5 py-2.5">
          <Legend />
        </div>
      </Panel>

      {/* Graphs */}
      {isLoading ? (
        <Panel title="Wallet-connection graph" icon={<Network />}>
          <GraphSkeleton />
        </Panel>
      ) : hasError ? (
        <Panel title="Wallet-connection graph" icon={<Network />}>
          <ErrorState message={error ?? "Failed to load wallet clusters"} />
        </Panel>
      ) : graphs.length === 0 ? (
        <Panel title="Wallet-connection graph" icon={<Network />}>
          <EmptyState
            icon={<Network />}
            label="No wallet clusters detected yet."
          />
        </Panel>
      ) : (
        <div className="grid grid-cols-1 gap-4 2xl:grid-cols-2">
          {graphs.map((g) => (
            <GraphCard key={g.mint} graph={g} />
          ))}
        </div>
      )}
    </div>
  );
}
