import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { Lock, Minus, Pencil, Plus, ScanQrCode, Trash2, UserPlus, Volume2, VolumeX, X } from "lucide-react";
import { api } from "../../api/client";
import type { ShareMode, Speaker, SpeakerControl, User } from "../../api/types";
import { useLive, useLiveEvent } from "../../hooks/live";
import { cx, timeAgo } from "../../util";
import { Checkbox, PageHeader, flash } from "./common";
import { useConfirm } from "./ConfirmDialog";
import { AcceptShareDialog, AssignDialog, ShareDialog } from "./SpeakerDialogs";
import { SpeakerScanner } from "./SpeakerScanner";

/** −/+ in the bulk bar move every selected speaker by this much. */
const VOLUME_STEP = 5;
/** What the server assumes for a speaker that never reported a volume. */
const DEFAULT_VOLUME = 80;

const plural = (n: number) => `${n} speaker${n === 1 ? "" : "s"}`;

/**
 * Speakers (browsers listening on /tv or /stream) the user owns, or every speaker for admins:
 * volume, mute, "Lock local controls", names and co-owners, one at a time or in bulk. Admins see
 * only their own speakers until they switch on "All speakers", and can claim any speaker or
 * assign it to anyone without scanning.
 */
export function SpeakersTab({ me }: { me: User }) {
  const [speakers, setSpeakers] = useState<Speaker[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [sharing, setSharing] = useState<{ ids: string[]; mode: ShareMode } | null>(null);
  const [assigning, setAssigning] = useState<string[] | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [confirmDialog, confirm] = useConfirm();
  const [params, setParams] = useSearchParams();
  const shareToken = params.get("share");
  const { clockOffset } = useLive();

  // Only the newest list applies, and local updates bump the sequence so an older fetch can't undo them.
  const seq = useRef(0);
  const load = useCallback(() => {
    const my = ++seq.current;
    api
      .speakers()
      .then((r) => {
        if (my === seq.current) setSpeakers(r.speakers);
      })
      .catch((e) => my === seq.current && flash.err(e));
  }, []);
  useEffect(load, [load]);
  useLiveEvent("speakers_changed", load);

  // "last seen 3m ago" stays current.
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => setTick((n) => n + 1), 30_000);
    return () => window.clearInterval(t);
  }, []);

  const all = speakers ?? [];
  // Admins see only their own speakers unless "All speakers" is on.
  const filtered = me.is_admin && !showAll;
  const list = useMemo(() => (filtered ? all.filter((s) => s.mine) : all), [all, filtered]);

  // Drop selections of speakers that went away (or are filtered out).
  useEffect(() => {
    if (!speakers) return;
    setSelected((sel) => {
      const ids = new Set(list.map((s) => s.id));
      const next = new Set([...sel].filter((id) => ids.has(id)));
      return next.size === sel.size ? sel : next;
    });
  }, [speakers, list]);

  const merge = (updated: Speaker[]) => {
    seq.current++;
    const byId = new Map(updated.map((s) => [s.id, s]));
    setSpeakers((list) => list && list.map((s) => byId.get(s.id) ?? s));
  };
  const drop = (id: string) => {
    seq.current++;
    setSpeakers((list) => list && list.filter((s) => s.id !== id));
  };

  /** Returns whether it worked. `lock` disables the bulk/header buttons while it runs. */
  const control = async (body: SpeakerControl, okText?: string, lock = true): Promise<boolean> => {
    if (body.ids.length === 0) return false;
    if (lock) setBusy(true);
    try {
      const r = await api.controlSpeakers(body);
      merge(r.speakers);
      if (okText) flash.ok(okText);
      return true;
    } catch (e) {
      flash.err(e);
      load();
      return false;
    } finally {
      if (lock) setBusy(false);
    }
  };

  const patch = async (s: Speaker, p: { name?: string; locked?: boolean }) => {
    try {
      const r = await api.patchSpeaker(s.id, p);
      merge([r.speaker]);
    } catch (e) {
      flash.err(e);
      load();
    }
  };

  const removeOwner = async (s: Speaker, owner: { id: number; name: string }) => {
    const self = owner.id === me.id;
    const ok = await confirm(
      self
        ? {
            title: `Leave “${s.name}”?`,
            description: me.is_admin
              ? "You'll no longer be one of its owners. As an admin you can still control it."
              : "You'll no longer see or control this speaker. Scan its code again to claim it back.",
            confirmLabel: "Leave",
            danger: true,
          }
        : {
            title: `Remove ${owner.name} from “${s.name}”?`,
            description: "They'll no longer be able to see or control this speaker.",
            confirmLabel: "Remove",
            danger: true,
          },
    );
    if (!ok) return;
    try {
      const r = await api.removeSpeakerOwner(s.id, owner.id);
      if (r.speaker) merge([r.speaker]);
      else drop(s.id);
      flash.ok(self ? `Left “${s.name}”` : `Removed ${owner.name}`);
    } catch (e) {
      flash.err(e);
      load();
    }
  };

  const forget = async (s: Speaker) => {
    const ok = await confirm({
      title: `Forget “${s.name}”?`,
      description: "It disappears from the list along with its owners and settings. If it listens again, it shows up as a new, unclaimed speaker.",
      confirmLabel: "Forget",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.forgetSpeaker(s.id);
      drop(s.id);
      flash.ok(`Forgot “${s.name}”`);
    } catch (e) {
      flash.err(e);
      load();
    }
  };

  const claim = async (ids: string[]) => {
    if (ids.length === 0) return;
    setBusy(true);
    try {
      const r = await api.assignSpeakers(ids, me.id);
      merge(r.speakers);
      flash.ok(`Claimed ${plural(r.speakers.length)}`);
    } catch (e) {
      flash.err(e);
      load();
    } finally {
      setBusy(false);
    }
  };

  const mine = useMemo(() => all.filter((s) => s.mine), [all]);
  const selectedSpeakers = list.filter((s) => selected.has(s.id));
  const selIds = selectedSpeakers.map((s) => s.id);
  const selNotMine = selectedSpeakers.filter((s) => !s.mine).map((s) => s.id);
  const allSelected = list.length > 0 && selected.size === list.length;
  const someSelected = selected.size > 0 && !allSelected;
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(list.map((s) => s.id)));
  const toggle = (id: string) =>
    setSelected((sel) => {
      const next = new Set(sel);
      if (!next.delete(id)) next.add(id);
      return next;
    });

  /** "Mute …" unless every one of them is muted already. */
  const muteAction = (group: Speaker[], what: string) => {
    const allMuted = group.length > 0 && group.every((s) => s.muted);
    return {
      label: `${allMuted ? "Unmute" : "Mute"} ${what}`,
      run: () => control({ ids: group.map((s) => s.id), muted: !allMuted }, `${allMuted ? "Unmuted" : "Muted"} ${plural(group.length)}`),
    };
  };
  const mineMute = muteAction(mine, "my speakers");
  const allMute = muteAction(all, "all");

  const selAllLocked = selectedSpeakers.length > 0 && selectedSpeakers.every((s) => s.locked);
  const selVolume = selectedSpeakers.length
    ? Math.round(selectedSpeakers.reduce((a, s) => a + (s.volume ?? DEFAULT_VOLUME), 0) / selectedSpeakers.length)
    : DEFAULT_VOLUME;

  const finishShare = (accepted?: Speaker[]) => {
    const next = new URLSearchParams(params);
    next.delete("share");
    setParams(next, { replace: true });
    if (accepted) load();
  };

  return (
    <div className="page page--speakers">
      <PageHeader
        title={me.is_admin ? "Speakers" : "My speakers"}
        sub={
          me.is_admin
            ? "Devices listening on the TV screen or the Listen page. Scan a speaker's code, or switch on “All speakers” to claim or assign any of them."
            : "Devices listening on the TV screen or the Listen page. Scan a speaker's code to add it."
        }
      >
        {me.is_admin && (
          <label className="switch-row switch-row--inline">
            <input type="checkbox" role="switch" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
            <span className="switch" aria-hidden />
            <span>All speakers{speakers ? ` (${all.length})` : ""}</span>
          </label>
        )}
        <button className="btn" onClick={() => setScanning(true)}>
          <ScanQrCode size={18} aria-hidden />
          Claim speakers
        </button>
        {mine.length > 0 && (
          <button className="btn btn--primary" disabled={busy} onClick={mineMute.run}>
            {mineMute.label.startsWith("Mute") ? <VolumeX size={18} aria-hidden /> : <Volume2 size={18} aria-hidden />}
            {mineMute.label}
          </button>
        )}
        {me.is_admin && showAll && all.length > 0 && (
          <button className="btn" disabled={busy} onClick={allMute.run}>
            {allMute.label}
          </button>
        )}
      </PageHeader>

      {speakers === null ? (
        <div className="spinner" />
      ) : list.length === 0 ? (
        <p className="empty">
          {me.is_admin && all.length === 0
            ? "No speakers yet. They appear here once a device listens on the TV screen or the Listen page."
            : me.is_admin
            ? `You haven't claimed any speakers. Switch on “All speakers” to see all ${plural(all.length)} and claim or assign them.`
            : "You don't have any speakers yet. Tap “Claim speakers” and scan the QR code on a TV that's listening, or the one under “Claim this speaker” on a phone's Listen page."}
        </p>
      ) : (
        <div className="people">
          <div className="people__toolbar">
            <label className="people__select-all">
              <Checkbox checked={allSelected} indeterminate={someSelected} onChange={toggleAll} aria-label="Select all speakers" />
              <span>{selected.size > 0 ? `${selected.size} of ${list.length} selected` : "Select all"}</span>
            </label>
          </div>
          <ul className="ulist" aria-label="Speakers">
            {list.map((s) => (
              <SpeakerRow
                key={s.id}
                s={s}
                me={me}
                clockOffset={clockOffset}
                selected={selected.has(s.id)}
                onSelect={() => toggle(s.id)}
                onVolume={(v) => control({ ids: [s.id], volume: v }, undefined, false)}
                onMute={() => void control({ ids: [s.id], muted: !s.muted }, undefined, false)}
                onPatch={(p) => patch(s, p)}
                onRemoveOwner={(o) => removeOwner(s, o)}
                onForget={() => forget(s)}
                busy={busy}
                onClaim={() => void claim([s.id])}
                onAssign={() => setAssigning([s.id])}
              />
            ))}
          </ul>
        </div>
      )}

      {selectedSpeakers.length > 0 && (
        <div className="bulk-bar bulk-bar--speakers" role="region" aria-label="Bulk actions">
          <div className="bulk-bar__count">
            <strong>{selectedSpeakers.length}</strong> selected
          </div>
          <div className="bulk-bar__actions">
            <div className="sbulk__volume">
              <button
                className="icon-btn"
                disabled={busy}
                onClick={() => control({ ids: selIds, volume_delta: -VOLUME_STEP })}
                aria-label={`Turn the selected speakers down by ${VOLUME_STEP}`}
                title={`−${VOLUME_STEP}`}
              >
                <Minus size={18} />
              </button>
              <VolumeSlider value={selVolume} label="Volume of the selected speakers" onCommit={(v) => control({ ids: selIds, volume: v }, undefined, false)} />
              <button
                className="icon-btn"
                disabled={busy}
                onClick={() => control({ ids: selIds, volume_delta: VOLUME_STEP })}
                aria-label={`Turn the selected speakers up by ${VOLUME_STEP}`}
                title={`+${VOLUME_STEP}`}
              >
                <Plus size={18} />
              </button>
            </div>
            <button className="btn btn--sm" disabled={busy} onClick={() => control({ ids: selIds, muted: true }, `Muted ${plural(selIds.length)}`)}>
              Mute
            </button>
            <button className="btn btn--sm" disabled={busy} onClick={() => control({ ids: selIds, muted: false }, `Unmuted ${plural(selIds.length)}`)}>
              Unmute
            </button>
            <button
              className="btn btn--sm"
              disabled={busy}
              aria-pressed={selAllLocked}
              onClick={() =>
                control({ ids: selIds, locked: !selAllLocked }, `${selAllLocked ? "Unlocked" : "Locked"} local controls on ${plural(selIds.length)}`)
              }
            >
              <Lock size={14} aria-hidden />
              {selAllLocked ? "Unlock local controls" : "Lock local controls"}
            </button>
            <button className="btn btn--sm" disabled={busy} onClick={() => setSharing({ ids: selIds, mode: "share" })}>
              Share
            </button>
            <button className="btn btn--sm" disabled={busy} onClick={() => setSharing({ ids: selIds, mode: "handover" })}>
              Hand over
            </button>
            {me.is_admin && selNotMine.length > 0 && (
              <button className="btn btn--sm" disabled={busy} onClick={() => void claim(selNotMine)}>
                Claim
              </button>
            )}
            {me.is_admin && (
              <button className="btn btn--sm" disabled={busy} onClick={() => setAssigning(selIds)}>
                <UserPlus size={14} aria-hidden /> Assign
              </button>
            )}
          </div>
          <button className="icon-btn bulk-bar__clear" onClick={() => setSelected(new Set())} aria-label="Clear selection" title="Clear selection">
            <X size={18} />
          </button>
        </div>
      )}

      {assigning && (
        <AssignDialog
          ids={assigning}
          me={me}
          onClose={(updated) => {
            setAssigning(null);
            if (updated) merge(updated);
          }}
        />
      )}
      {sharing && <ShareDialog ids={sharing.ids} mode={sharing.mode} onClose={() => setSharing(null)} />}
      {scanning && (
        <SpeakerScanner
          onClose={() => {
            setScanning(false);
            load();
          }}
          onShare={(token) => {
            setScanning(false);
            const next = new URLSearchParams(params);
            next.set("share", token);
            setParams(next, { replace: true });
          }}
        />
      )}
      {shareToken && <AcceptShareDialog key={shareToken} token={shareToken} onDone={finishShare} />}
      {confirmDialog}
    </div>
  );
}

function SpeakerRow({
  s,
  me,
  clockOffset,
  selected,
  onSelect,
  onVolume,
  onMute,
  onPatch,
  onRemoveOwner,
  onForget,
  busy,
  onClaim,
  onAssign,
}: {
  s: Speaker;
  me: User;
  clockOffset: number;
  selected: boolean;
  onSelect: () => void;
  onVolume: (v: number) => Promise<boolean>;
  onMute: () => void;
  onPatch: (p: { name?: string; locked?: boolean }) => void;
  onRemoveOwner: (o: { id: number; name: string }) => void;
  onForget: () => void;
  busy: boolean;
  onClaim: () => void;
  onAssign: () => void;
}) {
  const canManageOwners = s.mine || me.is_admin;
  return (
    <li className={cx("srow", selected && "is-selected", !s.connected && "is-offline")}>
      <div className="srow__select">
        <Checkbox checked={selected} onChange={onSelect} aria-label={`Select ${s.name}`} />
      </div>
      <div className="srow__body">
        <div className="srow__main">
          <div className="srow__title">
            <SpeakerName name={s.name} onRename={(name) => onPatch({ name })} />
            <div className="srow__status muted">
              <span className={cx("srow__dot", s.connected && "is-on")} aria-hidden />
              {s.connected ? "Online" : `Unavailable${s.last_seen ? ` · last seen ${timeAgo(s.last_seen, clockOffset)}` : ""}`}
            </div>
          </div>
          <div className="srow__controls">
            <button
              type="button"
              className={cx("icon-btn", s.muted && "is-muted")}
              onClick={onMute}
              aria-label={s.muted ? `Unmute ${s.name}` : `Mute ${s.name}`}
              aria-pressed={s.muted}
              title={s.muted ? "Unmute" : "Mute"}
            >
              {s.muted || s.volume === 0 ? <VolumeX size={20} /> : <Volume2 size={20} />}
            </button>
            <VolumeSlider value={s.volume ?? DEFAULT_VOLUME} muted={s.muted} label={`Volume of ${s.name}`} onCommit={onVolume} />
            <span className="srow__vol">{s.muted ? "Muted" : s.volume === null ? "—" : `${s.volume}%`}</span>
            <label className="switch-row switch-row--inline srow__lock" title="Lock local controls: the device itself can't change its volume, mute or name">
              <input type="checkbox" role="switch" checked={s.locked} onChange={(e) => onPatch({ locked: e.target.checked })} aria-label={`Lock local controls on ${s.name}`} />
              <span className="switch switch--sm" aria-hidden />
              <span>Lock</span>
            </label>
          </div>
        </div>
        <div className="srow__owners">
          {s.owners.length === 0 ? (
            <span className="muted">Unclaimed</span>
          ) : (
            s.owners.map((o) => (
              <span key={o.id} className="owner-chip">
                <span className="truncate">{o.id === me.id ? `${o.name} (you)` : o.name}</span>
                {canManageOwners && (
                  <button
                    type="button"
                    className="owner-chip__x"
                    onClick={() => onRemoveOwner(o)}
                    aria-label={o.id === me.id ? `Leave ${s.name}` : `Remove ${o.name} from ${s.name}`}
                    title={o.id === me.id ? "Leave" : "Remove"}
                  >
                    <X size={14} />
                  </button>
                )}
              </span>
            ))
          )}
          {me.is_admin && (
            <span className="srow__admin">
              {!s.mine && (
                <button type="button" className="btn btn--ghost btn--sm" disabled={busy} onClick={onClaim} aria-label={`Claim ${s.name}`}>
                  Claim
                </button>
              )}
              <button type="button" className="btn btn--ghost btn--sm" disabled={busy} onClick={onAssign} aria-label={`Assign ${s.name} to someone`}>
                <UserPlus size={14} aria-hidden /> Assign
              </button>
              {!s.connected && (
                <button type="button" className="btn btn--danger-ghost btn--sm" onClick={onForget}>
                  <Trash2 size={14} aria-hidden /> Forget
                </button>
              )}
            </span>
          )}
        </div>
      </div>
    </li>
  );
}

/** Name with a pencil: click to edit inline; Enter / blur saves, Esc cancels. */
function SpeakerName({ name, onRename }: { name: string; onRename: (name: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(name);
  const cancelled = useRef(false);
  if (!editing) {
    return (
      <button
        type="button"
        className="srow__name"
        onClick={() => {
          setDraft(name);
          cancelled.current = false;
          setEditing(true);
        }}
        title="Rename"
      >
        <span className="truncate">{name}</span>
        <Pencil size={14} aria-hidden className="srow__pencil" />
        <span className="sr-only">Rename</span>
      </button>
    );
  }
  const commit = () => {
    setEditing(false);
    const n = draft.trim();
    if (!cancelled.current && n && n !== name) onRename(n);
  };
  return (
    <input
      className="input srow__name-input"
      value={draft}
      autoFocus
      maxLength={40}
      aria-label="Speaker name"
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.currentTarget.blur();
        else if (e.key === "Escape") {
          cancelled.current = true;
          e.currentTarget.blur();
        }
      }}
    />
  );
}

/** Range input that follows `value` from the server and commits shortly after dragging pauses. */
/**
 * Range input that commits after a short pause, and right away on release. While it's being
 * dragged (or a commit is in flight) it ignores incoming values, so live updates can't yank the
 * thumb; a failed commit snaps it back to the server's value. A pending change is committed,
 * not dropped, if the slider goes away, and always goes to the target it was made for.
 */
function VolumeSlider({
  value,
  muted,
  disabled,
  label,
  onCommit,
}: {
  value: number;
  muted?: boolean;
  disabled?: boolean;
  label: string;
  onCommit: (v: number) => Promise<boolean> | boolean | void;
}) {
  const [v, setV] = useState(value);
  const holding = useRef(false); // pointer down or commit in flight
  const pending = useRef<{ n: number; commit: typeof onCommit } | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const valueRef = useRef(value);
  valueRef.current = value;

  useEffect(() => {
    if (!holding.current && !pending.current) setV(value);
  }, [value]);

  const dragging = useRef(false);
  const inFlight = useRef(0);
  const seq = useRef(0);
  const flush = () => {
    window.clearTimeout(timer.current);
    const p = pending.current;
    if (!p) {
      holding.current = dragging.current || inFlight.current > 0;
      return;
    }
    pending.current = null;
    holding.current = true;
    inFlight.current++;
    const mine = ++seq.current;
    const done = (ok: unknown) => {
      inFlight.current--;
      holding.current = dragging.current || inFlight.current > 0;
      // only the latest commit decides; an older failure mustn't yank a newer value
      if (mine === seq.current && !holding.current && !pending.current) {
        if (ok === false) setV(valueRef.current);
      }
    };
    void Promise.resolve(p.commit(p.n)).then(done, () => done(false));
  };
  const flushRef = useRef(flush);
  flushRef.current = flush;
  useEffect(() => () => flushRef.current(), []);

  const shown = muted ? 0 : v;
  return (
    <input
      type="range"
      className="range srow__range"
      min={0}
      max={100}
      step={1}
      value={v}
      disabled={disabled}
      aria-label={label}
      style={{ ["--val" as string]: `${shown}%`, opacity: muted ? 0.5 : undefined }}
      onPointerDown={(e) => {
        dragging.current = holding.current = true;
        try {
          e.currentTarget.setPointerCapture(e.pointerId);
        } catch {
          /* not capturable: pointerup/blur still end the drag */
        }
      }}
      onPointerUp={() => {
        dragging.current = false;
        flush();
      }}
      onLostPointerCapture={() => {
        dragging.current = false;
        flush();
      }}
      onPointerCancel={() => {
        dragging.current = false;
        flush();
      }}
      onBlur={flush}
      onChange={(e) => {
        const n = Number(e.target.value);
        setV(n);
        pending.current = { n, commit: onCommit };
        window.clearTimeout(timer.current);
        timer.current = window.setTimeout(flush, 300);
      }}
    />
  );
}
