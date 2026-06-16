import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Brain,
  Clock,
  Coins,
  Copy,
  Crosshair,
  Gauge as GaugeIcon,
  Globe,
  KeyRound,
  Layers,
  Percent,
  Radio,
  RotateCcw,
  Save,
  Server,
  ShieldAlert,
  ShieldHalf,
  Sliders,
  Wallet,
  Wifi,
} from "lucide-react";
import { toast } from "sonner";
import {
  resetBreaker,
  setMode,
  useAgentState,
  useRiskConfig,
} from "@/lib/api";
import type { AgentMode, RiskConfig } from "@/lib/types";
import { Chip, KillSwitch, LiveDot, ModeBadge, Panel } from "@/components/kit";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import Layout from "@/components/Layout";

/* -------------------------------------------------------------------------- */
/*  Display helpers — every number rounded for display                        */
/* -------------------------------------------------------------------------- */

const fmt1 = (n: number) => (Math.round(n * 10) / 10).toFixed(1);
const fmt2 = (n: number) => (Math.round(n * 100) / 100).toFixed(2);
const fmtInt = (n: number) => `${Math.round(n)}`;

/* -------------------------------------------------------------------------- */
/*  Shared state primitives (mirror the other operator pages)                 */
/* -------------------------------------------------------------------------- */

function SkeletonRows({ rows }: { rows: number }) {
  return (
    <div className="space-y-2" aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-7 w-full animate-pulse rounded-md bg-panel2" />
      ))}
    </div>
  );
}

function ErrorNote({ message }: { message: string }) {
  return (
    <div className="flex items-center gap-2 rounded-md border border-[color:color-mix(in_srgb,var(--danger)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--danger)_8%,transparent)] px-3 py-2 text-[12px] text-danger">
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Risk slider control                                                       */
/* -------------------------------------------------------------------------- */

interface RiskField {
  key: keyof RiskConfig;
  label: string;
  icon: typeof Sliders;
  min: number;
  max: number;
  step: number;
  /** Format the live readout for this field. */
  display: (v: number) => string;
  hint?: string;
}

const RISK_FIELDS: RiskField[] = [
  {
    key: "maxPositionSol",
    label: "Max position",
    icon: Coins,
    min: 0.5,
    max: 25,
    step: 0.5,
    display: (v) => `${fmt1(v)} SOL`,
    hint: "Hard ceiling on per-snipe notional.",
  },
  {
    key: "maxPositionPct",
    label: "Max position % of portfolio",
    icon: Percent,
    min: 1,
    max: 50,
    step: 1,
    display: (v) => `${fmtInt(v)}%`,
    hint: "Sizing relative to deployable capital.",
  },
  {
    key: "stopLossPct",
    label: "Stop loss",
    icon: ShieldAlert,
    min: 5,
    max: 50,
    step: 1,
    display: (v) => `-${fmtInt(v)}%`,
    hint: "Hard stop below entry.",
  },
  {
    key: "takeProfitPct",
    label: "Take profit",
    icon: GaugeIcon,
    min: 10,
    max: 300,
    step: 5,
    display: (v) => `+${fmtInt(v)}%`,
    hint: "First-leg target before trailing.",
  },
  {
    key: "maxSlippageBps",
    label: "Max slippage",
    icon: Activity,
    min: 25,
    max: 1000,
    step: 25,
    display: (v) => `${fmtInt(v)} bps`,
    hint: "Reject fills worse than this.",
  },
  {
    key: "dailyLossLimitSol",
    label: "Daily loss limit",
    icon: ShieldAlert,
    min: 1,
    max: 50,
    step: 1,
    display: (v) => `${fmtInt(v)} SOL`,
    hint: "Breaker trips when breached.",
  },
  {
    key: "maxHoldMin",
    label: "Max hold time",
    icon: Clock,
    min: 5,
    max: 720,
    step: 5,
    display: (v) => `${fmtInt(v)} min`,
    hint: "Force-exit stale positions.",
  },
  {
    key: "snipeThreshold",
    label: "Snipe p threshold",
    icon: Crosshair,
    min: 0.3,
    max: 0.95,
    step: 0.01,
    display: (v) => fmt2(v),
    hint: "Classifier p at or above this fires.",
  },
  {
    key: "vetoThreshold",
    label: "Veto threshold",
    icon: ShieldHalf,
    min: 0.05,
    max: 0.6,
    step: 0.01,
    display: (v) => fmt2(v),
    hint: "Classifier p below this is vetoed.",
  },
  {
    key: "jitoTipCapSol",
    label: "Jito tip cap",
    icon: Coins,
    min: 0.001,
    max: 0.25,
    step: 0.001,
    display: (v) => `${fmt2(v)} SOL`,
    hint: "Per-bundle priority-tip ceiling.",
  },
];

function RiskSlider({
  field,
  value,
  onChange,
  disabled,
}: {
  field: RiskField;
  value: number;
  onChange: (v: number) => void;
  disabled: boolean;
}) {
  const Icon = field.icon;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-1.5 text-[12px] font-medium text-muted-foreground">
          <Icon className="h-3.5 w-3.5 text-faint" />
          {field.label}
        </span>
        <span className="num min-w-[64px] rounded-md border border-border bg-panel2 px-1.5 py-0.5 text-right text-[12px] font-semibold text-brand">
          {field.display(value)}
        </span>
      </div>
      <Slider
        value={[value]}
        onValueChange={([v]) => onChange(v)}
        min={field.min}
        max={field.max}
        step={field.step}
        disabled={disabled}
        aria-label={field.label}
        className="w-full"
      />
      {field.hint && (
        <div className="text-[11px] leading-snug text-faint">{field.hint}</div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Risk configuration panel                                                  */
/* -------------------------------------------------------------------------- */

function RiskConfigPanel() {
  const { config, loading, error, saving, save } = useRiskConfig();
  const [draft, setDraft] = useState<RiskConfig | null>(null);

  // Hydrate the editable draft once config arrives (and on external changes).
  useEffect(() => {
    if (config) setDraft(config);
  }, [config]);

  const dirty = useMemo(() => {
    if (!config || !draft) return false;
    return (Object.keys(config) as (keyof RiskConfig)[]).some(
      (k) => config[k] !== draft[k],
    );
  }, [config, draft]);

  function setField(key: keyof RiskConfig, v: number) {
    setDraft((d) => (d ? { ...d, [key]: v } : d));
  }

  async function onSave() {
    if (!draft) return;
    try {
      await save(draft);
      toast.success("Risk configuration saved", {
        description: "Limits applied to the live risk manager.",
      });
    } catch (e) {
      toast.error("Save failed", {
        description: e instanceof Error ? e.message : "Unknown error",
      });
    }
  }

  function onRevert() {
    if (config) {
      setDraft(config);
      toast.message("Reverted to last saved configuration");
    }
  }

  return (
    <Panel
      title="Risk parameters"
      icon={<ShieldAlert />}
      actions={
        dirty ? (
          <Chip tone="warn">unsaved</Chip>
        ) : (
          <Chip tone="ok">synced</Chip>
        )
      }
    >
      {loading && !draft ? (
        <SkeletonRows rows={8} />
      ) : error || !draft ? (
        <ErrorNote message={error ?? "No risk configuration"} />
      ) : (
        <div className="space-y-5">
          <div className="grid grid-cols-1 gap-x-8 gap-y-5 md:grid-cols-2">
            {RISK_FIELDS.map((f) => (
              <RiskSlider
                key={f.key}
                field={f}
                value={draft[f.key]}
                onChange={(v) => setField(f.key, v)}
                disabled={saving}
              />
            ))}
          </div>

          <div className="flex items-center justify-between gap-3 border-t border-border pt-3.5">
            <p className="text-[11px] leading-snug text-faint">
              Changes are staged locally and applied to the risk manager only on
              save.
            </p>
            <div className="flex shrink-0 items-center gap-2">
              <button
                type="button"
                onClick={onRevert}
                disabled={!dirty || saving}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-panel2 px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-hover hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                Revert
              </button>
              <button
                type="button"
                onClick={onSave}
                disabled={!dirty || saving}
                className="inline-flex items-center gap-1.5 rounded-md border border-[color:color-mix(in_srgb,var(--accent)_40%,transparent)] bg-[color:color-mix(in_srgb,var(--accent)_16%,transparent)] px-3 py-1.5 text-[12px] font-semibold text-brand transition-colors hover:bg-[color:color-mix(in_srgb,var(--accent)_26%,transparent)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:color-mix(in_srgb,var(--accent)_50%,transparent)] disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Save className="h-3.5 w-3.5" />
                {saving ? "Saving…" : "Save config"}
              </button>
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}

/* -------------------------------------------------------------------------- */
/*  Venue / RPC panel (operator-side endpoints — local stub state)            */
/* -------------------------------------------------------------------------- */

interface VenueField {
  key: string;
  label: string;
  icon: typeof Server;
  placeholder: string;
  initial: string;
}

const VENUE_FIELDS: VenueField[] = [
  {
    key: "rpcUrl",
    label: "RPC url",
    icon: Server,
    placeholder: "https://…",
    initial: "https://mainnet.helius-rpc.com/?api-key=•••",
  },
  {
    key: "stakedEndpoint",
    label: "Staked endpoint",
    icon: Wifi,
    placeholder: "https://…",
    initial: "https://staked.example.rpcpool.com",
  },
  {
    key: "region",
    label: "Region",
    icon: Globe,
    placeholder: "e.g. ams / fra / ny",
    initial: "fra (colo)",
  },
  {
    key: "jitoBlockEngine",
    label: "Jito block engine",
    icon: Radio,
    placeholder: "https://…",
    initial: "frankfurt.mainnet.block-engine.jito.wtf",
  },
];

function VenuePanel() {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(VENUE_FIELDS.map((f) => [f.key, f.initial])),
  );
  const [savedSnapshot] = useState(values);

  const dirty = useMemo(
    () => VENUE_FIELDS.some((f) => values[f.key] !== savedSnapshot[f.key]),
    [values, savedSnapshot],
  );

  return (
    <Panel
      title="Venue & RPC"
      icon={<Server />}
      actions={
        <button
          type="button"
          disabled={!dirty}
          onClick={() =>
            toast.success("Endpoints updated", {
              description: "Connection pool will rotate on next reconnect.",
            })
          }
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-panel2 px-2.5 py-1 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-hover hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
        >
          Apply
        </button>
      }
    >
      <div className="grid grid-cols-1 gap-3.5 md:grid-cols-2">
        {VENUE_FIELDS.map((f) => {
          const Icon = f.icon;
          return (
            <div key={f.key} className="space-y-1.5">
              <Label
                htmlFor={`venue-${f.key}`}
                className="text-[11px] font-medium uppercase tracking-wide text-faint"
              >
                <Icon className="h-3 w-3 text-faint" />
                {f.label}
              </Label>
              <Input
                id={`venue-${f.key}`}
                value={values[f.key]}
                placeholder={f.placeholder}
                spellCheck={false}
                autoComplete="off"
                onChange={(e) =>
                  setValues((v) => ({ ...v, [f.key]: e.target.value }))
                }
                className="num h-8 rounded-md border-border bg-panel2 text-[12px] text-text placeholder:text-faint"
              />
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

/* -------------------------------------------------------------------------- */
/*  Strategy toggles panel                                                    */
/* -------------------------------------------------------------------------- */

interface StrategyToggle {
  key: string;
  label: string;
  desc: string;
  initial: boolean;
}

const STRATEGY_TOGGLES: StrategyToggle[] = [
  {
    key: "migrationSnipes",
    label: "Migration snipes",
    desc: "Fire on pump.fun → AMM migration events.",
    initial: true,
  },
  {
    key: "safetyLateEntry",
    label: "Safety-selective late entry",
    desc: "Allow late entries only on gate-clean, high-conviction tokens.",
    initial: true,
  },
  {
    key: "copyTradeSelectivity",
    label: "Copy-trade selectivity",
    desc: "Follow only proven smart-money wallets above the cluster bar.",
    initial: false,
  },
  {
    key: "multiWallet",
    label: "Multi-wallet",
    desc: "Spread entries across signer wallets to reduce footprint.",
    initial: false,
  },
];

function StrategyPanel() {
  const [toggles, setToggles] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(STRATEGY_TOGGLES.map((t) => [t.key, t.initial])),
  );

  function flip(key: string, on: boolean) {
    setToggles((t) => ({ ...t, [key]: on }));
    const meta = STRATEGY_TOGGLES.find((s) => s.key === key);
    toast.message(`${meta?.label ?? key} ${on ? "enabled" : "disabled"}`);
  }

  return (
    <Panel title="Strategy" icon={<Layers />}>
      <ul className="divide-y divide-border">
        {STRATEGY_TOGGLES.map((t) => (
          <li
            key={t.key}
            className="flex items-center justify-between gap-4 py-2.5 first:pt-0 last:pb-0"
          >
            <div className="min-w-0">
              <Label
                htmlFor={`strat-${t.key}`}
                className="text-[12px] font-medium text-text"
              >
                {t.label}
              </Label>
              <p className="mt-0.5 text-[11px] leading-snug text-faint">
                {t.desc}
              </p>
            </div>
            <Switch
              id={`strat-${t.key}`}
              checked={toggles[t.key]}
              onCheckedChange={(on) => flip(t.key, on)}
              className="shrink-0 data-[state=checked]:bg-brand"
              aria-label={t.label}
            />
          </li>
        ))}
      </ul>
    </Panel>
  );
}

/* -------------------------------------------------------------------------- */
/*  Mode switch panel — live requires explicit confirm                        */
/* -------------------------------------------------------------------------- */

const MODE_META: Record<
  AgentMode,
  { label: string; desc: string; icon: typeof Radio }
> = {
  paper: {
    label: "Paper",
    desc: "Simulated fills, no broadcast. Zero capital at risk.",
    icon: Brain,
  },
  "dry-run": {
    label: "Dry run",
    desc: "Full pipeline, transactions built but not submitted.",
    icon: ShieldHalf,
  },
  live: {
    label: "Live",
    desc: "Real submissions to Jito. Capital is at risk.",
    icon: Radio,
  },
};

const MODE_ORDER: AgentMode[] = ["paper", "dry-run", "live"];

function ModePanel() {
  const { data: state, loading, refetch } = useAgentState();
  const [pending, setPending] = useState<AgentMode | null>(null);
  const [switching, setSwitching] = useState(false);

  const current = state?.mode ?? null;

  async function commit(next: AgentMode) {
    setSwitching(true);
    try {
      await setMode(next);
      toast.success(`Mode set to ${MODE_META[next].label.toLowerCase()}`, {
        description:
          next === "live"
            ? "Live submissions are now armed."
            : "No live capital is at risk.",
      });
      refetch();
    } catch (e) {
      toast.error("Mode change failed", {
        description: e instanceof Error ? e.message : "Unknown error",
      });
    } finally {
      setSwitching(false);
    }
  }

  function request(next: AgentMode) {
    if (next === current || switching) return;
    if (next === "live") {
      setPending("live"); // route through confirm dialog
      return;
    }
    void commit(next);
  }

  return (
    <Panel
      title="Agent mode"
      icon={<Radio />}
      actions={current ? <ModeBadge mode={current} /> : null}
    >
      {loading && !state ? (
        <SkeletonRows rows={3} />
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            {MODE_ORDER.map((m) => {
              const meta = MODE_META[m];
              const Icon = meta.icon;
              const active = current === m;
              const isLive = m === "live";
              return (
                <button
                  key={m}
                  type="button"
                  onClick={() => request(m)}
                  disabled={switching || active}
                  aria-pressed={active}
                  className={[
                    "flex flex-col gap-1.5 rounded-md border px-3 py-2.5 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-default",
                    active && isLive
                      ? "border-[color:color-mix(in_srgb,var(--accent)_45%,transparent)] bg-[color:color-mix(in_srgb,var(--accent)_14%,transparent)]"
                      : active
                        ? "border-[color:color-mix(in_srgb,var(--info)_40%,transparent)] bg-[color:color-mix(in_srgb,var(--info)_12%,transparent)]"
                        : "border-border bg-panel2 hover:bg-hover",
                  ].join(" ")}
                >
                  <span className="flex items-center justify-between">
                    <span
                      className={[
                        "flex items-center gap-1.5 text-[12px] font-semibold",
                        active && isLive
                          ? "text-brand"
                          : active
                            ? "text-info"
                            : "text-text",
                      ].join(" ")}
                    >
                      <Icon className="h-3.5 w-3.5" />
                      {meta.label}
                    </span>
                    {active && <LiveDot tone={isLive ? "accent" : "warn"} />}
                  </span>
                  <span className="text-[11px] leading-snug text-faint">
                    {meta.desc}
                  </span>
                </button>
              );
            })}
          </div>
          <p className="flex items-center gap-1.5 text-[11px] text-faint">
            <AlertTriangle className="h-3 w-3 text-warn" />
            Switching to <span className="text-warn">live</span> requires explicit
            confirmation and immediately arms real capital.
          </p>
        </div>
      )}

      <AlertDialog
        open={pending === "live"}
        onOpenChange={(open) => {
          if (!open) setPending(null);
        }}
      >
        <AlertDialogContent className="border-border bg-panel text-text">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2 text-brand">
              <Radio className="h-4 w-4" />
              Switch to live trading?
            </AlertDialogTitle>
            <AlertDialogDescription className="text-muted-foreground">
              The agent will broadcast real transactions to Jito and commit real
              capital under the configured risk limits. Confirm only if you intend
              to trade live.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Stay in {current ?? "current"}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setPending(null);
                void commit("live");
              }}
              className="border-transparent bg-brand text-[#06231a] hover:bg-[color:color-mix(in_srgb,var(--accent)_88%,black)]"
            >
              Go live
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Panel>
  );
}

/* -------------------------------------------------------------------------- */
/*  Wallet / custody read-out                                                 */
/* -------------------------------------------------------------------------- */

function WalletPanel() {
  const { data: state, loading } = useAgentState();

  function copyPubkey(pubkey: string) {
    void navigator.clipboard
      ?.writeText(pubkey)
      .then(() => toast.message("Pubkey copied"))
      .catch(() => toast.error("Copy failed"));
  }

  return (
    <Panel
      title="Wallet & custody"
      icon={<Wallet />}
      actions={
        <Chip tone="accent" icon={<KeyRound />}>
          signer isolated
        </Chip>
      }
    >
      {loading && !state ? (
        <SkeletonRows rows={3} />
      ) : !state ? (
        <ErrorNote message="No wallet state" />
      ) : (
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-panel2 px-3 py-2.5">
            <div className="min-w-0">
              <div className="text-[11px] uppercase tracking-wide text-faint">
                Pubkey
              </div>
              <div className="num mt-0.5 truncate text-[13px] font-semibold text-text">
                {state.wallet.pubkey}
              </div>
            </div>
            <button
              type="button"
              onClick={() => copyPubkey(state.wallet.pubkey)}
              aria-label="Copy pubkey"
              className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border bg-panel text-faint transition-colors hover:bg-hover hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40"
            >
              <Copy className="h-3.5 w-3.5" />
            </button>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div className="rounded-md border border-border bg-panel2 px-3 py-2">
              <div className="text-[11px] uppercase tracking-wide text-faint">
                Balance
              </div>
              <div className="num mt-1 text-[18px] font-semibold leading-none text-text">
                {fmt2(state.wallet.balanceSol)}
                <span className="ml-1 text-[11px] font-normal text-faint">
                  SOL
                </span>
              </div>
            </div>
            <div className="rounded-md border border-border bg-panel2 px-3 py-2">
              <div className="text-[11px] uppercase tracking-wide text-faint">
                Deploy cap
              </div>
              <div className="num mt-1 text-[18px] font-semibold leading-none text-brand">
                {fmtInt(state.wallet.capSol)}
                <span className="ml-1 text-[11px] font-normal text-faint">
                  SOL
                </span>
              </div>
            </div>
          </div>

          <div className="flex items-center justify-between gap-2 text-[11px]">
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <Activity className="h-3 w-3 text-up" />
              Custody
            </span>
            <span className="text-faint">
              Hot signer isolated from treasury · cap-enforced
            </span>
          </div>
        </div>
      )}
    </Panel>
  );
}

/* -------------------------------------------------------------------------- */
/*  Danger / lifecycle actions                                                */
/* -------------------------------------------------------------------------- */

function DangerPanel() {
  const [resetting, setResetting] = useState(false);

  async function onResetDaily() {
    setResetting(true);
    try {
      await resetBreaker();
      toast.success("Daily counters reset", {
        description: "Daily PnL zeroed and the loss-limit breaker re-armed.",
      });
    } catch (e) {
      toast.error("Reset failed", {
        description: e instanceof Error ? e.message : "Unknown error",
      });
    } finally {
      setResetting(false);
    }
  }

  return (
    <Panel title="Operations" icon={<ShieldAlert />}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-[11px] leading-snug text-faint">
          Reset zeroes the daily PnL window and re-arms the loss-limit breaker.
          The kill switch halts all loops immediately.
        </p>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={onResetDaily}
            disabled={resetting}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-panel2 px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-hover hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            {resetting ? "Resetting…" : "Reset daily"}
          </button>
          <KillSwitch />
        </div>
      </div>
    </Panel>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                      */
/* -------------------------------------------------------------------------- */

export default function Settings() {
  return (
    <Layout>
      <div className="space-y-4">
        {/* Page header */}
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <h1 className="text-[15px] font-semibold tracking-tight text-text">
              Settings
            </h1>
            <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <Sliders className="h-3.5 w-3.5 text-faint" />
              Risk, venue, strategy & custody
            </span>
          </div>
          <span className="num hidden text-[11px] text-faint sm:block">
            operator control plane
          </span>
        </div>

        {/* Risk parameters — full width */}
        <RiskConfigPanel />

        {/* Mode + wallet */}
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <div className="xl:col-span-2">
            <ModePanel />
          </div>
          <WalletPanel />
        </div>

        {/* Venue + strategy */}
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <VenuePanel />
          <StrategyPanel />
        </div>

        {/* Lifecycle actions */}
        <DangerPanel />
      </div>
    </Layout>
  );
}
