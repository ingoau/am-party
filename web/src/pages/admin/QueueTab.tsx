import { useState } from "react";
import { api } from "../../api/client";
import type { QueueItem, Track } from "../../api/types";
import { useLive } from "../../hooks/live";
import { useProgress } from "../../hooks/useProgress";
import { Artwork } from "../../components/Artwork";
import { ProgressBar } from "../../components/ProgressBar";
import { TrackRow } from "../../components/TrackRow";
import { SourceBadge } from "../../components/SourceBadge";
import { IconDown, IconNext, IconPause, IconPlay, IconPlus, IconSkip, IconTop, IconTrash, IconUp } from "../../components/Icons";
import { PageHeader, useFlash } from "./common";
import { TrackSearch } from "./TrackSearch";

export function QueueTab() {
  const { state, clockOffset, refresh } = useLive();
  const np = state?.now_playing ?? null;
  const paused = state?.paused ?? false;
  const position = useProgress(np, clockOffset, paused, 500);
  const flash = useFlash();
  const [busy, setBusy] = useState(false);
  // Local optimistic ordering while a move is in flight.
  const [optimistic, setOptimistic] = useState<QueueItem[] | null>(null);
  const upcoming = optimistic ?? state?.upcoming ?? [];

  const run = async (fn: () => Promise<unknown>, okMsg?: string) => {
    setBusy(true);
    try {
      await fn();
      if (okMsg) flash.ok(okMsg);
    } catch (e) {
      flash.err(e);
    } finally {
      setBusy(false);
      setOptimistic(null);
      refresh();
    }
  };

  const move = (item: QueueItem, to: number) => {
    const from = upcoming.findIndex((u) => u.id === item.id);
    if (from < 0 || to < 0 || to >= upcoming.length || from === to) return;
    const next = upcoming.slice();
    next.splice(from, 1);
    next.splice(to, 0, item);
    setOptimistic(next);
    void run(() => api.queueMove(item.id, to));
  };

  const add = (t: Track, position: "next" | "end") =>
    run(() => api.queueAdd(t.id, position), position === "next" ? `“${t.title}” plays next` : `Added “${t.title}”`);

  return (
    <div className="page">
      <PageHeader title="Queue" sub={`${state?.listeners ?? 0} devices listening`} />

      <section className="np-card">
        <Artwork url={np?.artwork_url} size={300} className="np-card__art" />
        <div className="np-card__body">
          <div className="eyebrow">{np ? (paused ? "Paused" : "Now playing") : "Nothing playing"}</div>
          {np ? (
            <>
              <div className="np-card__title truncate">{np.title}</div>
              <div className="np-card__artist truncate">
                {np.artist}
                {np.album ? ` · ${np.album}` : ""}
              </div>
              <div className="np-card__source">
                <SourceBadge source={np.source} />
              </div>
              <ProgressBar position={position} duration={np.duration} />
            </>
          ) : (
            <div className="muted">Add something below, or set a fallback playlist in Settings.</div>
          )}
          <div className="np-card__controls">
            {paused ? (
              <button className="btn btn--primary" disabled={busy} onClick={() => run(() => api.resume())}>
                <IconPlay size={18} /> Resume
              </button>
            ) : (
              <button className="btn" disabled={busy || !np} onClick={() => run(() => api.pause())}>
                <IconPause size={18} /> Pause
              </button>
            )}
            <button className="btn" disabled={busy || !np} onClick={() => run(() => api.skip(), "Skipped")}>
              <IconSkip size={18} /> Skip
            </button>
          </div>
        </div>
      </section>

      <div className="queue-grid">
        <section>
          <h2 className="section-title">
            Up next <span className="muted">{upcoming.length}</span>
          </h2>
          {upcoming.length === 0 ? (
            <div className="empty-state">
              <strong>Queue is empty</strong>
              {state?.up_next_fallback ? (
                <span>
                  Then from the playlist: {state.up_next_fallback.title} — {state.up_next_fallback.artist}
                </span>
              ) : (
                <span>Approve requests or add songs to keep the party going.</span>
              )}
            </div>
          ) : (
            <ol className="list queue-list">
              {upcoming.map((q, i) => (
                <li key={q.id}>
                  <TrackRow
                    track={q.track}
                    index={i + 1}
                    className="card-row"
                    meta={<SourceBadge source={q.source} />}
                    actions={
                      <div className="btn-group btn-group--icons">
                        <button className="icon-btn" disabled={busy || i === 0} onClick={() => move(q, 0)} aria-label="Move to top" title="Move to top">
                          <IconTop size={18} />
                        </button>
                        <button className="icon-btn" disabled={busy || i === 0} onClick={() => move(q, i - 1)} aria-label="Move up" title="Move up">
                          <IconUp size={18} />
                        </button>
                        <button
                          className="icon-btn"
                          disabled={busy || i === upcoming.length - 1}
                          onClick={() => move(q, i + 1)}
                          aria-label="Move down"
                          title="Move down"
                        >
                          <IconDown size={18} />
                        </button>
                        <button
                          className="icon-btn icon-btn--danger"
                          disabled={busy}
                          onClick={() => run(() => api.queueRemove(q.id), `Removed “${q.track.title}”`)}
                          aria-label="Remove from queue"
                          title="Remove"
                        >
                          <IconTrash size={18} />
                        </button>
                      </div>
                    }
                  />
                </li>
              ))}
            </ol>
          )}
          {upcoming.length > 0 && state?.up_next_fallback && (
            <TrackRow track={state.up_next_fallback} dim meta={<span className="muted">then, from the fallback playlist</span>} />
          )}
        </section>

        <section>
          <h2 className="section-title">Add a song</h2>
          <TrackSearch
            actions={(t) => (
              <div className="btn-group">
                <button className="btn btn--sm" disabled={busy} onClick={() => add(t, "next")} title="Play next">
                  <IconNext size={16} />
                  <span className="hide-sm">Next</span>
                </button>
                <button className="btn btn--primary btn--sm" disabled={busy} onClick={() => add(t, "end")} title="Add to end of queue">
                  <IconPlus size={16} />
                  <span className="hide-sm">Add</span>
                </button>
              </div>
            )}
          />
        </section>
      </div>
      {flash.node}
    </div>
  );
}
