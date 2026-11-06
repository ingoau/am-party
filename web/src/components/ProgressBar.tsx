import { useState } from "react";
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
  // Position being dragged/keyed to, shown instead of the live one until it is committed.
  const [pending, setPending] = useState<number | null>(null);
  const shown = pending ?? position;
  const pct = duration > 0 ? Math.min(100, (shown / duration) * 100) : 0;
  const commit = () => {
    if (pending === null) return;
    setPending(null);
    onSeek?.(pending);
  };
  return (
    <div className={compact ? "progress progress--compact" : "progress"}>
      {onSeek && duration > 0 ? (
        <div className="progress__track progress__track--seek">
          <div className="progress__fill" style={{ width: `${pct}%`, transition: pending !== null ? "none" : undefined }} />
          <input
            type="range"
            className="progress__input"
            min={0}
            max={Math.floor(duration)}
            step={1}
            value={Math.floor(shown)}
            aria-label="Seek"
            aria-valuetext={`${fmtTime(shown)} of ${fmtTime(duration)}`}
            onChange={(e) => setPending(Number(e.currentTarget.value))}
            onPointerUp={commit}
            onKeyUp={commit}
            onBlur={commit}
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
  return <ProgressBar position={position} duration={np.duration} compact={compact} onSeek={onSeek} />;
}
