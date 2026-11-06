import { useEffect, useState, type ReactNode } from "react";
import { api } from "../../api/client";
import type { Track } from "../../api/types";
import { useDebounced } from "../../hooks/useDebounced";
import { TrackRow } from "../../components/TrackRow";
import { Search, X } from "lucide-react";

/** Admin catalog search (no guest rate limit). */
export function TrackSearch({ actions, placeholder }: { actions: (t: Track) => ReactNode; placeholder?: string }) {
  const [query, setQuery] = useState("");
  const q = useDebounced(query.trim(), 300);
  const [results, setResults] = useState<Track[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (q.length < 2) {
      setResults(null);
      setError(null);
      setLoading(false);
      return;
    }
    const ctrl = new AbortController();
    setLoading(true);
    api
      .adminSearch(q, ctrl.signal)
      .then((r) => {
        setResults(r.tracks);
        setError(null);
      })
      .catch((e) => {
        if ((e as Error).name !== "AbortError") setError((e as Error).message);
      })
      .finally(() => !ctrl.signal.aborted && setLoading(false));
    return () => ctrl.abort();
  }, [q]);

  return (
    <div className="track-search">
      <div className="search">
        <Search size={20} className="search__icon" aria-hidden />
        <input
          className="search__input"
          type="search"
          placeholder={placeholder ?? "Search Apple Music"}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label={placeholder ?? "Search Apple Music"}
          autoComplete="off"
        />
        {query && (
          <button className="search__clear" type="button" aria-label="Clear search" onClick={() => setQuery("")}>
            <X size={18} />
          </button>
        )}
      </div>
      {error && <div className="banner banner--err">{error}</div>}
      {loading && !results && <div className="spinner spinner--sm" />}
      {results && results.length === 0 && <p className="empty">No matches.</p>}
      {results && results.length > 0 && (
        <div className="list list--scroll">
          {results.map((t) => (
            <TrackRow key={t.id} track={t} actions={actions(t)} />
          ))}
        </div>
      )}
    </div>
  );
}
