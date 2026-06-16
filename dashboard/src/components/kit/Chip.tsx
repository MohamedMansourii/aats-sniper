import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export type ChipTone =
  | "ok"
  | "danger"
  | "warn"
  | "info"
  | "neutral"
  | "accent"
  | "violet";

export interface ChipProps {
  tone: ChipTone;
  icon?: ReactNode;
  children: ReactNode;
  className?: string;
}

/**
 * tone → [text, border, bg] tinted at low alpha so chips read as quiet tags.
 * Uses color-mix (not the slash opacity modifier) because Tailwind v3 cannot
 * derive alpha from an arbitrary var() color.
 */
const toneClass: Record<ChipTone, string> = {
  ok: "text-up border-[color:color-mix(in_srgb,var(--up)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--up)_12%,transparent)]",
  danger:
    "text-danger border-[color:color-mix(in_srgb,var(--danger)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--danger)_12%,transparent)]",
  warn: "text-warn border-[color:color-mix(in_srgb,var(--warn)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--warn)_12%,transparent)]",
  info: "text-info border-[color:color-mix(in_srgb,var(--info)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--info)_12%,transparent)]",
  accent:
    "text-brand border-[color:color-mix(in_srgb,var(--accent)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--accent)_12%,transparent)]",
  violet:
    "text-violet border-[color:color-mix(in_srgb,var(--violet)_28%,transparent)] bg-[color:color-mix(in_srgb,var(--violet)_12%,transparent)]",
  neutral: "text-muted-foreground border-border bg-panel2",
};

/** Status pill. Compact, hairline-bordered, tinted by tone. */
export function Chip({ tone, icon, children, className }: ChipProps) {
  return (
    <span
      className={cn(
        "inline-flex w-fit items-center gap-1 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-[11px] font-medium leading-none",
        toneClass[tone],
        className,
      )}
    >
      {icon && (
        <span className="flex items-center [&_svg]:h-3 [&_svg]:w-3">{icon}</span>
      )}
      {children}
    </span>
  );
}

export default Chip;
