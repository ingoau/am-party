import { useEffect, useRef, useState } from "react";
import type { NowPlaying } from "../api/types";

/**
 * Current playback position (seconds) of `np`, ticking locally:
 *   position = (Date.now()/1000 + clockOffset) - started_at
 * While paused, the position is frozen at the value seen when the pause was first observed
 * (the backend is expected to shift started_at on resume).
 */
export function useProgress(np: NowPlaying | null, clockOffset: number, paused: boolean, tickMs = 250): number {
  const [now, setNow] = useState(() => Date.now());
  const frozen = useRef<{ key: string; at: number } | null>(null);
  const key = np ? `${np.id}:${np.started_at}` : "";

  useEffect(() => {
    if (!np || paused) return;
    setNow(Date.now());
    const t = window.setInterval(() => setNow(Date.now()), tickMs);
    return () => window.clearInterval(t);
  }, [key, paused, tickMs]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!np) return 0;
  let t = now;
  if (paused) {
    if (!frozen.current || frozen.current.key !== key) frozen.current = { key, at: Date.now() };
    t = frozen.current.at;
  } else {
    frozen.current = null;
  }
  const raw = t / 1000 + clockOffset - np.started_at;
  return Math.max(0, np.duration > 0 ? Math.min(np.duration, raw) : raw);
}
