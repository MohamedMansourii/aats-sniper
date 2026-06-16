import { useState } from "react";
import { Power } from "lucide-react";
import { toast } from "sonner";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { killSwitch } from "@/lib/api";
import { cn } from "@/lib/utils";

export interface KillSwitchProps {
  className?: string;
}

/**
 * The prominent red kill button. Requires explicit confirmation before calling
 * killSwitch() — halts all loops and cancels in-flight submissions.
 */
export function KillSwitch({ className }: KillSwitchProps) {
  const [busy, setBusy] = useState(false);

  async function confirmKill() {
    setBusy(true);
    try {
      await killSwitch();
      toast.error("Kill switch engaged", {
        description: "All loops halted. In-flight submissions cancelled.",
      });
    } catch (e) {
      toast.error("Kill switch failed", {
        description: e instanceof Error ? e.message : "Unknown error",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <button
          type="button"
          disabled={busy}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md border border-[color:color-mix(in_srgb,var(--danger)_40%,transparent)] bg-[color:color-mix(in_srgb,var(--danger)_14%,transparent)] px-2.5 py-1.5 text-[12px] font-semibold text-danger transition-colors hover:bg-[color:color-mix(in_srgb,var(--danger)_24%,transparent)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:color-mix(in_srgb,var(--danger)_50%,transparent)] disabled:cursor-not-allowed disabled:opacity-60",
            className,
          )}
        >
          <Power className="h-3.5 w-3.5" />
          Kill
        </button>
      </AlertDialogTrigger>
      <AlertDialogContent className="border-border bg-panel text-text">
        <AlertDialogHeader>
          <AlertDialogTitle className="flex items-center gap-2 text-danger">
            <Power className="h-4 w-4" />
            Engage kill switch?
          </AlertDialogTitle>
          <AlertDialogDescription className="text-muted-foreground">
            This immediately halts the snipe loop, cancels in-flight Jito
            submissions, and freezes new entries. Open positions are not flattened.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={confirmKill}
            className="border-transparent bg-danger text-white hover:bg-[color:color-mix(in_srgb,var(--danger)_90%,black)]"
          >
            Engage kill
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

export default KillSwitch;
