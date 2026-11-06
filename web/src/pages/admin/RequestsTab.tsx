import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { SongRequest } from "../../api/types";
import { useLive, useLiveEvent } from "../../hooks/live";
import { TrackRow } from "../../components/TrackRow";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, CheckCheck, ChevronDown, ListStart, X } from "lucide-react";
import { StatusBadge } from "../../components/StatusBadge";
import { cx, timeAgo } from "../../util";
import { PageHeader, flash } from "./common";
import { useConfirm } from "./ConfirmDialog";
import { RequireApprovalSwitch } from "./RequireApprovalSwitch";

type View = "pending" | "history";

export function RequestsTab() {
  const { clockOffset, state } = useLive();
  const [view, setView] = useState<View>("pending");
  const [pending, setPending] = useState<SongRequest[] | null>(null);
  const [history, setHistory] = useState<SongRequest[] | null>(null);
  const [busy, setBusy] = useState<Set<number>>(new Set());
  const [approvingAll, setApprovingAll] = useState(false);
  const [confirmDialog, confirm] = useConfirm();
  const requireApproval = state?.require_approval ?? true;

  const load = useCallback(() => {
    api
      .adminRequests("pending")
      .then((r) => setPending(r.requests))
      .catch(flash.err);
    api
      .adminRequests("all")
      .then((r) => setHistory(r.requests.filter((x) => x.status !== "pending")))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 30_000); // safety net if the socket is down
    return () => window.clearInterval(t);
  }, [load]);
  useLiveEvent("requests_changed", load);

  const act = async (r: SongRequest, fn: () => Promise<unknown>, msg: string) => {
    setBusy((b) => new Set(b).add(r.id));
    // Optimistically drop it from the pending list.
    setPending((p) => p?.filter((x) => x.id !== r.id) ?? p);
    try {
      await fn();
      flash.ok(msg);
    } catch (e) {
      flash.err(e);
    } finally {
      setBusy((b) => {
        const n = new Set(b);
        n.delete(r.id);
        return n;
      });
      load();
    }
  };

  const approveAll = async (position: "next" | "end") => {
    const n = pending?.length ?? 0;
    if (n === 0) return;
    if (n > 5) {
      const ok = await confirm({
        title: `Approve all ${n} requests?`,
        description:
          position === "next"
            ? `All ${n} waiting songs jump to the front of the queue, oldest request first.`
            : `All ${n} waiting songs are added to the end of the queue, oldest request first.`,
        confirmLabel: `Approve ${n}`,
        icon: <CheckCheck size={22} />,
      });
      if (!ok) return;
    }
    setApprovingAll(true);
    const before = pending;
    setPending([]);
    try {
      const r = await api.approveAll(position);
      const done = r.approved ?? n;
      flash.ok(`Approved ${done} request${done === 1 ? "" : "s"}${position === "next" ? " to play next" : ""}`);
    } catch (e) {
      setPending(before);
      flash.err(e);
    } finally {
      setApprovingAll(false);
      load();
    }
  };

  // Reload once approval flips (turning it off approves everything waiting).
  const lastRequire = useRef(state?.require_approval);
  useEffect(() => {
    const cur = state?.require_approval;
    if (lastRequire.current !== undefined && cur !== undefined && cur !== lastRequire.current) load();
    lastRequire.current = cur;
  }, [state?.require_approval, load]);

  const list = view === "pending" ? pending : history;
  const pendingCount = pending?.length ?? 0;

  return (
    <div className="page">
      <PageHeader title="Requests">
        <RequireApprovalSwitch className="switch-row switch-row--inline" />
        {pendingCount > 0 && (
          <div className="split-btn">
            <button
              className="btn btn--primary btn--sm split-btn__main"
              disabled={approvingAll}
              onClick={() => void approveAll("end")}
              title="Approve every waiting request — add them to the end of the queue, oldest first"
            >
              <CheckCheck size={16} /> Approve all ({pendingCount})
            </button>
            <DropdownMenu.Root>
              <DropdownMenu.Trigger asChild>
                <button className="btn btn--primary btn--sm split-btn__more" disabled={approvingAll} aria-label="More approve-all options">
                  <ChevronDown size={16} />
                </button>
              </DropdownMenu.Trigger>
              <DropdownMenu.Portal>
                <DropdownMenu.Content className="menu" align="end" sideOffset={8} collisionPadding={12}>
                  <DropdownMenu.Item className="menu__item menu__item--rich" onSelect={() => void approveAll("end")}>
                    Approve all
                    <span>Add to the end of the queue</span>
                  </DropdownMenu.Item>
                  <DropdownMenu.Item className="menu__item menu__item--rich" onSelect={() => void approveAll("next")}>
                    Approve all as play next
                    <span>Jump the queue, oldest request first</span>
                  </DropdownMenu.Item>
                </DropdownMenu.Content>
              </DropdownMenu.Portal>
            </DropdownMenu.Root>
          </div>
        )}
      </PageHeader>

      {state && !state.requests_open && <div className="banner banner--warn">Requests are closed — guests can't send new ones.</div>}

      <div className="segmented" role="tablist">
        <button role="tab" aria-selected={view === "pending"} className={cx(view === "pending" && "is-active")} onClick={() => setView("pending")}>
          Pending{pending && pending.length > 0 && <span className="tab__count">{pending.length}</span>}
        </button>
        <button role="tab" aria-selected={view === "history"} className={cx(view === "history" && "is-active")} onClick={() => setView("history")}>
          History
        </button>
      </div>

      {list === null ? (
        <div className="spinner" />
      ) : list.length === 0 ? (
        <div className="empty-state">
          {view === "pending" ? (
            <strong>{requireApproval ? "All caught up" : "Requests go straight to the queue"}</strong>
          ) : (
            <span>No decisions yet.</span>
          )}
        </div>
      ) : (
        <div className="list">
          {list.map((r) => (
            <TrackRow
              key={r.id}
              track={r.track}
              className="card-row"
              wrap
              meta={
                <span className="muted">
                  {r.guest_label} · {timeAgo(r.created_at, clockOffset)}
                  {r.decided_by && r.status !== "pending" ? ` · by ${r.decided_by}` : ""}
                </span>
              }
              actions={
                r.status === "pending" ? (
                  <div className="btn-group">
                    <button
                      className="btn btn--primary btn--sm"
                      disabled={busy.has(r.id)}
                      onClick={() => act(r, () => api.approve(r.id, "end"), `Queued “${r.track.title}”`)}
                      title="Approve — add to the end of the queue"
                    >
                      <Check size={16} /> Approve
                    </button>
                    <button
                      className="btn btn--sm"
                      disabled={busy.has(r.id)}
                      onClick={() => act(r, () => api.approve(r.id, "next"), `“${r.track.title}” plays next`)}
                      title="Approve and play next"
                    >
                      <ListStart size={16} /> Play next
                    </button>
                    <button
                      className="btn btn--danger-ghost btn--sm"
                      disabled={busy.has(r.id)}
                      onClick={() => act(r, () => api.reject(r.id), "Request declined")}
                      aria-label="Reject"
                      title="Reject"
                    >
                      <X size={16} />
                      <span className="hide-sm">Reject</span>
                    </button>
                  </div>
                ) : (
                  r.status === "approved" && !r.decided_by ? (
                    <span className="badge badge--approved" title="Approved automatically while approval was off">
                      Auto-approved
                    </span>
                  ) : (
                    <StatusBadge status={r.status} admin />
                  )
                )
              }
            />
          ))}
        </div>
      )}
      {confirmDialog}
    </div>
  );
}
