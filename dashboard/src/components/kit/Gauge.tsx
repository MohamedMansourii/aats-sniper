import { cn } from "@/lib/utils";

export type GaugeTone = "accent" | "up" | "down" | "warn" | "danger" | "info";

export interface GaugeProps {
  value: number;
  max: number;
  tone?: GaugeTone;
  label?: string;
  className?: string;
}

const fillColor: Record<GaugeTone, string> = {
  accent: "var(--accent)",
  up: "var(--up)",
  down: "var(--down)",
  warn: "var(--warn)",
  danger: "var(--danger)",
  info: "var(--info)",
};

/**
 * Thin horizontal bar gauge (e.g. daily-loss vs limit, tip budget). Shows an
 * optional label row with value/max, clamps the fill to [0, max], and exposes
 * ARIA progressbar semantics.
 */
export function Gauge({ value, max, tone = "accent", label, className }: GaugeProps) {
  const safeMax = max <= 0 ? 1 : max;
  const pct = Math.max(0, Math.min(100, (value / safeMax) * 100));

  return (
    <div className={cn("w-full", className)}>
      {label !== undefined && (
        <div className="mb-1 flex items-baseline justify-between">
          <span className="text-[11px] text-muted-foreground">{label}</span>
          <span className="num text-[11px] text-faint">
            {value} / {max}
          </span>
        </div>
      )}
      <div
        role="progressbar"
        aria-valuenow={value}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-label={label}
        className="h-1.5 w-full overflow-hidden rounded-full bg-panel2"
      >
        <div
          className="h-full rounded-full transition-[width] duration-500 ease-out"
          style={{ width: `${pct}%`, backgroundColor: fillColor[tone] }}
        />
      </div>
    </div>
  );
}

export default Gauge;
