export function fmtTime(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) sec = 0;
  const s = Math.floor(sec);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m % 60).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/** "3m ago" style relative time for a unix-seconds timestamp in server time. */
export function timeAgo(ts: number, clockOffset = 0): string {
  const d = Math.max(0, Date.now() / 1000 + clockOffset - ts);
  if (d < 45) return "just now";
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
}

export function fmtDate(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/** Apple artwork URLs sometimes carry {w}x{h} placeholders; also lets us request other sizes. */
export function artworkAt(url: string | null | undefined, size: number): string | null {
  if (!url) return null;
  if (url.includes("{w}")) return url.replace("{w}", String(size)).replace("{h}", String(size));
  return url.replace(/\/(\d{2,4})x(\d{2,4})(bb|cc|sr)?\.(jpg|jpeg|png|webp)$/i, `/${size}x${size}$3.$4`);
}

export const storage = {
  get(key: string): string | null {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key: string, value: string) {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* private mode etc. */
    }
  },
};
