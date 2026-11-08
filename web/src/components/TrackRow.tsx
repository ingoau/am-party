import type { ReactNode } from "react";
import { SquareLetterE } from "lucide-react";
import type { Track } from "../api/types";
import { cx, fmtTime } from "../util";
import { Artwork } from "./Artwork";

export function TrackRow({
  track,
  meta,
  actions,
  className,
  dim,
  index,
  lead,
  wrap,
  showExplicit,
}: {
  track: Track;
  meta?: ReactNode;
  actions?: ReactNode;
  className?: string;
  dim?: boolean;
  index?: number;
  /** Optional leading element (e.g. a drag handle). */
  lead?: ReactNode;
  /** Let long titles wrap onto a second line instead of truncating. */
  wrap?: boolean;
  /** Show the explicit-content badge next to the title (admin only). */
  showExplicit?: boolean;
}) {
  return (
    <div className={cx("track-row", dim && "track-row--dim", className)}>
      {lead}
      {index !== undefined && <span className="track-row__index">{index}</span>}
      <Artwork url={track.artwork_url} size={120} className="track-row__art" />
      <div className="track-row__text">
        <div className={cx("track-row__title", wrap && "track-row__title--wrap")}>
          <span className="truncate" title={track.title}>
            {track.title}
          </span>
          {showExplicit && track.explicit && <SquareLetterE size={14} className="explicit" role="img" aria-label="Explicit" />}
        </div>
        <div className="track-row__sub truncate">
          {track.artist}
          {track.album ? <span className="track-row__album"> · {track.album}</span> : null}
        </div>
        {meta && <div className="track-row__meta">{meta}</div>}
      </div>
      {actions ? <div className="track-row__actions">{actions}</div> : <span className="track-row__dur">{fmtTime(track.duration)}</span>}
    </div>
  );
}
