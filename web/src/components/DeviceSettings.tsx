import { useEffect, useRef, useState, type MutableRefObject } from "react";
import type { SendspinStatus, useSendspin } from "../hooks/useSendspin";
import { fmtBitrate, fmtBytes, useSendspinTraffic } from "../hooks/sendspinTraffic";

export type Sendspin = ReturnType<typeof useSendspin>;

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
  const { deviceName, setDeviceName, renamePending, volume, setVolume, muted, syncDelay, setSyncDelay, status } = sp;
  const [name, setName] = useState(deviceName);
  const [delay, setDelay] = useState(String(syncDelay));
  const traffic = useSendspinTraffic(status.listening);

  const commitName = useRef(() => {});
  useEffect(() => {
    commitName.current = () => {
      if (name.trim() && name.trim() !== deviceName) setDeviceName(name);
    };
    if (commitRef) commitRef.current = () => commitName.current();
  });
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
          maxLength={60}
          enterKeyHint="done"
        />
        {renamePending && <span className="field__hint">The new name shows up next time you tap Listen.</span>}
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
        />
        <span className="field__hint">Raise if this speaker sounds late compared with the others.</span>
      </label>
      <div className={`sync sync--${status.sync}`} role="status">
        <span className="sync__dot" aria-hidden />
        <span className="sync__label">{syncLabel(status)}</span>
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
