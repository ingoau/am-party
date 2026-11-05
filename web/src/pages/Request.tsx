import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { api, ApiError } from "../api/client";
import type { SongRequest, Track } from "../api/types";
import { useLive } from "../hooks/live";
import { useDebounced } from "../hooks/useDebounced";
import { useProgress } from "../hooks/useProgress";
import { useAccentVars, useArtworkColor } from "../hooks/useArtworkColor";
import { Artwork } from "../components/Artwork";
import { ProgressBar } from "../components/ProgressBar";
import { TrackRow } from "../components/TrackRow";
import { StatusBadge } from "../components/StatusBadge";
import { Check, Search, X } from "lucide-react";
import { LazyToaster, lazyToast as toast } from "../components/lazyToast";
import { artworkAt, cx, timeAgo } from "../util";
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
        return e.message && !/^\d{3}\b/.test(e.message) ? e.message : "Easy there! Wait a bit before requesting more.";
      case 503:
        return "Search isn't available right now. Ask the host!";
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
    document.title = `Request a song — ${state?.party_name || "Encore"}`;
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
    for (const r of mine) if (r.status !== "rejected") m.set(r.track.id, r.status);
    return m;
  }, [mine]);
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

  const closed = state ? !state.requests_open : false;
  const pendingCount = mine.filter((r) => r.status === "pending").length;

  return (
    <div className="req">
      <div className="req__glow" aria-hidden />
      <header className="req__header">
        <Link to="/" className="req__party">
          {state?.party_name || "Encore"}
        </Link>
        <span className="req__sub">Request a song</span>
      </header>

      {np && (
        <div className="req-np">
          <Artwork url={np.artwork_url} size={200} className="req-np__art" />
          <div className="req-np__text">
            <div className="eyebrow">{state?.paused ? "Paused" : "Now playing"}</div>
            <div className="req-np__title truncate">{np.title}</div>
            <div className="req-np__artist truncate">{np.artist}</div>
            <ProgressBar position={position} duration={np.duration} compact />
          </div>
        </div>
      )}

      {closed && <div className="banner banner--warn">Requests are closed right now — check back later.</div>}

      <nav className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === "search"} className={cx("tab", tab === "search" && "is-active")} onClick={() => setTab("search")}>
          Search
        </button>
        <button role="tab" aria-selected={tab === "mine"} className={cx("tab", tab === "mine" && "is-active")} onClick={() => setTab("mine")}>
          My requests{pendingCount > 0 && <span className="tab__count">{pendingCount}</span>}
        </button>
        <button role="tab" aria-selected={tab === "queue"} className={cx("tab", tab === "queue" && "is-active")} onClick={() => setTab("queue")}>
          Up next{state && state.upcoming.length > 0 && <span className="tab__count">{state.upcoming.length}</span>}
        </button>
      </nav>

      {tab === "search" && (
        <section>
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
                        <span className="badge badge--pending">Pending</span>
                      ) : st === "approved" || queued ? (
                        <span className="badge badge--approved">
                          <Check size={14} /> {np?.id === t.id ? "Playing" : queued ? "Queued" : "Approved"}
                        </span>
                      ) : (
                        <button
                          type="button"
                          className="btn btn--primary btn--sm"
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
          {!results && !searching && !searchError && !closed && (
            <p className="hint">
              Find a song and tap <strong>Request</strong>.{" "}
              {state?.require_approval === false
                ? "It goes straight into the queue."
                : "The host approves every request before it plays."}
            </p>
          )}
        </section>
      )}

      {tab === "mine" && (
        <section className="list">
          {mine.length === 0 ? (
            <p className="empty">You haven't requested anything yet.</p>
          ) : (
            mine.map((r) => (
              <TrackRow
                key={r.id}
                track={r.track}
                meta={<span className="muted">{timeAgo(r.created_at, clockOffset)}</span>}
                actions={<StatusBadge status={r.status} />}
                dim={r.status === "rejected"}
              />
            ))
          )}
          {mine.length > 0 && <p className="hint">Updates automatically.</p>}
        </section>
      )}

      {tab === "queue" && (
        <section className="list">
          {state && state.upcoming.length > 0 ? (
            state.upcoming.map((q, i) => <TrackRow key={q.id} track={q.track} index={i + 1} />)
          ) : (
            <p className="empty">Nothing queued yet — your request could be next!</p>
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
