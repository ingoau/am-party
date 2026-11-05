import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { SongRequest } from "../../api/types";
import { useLive, useLiveEvent } from "../../hooks/live";
import { TrackRow } from "../../components/TrackRow";
import { IconCheck, IconNext, IconX } from "../../components/Icons";
import { StatusBadge } from "../../components/StatusBadge";
import { cx, timeAgo } from "../../util";
import { PageHeader, useFlash } from "./common";

type View = "pending" | "history";

export function RequestsTab() {
  const { clockOffset, state } = useLive();
  const [view, setView] = useState<View>("pending");
  const [pending, setPending] = useState<SongRequest[] | null>(null);
  const [history, setHistory] = useState<SongRequest[] | null>(null);
  const [busy, setBusy] = useState<Set<number>>(new Set());
  const flash = useFlash();

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

  const list = view === "pending" ? pending : history;

  return (
    <div className="page">
      <PageHeader
        title="Requests"
        sub={state && !state.requests_open ? "Requests are closed — guests can't submit new ones." : "Approve what fits the vibe."}
      />
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
            <>
              <strong>All caught up</strong>
              <span>New requests show up here instantly.</span>
            </>
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
                      <IconCheck size={16} /> Approve
                    </button>
                    <button
                      className="btn btn--sm"
                      disabled={busy.has(r.id)}
                      onClick={() => act(r, () => api.approve(r.id, "next"), `“${r.track.title}” plays next`)}
                      title="Approve and play next"
                    >
                      <IconNext size={16} /> Play next
                    </button>
                    <button
                      className="btn btn--danger-ghost btn--sm"
                      disabled={busy.has(r.id)}
                      onClick={() => act(r, () => api.reject(r.id), "Request declined")}
                      aria-label="Reject"
                      title="Reject"
                    >
                      <IconX size={16} />
                      <span className="hide-sm">Reject</span>
                    </button>
                  </div>
                ) : (
                  <StatusBadge status={r.status} admin />
                )
              }
            />
          ))}
        </div>
      )}
      {flash.node}
    </div>
  );
}
