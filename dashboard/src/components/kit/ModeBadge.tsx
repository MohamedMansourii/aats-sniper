import { Beaker, Radio, ShieldHalf } from "lucide-react";
import type { AgentMode } from "@/lib/types";
import { Chip, type ChipTone } from "./Chip";

export interface ModeBadgeProps {
  mode: AgentMode;
}

const MODE_META: Record<
  AgentMode,
  { label: string; tone: ChipTone; icon: typeof Radio }
> = {
  paper: { label: "Paper", tone: "info", icon: Beaker },
  "dry-run": { label: "Dry run", tone: "warn", icon: ShieldHalf },
  live: { label: "Live", tone: "accent", icon: Radio },
};

/** Compact agent-mode pill. Live = vivid accent; paper/dry-run = quieter tones. */
export function ModeBadge({ mode }: ModeBadgeProps) {
  const meta = MODE_META[mode];
  const Icon = meta.icon;
  return (
    <Chip tone={meta.tone} icon={<Icon />}>
      {meta.label}
    </Chip>
  );
}

export default ModeBadge;
