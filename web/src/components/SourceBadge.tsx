import type { Source } from "../api/types";

const LABEL: Record<Source, string> = { request: "Request", admin: "Host pick", fallback: "Playlist" };

export function SourceBadge({ source }: { source: Source }) {
  return <span className={`badge badge--${source}`}>{LABEL[source]}</span>;
}
