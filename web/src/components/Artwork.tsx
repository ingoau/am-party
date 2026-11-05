import { useState } from "react";
import { artworkAt, cx } from "../util";
import { IconMusic } from "./Icons";

export function Artwork({
  url,
  size = 600,
  alt = "",
  className,
}: {
  url: string | null | undefined;
  size?: number;
  alt?: string;
  className?: string;
}) {
  const [failed, setFailed] = useState<string | null>(null);
  const src = artworkAt(url, size);
  if (!src || failed === src) {
    return (
      <div className={cx("artwork artwork--empty", className)} role="img" aria-label={alt || "No artwork"}>
        <IconMusic size={32} />
      </div>
    );
  }
  return (
    <img
      className={cx("artwork", className)}
      src={src}
      alt={alt}
      loading="lazy"
      decoding="async"
      draggable={false}
      onError={() => setFailed(src)}
    />
  );
}
