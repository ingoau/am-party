import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router";
import { ArrowUp, ChevronDown, Search, X } from "lucide-react";
import { api } from "../../api/client";
import type { AuditActorKind, AuditEntry, AuditQuery } from "../../api/types";
import { useLive, useLiveEvent } from "../../hooks/live";
import { useDebounced } from "../../hooks/useDebounced";
import { cx, fmtTime, timeAgo } from "../../util";
import { PageHeader, errorText, flash } from "./common";
import { usePermissionCatalog } from "./permissions";

const PAGE_SIZE = 50;
/** audit_changed can arrive many times a second at a busy party: wait for a quiet moment. */
const LIVE_DEBOUNCE_MS = 1500;
/** Safety net while the socket is down. */
const POLL_MS = 30_000;
/** Closer to the top than this (px), new entries are shown straight away instead of behind a button. */
const NEAR_TOP_PX = 160;

const CATEGORIES: { value: string; label: string }[] = [
  { value: "", label: "All actions" },
  { value: "request.", label: "Requests" },
  { value: "queue.", label: "Queue" },
  { value: "player.", label: "Playback" },
  { value: "settings.", label: "Settings" },
  { value: "user.", label: "Users" },
  { value: "speaker.", label: "Speakers" },
  { value: "credentials.", label: "Credentials" },
  { value: "auth.", label: "Sign-ins" },
];

const KINDS: { value: AuditActorKind | ""; label: string }[] = [
  { value: "", label: "Anyone" },
  { value: "user", label: "Hosts" },
  { value: "guest", label: "Guests" },
  { value: "system", label: "Automatic" },
  { value: "device", label: "Speakers" },
];
const KIND_VALUES = new Set<string>(KINDS.map((k) => k.value).filter(Boolean));

type Filters = { action: string; actor_kind: AuditActorKind | null; actor: number | null; q: string };

export function AuditTab() {
  const { clockOffset } = useLive();
  const [params, setParams] = useSearchParams();
  const catalog = usePermissionCatalog();
  const permLabel = useMemo(() => {
    const m = new Map(catalog.map((p) => [p.id as string, p.label]));
    return (id: string) => m.get(id) ?? id.replace(/_/g, " ");
  }, [catalog]);

  // Filters live in the URL (/admin/audit?action=queue.&kind=guest&actor=3&q=…) so they survive a reload.
  const action = params.get("action") ?? "";
  const kindParam = params.get("kind") ?? "";
  const actorKind = KIND_VALUES.has(kindParam) ? (kindParam as AuditActorKind) : null;
  const actorParam = Number(params.get("actor"));
  const actor = Number.isInteger(actorParam) && actorParam > 0 ? actorParam : null;
  const urlQ = params.get("q") ?? "";
  const [text, setText] = useState(urlQ);
  const debouncedText = useDebounced(text.trim(), 400);

  const setParam = useCallback(
    (key: string, value: string | null) =>
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (value) next.set(key, value);
          else next.delete(key);
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );
  useEffect(() => {
    if (debouncedText !== urlQ) setParam("q", debouncedText || null);
  }, [debouncedText]);
  // Back/forward (or a link) changed the search: show it in the box.
  const lastUrlQ = useRef(urlQ);
  useEffect(() => {
    if (urlQ !== lastUrlQ.current && urlQ !== debouncedText) setText(urlQ);
    lastUrlQ.current = urlQ;
  }, [urlQ]);

  const filters: Filters = useMemo(() => ({ action, actor_kind: actorKind, actor, q: urlQ }), [action, actorKind, actor, urlQ]);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  const query = (extra: AuditQuery = {}): AuditQuery => ({ ...filtersRef.current, limit: PAGE_SIZE, ...extra });

  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  /** Newer entries held back because the list is scrolled; `gap` = there are even more than these. */
  const [fresh, setFresh] = useState<{ entries: AuditEntry[]; gap: boolean }>({ entries: [], gap: false });
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const entriesRef = useRef(entries);
  entriesRef.current = entries;
  const freshRef = useRef(fresh);
  freshRef.current = fresh;

  // Every (re)load starts a new generation: anything an older one still delivers is dropped.
  const gen = useRef(0);
  const ctl = useRef<AbortController | null>(null);

  const load = useCallback(() => {
    ctl.current?.abort();
    const c = (ctl.current = new AbortController());
    const g = ++gen.current;
    setError(null);
    api
      .audit(query(), c.signal)
      .then((r) => {
        if (g !== gen.current) return;
        setEntries(r.entries);
        setNextBefore(r.next_before);
        setFresh({ entries: [], gap: false });
      })
      .catch((e) => {
        if (g !== gen.current || c.signal.aborted) return;
        setError(errorText(e));
      });
  }, []);

  const filterKey = JSON.stringify(filters);
  useEffect(() => {
    setEntries(null);
    setExpanded(new Set());
    load();
  }, [filterKey, load]);
  useEffect(() => () => ctl.current?.abort(), []);

  /** Fetch the newest page and add only what's new, without disturbing pages loaded below. */
  const refreshing = useRef(false);
  const refreshHead = useCallback(async () => {
    const current = entriesRef.current;
    if (current === null) return; // first load still running (or failed: the retry button reloads)
    if (refreshing.current) return;
    refreshing.current = true;
    const g = gen.current;
    try {
      const r = await api.audit(query());
      if (g !== gen.current) return;
      const known = Math.max(freshRef.current.entries[0]?.id ?? 0, entriesRef.current?.[0]?.id ?? 0);
      const newer = r.entries.filter((e) => e.id > known);
      if (!newer.length) return;
      // Everything on the newest page is new and there's more: we can't stitch it onto the list.
      const gap = newer.length === r.entries.length && r.next_before !== null;
      if (window.scrollY < NEAR_TOP_PX) {
        if (gap) {
          setEntries(r.entries);
          setNextBefore(r.next_before);
          setExpanded(new Set());
          setFresh({ entries: [], gap: false });
        } else showNewer(newer);
      } else setFresh((f) => ({ entries: [...newer, ...f.entries], gap: f.gap || gap }));
    } catch {
      /* the next push or poll tries again */
    } finally {
      refreshing.current = false;
    }
  }, []);

  const showNewer = (newer: AuditEntry[]) =>
    setEntries((cur) => {
      if (!cur) return cur;
      const ids = new Set(cur.map((e) => e.id));
      return [...newer.filter((e) => !ids.has(e.id)), ...cur];
    });

  const revealFresh = () => {
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (fresh.gap) {
      load();
      return;
    }
    showNewer(fresh.entries);
    setFresh({ entries: [], gap: false });
  };

  const liveTimer = useRef<number | undefined>(undefined);
  useLiveEvent("audit_changed", () => {
    window.clearTimeout(liveTimer.current);
    liveTimer.current = window.setTimeout(() => void refreshHead(), LIVE_DEBOUNCE_MS);
  });
  useEffect(() => {
    const t = window.setInterval(() => void refreshHead(), POLL_MS);
    return () => {
      window.clearInterval(t);
      window.clearTimeout(liveTimer.current);
    };
  }, [refreshHead]);

  const loadMore = async () => {
    if (nextBefore === null || loadingMore) return;
    setLoadingMore(true);
    const g = gen.current;
    try {
      const r = await api.audit(query({ before: nextBefore }));
      if (g !== gen.current) return;
      setEntries((cur) => {
        const list = cur ?? [];
        const ids = new Set(list.map((e) => e.id));
        return [...list, ...r.entries.filter((e) => !ids.has(e.id))];
      });
      setNextBefore(r.next_before);
    } catch (e) {
      if (g === gen.current) flash.err(e);
    } finally {
      if (g === gen.current) setLoadingMore(false);
    }
  };
  useEffect(() => setLoadingMore(false), [filterKey]);

  // Re-render relative times now and then.
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => setTick((n) => n + 1), 30_000);
    return () => window.clearInterval(t);
  }, []);

  const toggle = (id: number) =>
    setExpanded((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const filterActor = (id: number) => {
    setParam("actor", String(id));
    window.scrollTo({ top: 0 });
  };
  const actorName =
    actor === null ? null : (entries?.find((e) => e.actor.kind === "user" && e.actor.id === actor)?.actor.name ?? `user #${actor}`);
  const filtered = !!(action || actorKind || actor || urlQ);
  const clearAll = () => {
    setText("");
    setParams(new URLSearchParams(), { replace: true });
  };

  return (
    <div className="page audit">
      <PageHeader title="Audit log" sub="Who did what, newest first." />

      <div className="audit__filters">
        <div className="search audit__search">
          <Search size={20} className="search__icon" aria-hidden />
          <input
            className="search__input"
            type="search"
            placeholder="Search songs, people, settings"
            aria-label="Search the audit log"
            value={text}
            onChange={(e) => setText(e.target.value)}
            autoComplete="off"
          />
          {text && (
            <button className="search__clear" type="button" aria-label="Clear search" onClick={() => setText("")}>
              <X size={18} />
            </button>
          )}
        </div>
        <select className="input audit__select" aria-label="Kind of action" value={action} onChange={(e) => setParam("action", e.target.value || null)}>
          {CATEGORIES.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
          {action && !CATEGORIES.some((c) => c.value === action) && <option value={action}>{action}</option>}
        </select>
        <select className="input audit__select" aria-label="Who did it" value={actorKind ?? ""} onChange={(e) => setParam("kind", e.target.value || null)}>
          {KINDS.map((k) => (
            <option key={k.value} value={k.value}>
              {k.label}
            </option>
          ))}
        </select>
      </div>

      {actor !== null && (
        <div className="audit__active">
          <span className="chip">
            Only {actorName}
            <button className="audit__chip-x" type="button" aria-label="Show everyone" title="Show everyone" onClick={() => setParam("actor", null)}>
              <X size={14} />
            </button>
          </span>
        </div>
      )}

      {fresh.entries.length > 0 && (
        <div className="audit__fresh">
          <button className="btn btn--primary btn--sm" onClick={revealFresh}>
            <ArrowUp size={16} />
            {fresh.gap ? "Show newer entries" : `${fresh.entries.length} new ${fresh.entries.length === 1 ? "entry" : "entries"}`}
          </button>
        </div>
      )}

      {entries === null ? (
        error ? (
          <div className="empty-state">
            <strong>Couldn't load the audit log</strong>
            <span>{error}</span>
            <div>
              <button className="btn btn--ghost btn--sm" onClick={load}>
                Try again
              </button>
            </div>
          </div>
        ) : (
          <div className="spinner" />
        )
      ) : entries.length === 0 ? (
        <div className="empty-state">
          {filtered ? (
            <>
              <strong>Nothing matches</strong>
              <span>Try another search or filter.</span>
              <div>
                <button className="btn btn--ghost btn--sm" onClick={clearAll}>
                  Clear filters
                </button>
              </div>
            </>
          ) : (
            <>
              <strong>Nothing logged yet</strong>
              <span>Approvals, queue changes, skips and settings changes show up here.</span>
            </>
          )}
        </div>
      ) : (
        <>
          <ul className="alist">
            {entries.map((e) => (
              <AuditRow
                key={e.id}
                entry={e}
                clockOffset={clockOffset}
                permLabel={permLabel}
                open={expanded.has(e.id)}
                onToggle={() => toggle(e.id)}
                onActor={actor === null ? filterActor : undefined}
              />
            ))}
          </ul>
          <div className="audit__more">
            {nextBefore !== null ? (
              <button className="btn btn--ghost" disabled={loadingMore} onClick={() => void loadMore()}>
                {loadingMore ? "Loading…" : "Load more"}
              </button>
            ) : (
              <span className="muted">That's everything{filtered ? " that matches" : ""}.</span>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// ---------- One entry ----------

function AuditRow({
  entry: e,
  clockOffset,
  permLabel,
  open,
  onToggle,
  onActor,
}: {
  entry: AuditEntry;
  clockOffset: number;
  permLabel: (id: string) => string;
  open: boolean;
  onToggle: () => void;
  onActor?: (id: number) => void;
}) {
  const { text, used } = describe(e, permLabel);
  const rest = Object.entries(e.details ?? {}).filter(([k, v]) => !used.includes(k) && v !== null && v !== undefined && v !== "");
  const name = actorLabel(e);
  const date = new Date(e.at * 1000);
  const detailsId = `audit-details-${e.id}`;
  return (
    <li className={cx("arow", open && "is-open")}>
      <div className="arow__main">
        <div className="arow__text">
          {e.actor.kind === "user" && e.actor.id !== null && onActor ? (
            <button className="arow__actor arow__actor--link" type="button" title={`Only show what ${name} did`} onClick={() => onActor(e.actor.id!)}>
              {name}
            </button>
          ) : (
            <span className="arow__actor">{name}</span>
          )}{" "}
          {text}
        </div>
        <div className="arow__meta">
          <time dateTime={date.toISOString()} title={date.toLocaleString(undefined, { dateStyle: "full", timeStyle: "medium" })}>
            {timeAgo(e.at, clockOffset)} · {fmtClock(date)}
          </time>
          {e.ip && <span>IP {e.ip}</span>}
        </div>
      </div>
      {rest.length > 0 && (
        <button
          className="icon-btn arow__toggle"
          aria-expanded={open}
          aria-controls={detailsId}
          aria-label={open ? "Hide details" : "Show details"}
          title={open ? "Hide details" : "Show details"}
          onClick={onToggle}
        >
          <ChevronDown size={18} />
        </button>
      )}
      {open && rest.length > 0 && (
        <dl className="kv arow__details" id={detailsId}>
          {rest.map(([k, v]) => (
            <Kv key={k} k={k} v={v} />
          ))}
          <dt>action</dt>
          <dd className="arow__mono">{e.action}</dd>
        </dl>
      )}
    </li>
  );
}

function Kv({ k, v }: { k: string; v: unknown }) {
  return (
    <>
      <dt>{k.replace(/_/g, " ")}</dt>
      <dd>{fmtValue(v)}</dd>
    </>
  );
}

function fmtClock(d: Date) {
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// ---------- Turning entries into sentences ----------

type TrackRef = { id?: string; title: string; artist?: string | null };

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const asTrack = (v: unknown): TrackRef | null => (isObj(v) && typeof v.title === "string" ? (v as TrackRef) : null);
const asNum = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const asStr = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const pct = (p: number) => `${Math.round(p * 100)}%`;
const clip = (s: string, n = 40) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

function actorLabel(e: AuditEntry): string {
  const a = e.actor;
  switch (a.kind) {
    case "user":
      return a.name ?? "A deleted user";
    case "guest":
      return a.guest ?? a.name ?? "A guest";
    case "device":
      return a.name ?? "A speaker";
    default:
      return a.name ?? "The server";
  }
}

function Track({ t, artist = true }: { t: TrackRef; artist?: boolean }) {
  return (
    <span className="arow__track">
      {t.title}
      {artist && t.artist ? <span className="arow__artist"> – {t.artist}</span> : null}
    </span>
  );
}

/** The track the entry is about, from details.track or else the target label. */
function subject(e: AuditEntry): ReactNode {
  const t = asTrack(e.details?.track);
  if (t) return <Track t={t} />;
  return <span className="arow__track">{e.target?.label ?? "a song"}</span>;
}

function target(e: AuditEntry, fallback = ""): ReactNode {
  const label = e.target?.label;
  return label ? <span className="arow__track">{label}</span> : fallback;
}

const SETTING_LABELS: Record<string, string> = {
  party_name: "party name",
  requests_open: "requests open",
  require_approval: "require approval",
  fallback_playlist_id: "fallback playlist",
  fallback_playlist_name: "fallback playlist name",
  fallback_shuffle: "shuffle fallback",
  crossfade_mode: "crossfade",
  crossfade_duration: "crossfade length",
  request_rate_limits: "request limits",
  speaker_naming_notes: "speaker naming notes",
  auto_approval: "automatic approval",
  auto_approval_threshold: "auto-approval threshold",
  auto_approval_provider: "auto-approval provider",
};

function fmtPeriod(s: number): string {
  for (const [size, unit] of [
    [86400, "day"],
    [3600, "hour"],
    [60, "min"],
  ] as const)
    if (s >= size && s % size === 0) return `${s / size} ${unit}${s / size === 1 || unit === "min" ? "" : "s"}`;
  return `${s} s`;
}

/** Compact human text for any details value. */
function fmtValue(v: unknown, settingsStyle = false): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return settingsStyle ? (v ? "on" : "off") : v ? "yes" : "no";
  if (typeof v === "number" || typeof v === "string") return String(v);
  if (Array.isArray(v)) return v.length ? v.map((x) => fmtValue(x, settingsStyle)).join(", ") : "none";
  if (isObj(v)) {
    const t = asTrack(v);
    if (t) return t.artist ? `${t.title} – ${t.artist}` : t.title;
    if (asTrack(v.track)) return fmtValue(v.track);
    if (asNum(v.requests) !== null && asNum(v.seconds) !== null) return `${v.requests} per ${fmtPeriod(v.seconds as number)}`;
    if ("from" in v && "to" in v) return `${fmtValue(v.from, settingsStyle)} → ${fmtValue(v.to, settingsStyle)}`;
    return Object.entries(v)
      .map(([k, x]) => `${k.replace(/_/g, " ")}: ${fmtValue(x, settingsStyle)}`)
      .join("; ");
  }
  return JSON.stringify(v);
}

function describe(e: AuditEntry, permLabel: (id: string) => string): { text: ReactNode; used: string[] } {
  const d = e.details ?? {};
  const used = ["track"];
  const out = (text: ReactNode, ...keys: string[]) => ({ text, used: [...used, ...keys] });
  const position = d.position === "next" ? " to play next" : "";
  const guest = asStr(d.guest);
  const fromGuest = guest ? ` from ${guest}` : "";
  const at = asNum(d.at);

  switch (e.action) {
    // Requests
    case "request.create":
      return out(
        <>
          requested {subject(e)}
          {d.status === "approved" ? " (straight to the queue)" : ""}
        </>,
        "status",
      );
    case "request.withdraw":
      return out(<>withdrew their request for {subject(e)}</>, "was");
    case "request.approve":
      return out(
        <>
          approved {subject(e)}
          {position}
          {fromGuest}
        </>,
        "position",
        "guest",
      );
    case "request.reject":
      return out(
        <>
          rejected {subject(e)}
          {fromGuest}
        </>,
        "guest",
        "position",
      );
    case "request.approve_all": {
      const n = asNum(d.count) ?? 0;
      return out(
        <>
          approved all {n} waiting request{n === 1 ? "" : "s"}
          {position}
        </>,
        "count",
        "position",
      );
    }
    case "request.recheck":
      return out(<>asked Jev to check {subject(e)} again</>);
    case "request.auto_approve":
    case "request.auto_reject":
    case "request.auto_flag": {
      const p = asNum(d.probability);
      const note = asStr(d.note);
      const sure = p !== null ? ` (${pct(p)} sure${note ? `, ${note.toLowerCase()}` : ""})` : note ? ` (${note.toLowerCase()})` : "";
      const verb =
        e.action === "request.auto_approve" ? "auto-approved" : e.action === "request.auto_reject" ? "auto-rejected" : "flagged";
      const lean = e.action === "request.auto_flag" && asStr(d.decision) ? `, leaning ${d.decision}` : "";
      return out(
        <>
          {verb} {subject(e)}
          {sure}
          {lean}
          {e.action === "request.auto_flag" ? " for a person to decide" : ""}
        </>,
        "probability",
        "note",
        "decision",
      );
    }
    case "request.auto_error":
      return out(
        <>
          couldn't check {subject(e)}
          {asStr(d.error) ? `: ${clip(d.error as string, 80)}` : ""}
        </>,
        "error",
      );

    // Queue
    case "queue.add":
      return d.position === "next" ? out(<>played next: {subject(e)}</>, "position") : out(<>added {subject(e)} to the queue</>, "position");
    case "queue.remove":
      return out(
        <>
          removed {subject(e)} from the queue{d.source === "request" ? " (a guest request)" : ""}
        </>,
        "source",
        "index",
      );
    case "queue.move": {
      const from = asNum(d.from);
      const to = asNum(d.to);
      return out(
        <>
          moved {subject(e)}
          {from !== null && to !== null ? ` from #${from + 1} to #${to + 1}` : ""}
        </>,
        "from",
        "to",
      );
    }

    // Playback
    case "player.skip": {
      const t = asTrack(d.track);
      const next = asTrack(d.next);
      return out(
        <>
          skipped {t ? <Track t={t} artist={!next} /> : target(e, "the song")}
          {at !== null ? ` (${fmtTime(at)} in)` : ""}
          {next ? (
            <>
              {" → "}
              <Track t={next} artist={false} />
            </>
          ) : null}
        </>,
        "at",
        "next",
        "source",
      );
    }
    case "player.previous": {
      const from = asTrack(d.from);
      return out(
        <>
          went back to {subject(e)}
          {from ? (
            <>
              {" from "}
              <Track t={from} artist={false} />
            </>
          ) : null}
        </>,
        "from",
        "at",
        "source",
      );
    }
    case "player.restart":
      return out(
        <>
          restarted {subject(e)}
          {at !== null ? ` (was ${fmtTime(at)} in)` : ""}
        </>,
        "at",
        "source",
      );
    case "player.seek": {
      const from = asNum(d.from);
      const to = asNum(d.to);
      return out(
        <>
          jumped {from !== null ? `from ${fmtTime(from)} ` : ""}
          {to !== null ? `to ${fmtTime(to)}` : ""} in {subject(e)}
        </>,
        "from",
        "to",
        "source",
      );
    }
    case "player.pause":
      return out(<>paused {e.target?.label || asTrack(d.track) ? subject(e) : "playback"}</>, "source");
    case "player.resume":
      return out(<>resumed {e.target?.label || asTrack(d.track) ? subject(e) : "playback"}</>, "source");

    // Settings
    case "settings.update": {
      const changes = isObj(d.changes) ? Object.entries(d.changes) : [];
      if (!changes.length) return out(<>changed settings</>);
      const shown = changes.slice(0, 3);
      let complete = shown.length === changes.length;
      const parts = shown.map(([k, c]) => {
        const label = SETTING_LABELS[k] ?? k.replace(/_/g, " ");
        if (!isObj(c) || !("to" in c)) return `${label} ${clip(fmtValue(c, true))}`;
        const from = fmtValue(c.from, true);
        const to = fmtValue(c.to, true);
        if (from.length > 40 || to.length > 40) complete = false;
        return `${label} ${clip(from)} → ${clip(to)}`;
      });
      const more = changes.length - shown.length;
      return out(
        <>
          changed settings: {parts.join("; ")}
          {more > 0 ? ` and ${more} more` : ""}
        </>,
        ...(complete ? ["changes"] : []),
      );
    }
    case "settings.require_approval": {
      const n = asNum(d.approved_waiting) ?? 0;
      return out(
        <>
          turned approval {d.enabled ? "on" : "off"}
          {!d.enabled && n > 0 ? ` (approved ${n} waiting request${n === 1 ? "" : "s"})` : ""}
        </>,
        "enabled",
        "approved_waiting",
      );
    }

    // People
    case "user.create":
      return out(<>signed in for the first time{d.admin ? " and became an admin" : ""}</>, "admin");
    case "user.update": {
      const granted = Array.isArray(d.granted) ? (d.granted as string[]) : [];
      const revoked = Array.isArray(d.revoked) ? (d.revoked as string[]) : [];
      const bits: string[] = [];
      if (d.admin === true) bits.push("made admin");
      if (d.admin === false) bits.push("no longer admin");
      if (granted.length) bits.push(`gave ${granted.map(permLabel).join(", ")}`);
      if (revoked.length) bits.push(`took away ${revoked.map(permLabel).join(", ")}`);
      return out(
        <>
          changed access for {target(e, "someone")}
          {bits.length ? `: ${bits.join("; ")}` : ""}
        </>,
        "granted",
        "revoked",
        "admin",
      );
    }
    case "user.delete":
      return out(<>removed {target(e, "a user")}</>);

    // Speakers
    case "speaker.claim":
      return out(<>claimed {target(e, "a speaker")}</>);
    case "speaker.control": {
      const bits: string[] = [];
      if (asNum(d.volume) !== null) bits.push(`volume ${d.volume}`);
      if (asNum(d.volume_delta) !== null) bits.push(`volume ${(d.volume_delta as number) > 0 ? "+" : ""}${d.volume_delta}`);
      if (typeof d.muted === "boolean") bits.push(d.muted ? "muted" : "unmuted");
      if (typeof d.locked === "boolean") bits.push(d.locked ? "locked" : "unlocked");
      return out(
        <>
          {bits.length ? bits.join(", ") : "changed"} {target(e, "speakers")}
        </>,
        "volume",
        "volume_delta",
        "muted",
        "locked",
      );
    }
    case "speaker.update": {
      const name = isObj(d.name) ? d.name : null;
      const lock = isObj(d.locked) ? d.locked : null;
      const bits: string[] = [];
      if (name) bits.push(`renamed it from ${fmtValue(name.from)} to ${fmtValue(name.to)}`);
      if (lock) bits.push(lock.to ? "locked it" : "unlocked it");
      return out(
        <>
          changed {target(e, "a speaker")}
          {bits.length ? `: ${bits.join(", ")}` : ""}
        </>,
        "name",
        "locked",
      );
    }
    case "speaker.rename":
      return out(
        <>
          renamed itself{asStr(d.from) ? ` from ${d.from}` : ""} to {asStr(d.to) ?? e.target?.label ?? "?"}
        </>,
        "from",
        "to",
        "by",
      );
    case "speaker.share":
      return out(
        <>
          {d.mode === "handover" ? "offered to hand over" : "shared"} {target(e, "speakers")}
        </>,
        "mode",
      );
    case "speaker.share_accept":
      return out(
        <>
          {d.mode === "handover" ? "took over" : "accepted"} {target(e, "speakers")}
          {asStr(d.from) ? ` from ${d.from}` : ""}
        </>,
        "mode",
        "from",
      );
    case "speaker.assign":
      return out(
        <>
          gave {target(e, "speakers")} to {asStr(d.user) ?? "someone"}
        </>,
        "user",
      );
    case "speaker.remove_owner":
      return out(
        <>
          removed {asStr(d.user) ?? "an owner"} from {target(e, "a speaker")}
        </>,
        "user",
      );
    case "speaker.forget":
      return out(<>forgot {target(e, "a speaker")}</>);

    // Credentials (never the secret, only set / cleared)
    case "credentials.apple_music": {
      const bits: string[] = [];
      if (asStr(d.developer_token)) bits.push(`developer token ${d.developer_token}`);
      if (asStr(d.user_token)) bits.push(`user token ${d.user_token}`);
      return out(
        <>
          updated Apple Music access{bits.length ? `: ${bits.join(", ")}` : ""}
          {asStr(d.via) ? ` (via ${d.via})` : ""}
        </>,
        "developer_token",
        "user_token",
        "via",
      );
    }
    case "credentials.jev_api_key":
      return out(
        <>
          {d.change === "cleared" ? "cleared" : "set"} the Jev API key{asStr(d.provider) ? ` (${d.provider})` : ""}
        </>,
        "change",
        "provider",
      );

    // Sign-ins
    case "auth.login":
      return out(<>signed in</>);
    case "auth.logout":
      return out(<>signed out</>);
    case "auth.login_denied":
      return out(<>was refused sign-in</>);
  }

  // Unknown action: its name and whatever it was about. Every detail stays in the expandable list.
  return {
    text: (
      <>
        <span className="arow__mono">{e.action}</span>
        {e.target?.label ? (
          <>
            {" "}
            {target(e)}
          </>
        ) : null}
      </>
    ),
    used: [],
  };
}
