import { useEffect, useState } from "react";
import { ListChecks } from "lucide-react";
import { api } from "../../api/client";
import { useLive } from "../../hooks/live";
import { useConfirm } from "./ConfirmDialog";
import { flash } from "./common";

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/**
 * "Require approval" toggle. Reads the live State (so every open admin view stays in sync)
 * and applies immediately via POST /api/admin/requests/require-approval. Turning it off
 * auto-approves everything waiting, so that direction asks for confirmation first.
 */
export function RequireApprovalSwitch({ className }: { className?: string }) {
  const { state } = useLive();
  const live = state?.require_approval;
  const [pendingValue, setPendingValue] = useState<boolean | null>(null);
  const [confirmDialog, confirm] = useConfirm();

  // Drop the optimistic value once the live state catches up.
  useEffect(() => {
    if (pendingValue !== null && live === pendingValue) setPendingValue(null);
  }, [live, pendingValue]);

  const checked = pendingValue ?? live ?? true;
  const busy = pendingValue !== null;

  const change = async (enabled: boolean) => {
    if (!enabled) {
      let waiting = 0;
      try {
        waiting = (await api.adminRequests("pending")).requests.length;
      } catch {
        /* count is only for the message */
      }
      const ok = await confirm({
        title: "Turn off approval?",
        description:
          "Guest requests will go straight into the queue." +
          (waiting > 0 ? ` The ${plural(waiting, "waiting request")} will be approved now.` : ""),
        confirmLabel: waiting > 0 ? `Turn off & approve ${waiting}` : "Turn off approval",
        icon: <ListChecks size={22} />,
      });
      if (!ok) return;
    }
    setPendingValue(enabled);
    try {
      const r = await api.setRequireApproval(enabled);
      if (enabled) flash.ok("Approval is on — new requests wait for you");
      else flash.ok(r.approved > 0 ? `Approval is off — approved ${plural(r.approved, "waiting request")}` : "Approval is off — requests go straight to the queue");
      // If the socket is down, don't leave the switch stuck in its optimistic state.
      window.setTimeout(() => setPendingValue(null), 4000);
    } catch (e) {
      setPendingValue(null);
      flash.err(e);
    }
  };

  return (
    <>
      <label className={className ?? "switch-row"}>
        <input
          type="checkbox"
          role="switch"
          checked={checked}
          disabled={!state || busy}
          aria-checked={checked}
          onChange={(e) => void change(e.target.checked)}
        />
        <span className="switch" aria-hidden />
        <span>Require approval</span>
      </label>
      {confirmDialog}
    </>
  );
}
