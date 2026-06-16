import { cn } from "@/lib/utils";

export interface LiveDotProps {
  tone?: "accent" | "danger" | "warn";
  className?: string;
}

const toneColor: Record<NonNullable<LiveDotProps["tone"]>, string> = {
  accent: "text-brand",
  danger: "text-danger",
  warn: "text-warn",
};

/**
 * Small pulsing dot signalling a live stream. Uses currentColor so the
 * pulse-dot keyframe's expanding ring inherits the tone. Respects
 * prefers-reduced-motion (animation disabled there by the OS / Tailwind).
 */
export function LiveDot({ tone = "accent", className }: LiveDotProps) {
  return (
    <span
      role="status"
      aria-label="live"
      className={cn(
        "inline-block h-2 w-2 shrink-0 rounded-full bg-current animate-pulse-dot motion-reduce:animate-none",
        toneColor[tone],
        className,
      )}
    />
  );
}

export default LiveDot;
