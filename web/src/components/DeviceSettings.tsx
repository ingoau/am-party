import { useEffect, useRef, useState, type MutableRefObject } from "react";
import { LOCKED_NOTE, type SendspinStatus, type useSendspin } from "../hooks/useSendspin";
import { Lock } from "lucide-react";
import { fmtBitrate, fmtBytes, useSendspinTraffic } from "../hooks/sendspinTraffic";

export type Sendspin = ReturnType<typeof useSendspin>;

/** Same range as useSendspin's setSyncDelay. */
const clampDelay = (ms: number) => Math.max(0, Math.min(5000, Math.round(ms) || 0));

export function syncLabel(status: SendspinStatus): string {
  switch (status.sync) {
    case "off":
      return "Not listening";
    case "connecting":
      return "Connecting audio…";
    case "syncing":
      return "Syncing clock…";
    case "synced":
      return status.syncErrorMs !== null
        ? `In sync · ±${Math.max(1, Math.round(Math.abs(status.syncErrorMs)))} ms`
        : "In sync";
    case "error":
      return status.error ? `Audio error: ${status.error}` : "Audio error";
  }
}

/**
 * Per-device listening settings: volume, device name, speaker delay, sync status and stream stats.
 * The fields start from the player's current values when this mounts. Volume and delay apply live
 * (the delay shortly after typing stops); the name is committed on Enter / blur, or when the owner
 * calls `commitRef.current()` (e.g. as a popover closes), and takes effect the next time Listen is
 * tapped.
 */
export function DeviceSettingsForm({
  sp,
  idPrefix,
  commitRef,
}: {
  sp: Sendspin;
  idPrefix: string;
  commitRef?: MutableRefObject<() => void>;
}) {
  const { deviceName, setDeviceName, renamePending, volume, setVolume, muted, syncDelay, setSyncDelay, status, locked } = sp;
  const [name, setName] = useState(deviceName);
  const [delay, setDelay] = useState(String(syncDelay));
  const traffic = useSendspinTraffic(status.listening);

  const commitName = useRef(() => {});
  useEffect(() => {
    commitName.current = () => {
      if (!locked && name.trim() && name.trim() !== deviceName) setDeviceName(name);
      // A blank (or unchanged) name isn't saved: show the current one again.
      else setName(deviceName);
    };
    if (commitRef) commitRef.current = () => commitName.current();
  });
  // The form can stay mounted (/stream): keep the fields in step with what was actually saved
  // (trimmed name, clamped delay).
  useEffect(() => setName(deviceName), [deviceName]);
  useEffect(() => setDelay(String(syncDelay)), [syncDelay]);
  useEffect(() => {
    if (delay === "" || Number(delay) === syncDelay) return;
    const t = window.setTimeout(() => setSyncDelay(Number(delay)), 400);
    return () => window.clearTimeout(t);
  }, [delay, syncDelay, setSyncDelay]);

  const volLabel = `${idPrefix}-vol-label`;
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        commitName.current();
      }}
    >
      {locked && (
        <p className="field__hint device-locked" role="status">
          <Lock size={14} aria-hidden /> {LOCKED_NOTE}
        </p>
      )}
      <div className="field">
        <span className="field__label" id={volLabel}>
          Volume
          <span className="field__value">{muted ? "Muted" : `${Math.round(volume)}%`}</span>
        </span>
        <input
          type="range"
          min={0}
          max={100}
          step={1}
          value={muted ? 0 : volume}
          onChange={(e) => setVolume(Number(e.target.value))}
          aria-labelledby={volLabel}
          disabled={locked}
          className="range"
          style={{ ["--val" as string]: `${muted ? 0 : volume}%` }}
        />
      </div>
      <label className="field">
        <span className="field__label">Device name</span>
        <input
          className="input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => commitName.current()}
          disabled={locked}
          maxLength={40}
          enterKeyHint="done"
        />
        {renamePending && !locked && <span className="field__hint">The new name shows up next time you tap Listen.</span>}
      </label>
      <label className="field">
        <span className="field__label">Speaker delay (ms)</span>
        <input
          className="input"
          type="number"
          inputMode="numeric"
          min={0}
          max={5000}
          step={10}
          value={delay}
          onChange={(e) => setDelay(e.target.value)}
          onBlur={() => {
            // Apply now and show the stored (clamped) value, e.g. 9000 → 5000 or "" → current.
            if (delay !== "") setSyncDelay(Number(delay));
            setDelay(String(clampDelay(delay === "" ? syncDelay : Number(delay))));
          }}
        />
        <span className="field__hint">Raise if this speaker sounds late compared with the others.</span>
      </label>
      <div className={`sync sync--${status.sync}`}>
        <span className="sync__dot" aria-hidden />
        {/* Only state changes are announced, not every ±ms tick of the sync error. */}
        <span className="sync__label">
          <span role="status">{status.sync === "synced" ? "In sync" : syncLabel(status)}</span>
          {status.sync === "synced" && status.syncErrorMs !== null && ` · ±${Math.max(1, Math.round(Math.abs(status.syncErrorMs)))} ms`}
        </span>
      </div>
      {status.listening && status.codec && (
        <div className="field__hint">
          Stream: {status.codec}
          {status.outputLatencyMs !== null ? ` · output latency ${Math.round(status.outputLatencyMs)} ms` : ""}
        </div>
      )}
      {status.listening && (
        <div className="field__hint" title="Audio stream data received by this device since you tapped Listen">
          Data received: {fmtBytes(traffic.bytes)}
          {traffic.bytesPerSec !== null ? ` · ${fmtBitrate(traffic.bytesPerSec)}` : ""}
        </div>
      )}
    </form>
  );
}
