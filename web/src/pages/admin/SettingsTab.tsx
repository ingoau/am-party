import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "../../api/client";
import type { AdminStatus, AppleMusicStatus, CrossfadeMode, Playlist, Settings } from "../../api/types";
import { Artwork } from "../../components/Artwork";
import { Check } from "lucide-react";
import { TV_TEXT_DEFAULTS, cx, fmtDate } from "../../util";
import { PageHeader, flash } from "./common";
import { RequireApprovalSwitch } from "./RequireApprovalSwitch";
import { useLive } from "../../hooks/live";

type SaveState = "idle" | "saving" | "saved" | "error";
type SetSetting = <K extends keyof Settings>(k: K, v: Settings[K], debounceMs?: number) => void;

/** Fields that are not sent through PATCH /api/admin/settings. */
const LOCAL_ONLY: (keyof Settings)[] = ["require_approval"];

function diff(draft: Settings, saved: Settings): Partial<Settings> {
  const p: Partial<Settings> = {};
  (Object.keys(draft) as (keyof Settings)[]).forEach((k) => {
    if (LOCAL_ONLY.includes(k)) return; // applied immediately via its own endpoint (RequireApprovalSwitch)
    if (k === "party_name" && !draft.party_name.trim()) return; // never save an empty name
    if (draft[k] !== saved[k]) (p as Record<string, unknown>)[k] = draft[k];
  });
  return p;
}

/**
 * Every setting applies as soon as it changes (text and number fields after a short pause in
 * typing), with a small "Saved" indicator in the header instead of a save bar.
 */
export function SettingsTab() {
  const [draft, setDraft] = useState<Settings | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [status, setStatus] = useState<AdminStatus | null>(null);
  const draftRef = useRef<Settings | null>(null);
  const savedRef = useRef<Settings | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const inflight = useRef(false);
  const again = useRef(false);

  const loadStatus = useCallback(() => {
    api.status().then(setStatus).catch(() => setStatus(null));
  }, []);

  useEffect(() => {
    api
      .settings()
      .then((s) => {
        savedRef.current = s;
        draftRef.current = s;
        setDraft(s);
      })
      .catch(flash.err);
    loadStatus();
    const t = window.setInterval(loadStatus, 15_000);
    return () => window.clearInterval(t);
  }, [loadStatus]);

  const flush = useCallback(async () => {
    window.clearTimeout(timer.current);
    const sent = draftRef.current;
    const saved = savedRef.current;
    if (!sent || !saved) return;
    if (inflight.current) {
      again.current = true;
      return;
    }
    const patch = diff(sent, saved);
    if (Object.keys(patch).length === 0) return;
    inflight.current = true;
    setSaveState("saving");
    try {
      const s = await api.patchSettings(patch);
      const next: Settings = s && typeof s === "object" && "party_name" in s ? s : { ...saved, ...patch };
      // The server trims and collapses whitespace in text fields. If that's the only difference,
      // keep the host's text as typed so a pause after a space doesn't eat it mid-sentence.
      (Object.keys(patch) as (keyof Settings)[]).forEach((k) => {
        const v = sent[k];
        if (typeof v === "string" && v !== next[k] && v.trim().replace(/\s+/g, " ") === next[k]) {
          (next as Record<string, unknown>)[k] = v;
        }
      });
      savedRef.current = next;
      // Keep anything edited while the request was in flight.
      const cur = draftRef.current ?? sent;
      const merged = { ...next };
      (Object.keys(cur) as (keyof Settings)[]).forEach((k) => {
        if (cur[k] !== sent[k]) (merged as Record<string, unknown>)[k] = cur[k];
      });
      draftRef.current = merged;
      setDraft(merged);
      setSaveState("saved");
      loadStatus();
    } catch (e) {
      flash.err(e);
      setSaveState("error");
      draftRef.current = savedRef.current;
      setDraft(savedRef.current);
    } finally {
      inflight.current = false;
      if (again.current) {
        again.current = false;
        void flush();
      }
    }
  }, [loadStatus]);

  // Save anything still waiting on its debounce when leaving the page.
  useEffect(() => () => void flush(), [flush]);

  const set: SetSetting = useCallback(
    (k, v, debounceMs = 0) => {
      if (!draftRef.current) return;
      draftRef.current = { ...draftRef.current, [k]: v };
      setDraft(draftRef.current);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => void flush(), debounceMs);
    },
    [flush],
  );

  useEffect(() => {
    if (saveState !== "saved") return;
    const t = window.setTimeout(() => setSaveState("idle"), 2500);
    return () => window.clearTimeout(t);
  }, [saveState]);

  // Pick up toggles flipped elsewhere (another admin, the Requests tab) from the live state, unless
  // this page has its own unsaved change to that field.
  const { state: live } = useLive();
  const liveOpen = live?.requests_open;
  const liveApproval = live?.require_approval;
  useEffect(() => {
    const draft = draftRef.current;
    const saved = savedRef.current;
    if (!draft || !saved) return;
    const sync: Partial<Settings> = {};
    if (liveOpen !== undefined && liveOpen !== saved.requests_open && draft.requests_open === saved.requests_open && !inflight.current) sync.requests_open = liveOpen;
    if (liveApproval !== undefined && liveApproval !== saved.require_approval) sync.require_approval = liveApproval;
    if (Object.keys(sync).length === 0) return;
    savedRef.current = { ...saved, ...sync };
    draftRef.current = { ...draft, ...sync };
    setDraft(draftRef.current);
  }, [liveOpen, liveApproval]);

  const smartUnavailable = !!status && !status.smart_fades.available;

  return (
    <div className="page page--settings">
      <PageHeader title="Settings">
        <span className={cx("save-indicator", saveState !== "idle" && "is-shown", saveState === "error" && "is-error")} role="status" aria-live="polite">
          {saveState === "saving" ? (
            "Saving…"
          ) : saveState === "saved" ? (
            <>
              <Check size={16} /> Saved
            </>
          ) : saveState === "error" ? (
            "Not saved"
          ) : null}
        </span>
      </PageHeader>

      {!draft ? (
        <div className="spinner" />
      ) : (
        <>
          <section className="card">
            <h2 className="card__title">Party</h2>
            <label className="field">
              <span className="field__label">Party name</span>
              <input
                className="input"
                value={draft.party_name}
                maxLength={80}
                onChange={(e) => set("party_name", e.target.value, 700)}
                onBlur={() => void flush()}
              />
            </label>
            <label className="switch-row">
              <input type="checkbox" role="switch" checked={draft.requests_open} onChange={(e) => set("requests_open", e.target.checked)} />
              <span className="switch" aria-hidden />
              <span>Requests open</span>
            </label>
            <RequireApprovalSwitch />
          </section>

          <section className="card">
            <h2 className="card__title">TV screen</h2>
            {(
              [
                ["tv_cta_text", "Request prompt (next to the QR code)", TV_TEXT_DEFAULTS.cta],
                ["tv_paused_text", "When requests are paused", TV_TEXT_DEFAULTS.paused],
                ["tv_empty_text", "When nothing is queued", TV_TEXT_DEFAULTS.empty],
              ] as const
            ).map(([k, label, placeholder]) => (
              <label className="field" key={k}>
                <span className="field__label">{label}</span>
                <input
                  className="input"
                  value={draft[k]}
                  placeholder={placeholder}
                  maxLength={80}
                  onChange={(e) => set(k, e.target.value, 700)}
                  onBlur={() => void flush()}
                />
              </label>
            ))}
            <span className="field__hint">Leave blank to use the default text.</span>
          </section>

          <FallbackCard draft={draft} set={set} />

          <section className="card">
            <h2 className="card__title">Crossfade</h2>
            <div className="field">
              <div className="segmented" role="radiogroup" aria-label="Crossfade mode">
                {(["smart", "standard", "off"] as CrossfadeMode[]).map((m) => {
                  const disabled = m === "smart" && smartUnavailable && draft.crossfade_mode !== "smart";
                  return (
                    <button
                      key={m}
                      type="button"
                      role="radio"
                      aria-checked={draft.crossfade_mode === m}
                      className={cx(draft.crossfade_mode === m && "is-active")}
                      disabled={disabled}
                      title={disabled ? "Smart fades are unavailable on this server" : undefined}
                      onClick={() => set("crossfade_mode", m)}
                    >
                      {m === "smart" ? "Smart" : m === "standard" ? "Standard" : "Off"}
                    </button>
                  );
                })}
              </div>
              <span className="field__hint">
                {draft.crossfade_mode === "smart"
                  ? "Beat-matched, DJ-style transitions."
                  : draft.crossfade_mode === "standard"
                    ? "A simple fade between tracks."
                    : "Tracks play back to back."}
                {smartUnavailable && (
                  <span className="warn-text">
                    {" "}
                    Smart fades are unavailable{status?.smart_fades.error ? ` (${status.smart_fades.error})` : ""}
                    {draft.crossfade_mode === "smart" ? ". A standard fade is used instead." : "."}
                  </span>
                )}
              </span>
            </div>
            {draft.crossfade_mode === "standard" && (
              <label className="field">
                <span className="field__label">
                  Fade length <span className="field__value">{draft.crossfade_duration} s</span>
                </span>
                <input
                  type="range"
                  className="range"
                  min={1}
                  max={15}
                  step={1}
                  value={draft.crossfade_duration}
                  style={{ ["--val" as string]: `${((draft.crossfade_duration - 1) / 14) * 100}%` }}
                  onChange={(e) => set("crossfade_duration", Number(e.target.value), 400)}
                />
              </label>
            )}
          </section>

          <section className="card">
            <h2 className="card__title">Guest limits</h2>
            <div className="field-row">
              <label className="field">
                <span className="field__label">Max waiting requests per guest</span>
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={100}
                  value={draft.max_pending_per_guest}
                  onChange={(e) => set("max_pending_per_guest", Math.max(1, Number(e.target.value) || 1), 700)}
                  onBlur={() => void flush()}
                />
              </label>
              <label className="field">
                <span className="field__label">Cooldown between requests (s)</span>
                <input
                  className="input"
                  type="number"
                  min={0}
                  max={3600}
                  value={draft.request_cooldown_seconds}
                  onChange={(e) => set("request_cooldown_seconds", Math.max(0, Number(e.target.value) || 0), 700)}
                  onBlur={() => void flush()}
                />
              </label>
            </div>
          </section>
        </>
      )}

      <AppleMusicCard onChange={loadStatus} />
      <StatusCard status={status} />
    </div>
  );
}

function FallbackCard({ draft, set }: { draft: Settings; set: SetSetting }) {
  const [playlists, setPlaylists] = useState<Playlist[] | null>(null);
  const [plError, setPlError] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const [resolving, setResolving] = useState(false);
  const [resolveError, setResolveError] = useState<string | null>(null);

  useEffect(() => {
    api
      .playlists()
      .then((r) => setPlaylists(r.playlists))
      .catch((e) => setPlError((e as Error).message));
  }, []);

  const choose = (id: string | null, name: string | null) => {
    set("fallback_playlist_id", id);
    set("fallback_playlist_name", name);
  };

  const resolve = async (e: FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;
    setResolving(true);
    setResolveError(null);
    try {
      const r = await api.resolvePlaylist(url.trim());
      choose(r.id, r.name);
      setUrl("");
    } catch (err) {
      setResolveError((err as Error).message);
    } finally {
      setResolving(false);
    }
  };

  return (
    <section className="card">
      <div className="card__head">
        <h2 className="card__title">Fallback playlist</h2>
        {draft.fallback_playlist_id && (
          <button className="btn btn--ghost btn--sm" type="button" onClick={() => choose(null, null)}>
            Clear
          </button>
        )}
      </div>
      <p className="muted card__sub">Plays when the queue is empty.</p>
      {draft.fallback_playlist_id && !playlists?.some((p) => p.id === draft.fallback_playlist_id) && (
        <p className="current-pl">
          <Check size={16} /> <strong>{draft.fallback_playlist_name || draft.fallback_playlist_id}</strong>
        </p>
      )}

      <div className="field">
        {plError ? (
          <div className="banner banner--err">Couldn't load playlists: {plError}</div>
        ) : playlists === null ? (
          <div className="spinner spinner--sm" />
        ) : playlists.length === 0 ? (
          <p className="muted">No playlists in the connected Apple Music library.</p>
        ) : (
          <div className="pl-grid">
            {playlists.map((p) => {
              const sel = p.id === draft.fallback_playlist_id;
              return (
                <button key={p.id} type="button" className={cx("pl", sel && "is-selected")} onClick={() => choose(p.id, p.name)} aria-pressed={sel}>
                  <Artwork url={p.artwork_url} size={200} className="pl__art" />
                  <span className="pl__name">{p.name}</span>
                  {sel && (
                    <span className="pl__check">
                      <Check size={14} />
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        )}
      </div>

      <form className="field" onSubmit={resolve}>
        <div className="input-group">
          <input
            className="input"
            placeholder="Or paste an Apple Music playlist link"
            aria-label="Apple Music playlist link"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
          <button className="btn" type="submit" disabled={resolving || !url.trim()}>
            {resolving ? "Looking up…" : "Use"}
          </button>
        </div>
        {resolveError && <span className="field__error">{resolveError}</span>}
      </form>

      <label className="switch-row">
        <input type="checkbox" role="switch" checked={draft.fallback_shuffle} onChange={(e) => set("fallback_shuffle", e.target.checked)} />
        <span className="switch" aria-hidden />
        <span>Shuffle</span>
      </label>
    </section>
  );
}

function relDays(ts: number | null): string {
  if (!ts) return "";
  const days = (ts * 1000 - Date.now()) / 86_400_000;
  if (days < 0) return "expired";
  if (days < 1) return "expires today";
  return `expires in ${Math.floor(days)} day${Math.floor(days) === 1 ? "" : "s"}`;
}

function AppleMusicCard({ onChange }: { onChange: () => void }) {
  const [am, setAm] = useState<AppleMusicStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [devToken, setDevToken] = useState("");
  const [userToken, setUserToken] = useState("");
  const [showTokens, setShowTokens] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    api
      .appleMusic()
      .then((r) => {
        setAm(r);
        setError(null);
      })
      .catch((e) => setError((e as Error).message));
  }, []);
  useEffect(load, [load]);

  const put = async (body: { developer_token?: string; user_token?: string }, msg: string) => {
    setSaving(true);
    try {
      setAm(await api.putAppleMusic(body));
      flash.ok(msg);
      onChange();
    } catch (e) {
      flash.err(e);
    } finally {
      setSaving(false);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const body: { developer_token?: string; user_token?: string } = {};
    if (devToken.trim()) body.developer_token = devToken.trim();
    if (userToken.trim()) body.user_token = userToken.trim();
    if (!Object.keys(body).length) return;
    void put(body, "Tokens saved").then(() => {
      setDevToken("");
      setUserToken("");
    });
  };

  const expired = am?.developer_token_expires_at ? am.developer_token_expires_at * 1000 < Date.now() : false;
  const canConnect = !!am?.developer_token_source && !expired;

  return (
    <section className="card am-card">
      <div className="am-card__head">
        <h2 className="card__title">Apple Music</h2>
        {am && (
          <span className={cx("pill", am.ok ? "pill--ok" : am.configured ? "pill--err" : "pill--off")}>
            {am.ok ? "Connected" : am.configured ? "Error" : "Not connected"}
          </span>
        )}
      </div>

      {error && <div className="banner banner--err">{error}</div>}
      {am && (
        <>
          {am.error && <div className="banner banner--err">{am.error}</div>}
          <dl className="kv">
            <dt>Storefront</dt>
            <dd>{am.storefront ? am.storefront.toUpperCase() : <span className="muted">Not set</span>}</dd>
            <dt>Developer token</dt>
            <dd>
              {am.developer_token_source ? (
                <>
                  from {am.developer_token_source === "env" ? "server config" : "this panel"}
                  {am.developer_token_expires_at && (
                    <span className={cx("muted", expired && "warn-text")}>
                      {" "}
                      · {relDays(am.developer_token_expires_at)} ({fmtDate(am.developer_token_expires_at)})
                    </span>
                  )}
                </>
              ) : (
                <span className="warn-text">missing</span>
              )}
            </dd>
            <dt>Music user token</dt>
            <dd>
              {am.user_token_source ? (
                <>
                  from {am.user_token_source === "env" ? "server config" : "this panel"}
                  {am.user_token_set_at && <span className="muted"> · set {fmtDate(am.user_token_set_at)}</span>}
                </>
              ) : (
                <span className="warn-text">not connected</span>
              )}
            </dd>
            <dt>Widevine</dt>
            <dd>{am.widevine ? "available" : <span className="warn-text">unavailable</span>}</dd>
          </dl>

          <div className="am-card__actions">
            {canConnect ? (
              // Full page load: this is a server-rendered MusicKit page, not part of the SPA.
              <a className="btn btn--primary" href="/admin/apple-music/connect">
                {am.user_token_source ? "Reconnect Apple Music" : "Connect Apple Music"}
              </a>
            ) : (
              <>
                <button className="btn btn--primary" disabled>
                  Connect Apple Music
                </button>
                <span className="field__hint">
                  {expired ? "The developer token has expired. Paste a new one below." : "Add a developer token first (below or in the server config)."}
                </span>
              </>
            )}
          </div>

          <details className="advanced">
            <summary>Advanced: paste tokens manually</summary>
            <form onSubmit={submit}>
              <label className="field">
                <span className="field__label">Developer token (JWT)</span>
                <textarea
                  className={cx("input input--mono", !showTokens && "input--masked")}
                  rows={3}
                  value={devToken}
                  autoComplete="off"
                  autoCorrect="off"
                  autoCapitalize="off"
                  onChange={(e) => setDevToken(e.target.value)}
                  placeholder={am.developer_token_source === "env" ? "Set by server config (takes precedence)" : "eyJhbGciOiJFUzI1NiIs…"}
                  spellCheck={false}
                />
              </label>
              <label className="field">
                <span className="field__label">Music user token</span>
                <textarea
                  className={cx("input input--mono", !showTokens && "input--masked")}
                  rows={3}
                  value={userToken}
                  autoComplete="off"
                  autoCorrect="off"
                  autoCapitalize="off"
                  onChange={(e) => setUserToken(e.target.value)}
                  placeholder={am.user_token_source === "env" ? "Set by server config (takes precedence)" : "Paste a Music-User-Token"}
                  spellCheck={false}
                />
              </label>
              <label className="switch-row">
                <input type="checkbox" role="switch" checked={showTokens} onChange={(e) => setShowTokens(e.target.checked)} />
                <span className="switch" aria-hidden />
                <span>Show tokens</span>
              </label>
              <div className="btn-group btn-group--wrap">
                <button className="btn btn--primary" type="submit" disabled={saving || (!devToken.trim() && !userToken.trim())}>
                  Save tokens
                </button>
                {am.developer_token_source === "db" && (
                  <button type="button" className="btn btn--danger-ghost" disabled={saving} onClick={() => put({ developer_token: "" }, "Developer token cleared")}>
                    Clear stored developer token
                  </button>
                )}
                {am.user_token_source === "db" && (
                  <button type="button" className="btn btn--danger-ghost" disabled={saving} onClick={() => put({ user_token: "" }, "User token cleared")}>
                    Clear stored user token
                  </button>
                )}
              </div>
            </form>
          </details>
        </>
      )}
    </section>
  );
}

function fmtDuration(sec: number): string {
  const m = Math.floor(sec / 60);
  const h = Math.floor(m / 60);
  return h > 0 ? `${h}h ${m % 60}m` : m > 0 ? `${m}m` : `${Math.round(sec)} s`;
}

/** Engine fields worth showing, in plain words. Anything not listed here is internal and hidden. */
const ENGINE_FIELDS: Record<string, (v: unknown) => string | null> = {
  paused: (v) => (v ? "Paused" : "Playing"),
  state: (v) => (typeof v === "string" ? v.charAt(0).toUpperCase() + v.slice(1) : null),
  locked_next: (v) => (v ? `Mixing in: ${v}` : null),
  last_transition: (v) => (typeof v === "string" && v ? `Last transition: ${v}` : null),
  uptime_seconds: (v) => (typeof v === "number" ? `Uptime ${fmtDuration(v)}` : null),
  buffer_seconds: (v) => (typeof v === "number" ? `Buffer ${v.toFixed(1)} s` : null),
  sample_rate: (v) => (typeof v === "number" ? `${(v / 1000).toLocaleString()} kHz` : null),
  codec: (v) => (typeof v === "string" ? v.toUpperCase() : null),
};

function StatusCard({ status }: { status: AdminStatus | null }) {
  if (!status) return null;
  const engine = status.engine ?? {};
  const facts = Object.keys(ENGINE_FIELDS)
    .filter((k) => k in engine)
    .map((k) => ENGINE_FIELDS[k]!(engine[k]))
    .filter((x): x is string => !!x);
  const lastError = typeof engine.last_error === "string" && engine.last_error ? engine.last_error : null;
  return (
    <section className="card">
      <h2 className="card__title">System status</h2>
      <div className="status-grid">
        <StatusItem label="Apple Music" ok={status.apple_music.ok} text={status.apple_music.ok ? "Connected" : status.apple_music.error || (status.apple_music.configured ? "Error" : "Not configured")} />
        <StatusItem label="Smart fades" ok={status.smart_fades.available} text={status.smart_fades.available ? "Available" : status.smart_fades.error || "Unavailable"} />
        {status.fallback && (
          <StatusItem
            label="Fallback playlist"
            ok={!status.fallback.error}
            text={status.fallback.error || (status.fallback.tracks > 0 ? `${status.fallback.tracks.toLocaleString()} tracks loaded` : "No tracks loaded")}
          />
        )}
        <StatusItem label="Listening devices" ok text={String(status.listeners)} />
      </div>
      {facts.length > 0 && <p className="status-facts">{facts.join(" · ")}</p>}
      {lastError && <div className="banner banner--err">Last playback error: {lastError}</div>}
    </section>
  );
}

function StatusItem({ label, ok, text }: { label: string; ok: boolean; text: string }) {
  return (
    <div className="status-item">
      <span className={cx("dot", ok ? "dot--ok" : "dot--err")} aria-hidden />
      <div>
        <div className="status-item__label">{label}</div>
        <div className="status-item__text">{text}</div>
      </div>
    </div>
  );
}
