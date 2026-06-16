import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface PanelProps {
  title?: string;
  icon?: ReactNode;
  actions?: ReactNode;
  className?: string;
  children?: ReactNode;
}

/**
 * The standard bordered --panel card. Renders a hairline header row
 * (title + icon left, actions right) only when a title is supplied.
 */
export function Panel({ title, icon, actions, className, children }: PanelProps) {
  return (
    <section
      className={cn(
        "rounded-[10px] border border-border bg-panel text-text",
        className,
      )}
    >
      {title !== undefined && (
        <header className="flex items-center justify-between gap-3 border-b border-border px-3.5 py-2.5">
          <div className="flex min-w-0 items-center gap-2">
            {icon && (
              <span className="flex shrink-0 items-center text-muted-foreground [&_svg]:h-3.5 [&_svg]:w-3.5">
                {icon}
              </span>
            )}
            <h2 className="truncate text-[12px] font-semibold tracking-tight text-text">
              {title}
            </h2>
          </div>
          {actions && (
            <div className="flex shrink-0 items-center gap-1.5">{actions}</div>
          )}
        </header>
      )}
      <div className={cn(title !== undefined ? "p-3.5" : "p-0")}>{children}</div>
    </section>
  );
}

export default Panel;
