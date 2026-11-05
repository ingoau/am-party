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
        <div className="progress__times">
          <span>{fmtTime(position)}</span>
          <span>-{fmtTime(Math.max(0, duration - position))}</span>
        </div>
      )}
    </div>
  );
}
