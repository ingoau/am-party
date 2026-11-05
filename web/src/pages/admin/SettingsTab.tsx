import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { api } from "../../api/client";
import type { AdminStatus, AppleMusicStatus, CrossfadeMode, Playlist, Settings } from "../../api/types";
import { Artwork } from "../../components/Artwork";
import { Check, Music } from "lucide-react";
import { cx, fmtDate } from "../../util";
import { PageHeader, flash } from "./common";
import { RequireApprovalSwitch } from "./RequireApprovalSwitch";

export function SettingsTab() {
  const [saved, setSaved] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<AdminStatus | null>(null);

  const loadStatus = useCallback(() => {
    api.status().then(setStatus).catch(() => setStatus(null));
  }, []);

  useEffect(() => {
    api
      .settings()
      .then((s) => {
        setSaved(s);
        setDraft(s);
      })
      .catch(flash.err);
    loadStatus();
    const t = window.setInterval(loadStatus, 15_000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadStatus]);

  const patch = useMemo(() => {
    if (!saved || !draft) return {};
    const p: Partial<Settings> = {};
    (Object.keys(draft) as (keyof Settings)[]).forEach((k) => {
      // require_approval applies immediately via its own endpoint (see RequireApprovalSwitch).
      if (k === "require_approval") return;
      if (draft[k] !== saved[k]) (p as Record<string, unknown>)[k] = draft[k];
    });
    return p;
  }, [saved, draft]);
  const dirty = Object.keys(patch).length > 0;

  const set = <K extends keyof Settings>(k: K, v: Settings[K]) => setDraft((d) => (d ? { ...d, [k]: v } : d));

  const save = async () => {
    if (!dirty) return;
    setSaving(true);
    try {
      const s = await api.patchSettings(patch);
      // Backend returns Settings; fall back to merging if it returns something else.
      const next = s && typeof s === "object" && "party_name" in s ? s : { ...saved!, ...patch };
      setSaved(next);
      setDraft(next);
      flash.ok("Settings saved");
      loadStatus();
    } catch (e) {
      flash.err(e);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page page--settings">
      <PageHeader title="Settings" sub="Changes apply to everyone at the party." />

      {!draft ? (
        <div className="spinner" />
      ) : (
        <>
          <section className="card">
            <h2 className="card__title">Party</h2>
            <label className="field">
              <span className="field__label">Party name</span>
              <input className="input" value={draft.party_name} maxLength={80} onChange={(e) => set("party_name", e.target.value)} />
            </label>
            <label className="switch-row">
              <input type="checkbox" checked={draft.requests_open} onChange={(e) => set("requests_open", e.target.checked)} />
              <span className="switch" aria-hidden />
              <span>
                Requests open
                <small>Guests can search and request songs.</small>
              </span>
            </label>
            <RequireApprovalSwitch />
          </section>

          <FallbackCard draft={draft} set={set} />

          <section className="card">
            <h2 className="card__title">Transitions</h2>
            <div className="field">
              <span className="field__label">Crossfade</span>
              <div className="segmented" role="radiogroup" aria-label="Crossfade mode">
                {(["smart", "standard", "off"] as CrossfadeMode[]).map((m) => (
                  <button
                    key={m}
                    type="button"
                    role="radio"
                    aria-checked={draft.crossfade_mode === m}
                    className={cx(draft.crossfade_mode === m && "is-active")}
                    onClick={() => set("crossfade_mode", m)}
                  >
                    {m === "smart" ? "Smart" : m === "standard" ? "Standard" : "Off"}
                  </button>
                ))}
              </div>
              <span className="field__hint">
                {draft.crossfade_mode === "smart"
                  ? "Beat-matched DJ-style transitions."
                  : draft.crossfade_mode === "standard"
                    ? "A simple fade between tracks."
                    : "Tracks play back to back."}
                {draft.crossfade_mode === "smart" && status && !status.smart_fades.available && (
                  <span className="warn-text">
                    {" "}
                    Smart fades are unavailable{status.smart_fades.error ? ` (${status.smart_fades.error})` : ""} — a standard fade will be used.
                  </span>
                )}
              </span>
            </div>
            {draft.crossfade_mode === "standard" && (
              <label className="field">
                <span className="field__label">
                  Fade duration <strong>{draft.crossfade_duration}s</strong>
                </span>
                <input
                  type="range"
                  className="range"
                  min={1}
                  max={15}
                  step={1}
                  value={draft.crossfade_duration}
                  style={{ ["--val" as string]: `${((draft.crossfade_duration - 1) / 14) * 100}%` }}
                  onChange={(e) => set("crossfade_duration", Number(e.target.value))}
                />
              </label>
            )}
          </section>

          <section className="card">
            <h2 className="card__title">Guest limits</h2>
            <div className="field-row">
              <label className="field">
                <span className="field__label">Max pending requests per guest</span>
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={100}
                  value={draft.max_pending_per_guest}
                  onChange={(e) => set("max_pending_per_guest", Math.max(1, Number(e.target.value) || 1))}
                />
              </label>
              <label className="field">
                <span className="field__label">Cooldown between requests (seconds)</span>
                <input
                  className="input"
                  type="number"
                  min={0}
                  max={3600}
                  value={draft.request_cooldown_seconds}
                  onChange={(e) => set("request_cooldown_seconds", Math.max(0, Number(e.target.value) || 0))}
                />
              </label>
            </div>
          </section>

          {dirty && (
          <div className="save-bar" role="region" aria-label="Unsaved changes">
            <span>You have unsaved changes</span>
            <div className="btn-group">
              <button className="btn btn--ghost" onClick={() => setDraft(saved)} disabled={saving || !dirty}>
                Discard
              </button>
              <button className="btn btn--primary" onClick={save} disabled={saving || !dirty}>
                {saving ? "Saving…" : "Save changes"}
              </button>
            </div>
          </div>
          )}
        </>
      )}

      <AppleMusicCard onChange={loadStatus} />
      <StatusCard status={status} />
    </div>
  );
}

function FallbackCard({ draft, set }: { draft: Settings; set: <K extends keyof Settings>(k: K, v: Settings[K]) => void }) {
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
      <h2 className="card__title">Fallback playlist</h2>
      <p className="muted card__sub">Plays when the queue runs dry, so the music never stops.</p>
      <div className="current-pl">
        <span className="field__label">Selected</span>
        <div className="current-pl__row">
          <strong>{draft.fallback_playlist_name || (draft.fallback_playlist_id ? draft.fallback_playlist_id : "None")}</strong>
          {draft.fallback_playlist_id && (
            <button className="btn btn--ghost btn--sm" type="button" onClick={() => choose(null, null)}>
              Clear
            </button>
          )}
        </div>
      </div>

      <label className="switch-row">
        <input type="checkbox" checked={draft.fallback_shuffle} onChange={(e) => set("fallback_shuffle", e.target.checked)} />
        <span className="switch" aria-hidden />
        <span>Shuffle the fallback playlist</span>
      </label>

      <div className="field">
        <span className="field__label">Your library playlists</span>
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
        <span className="field__label">…or paste an Apple Music playlist link</span>
        <div className="input-group">
          <input
            className="input"
            placeholder="https://music.apple.com/…/playlist/…/pl.…"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
          <button className="btn" type="submit" disabled={resolving || !url.trim()}>
            {resolving ? "Looking up…" : "Use playlist"}
          </button>
        </div>
        {resolveError && <span className="field__error">{resolveError}</span>}
      </form>
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
        <div className="am-logo" aria-hidden>
          <Music size={22} />
        </div>
        <div>
          <h2 className="card__title">Apple Music</h2>
          <p className="muted card__sub">The catalog guests search and the source of every track.</p>
        </div>
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
            <dd>{am.storefront ? am.storefront.toUpperCase() : "—"}</dd>
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
                  {expired ? "The developer token has expired — paste a new one below." : "Add a developer token first (below or in the server config)."}
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
                  className="input input--mono"
                  rows={3}
                  value={devToken}
                  onChange={(e) => setDevToken(e.target.value)}
                  placeholder={am.developer_token_source === "env" ? "Set by server config (takes precedence)" : "eyJhbGciOiJFUzI1NiIs…"}
                  spellCheck={false}
                />
              </label>
              <label className="field">
                <span className="field__label">Music user token</span>
                <textarea
                  className="input input--mono"
                  rows={3}
                  value={userToken}
                  onChange={(e) => setUserToken(e.target.value)}
                  placeholder={am.user_token_source === "env" ? "Set by server config (takes precedence)" : "Paste a Music-User-Token"}
                  spellCheck={false}
                />
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

function StatusCard({ status }: { status: AdminStatus | null }) {
  if (!status) return null;
  const engine = Object.entries(status.engine ?? {});
  return (
    <section className="card">
      <h2 className="card__title">System status</h2>
      <div className="status-grid">
        <StatusItem label="Apple Music" ok={status.apple_music.ok} text={status.apple_music.ok ? "OK" : status.apple_music.error || (status.apple_music.configured ? "Error" : "Not configured")} />
        <StatusItem label="Smart fades" ok={status.smart_fades.available} text={status.smart_fades.available ? "Available" : status.smart_fades.error || "Unavailable"} />
        <StatusItem label="Listeners" ok text={String(status.listeners)} />
      </div>
      {engine.length > 0 && (
        <dl className="kv kv--compact">
          {engine.map(([k, v]) => (
            <div key={k} className="kv__pair">
              <dt>{k.replace(/_/g, " ")}</dt>
              <dd>{formatValue(v)}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
  if (typeof v === "string") return v;
  return JSON.stringify(v);
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
