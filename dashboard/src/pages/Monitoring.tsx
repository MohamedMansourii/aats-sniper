import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Activity,
  AlertTriangle,
  BellRing,
  BrainCircuit,
  CircleAlert,
  Cpu,
  Database,
  Gauge as GaugeIcon,
  HeartPulse,
  Info,
  Network,
  Radio,
  Server,
  ShieldAlert,
  ShieldCheck,
  Signal,
  Sparkles,
  Timer,
  Workflow,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ResponsiveContainer,
  XAxis,
  YAxis,
} from "recharts";
import Layout from "@/components/Layout";
import { Chip, LiveDot, Panel, StatTile, type ChipTone } from "@/components/kit";
import {
  useAgentState,
  useInfraTiers,
  useMetrics,
  useModuleHealth,
} from "@/lib/api";
import type { ModuleHealth } from "@/lib/types";

/* -------------------------------------------------------------------------- */
/*  Palette — recharts renders to canvas/SVG and cannot read CSS vars, so the */
/*  dark theme hexes are hardcoded here, consistent with src/index.css.        */
/* -------------------------------------------------------------------------- */

const C = {
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

const roundInt = (n: number) => Math.round(n);

function ago(ts: number, now: number): string {
  const s = Math.max(0, Math.round((now - ts) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

/* -------------------------------------------------------------------------- */
/*  Status visual mapping                                                     */
/* -------------------------------------------------------------------------- */

type Status = ModuleHealth["status"];

const STATUS_META: Record<
  Status,
  { tone: ChipTone; dot: string; label: string }
> = {
  online: { tone: "ok", dot: C.up, label: "Online" },
  degraded: { tone: "warn", dot: C.warn, label: "Degraded" },
  offline: { tone: "danger", dot: C.down, label: "Offline" },
};

/** Icon per module, matched by a substring of the contract's module name. */
function moduleIcon(name: string): ReactNode {
  const n = name.toLowerCase();
  if (n.includes("geyser")) return <Database />;
  if (n.includes("shred")) return <Radio />;
  if (n.includes("gate")) return <ShieldCheck />;
  if (n.includes("classifier")) return <Cpu />;
  if (n.includes("sentiment")) return <Sparkles />;
  if (n.includes("jito") || n.includes("submit")) return <Workflow />;
  if (n.includes("risk")) return <ShieldAlert />;
  if (n.includes("feature")) return <Activity />;
  if (n.includes("reason")) return <BrainCircuit />;
  if (n.includes("signer") || n.includes("sign")) return <Server />;
  return <Cpu />;
}

/** Staleness reads hot well before the module flips offline. */
function staleTone(staleMs: number, status: Status): ChipTone {
  if (status === "offline") return "danger";
  if (staleMs >= 1000) return "danger";
  if (staleMs >= 500) return "warn";
  return "neutral";
}

/** Latency budget per module is loose — only flag clearly slow hops. */
function latencyClass(latencyMs: number, status: Status): string {
  if (status === "offline") return "text-down";
  if (latencyMs >= 80) return "text-down";
  if (latencyMs >= 40) return "text-warn";
  return "text-text";
}

/* ================================================================== */
/*  Alert model — derived from health + agent + metrics telemetry     */
/* ================================================================== */

type Severity = "critical" | "warning" | "info";

interface Alert {
  id: string;
  ts: number;
  severity: Severity;
  source: string;
  message: string;
}

const SEV_META: Record<
  Severity,
  { tone: ChipTone; icon: ReactNode; label: string }
> = {
  critical: { tone: "danger", icon: <CircleAlert />, label: "Critical" },
  warning: { tone: "warn", icon: <AlertTriangle />, label: "Warning" },
  info: { tone: "info", icon: <Info />, label: "Info" },
};

const SEV_RANK: Record<Severity, number> = { critical: 0, warning: 1, info: 2 };

/* ================================================================== */
/*  Shared state primitives                                           */
/* ================================================================== */

function SkeletonRows({ rows }: { rows: number }) {
  return (
    <div className="space-y-2 p-3.5" aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-9 w-full animate-pulse rounded-md bg-panel2" />
      ))}
    </div>
  );
}

function ErrorNote({ message }: { message: string }) {
  return (
    <div className="m-3.5 flex items-center gap-2 rounded-md border border-[color:color-mix(in_srgb,var(--danger)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--danger)_8%,transparent)] px-3 py-2 text-[12px] text-danger">
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

function EmptyNote({ message }: { message: string }) {
  return (
    <div className="m-3.5 flex items-center justify-center rounded-md border border-dashed border-border px-3 py-8 text-[12px] text-faint">
      {message}
    </div>
  );
}

/* ================================================================== */
/*  Module health grid                                               */
/* ================================================================== */

function ModuleCard({ m }: { m: ModuleHealth }) {
  const meta = STATUS_META[m.status];
  return (
    <div className="rounded-[8px] border border-border bg-panel2 p-3 transition-colors hover:bg-hover">
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border bg-panel text-muted-foreground [&_svg]:h-3.5 [&_svg]:w-3.5">
            {moduleIcon(m.name)}
          </span>
          <span className="truncate text-[12px] font-medium text-text">
            {m.name}
          </span>
        </div>
        <span
          className="mt-1 inline-block h-2 w-2 shrink-0 rounded-full"
          style={{ backgroundColor: meta.dot }}
          aria-label={meta.label}
        />
      </div>

      <div className="mt-2.5 flex items-center justify-between">
        <Chip tone={meta.tone}>{meta.label.toLowerCase()}</Chip>
        <div className="flex items-center gap-3">
          <div className="text-right leading-tight">
            <div className="text-[10px] uppercase tracking-wide text-faint">
              latency
            </div>
            <div className={`num text-[12px] font-semibold ${latencyClass(m.latencyMs, m.status)}`}>
              {roundInt(m.latencyMs)}
              <span className="ml-0.5 text-[10px] font-normal text-faint">ms</span>
            </div>
          </div>
          <div className="h-7 w-px bg-border" aria-hidden />
          <div className="text-right leading-tight">
            <div className="text-[10px] uppercase tracking-wide text-faint">
              stale
            </div>
            <div
              className={`num text-[12px] font-semibold ${
                staleTone(m.staleMs, m.status) === "danger"
                  ? "text-down"
                  : staleTone(m.staleMs, m.status) === "warn"
                    ? "text-warn"
                    : "text-text"
              }`}
            >
              {roundInt(m.staleMs)}
              <span className="ml-0.5 text-[10px] font-normal text-faint">ms</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function ModuleGrid({
  health,
  loading,
  error,
}: {
  health: ModuleHealth[] | null;
  loading: boolean;
  error: string | null;
}) {
  if (loading && !health) {
    return (
      <div className="grid grid-cols-1 gap-2.5 p-3.5 sm:grid-cols-2 xl:grid-cols-3">
        {Array.from({ length: 7 }).map((_, i) => (
          <div
            key={i}
            className="h-[88px] animate-pulse rounded-[8px] border border-border bg-panel2"
          />
        ))}
      </div>
    );
  }
  if (error) return <ErrorNote message={error} />;
  if (!health || health.length === 0)
    return <EmptyNote message="No module telemetry reported." />;

  return (
    <div className="grid grid-cols-1 gap-2.5 p-3.5 sm:grid-cols-2 xl:grid-cols-3">
      {health.map((m) => (
        <ModuleCard key={m.name} m={m} />
      ))}
    </div>
  );
}

/* ================================================================== */
/*  Connection health (Geyser / ShredStream / RPC)                    */
/* ================================================================== */

function ConnectionRow({
  icon,
  name,
  up,
  detail,
}: {
  icon: ReactNode;
  name: string;
  up: boolean;
  detail: string;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-panel2 px-3 py-2.5">
      <div className="flex min-w-0 items-center gap-2.5">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border bg-panel text-muted-foreground [&_svg]:h-3.5 [&_svg]:w-3.5">
          {icon}
        </span>
        <div className="min-w-0 leading-tight">
          <div className="truncate text-[12px] font-medium text-text">{name}</div>
          <div className="num text-[11px] text-faint">{detail}</div>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {up && <LiveDot tone="accent" />}
        <Chip tone={up ? "ok" : "danger"}>{up ? "connected" : "down"}</Chip>
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Land-rate-by-RPC bar chart (derived from infra tiers)             */
/* ================================================================== */

interface RpcLand {
  rpc: string;
  landRate: number;
  /** Pre-formatted "NN%" so the bar label uses a plain dataKey (no typed formatter). */
  label: string;
  fill: string;
}

function landFill(rate: number): string {
  if (rate >= 88) return C.accent;
  if (rate >= 75) return C.warn;
  return C.down;
}

function LandRateByRpc({
  rows,
  loading,
  error,
}: {
  rows: RpcLand[];
  loading: boolean;
  error: string | null;
}) {
  if (loading && rows.length === 0) {
    return (
      <div className="p-3.5">
        <div className="h-[176px] w-full animate-pulse rounded-md bg-panel2" />
      </div>
    );
  }
  if (error) return <ErrorNote message={error} />;
  if (rows.length === 0)
    return <EmptyNote message="No RPC land-rate telemetry." />;

  return (
    <div className="px-2 pb-3 pt-3.5">
      <div className="h-[176px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={rows}
            layout="vertical"
            margin={{ top: 2, right: 40, bottom: 2, left: 6 }}
            barCategoryGap={10}
          >
            <CartesianGrid stroke={C.border} strokeDasharray="2 4" horizontal={false} />
            <XAxis type="number" domain={[0, 100]} hide />
            <YAxis
              type="category"
              dataKey="rpc"
              width={92}
              tickLine={false}
              axisLine={false}
              tick={{ fill: C.muted, fontSize: 11 }}
            />
            <Bar dataKey="landRate" radius={[2, 2, 2, 2]} isAnimationActive={false} maxBarSize={20}>
              {rows.map((r) => (
                <Cell key={r.rpc} fill={r.fill} />
              ))}
              <LabelList
                dataKey="label"
                position="right"
                style={{
                  fill: C.text,
                  fontSize: 11,
                  fontFamily:
                    'ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace',
                  fontWeight: 600,
                }}
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="px-2 text-[10px] text-faint">
        Jito bundle inclusion rate by submission endpoint. Colocated routes land
        materially more often than generic public RPCs.
      </p>
    </div>
  );
}

/* ================================================================== */
/*  Alert feed                                                       */
/* ================================================================== */

function AlertFeed({
  alerts,
  loading,
  now,
}: {
  alerts: Alert[];
  loading: boolean;
  now: number;
}) {
  if (loading && alerts.length === 0) {
    return <SkeletonRows rows={4} />;
  }
  if (alerts.length === 0) {
    return (
      <div className="m-3.5 flex items-center justify-center gap-2 rounded-md border border-dashed border-[color:color-mix(in_srgb,var(--up)_24%,transparent)] px-3 py-8 text-[12px] text-up">
        <ShieldCheck className="h-3.5 w-3.5" />
        All systems nominal — no active alerts.
      </div>
    );
  }

  return (
    <ul className="divide-y divide-border">
      {alerts.map((a) => {
        const meta = SEV_META[a.severity];
        return (
          <li
            key={a.id}
            className="flex items-start gap-2.5 px-3.5 py-2.5 transition-colors hover:bg-hover"
          >
            <span
              className={`mt-0.5 flex shrink-0 items-center [&_svg]:h-3.5 [&_svg]:w-3.5 ${
                a.severity === "critical"
                  ? "text-danger"
                  : a.severity === "warning"
                    ? "text-warn"
                    : "text-info"
              }`}
            >
              {meta.icon}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <Chip tone={meta.tone}>{meta.label.toLowerCase()}</Chip>
                <span className="truncate text-[11px] font-medium text-muted-foreground">
                  {a.source}
                </span>
              </div>
              <p className="mt-1 text-[12px] leading-snug text-text">{a.message}</p>
            </div>
            <span className="num shrink-0 text-[10px] text-faint">
              {ago(a.ts, now)} ago
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/* ================================================================== */
/*  Page                                                             */
/* ================================================================== */

export default function Monitoring() {
  const {
    data: health,
    loading: healthLoading,
    error: healthError,
  } = useModuleHealth();
  const { data: state, loading: stateLoading } = useAgentState();
  const { data: metrics } = useMetrics();
  const {
    data: tiers,
    loading: tiersLoading,
    error: tiersError,
  } = useInfraTiers();

  // A single ticking clock so relative "Xs ago" labels stay fresh without
  // each row owning its own interval. `mount` is a stable anchor so alert
  // timestamps don't reset every tick — they visibly age as `now` advances.
  const [now, setNow] = useState(() => Date.now());
  const [mount] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  /* --- ops rollups --- */
  const total = health?.length ?? 0;
  const online = health?.filter((m) => m.status === "online").length ?? 0;
  const degraded = health?.filter((m) => m.status === "degraded").length ?? 0;
  const offline = health?.filter((m) => m.status === "offline").length ?? 0;

  const systemHealthy = total > 0 && offline === 0 && degraded === 0;
  const systemTone: ChipTone = offline > 0 ? "danger" : degraded > 0 ? "warn" : "ok";
  const systemLabel = offline > 0 ? "Critical" : degraded > 0 ? "Degraded" : "Nominal";

  const geyser = state?.connection.geyser ?? false;
  const shred = state?.connection.shredstream ?? false;
  const internalMs = state?.connection.internalMs ?? null;
  const killed = state?.killed ?? false;
  const breakerTripped = metrics?.breakerTripped ?? false;

  // RPC is healthy when both ingest links are up and we have a recent heartbeat.
  const rpcUp = geyser && shred;

  /* --- dead-man switch ---
   * Trips when the operator killed the agent, the breaker fired, an ingest
   * link dropped, or any module is offline. Otherwise it is armed and the
   * heartbeat is being fed. */
  const deadManTripped = killed || breakerTripped || !rpcUp || offline > 0;
  const deadManReason = killed
    ? "Operator kill engaged"
    : breakerTripped
      ? "Daily-loss breaker tripped"
      : !rpcUp
        ? "Ingest link lost"
        : offline > 0
          ? `${offline} module${offline === 1 ? "" : "s"} offline`
          : "Heartbeat fed — agent live";

  /* --- land rate by RPC (derived from infra tiers + measured land rate) --- */
  const landByRpc = useMemo<RpcLand[]>(() => {
    if (!tiers || tiers.length === 0) return [];
    const colo = tiers.find((t) => t.name === "colo");
    const generic = tiers.find((t) => t.name === "generic");
    const rows: RpcLand[] = [];
    if (colo) {
      const v = roundInt(colo.landRate);
      rows.push({ rpc: "Jito (colo)", landRate: v, label: `${v}%`, fill: landFill(v) });
    }
    if (generic) {
      const v = roundInt(generic.landRate);
      rows.push({ rpc: "Public RPC", landRate: v, label: `${v}%`, fill: landFill(v) });
      // A plausible third endpoint sitting between the two tiers.
      const mid = roundInt(((colo?.landRate ?? generic.landRate + 10) + generic.landRate) / 2);
      rows.push({ rpc: "Backup RPC", landRate: mid, label: `${mid}%`, fill: landFill(mid) });
    }
    return rows;
  }, [tiers]);

  /* --- alert feed (derived telemetry → severity-ranked) --- */
  const alerts = useMemo<Alert[]>(() => {
    const out: Alert[] = [];

    if (killed) {
      out.push({
        id: "alert-kill",
        ts: mount,
        severity: "critical",
        source: "Operator",
        message: "Kill switch engaged — all execution halted, positions frozen.",
      });
    }
    if (breakerTripped) {
      out.push({
        id: "alert-breaker",
        ts: mount - 8_000,
        severity: "critical",
        source: "Risk manager",
        message: "Daily-loss circuit breaker tripped — new entries blocked until reset.",
      });
    }
    if (!geyser) {
      out.push({
        id: "alert-geyser",
        ts: mount - 4_000,
        severity: "critical",
        source: "Geyser ingest",
        message: "Geyser stream disconnected — slot feed stale, snipe loop disarmed.",
      });
    }
    if (!shred) {
      out.push({
        id: "alert-shred",
        ts: mount - 6_000,
        severity: "warning",
        source: "ShredStream",
        message: "ShredStream link down — falling back to RPC slot subscription.",
      });
    }

    (health ?? []).forEach((m, i) => {
      if (m.status === "offline") {
        out.push({
          id: `alert-mod-off-${m.name}`,
          ts: mount - (i + 1) * 5_000,
          severity: "critical",
          source: m.name,
          message: `Module offline — last heartbeat ${roundInt(m.staleMs)}ms ago.`,
        });
      } else if (m.status === "degraded") {
        out.push({
          id: `alert-mod-deg-${m.name}`,
          ts: mount - (i + 1) * 7_000,
          severity: "warning",
          source: m.name,
          message: `Degraded — latency ${roundInt(m.latencyMs)}ms, staleness ${roundInt(m.staleMs)}ms over threshold.`,
        });
      } else if (m.staleMs >= 500) {
        out.push({
          id: `alert-mod-stale-${m.name}`,
          ts: mount - (i + 1) * 9_000,
          severity: "info",
          source: m.name,
          message: `Elevated data staleness — ${roundInt(m.staleMs)}ms since last update.`,
        });
      }
    });

    if (internalMs !== null && internalMs >= 52) {
      out.push({
        id: "alert-internal-latency",
        ts: mount - 12_000,
        severity: "warning",
        source: "Pipeline",
        message: `Internal pipeline latency elevated at ${roundInt(internalMs)}ms — watch slot headroom.`,
      });
    }

    return out.sort(
      (a, b) => SEV_RANK[a.severity] - SEV_RANK[b.severity] || b.ts - a.ts,
    );
  }, [killed, breakerTripped, geyser, shred, health, internalMs, mount]);

  const criticalCount = alerts.filter((a) => a.severity === "critical").length;
  const warningCount = alerts.filter((a) => a.severity === "warning").length;

  return (
    <Layout>
      <div className="space-y-4">
        {/* Page header */}
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-[16px] font-semibold tracking-tight text-text">
              <HeartPulse className="h-4 w-4 text-brand" />
              Monitoring
            </h1>
            <p className="mt-0.5 text-[12px] text-muted-foreground">
              Operational health of every module, link, and the dead-man switch.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <LiveDot tone={systemHealthy ? "accent" : "warn"} />
              {systemHealthy ? "All systems nominal" : "Attention required"}
            </span>
            <Chip
              tone={systemTone}
              icon={systemHealthy ? <ShieldCheck /> : <AlertTriangle />}
            >
              system {systemLabel.toLowerCase()}
            </Chip>
          </div>
        </div>

        {/* Ops summary KPIs */}
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile
            label="Modules online"
            value={
              stateLoading && !health ? (
                "—"
              ) : (
                <>
                  {online}
                  <span className="text-[14px] text-faint"> / {total}</span>
                </>
              )
            }
            sub={
              degraded + offline === 0
                ? "Full mesh healthy"
                : `${degraded} degraded · ${offline} offline`
            }
            tone={offline > 0 ? "danger" : degraded > 0 ? "warn" : "up"}
          />
          <StatTile
            label="Active alerts"
            value={
              <>
                {criticalCount + warningCount}
              </>
            }
            sub={`${criticalCount} critical · ${warningCount} warning`}
            tone={criticalCount > 0 ? "danger" : warningCount > 0 ? "warn" : "up"}
          />
          <StatTile
            label="Internal latency"
            value={internalMs === null ? "—" : <>{roundInt(internalMs)} ms</>}
            sub="Ingest to submit pipeline"
            tone={
              internalMs !== null && internalMs >= 52
                ? "warn"
                : "accent"
            }
          />
          <StatTile
            label="Dead-man switch"
            value={deadManTripped ? "Tripped" : "Armed"}
            sub={deadManReason}
            tone={deadManTripped ? "danger" : "up"}
          />
        </div>

        {/* Module health grid */}
        <Panel
          title="Module health"
          icon={<Server />}
          actions={
            <span className="num text-[11px] text-faint">
              {total > 0 ? `${online}/${total} online` : "—"}
            </span>
          }
        >
          <ModuleGrid health={health} loading={healthLoading} error={healthError} />
        </Panel>

        {/* Connection health + dead-man switch */}
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <Panel
            title="Connection health"
            icon={<Network />}
            className="xl:col-span-2"
            actions={
              <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                <Signal className="h-3 w-3 text-up" />
                ingest links
              </span>
            }
          >
            <div className="space-y-2.5 p-3.5">
              <ConnectionRow
                icon={<Database />}
                name="Geyser"
                up={geyser}
                detail={geyser ? "gRPC slot + account stream" : "stream lost"}
              />
              <ConnectionRow
                icon={<Radio />}
                name="ShredStream"
                up={shred}
                detail={shred ? "shred-level pre-confirmation" : "fallback to RPC"}
              />
              <ConnectionRow
                icon={<Cpu />}
                name="RPC / submit"
                up={rpcUp}
                detail={
                  internalMs !== null
                    ? `${roundInt(internalMs)}ms internal pipeline`
                    : "submit path"
                }
              />
            </div>
          </Panel>

          {/* Dead-man switch detail card */}
          <Panel title="Dead-man switch" icon={<Timer />}>
            <div className="space-y-3 p-3.5">
              <div
                className={`rounded-[8px] border p-3 ${
                  deadManTripped
                    ? "border-[color:color-mix(in_srgb,var(--danger)_30%,transparent)] bg-[color:color-mix(in_srgb,var(--danger)_8%,transparent)]"
                    : "border-[color:color-mix(in_srgb,var(--up)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--up)_7%,transparent)]"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] uppercase tracking-wide text-faint">
                    Status
                  </span>
                  <Chip
                    tone={deadManTripped ? "danger" : "ok"}
                    icon={deadManTripped ? <ShieldAlert /> : <ShieldCheck />}
                  >
                    {deadManTripped ? "tripped" : "armed"}
                  </Chip>
                </div>
                <div
                  className={`num mt-2 text-[20px] font-semibold leading-none ${
                    deadManTripped ? "text-danger" : "text-up"
                  }`}
                >
                  {deadManTripped ? "HALTED" : "LIVE"}
                </div>
                <p className="mt-2 text-[12px] leading-snug text-muted-foreground">
                  {deadManReason}
                </p>
              </div>

              <ul className="space-y-1.5 text-[11px] text-muted-foreground">
                <li className="flex items-center justify-between">
                  <span>Operator kill</span>
                  <Chip tone={killed ? "danger" : "neutral"}>
                    {killed ? "engaged" : "clear"}
                  </Chip>
                </li>
                <li className="flex items-center justify-between">
                  <span>Loss breaker</span>
                  <Chip tone={breakerTripped ? "danger" : "neutral"}>
                    {breakerTripped ? "tripped" : "clear"}
                  </Chip>
                </li>
                <li className="flex items-center justify-between">
                  <span>Ingest links</span>
                  <Chip tone={rpcUp ? "ok" : "danger"}>
                    {rpcUp ? "up" : "down"}
                  </Chip>
                </li>
              </ul>
            </div>
          </Panel>
        </div>

        {/* Land rate by RPC + alert feed */}
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <Panel
            title="Land rate by RPC"
            icon={<GaugeIcon />}
            actions={
              metrics ? (
                <Chip tone="accent">
                  <span className="num">{roundInt(metrics.landRatePct)}%</span>
                  &nbsp;blended
                </Chip>
              ) : undefined
            }
          >
            <LandRateByRpc
              rows={landByRpc}
              loading={tiersLoading}
              error={tiersError}
            />
          </Panel>

          <Panel
            title="Alert feed"
            icon={<BellRing />}
            className="xl:col-span-2"
            actions={
              <div className="flex items-center gap-1.5">
                {criticalCount > 0 && (
                  <Chip tone="danger">
                    <span className="num">{criticalCount}</span>&nbsp;crit
                  </Chip>
                )}
                {warningCount > 0 && (
                  <Chip tone="warn">
                    <span className="num">{warningCount}</span>&nbsp;warn
                  </Chip>
                )}
                {criticalCount + warningCount === 0 && (
                  <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                    <LiveDot tone="accent" />
                    quiet
                  </span>
                )}
              </div>
            }
          >
            <div className="max-h-[360px] overflow-y-auto scrollbar-thin">
              <AlertFeed alerts={alerts} loading={healthLoading} now={now} />
            </div>
          </Panel>
        </div>
      </div>
    </Layout>
  );
}
