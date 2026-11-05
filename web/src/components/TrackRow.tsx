import type { ReactNode } from "react";
import type { Track } from "../api/types";
import { cx, fmtTime } from "../util";
import { Artwork } from "./Artwork";
import { SquareLetterE } from "lucide-react";

export function TrackRow({
  track,
  meta,
  actions,
  className,
  dim,
  index,
  lead,
}: {
  track: Track;
  meta?: ReactNode;
  actions?: ReactNode;
  className?: string;
  dim?: boolean;
  index?: number;
  /** Optional leading element (e.g. a drag handle). */
  lead?: ReactNode;
}) {
  return (
    <div className={cx("track-row", dim && "track-row--dim", className)}>
      {lead}
      {index !== undefined && <span className="track-row__index">{index}</span>}
      <Artwork url={track.artwork_url} size={120} className="track-row__art" />
      <div className="track-row__text">
        <div className="track-row__title">
          <span className="truncate">{track.title}</span>
          {track.explicit && <SquareLetterE size={14} className="explicit" role="img" aria-label="Explicit" />}
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
