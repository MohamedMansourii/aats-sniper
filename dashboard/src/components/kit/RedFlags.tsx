import { AlertTriangle, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils";
import { redFlagLabel } from "@/lib/red-flags";
import { Chip } from "./Chip";
import type { RedFlag } from "@/lib/types";

/**
 * Token-safety scanner red-flag display (T-323/T-329 → wire `red_flags`).
 *
 * Pure presentational. Renders each red flag as a danger chip with a human
 * label; unknown codes (the scanner may add fingerprints over time) are shown
 * verbatim rather than dropped — an operator must never miss a safety warning.
 * When there are no flags it renders a quiet "clean" affordance (or nothing,
 * when `hideWhenClean`).
 */

export interface RedFlagsProps {
  flags: RedFlag[];
  /** Render nothing instead of a "clean" chip when there are no flags. */
  hideWhenClean?: boolean;
  className?: string;
}

export function RedFlags({ flags, hideWhenClean, className }: RedFlagsProps) {
  if (flags.length === 0) {
    if (hideWhenClean) return null;
    return (
      <Chip tone="ok" icon={<ShieldCheck />} className={className}>
        no red flags
      </Chip>
    );
  }
  return (
    <div className={cn("flex flex-wrap items-center gap-1", className)}>
      {flags.map((f) => (
        <Chip key={f} tone="danger" icon={<AlertTriangle />}>
          {redFlagLabel(f)}
        </Chip>
      ))}
    </div>
  );
}

export default RedFlags;
