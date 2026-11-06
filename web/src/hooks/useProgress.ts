import { useEffect, useState } from "react";
import type { NowPlaying } from "../api/types";

/**
 * Current playback position (seconds) of `np`, ticking locally:
 *   position = (Date.now()/1000 + clockOffset) - started_at
 * While paused, the position is frozen at the value seen when the pause was first observed
 * (the backend is expected to shift started_at on resume).
 */
export function useProgress(np: NowPlaying | null, clockOffset: number, paused: boolean, tickMs = 250): number {
  const [now, setNow] = useState(() => Date.now());
  const key = np ? `${np.id}:${np.started_at}` : "";

  useEffect(() => {
    if (!key) return;
    // Paused: one sample at the moment the pause (or a new track) is observed, then frozen.
    setNow(Date.now());
    if (paused) return;
    const t = window.setInterval(() => setNow(Date.now()), tickMs);
    return () => window.clearInterval(t);
  }, [key, paused, tickMs]);

  if (!np) return 0;
  const raw = now / 1000 + clockOffset - np.started_at;
  return Math.max(0, np.duration > 0 ? Math.min(np.duration, raw) : raw);
}
