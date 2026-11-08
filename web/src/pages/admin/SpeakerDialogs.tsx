import { useEffect, useMemo, useState } from "react";
import * as AlertDialog from "@radix-ui/react-alert-dialog";
import { Copy, Search } from "lucide-react";
import { api, ApiError } from "../../api/client";
import type { ShareMode, Speaker, SpeakerShare, SpeakerSharePreview, User } from "../../api/types";
import { QrCode } from "../../components/QrCode";
import { useLive } from "../../hooks/live";
import { errorText, flash } from "./common";

const plural = (n: number) => `${n} speaker${n === 1 ? "" : "s"}`;

/** Seconds until `expiresAt` (server unix seconds), ticking once a second. */
function useCountdown(expiresAt: number | null): number {
  const { clockOffset } = useLive();
  const calc = () => (expiresAt === null ? 0 : Math.max(0, Math.round(expiresAt - (Date.now() / 1000 + clockOffset))));
  const [left, setLeft] = useState(calc);
  useEffect(() => {
    setLeft(calc());
    if (expiresAt === null) return;
    const t = window.setInterval(() => setLeft(calc()), 1000);
    return () => window.clearInterval(t);
  }, [expiresAt, clockOffset]); // eslint-disable-line react-hooks/exhaustive-deps
  return left;
}

const fmtLeft = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

/**
 * Share / Hand over: creates a single-use code for `ids` and shows it as a QR (plus the link) with
 * a countdown. "New code" makes another one once it has expired.
 */
export function ShareDialog({ ids, mode, onClose }: { ids: string[]; mode: ShareMode; onClose: () => void }) {
  const { state } = useLive();
  const [share, setShare] = useState<SpeakerShare | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const left = useCountdown(share?.expires_at ?? null);
  const base = (state?.public_url || location.origin).replace(/\/+$/, "");
  const url = share ? `${base}/admin/speakers?share=${encodeURIComponent(share.token)}` : "";
  const expired = !!share && left === 0;

  useEffect(() => {
    let alive = true;
    setShare(null);
    setError(null);
    api.shareSpeakers(ids, mode).then(
      (s) => alive && setShare(s),
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [ids, mode, attempt]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      flash.ok("Link copied");
    } catch {
      flash.err("Couldn't copy the link");
    }
  };

  const handover = mode === "handover";
  return (
    <AlertDialog.Root open onOpenChange={(o) => !o && onClose()}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="dialog-overlay" />
        <AlertDialog.Content className="dialog share-dialog">
          <AlertDialog.Title className="dialog__title">
            {handover ? "Hand over" : "Share"} {plural(ids.length)}
          </AlertDialog.Title>
          <AlertDialog.Description className="dialog__desc">
            {handover
              ? "Have the new owner scan this code while signed in. They become an owner and you're removed as one."
              : "Have the other person scan this code while signed in. They become a co-owner alongside you."}
          </AlertDialog.Description>
          {error ? (
            <p className="field__error">{error}</p>
          ) : !share ? (
            <div className="spinner" />
          ) : (
            <div className="share-dialog__code">
              <div className={expired ? "share-dialog__qr is-expired" : "share-dialog__qr"}>
                <QrCode text={url} label={`QR code: ${handover ? "take over" : "join"} ${plural(ids.length)}`} />
              </div>
              <p className="share-dialog__meta" role="status">
                {expired ? "This code has expired." : `Expires in ${fmtLeft(left)} · Single use`}
              </p>
              {!expired && (
                <button type="button" className="btn btn--ghost btn--sm" onClick={copy}>
                  <Copy size={14} aria-hidden /> Copy link
                </button>
              )}
            </div>
          )}
          <div className="dialog__actions">
            {(expired || error) && (
              <button type="button" className="btn btn--ghost" onClick={() => setAttempt((a) => a + 1)}>
                New code
              </button>
            )}
            <AlertDialog.Cancel className="btn btn--primary">Done</AlertDialog.Cancel>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}

/** /admin/speakers?share=<token>: preview what's being shared, then Accept or Cancel. */
export function AcceptShareDialog({
  token,
  onDone,
}: {
  token: string;
  /** Called once the dialog is finished with (accepted, cancelled or invalid); `speakers` on accept. */
  onDone: (speakers?: Speaker[]) => void;
}) {
  const [preview, setPreview] = useState<SpeakerSharePreview | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    api.sharePreview(token).then(
      (p) => alive && setPreview(p),
      (e) => {
        if (!alive) return;
        flash.err(e instanceof ApiError && e.status === 404 ? "This share code has expired or was already used." : e);
        onDone();
      },
    );
    return () => {
      alive = false;
    };
  }, [token]); // eslint-disable-line react-hooks/exhaustive-deps

  const accept = async () => {
    setBusy(true);
    try {
      const r = await api.acceptShare(token);
      flash.ok(preview?.mode === "handover" ? `You now own ${plural(r.speakers.length)}` : `Added ${plural(r.speakers.length)}`);
      onDone(r.speakers);
    } catch (e) {
      flash.err(e instanceof ApiError && e.status === 404 ? "This share code has expired or was already used." : e);
      onDone();
    }
  };

  if (!preview) return null;
  const n = preview.speakers.length;
  return (
    <AlertDialog.Root open onOpenChange={(o) => !o && !busy && onDone()}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="dialog-overlay" />
        <AlertDialog.Content className="dialog">
          <AlertDialog.Title className="dialog__title">
            {preview.mode === "handover"
              ? `${preview.from} is handing over ${plural(n)} to you`
              : `${preview.from} shared ${plural(n)} with you`}
          </AlertDialog.Title>
          <AlertDialog.Description className="dialog__desc">
            {preview.mode === "handover"
              ? "Accept to become their owner. They'll stop being one."
              : "Accept to become a co-owner and control them from here."}
          </AlertDialog.Description>
          <ul className="share-list">
            {preview.speakers.map((s) => (
              <li key={s.id}>
                <span className="truncate">{s.name}</span>
                <span className="muted">{s.connected ? "Online" : "Unavailable"}</span>
              </li>
            ))}
          </ul>
          <div className="dialog__actions">
            <AlertDialog.Cancel className="btn btn--ghost" disabled={busy}>
              Cancel
            </AlertDialog.Cancel>
            <button type="button" className="btn btn--primary" disabled={busy} onClick={accept}>
              {busy ? "Accepting…" : "Accept"}
            </button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}

/**
 * Admin only: pick a user to become a co-owner of `ids` (existing owners stay). Users without
 * "Claim speakers" are listed too, but flagged: they can't open the Speakers page.
 */
export function AssignDialog({
  ids,
  me,
  onClose,
}: {
  ids: string[];
  me: User;
  /** `speakers` once assigned. */
  onClose: (speakers?: Speaker[]) => void;
}) {
  const [users, setUsers] = useState<User[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    api.users().then(
      (r) => alive && setUsers(r.users),
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, []);

  const shown = useMemo(() => {
    const q = query.trim().toLocaleLowerCase();
    const label = (u: User) => u.name || u.email || "";
    return (users ?? [])
      .filter((u) => !q || label(u).toLocaleLowerCase().includes(q) || (u.email ?? "").toLocaleLowerCase().includes(q))
      .sort((a, b) => Number(b.id === me.id) - Number(a.id === me.id) || label(a).localeCompare(label(b)));
  }, [users, query, me.id]);

  const assign = async (u: User) => {
    setBusy(true);
    try {
      const r = await api.assignSpeakers(ids, u.id);
      flash.ok(u.id === me.id ? `Claimed ${plural(ids.length)}` : `Assigned ${plural(ids.length)} to ${u.name || u.email}`);
      onClose(r.speakers);
    } catch (e) {
      flash.err(e);
      setBusy(false);
    }
  };

  return (
    <AlertDialog.Root open onOpenChange={(o) => !o && !busy && onClose()}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="dialog-overlay" />
        <AlertDialog.Content className="dialog assign-dialog">
          <AlertDialog.Title className="dialog__title">Assign {plural(ids.length)}</AlertDialog.Title>
          <AlertDialog.Description className="dialog__desc">
            They become a co-owner and can control {ids.length === 1 ? "it" : "them"}. Existing owners stay.
          </AlertDialog.Description>
          {error ? (
            <p className="field__error">{error}</p>
          ) : users === null ? (
            <div className="spinner" />
          ) : (
            <>
              <label className="assign-dialog__search">
                <Search size={16} aria-hidden />
                <input
                  className="input"
                  type="search"
                  placeholder="Search people"
                  aria-label="Search people"
                  value={query}
                  autoFocus
                  onChange={(e) => setQuery(e.target.value)}
                />
              </label>
              <ul className="assign-list" aria-label="People">
                {shown.length === 0 && <li className="muted assign-list__empty">Nobody matches “{query.trim()}”</li>}
                {shown.map((u) => (
                  <li key={u.id}>
                    <button type="button" className="assign-list__btn" disabled={busy} onClick={() => void assign(u)}>
                      <span className="truncate">
                        {u.name || u.email}
                        {u.id === me.id && " (you)"}
                      </span>
                      {!u.permissions.includes("claim_speakers") && <span className="muted assign-list__note">Can't open Speakers</span>}
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
          <div className="dialog__actions">
            <AlertDialog.Cancel className="btn btn--ghost" disabled={busy}>
              Cancel
            </AlertDialog.Cancel>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
