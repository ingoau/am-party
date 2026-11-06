import { Suspense, useCallback, useEffect, useMemo, useState, type KeyboardEvent } from "react";
import { api, ApiError } from "../api/client";
import type { SongRequest, Track } from "../api/types";
import { useLive } from "../hooks/live";
import { useDebounced } from "../hooks/useDebounced";
import { useProgress } from "../hooks/useProgress";
import { useAccentVars, useArtworkColor } from "../hooks/useArtworkColor";
import { Artwork } from "../components/Artwork";
import { ProgressBar } from "../components/ProgressBar";
import { TrackRow } from "../components/TrackRow";
import { Search, X } from "lucide-react";
import { LazyToaster, lazyToast as toast } from "../components/lazyToast";
import { artworkAt, cx } from "../util";
import "../styles/request.css";

type Tab = "search" | "mine" | "queue";

function friendlyError(e: unknown): string {
  if (e instanceof ApiError) {
    switch (e.status) {
      case 0:
        return e.message;
      case 403:
        return "Requests are closed right now.";
      case 409:
        return "That song is already in the queue or waiting for approval.";
      case 429:
        return e.message && !/^\d{3}\b/.test(e.message) ? e.message : "Wait a moment before requesting more.";
      case 503:
        return "Search isn't available right now.";
      default:
        return e.message || "Something went wrong.";
    }
  }
  return "Something went wrong.";
}

export function RequestPage() {
  const { state, clockOffset } = useLive();
  const np = state?.now_playing ?? null;
  const position = useProgress(np, clockOffset, state?.paused ?? false, 1000);
  useAccentVars(useArtworkColor(artworkAt(np?.artwork_url, 100)));

  const [tab, setTab] = useState<Tab>("search");
  const [query, setQuery] = useState("");
  const q = useDebounced(query.trim(), 350);
  const [results, setResults] = useState<Track[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [mine, setMine] = useState<SongRequest[]>([]);
  const [pendingTrack, setPendingTrack] = useState<string | null>(null);

  useEffect(() => {
    document.title = `Request a song · ${state?.party_name || "Encore"}`;
  }, [state?.party_name]);

  // Debounced search with abort of stale requests.
  useEffect(() => {
    if (q.length < 2) {
      setResults(null);
      setSearchError(null);
      setSearching(false);
      return;
    }
    const ctrl = new AbortController();
    setSearching(true);
    setSearchError(null);
    api
      .search(q, ctrl.signal)
      .then((r) => setResults(r.tracks))
      .catch((e) => {
        if ((e as Error).name === "AbortError") return;
        setSearchError(friendlyError(e));
        setResults(null);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setSearching(false);
      });
    return () => ctrl.abort();
  }, [q]);

  const loadMine = useCallback(() => {
    api
      .myRequests()
      .then((r) => setMine(r.requests))
      .catch(() => {});
  }, []);

  useEffect(() => {
    loadMine();
    const t = window.setInterval(loadMine, 10_000);
    return () => window.clearInterval(t);
  }, [loadMine]);

  // Refresh my requests when the queue changes (approval likely happened).
  const upcomingKey = state?.upcoming.map((u) => u.id).join(",");
  useEffect(() => {
    if (upcomingKey !== undefined) loadMine();
  }, [upcomingKey, loadMine]);

  const requested = useMemo(() => {
    const m = new Map<string, SongRequest["status"]>();
    for (const r of mine) if (r.status === "pending" || r.status === "approved") m.set(r.track.id, r.status);
    return m;
  }, [mine]);
  const myIds = useMemo(() => new Set(mine.map((r) => r.id)), [mine]);
  const shownMine = useMemo(() => mine.filter((r) => r.status !== "withdrawn"), [mine]);
  const queuedIds = useMemo(() => {
    const s = new Set<string>();
    state?.upcoming.forEach((u) => s.add(u.track.id));
    if (np) s.add(np.id);
    return s;
  }, [state?.upcoming, np]);

  const doRequest = async (t: Track) => {
    setPendingTrack(t.id);
    try {
      const { request } = await api.createRequest(t.id);
      setMine((m) => [request, ...m.filter((x) => x.id !== request.id)]);
      // Trust the server's answer first: with approval off it comes back already approved.
      const queuedNow = request.status === "approved" || state?.require_approval === false;
      toast.success(
        queuedNow ? `Added “${t.title}” to the queue.` : `Requested “${t.title}”. The host will review it soon.`,
        { duration: 4500 },
      );
    } catch (e) {
      toast.error(friendlyError(e), { duration: 4500 });
      if (e instanceof ApiError && e.status === 409) loadMine();
    } finally {
      setPendingTrack(null);
    }
  };

  const withdraw = async (r: SongRequest) => {
    setMine((m) => m.map((x) => (x.id === r.id ? { ...x, status: "withdrawn" } : x)));
    try {
      await api.withdrawRequest(r.id);
      toast.success(`Withdrew “${r.track.title}”.`, { duration: 3000 });
    } catch (e) {
      toast.error(
        e instanceof ApiError && e.status === 409 ? "It's about to play, so it can't be withdrawn." : friendlyError(e),
        { duration: 4500 },
      );
    } finally {
      loadMine();
    }
  };

  const closed = state ? !state.requests_open : false;
  const pendingCount = mine.filter((r) => r.status === "pending").length;
  const tabProps = (t: Tab) => ({
    role: "tab" as const,
    id: `tab-${t}`,
    "aria-selected": tab === t,
    "aria-controls": `panel-${t}`,
    tabIndex: tab === t ? 0 : -1,
    className: cx("tab", tab === t && "is-active"),
    onClick: () => setTab(t),
  });
  const onTabKey = (e: KeyboardEvent) => {
    const order: Tab[] = ["search", "mine", "queue"];
    const i = order.indexOf(tab);
    const next = e.key === "ArrowRight" ? order[(i + 1) % 3] : e.key === "ArrowLeft" ? order[(i + 2) % 3] : null;
    if (!next) return;
    e.preventDefault();
    setTab(next);
    document.getElementById(`tab-${next}`)?.focus();
  };

  return (
    <div className="req">
      <header className="req__header">
        <h1 className="req__party">{state?.party_name || "Encore"}</h1>
      </header>

      {np && (
        <div className="req-np">
          <Artwork url={np.artwork_url} size={200} className="req-np__art" />
          <div className="req-np__text">
            <div className="req-np__title truncate">{np.title}</div>
            <div className="req-np__artist truncate">
              {state?.paused ? "Paused · " : ""}
              {np.artist}
            </div>
            <ProgressBar position={position} duration={np.duration} compact />
          </div>
        </div>
      )}

      {closed && <div className="banner banner--warn">Requests are closed right now.</div>}

      <nav className="tabs" role="tablist" aria-label="Request a song" onKeyDown={onTabKey}>
        <button {...tabProps("search")}>Search</button>
        <button {...tabProps("mine")}>
          My requests{pendingCount > 0 && <span className="tab__count">{pendingCount}</span>}
        </button>
        <button {...tabProps("queue")}>Up next</button>
      </nav>

      {tab === "search" && (
        <section role="tabpanel" id="panel-search" aria-labelledby="tab-search">
          <div className="search">
            <Search size={20} className="search__icon" aria-hidden />
            <input
              className="search__input"
              type="search"
              inputMode="search"
              enterKeyHint="search"
              placeholder="Search songs or artists"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              disabled={closed}
              autoComplete="off"
              autoCorrect="off"
              spellCheck={false}
            />
            {query && (
              <button className="search__clear" type="button" aria-label="Clear search" onClick={() => setQuery("")}>
                <X size={18} />
              </button>
            )}
          </div>

          {searchError && <div className="banner banner--err">{searchError}</div>}
          {searching && !results && <ResultSkeleton />}
          {results && results.length === 0 && !searching && <p className="empty">No matches for “{q}”.</p>}
          {results && results.length > 0 && (
            <div className={cx("list", searching && "is-loading")}>
              {results.map((t) => {
                const st = requested.get(t.id);
                const queued = queuedIds.has(t.id);
                return (
                  <TrackRow
                    key={t.id}
                    track={t}
                    actions={
                      st === "pending" ? (
                        <span className="row-state row-state--wait">Waiting</span>
                      ) : st === "approved" || queued ? (
                        <span className="row-state">{np?.id === t.id ? "Playing" : queued ? "Queued" : "Approved"}</span>
                      ) : (
                        <button
                          type="button"
                          className="btn btn--sm"
                          disabled={closed || pendingTrack !== null}
                          onClick={() => doRequest(t)}
                        >
                          {pendingTrack === t.id ? "Sending…" : "Request"}
                        </button>
                      )
                    }
                  />
                );
              })}
            </div>
          )}
          {!results && !searching && !searchError && !closed && state?.require_approval !== false && (
            <p className="hint">The host approves requests before they play.</p>
          )}
        </section>
      )}

      {tab === "mine" && (
        <section className="list" role="tabpanel" id="panel-mine" aria-labelledby="tab-mine">
          {shownMine.length === 0 ? (
            <p className="empty">No requests yet. Search to add one.</p>
          ) : (
            shownMine.map((r) => <MyRequestRow key={r.id} r={r} nowPlayingId={np?.id ?? null} onWithdraw={() => withdraw(r)} />)
          )}
        </section>
      )}

      {tab === "queue" && (
        <section className="list" role="tabpanel" id="panel-queue" aria-labelledby="tab-queue">
          {state && state.upcoming.length > 0 ? (
            state.upcoming.map((q, i) => (
              <TrackRow
                key={q.id}
                track={q.track}
                index={i + 1}
                actions={q.request_id != null && myIds.has(q.request_id) ? <span className="yours">Yours</span> : undefined}
              />
            ))
          ) : (
            <p className="empty">No requests yet. Search to add one.</p>
          )}
          {state && state.upcoming.length === 0 && state.up_next_fallback && (
            <TrackRow track={state.up_next_fallback} dim meta={<span className="muted">then, from the playlist</span>} />
          )}
        </section>
      )}

      <Suspense fallback={null}>
        <LazyToaster />
      </Suspense>
    </div>
  );
}

function fmtEta(sec: number): string {
  if (sec < 60) return "under a minute";
  const m = Math.round(sec / 60);
  return m < 60 ? `~${m} min` : `~${Math.floor(m / 60)}h ${m % 60}m`;
}

/** One of the guest's own requests: where it is, and a two-step Withdraw while that's still possible. */
function MyRequestRow({ r, nowPlayingId, onWithdraw }: { r: SongRequest; nowPlayingId: string | null; onWithdraw: () => void }) {
  const [confirming, setConfirming] = useState(false);
  useEffect(() => {
    if (!confirming) return;
    const t = window.setTimeout(() => setConfirming(false), 5000);
    return () => window.clearTimeout(t);
  }, [confirming]);

  let status: string;
  let tone: "wait" | "queued" | "live" | "done" = "done";
  if (r.status === "pending") {
    status = "Waiting for approval";
    tone = "wait";
  } else if (r.status === "approved" && r.queue_position != null) {
    status = r.queue_position === 1 ? "Up next" : `#${r.queue_position} in queue`;
    if (r.eta_seconds != null) status += ` · ${fmtEta(r.eta_seconds)}`;
    tone = "queued";
  } else if (r.status === "approved" && nowPlayingId === r.track.id) {
    status = "Playing now";
    tone = "live";
  } else if (r.status === "approved") {
    status = "Played";
  } else {
    status = "Declined";
  }

  return (
    <TrackRow
      track={r.track}
      wrap
      dim={r.status === "rejected"}
      meta={<span className={`req-status req-status--${tone}`}>{status}</span>}
      actions={
        r.can_withdraw ? (
          <button
            type="button"
            className={cx("withdraw", confirming && "is-confirming")}
            onClick={() => (confirming ? onWithdraw() : setConfirming(true))}
            onBlur={() => setConfirming(false)}
            onKeyDown={(e) => e.key === "Escape" && setConfirming(false)}
            aria-label={confirming ? `Confirm: withdraw “${r.track.title}”` : `Withdraw “${r.track.title}”`}
          >
            {confirming ? "Yes, withdraw" : "Withdraw"}
          </button>
        ) : (
          <span />
        )
      }
    />
  );
}

function ResultSkeleton() {
  return (
    <div className="list" aria-hidden>
      {Array.from({ length: 5 }, (_, i) => (
        <div key={i} className="track-row skeleton">
          <div className="artwork track-row__art" />
          <div className="track-row__text">
            <div className="sk-line" />
            <div className="sk-line sk-line--short" />
          </div>
        </div>
      ))}
    </div>
  );
}
