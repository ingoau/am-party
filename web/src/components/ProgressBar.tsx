import type { NowPlaying } from "../api/types";
import { useProgress } from "../hooks/useProgress";
import { fmtTime } from "../util";

export function ProgressBar({ position, duration, compact }: { position: number; duration: number; compact?: boolean }) {
  const pct = duration > 0 ? Math.min(100, (position / duration) * 100) : 0;
  return (
    <div className={compact ? "progress progress--compact" : "progress"}>
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
      {!compact && (
        // The progressbar already exposes the position; the ticking text is visual only.
        <div className="progress__times" aria-hidden>
          <span>{fmtTime(position)}</span>
          <span>-{fmtTime(Math.max(0, duration - position))}</span>
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
}: {
  np: NowPlaying;
  clockOffset: number;
  paused: boolean;
  tickMs?: number;
  compact?: boolean;
}) {
  const position = useProgress(np, clockOffset, paused, tickMs);
  return <ProgressBar position={position} duration={np.duration} compact={compact} />;
}
