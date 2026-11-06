import { useEffect, useRef, useState } from "react";
import type { NowPlaying } from "../api/types";
import { useProgress } from "../hooks/useProgress";
import { fmtTime } from "../util";

export function ProgressBar({
  position,
  duration,
  compact,
  onSeek,
}: {
  position: number;
  duration: number;
  compact?: boolean;
  /** Makes the bar a slider; called with the chosen position (seconds) on release. */
  onSeek?: (seconds: number) => void;
}) {
  // Position being dragged/keyed to, then the one we asked for until the server confirms it:
  // both are shown instead of the live position so the bar doesn't jump around.
  const [pending, setPending] = useState<number | null>(null);
  const [sent, setSent] = useState<number | null>(null);
  const shown = pending ?? sent ?? position;
  const pct = duration > 0 ? Math.min(100, (shown / duration) * 100) : 0;
  const inputRef = useRef<HTMLInputElement>(null);
  const onSeekRef = useRef(onSeek);
  onSeekRef.current = onSeek;
  const seekable = !!onSeek && duration > 0;

  // Commit on the native `change` event: it fires on release for pointer and touch (not on a
  // cancelled drag) and per step for keys, so a short debounce coalesces held arrow keys.
  useEffect(() => {
    const el = inputRef.current;
    if (!el || !seekable) return;
    let timer = 0;
    const onChange = () => {
      const v = Number(el.value);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        setPending(null);
        setSent(v);
        onSeekRef.current?.(v);
      }, 250);
    };
    const onCancel = () => setPending(null);
    el.addEventListener("change", onChange);
    el.addEventListener("pointercancel", onCancel);
    return () => {
      window.clearTimeout(timer);
      el.removeEventListener("change", onChange);
      el.removeEventListener("pointercancel", onCancel);
    };
  }, [seekable]);

  // Drop the requested position once the live one has caught up (or after a moment).
  useEffect(() => {
    if (sent === null) return;
    if (Math.abs(position - sent) < 2) {
      setSent(null);
      return;
    }
    const t = window.setTimeout(() => setSent(null), 2500);
    return () => window.clearTimeout(t);
  }, [sent, position]);
  return (
    <div className={compact ? "progress progress--compact" : "progress"}>
      {seekable ? (
        <div className="progress__track progress__track--seek">
          <div className="progress__fill" style={{ width: `${pct}%`, transition: pending !== null ? "none" : undefined }} />
          <input
            ref={inputRef}
            type="range"
            className="progress__input"
            min={0}
            max={Math.floor(duration)}
            step={1}
            value={Math.floor(shown)}
            aria-label="Seek"
            aria-valuetext={`${fmtTime(shown)} of ${fmtTime(duration)}`}
            onChange={(e) => setPending(Number(e.currentTarget.value))}
          />
        </div>
      ) : (
        <div
          className="progress__track"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={Math.round(duration)}
          aria-valuenow={Math.round(position)}
          aria-valuetext={`${fmtTime(position)} of ${fmtTime(duration)}`}
        >
          <div className="progress__fill" style={{ width: `${pct}%` }} />
        </div>
      )}
      {!compact && (
        // The progressbar already exposes the position; the ticking text is visual only.
        <div className="progress__times" aria-hidden>
          <span>{fmtTime(shown)}</span>
          <span>-{fmtTime(Math.max(0, duration - shown))}</span>
        </div>
      )}
    </div>
  );
}

/**
 * Self-ticking progress bar. Keeps the per-tick re-render inside this small component instead of
 * the whole page that owns `np`.
 */
export function LiveProgress({
  np,
  clockOffset,
  paused,
  tickMs,
  compact,
  onSeek,
}: {
  np: NowPlaying;
  clockOffset: number;
  paused: boolean;
  tickMs?: number;
  compact?: boolean;
  onSeek?: (seconds: number) => void;
}) {
  const position = useProgress(np, clockOffset, paused, tickMs);
  // keyed by track so a pending seek never carries over to the next song
  return <ProgressBar key={np.id} position={position} duration={np.duration} compact={compact} onSeek={onSeek} />;
}
