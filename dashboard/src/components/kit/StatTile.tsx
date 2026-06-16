import type { ReactNode } from "react";
import { ArrowDownRight, ArrowUpRight } from "lucide-react";
import { cn } from "@/lib/utils";

export type StatTone =
  | "up"
  | "down"
  | "accent"
  | "neutral"
  | "danger"
  | "warn"
  | "info";

export interface StatTileProps {
  label: string;
  value: ReactNode;
  sub?: string;
  delta?: number;
  tone?: StatTone;
}

const valueToneClass: Record<StatTone, string> = {
  up: "text-up",
  down: "text-down",
  accent: "text-brand",
  neutral: "text-text",
  danger: "text-danger",
  warn: "text-warn",
  info: "text-info",
};

/**
 * KPI tile: small muted label, big tabular-mono value, optional sub line and a
 * signed delta with a directional arrow (green up / red down).
 */
export function StatTile({ label, value, sub, delta, tone = "neutral" }: StatTileProps) {
  const hasDelta = typeof delta === "number" && Number.isFinite(delta);
  const deltaUp = hasDelta && (delta as number) >= 0;

  return (
    <div className="rounded-[10px] border border-border bg-panel px-3.5 py-3">
      <div className="text-[11px] font-medium uppercase tracking-wide text-faint">
        {label}
      </div>
      <div className="mt-1.5 flex items-baseline gap-2">
        <span
          className={cn(
            "num text-[22px] font-semibold leading-none tracking-tight",
            valueToneClass[tone],
          )}
        >
          {value}
        </span>
        {hasDelta && (
          <span
            className={cn(
              "num inline-flex items-center gap-0.5 text-[11px] font-medium leading-none",
              deltaUp ? "text-up" : "text-down",
            )}
          >
            {deltaUp ? (
              <ArrowUpRight className="h-3 w-3" />
            ) : (
              <ArrowDownRight className="h-3 w-3" />
            )}
            {deltaUp ? "+" : ""}
            {delta}
          </span>
        )}
      </div>
      {sub && <div className="mt-1.5 text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

export default StatTile;
