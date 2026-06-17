import { useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router";
import {
  Activity,
  Brain,
  ChevronLeft,
  ChevronRight,
  Crosshair,
  Gauge as GaugeIcon,
  HeartPulse,
  LayoutDashboard,
  ListChecks,
  MessageSquareText,
  Network,
  Newspaper,
  Radio,
  Settings,
  ShieldAlert,
  Sparkles,
  Users,
  Wallet,
} from "lucide-react";
import { Toaster } from "@/components/ui/sonner";
import { KillSwitch, LiveDot, ModeBadge } from "@/components/kit";
import { flattenAll, useAgentState } from "@/lib/api";
import { cn } from "@/lib/utils";
import { toast } from "sonner";

interface NavItem {
  path: string;
  icon: typeof LayoutDashboard;
  label: string;
}

const NAV: NavItem[] = [
  { path: "/", icon: LayoutDashboard, label: "Command" },
  { path: "/feed", icon: Crosshair, label: "Snipe feed" },
  { path: "/candidates", icon: ListChecks, label: "Candidates" },
  { path: "/wallet-clusters", icon: Network, label: "Wallet clusters" },
  { path: "/latency", icon: GaugeIcon, label: "Latency" },
  { path: "/positions", icon: Wallet, label: "Positions" },
  { path: "/copy-trade", icon: Users, label: "Copy-trade" },
  { path: "/sentiment", icon: MessageSquareText, label: "Sentiment" },
  { path: "/narrative", icon: Newspaper, label: "Narrative" },
  { path: "/model", icon: Brain, label: "Model" },
  { path: "/reasoning", icon: Sparkles, label: "Reasoning" },
  { path: "/risk", icon: ShieldAlert, label: "Risk" },
  { path: "/monitoring", icon: HeartPulse, label: "Monitoring" },
  { path: "/settings", icon: Settings, label: "Settings" },
];

function FlattenButton() {
  const [busy, setBusy] = useState(false);
  async function onFlatten() {
    setBusy(true);
    try {
      await flattenAll();
      toast.warning("Flatten all sent", {
        description: "Market-exiting every open position.",
      });
    } catch (e) {
      toast.error("Flatten failed", {
        description: e instanceof Error ? e.message : "Unknown error",
      });
    } finally {
      setBusy(false);
    }
  }
  return (
    <button
      type="button"
      onClick={onFlatten}
      disabled={busy}
      className="inline-flex items-center gap-1.5 rounded-md border border-border bg-panel2 px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-hover hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-not-allowed disabled:opacity-60"
    >
      Flatten all
    </button>
  );
}

export default function Layout({ children }: { children: ReactNode }) {
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(false);
  const { data: state } = useAgentState();

  const geyser = state?.connection.geyser ?? false;
  const shred = state?.connection.shredstream ?? false;
  const internalMs = state?.connection.internalMs ?? null;
  const linkUp = geyser && shred;

  return (
    <div className="flex h-screen min-h-screen bg-bg text-text">
      {/* Sidebar */}
      <aside
        className={cn(
          "flex shrink-0 flex-col border-r border-border bg-panel transition-[width] duration-200",
          collapsed ? "w-[60px]" : "w-[208px]",
        )}
      >
        {/* Brand */}
        <div className="flex h-14 items-center gap-2 border-b border-border px-3.5">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-[color:color-mix(in_srgb,var(--accent)_30%,transparent)] bg-[color:color-mix(in_srgb,var(--accent)_12%,transparent)] text-brand">
            <Crosshair className="h-4 w-4" />
          </span>
          {!collapsed && (
            <div className="min-w-0 leading-tight">
              <div className="truncate text-[13px] font-semibold tracking-tight text-text">
                AATS Sniper
              </div>
              <div className="text-[10px] text-faint">Operator deck</div>
            </div>
          )}
        </div>

        {/* Nav */}
        <nav className="flex-1 space-y-0.5 overflow-y-auto px-2 py-2 scrollbar-thin">
          {NAV.map((item) => {
            const active = location.pathname === item.path;
            const Icon = item.icon;
            return (
              <Link
                key={item.path}
                to={item.path}
                title={collapsed ? item.label : undefined}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "group relative flex items-center gap-2.5 rounded-md px-2.5 py-2 text-[12px] font-medium transition-colors",
                  active
                    ? "bg-[color:color-mix(in_srgb,var(--accent)_10%,transparent)] text-brand"
                    : "text-muted-foreground hover:bg-hover hover:text-text",
                )}
              >
                {active && (
                  <span className="absolute inset-y-1.5 left-0 w-0.5 rounded-full bg-brand" />
                )}
                <Icon className="h-4 w-4 shrink-0" />
                {!collapsed && <span className="truncate">{item.label}</span>}
              </Link>
            );
          })}
        </nav>

        {/* Collapse toggle */}
        <button
          type="button"
          onClick={() => setCollapsed((c) => !c)}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="flex h-9 items-center justify-center gap-1.5 border-t border-border text-[11px] text-faint transition-colors hover:bg-hover hover:text-muted-foreground"
        >
          {collapsed ? (
            <ChevronRight className="h-3.5 w-3.5" />
          ) : (
            <>
              <ChevronLeft className="h-3.5 w-3.5" />
              <span>Collapse</span>
            </>
          )}
        </button>
      </aside>

      {/* Main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* Top bar */}
        <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-border bg-panel px-4">
          {/* Identity + connection */}
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex items-center gap-2">
              <LiveDot tone={linkUp ? "accent" : "danger"} />
              <span className="text-[13px] font-semibold tracking-tight text-text">
                AATS Sniper
              </span>
            </div>
            <span className="hidden h-4 w-px bg-border sm:block" />
            <div className="hidden items-center gap-1.5 text-[11px] text-muted-foreground sm:flex">
              <Radio
                className={cn("h-3 w-3", geyser ? "text-up" : "text-down")}
              />
              <span>Geyser</span>
              <Activity
                className={cn(
                  "ml-1.5 h-3 w-3",
                  shred ? "text-up" : "text-down",
                )}
              />
              <span>ShredStream</span>
              {internalMs !== null && (
                <span className="num ml-1.5 text-faint">{internalMs}ms internal</span>
              )}
            </div>
          </div>

          {/* Controls */}
          <div className="flex shrink-0 items-center gap-2.5">
            {state && <ModeBadge mode={state.mode} />}
            <FlattenButton />
            <KillSwitch />
          </div>
        </header>

        {/* Content */}
        <main className="flex-1 overflow-auto p-4 scrollbar-thin">{children}</main>
      </div>

      {/* Toasts — themed to the dark panel palette so they blend in. */}
      <Toaster
        theme="dark"
        position="bottom-right"
        toastOptions={{
          style: {
            background: "var(--panel-2)",
            color: "var(--text)",
            border: "1px solid var(--border)",
          },
        }}
      />
    </div>
  );
}
