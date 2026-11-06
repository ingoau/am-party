import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { useLive } from "../hooks/live";
import { useProgress } from "../hooks/useProgress";
import { useAccentVars, useArtworkColor } from "../hooks/useArtworkColor";
import { useIdle } from "../hooks/useIdle";
import { useSendspin, type SendspinStatus } from "../hooks/useSendspin";
import { Artwork } from "../components/Artwork";
import { ProgressBar } from "../components/ProgressBar";
import { QrCode } from "../components/QrCode";
import { Headphones, Settings, SquareLetterE, Volume2, VolumeX, WifiOff } from "lucide-react";
import { artworkAt, cx, storage } from "../util";
import "../styles/tv.css";

/** How long the controls stay up after the last mouse move / touch / key press. */
const IDLE_MS = 3000;
/** Controls are shown for this long on first load so people know they exist. */
const INTRO_MS = 6000;
/** Taps on hidden controls only reveal them; they arm this long after appearing. */
const ARM_MS = 300;

export function TvPage() {
  const { state, clockOffset, status: wsStatus } = useLive();
  const [params] = useSearchParams();
  const np = state?.now_playing ?? null;
  const paused = state?.paused ?? false;
  const position = useProgress(np, clockOffset, paused);
  const accent = useArtworkColor(artworkAt(np?.artwork_url, 100));
  useAccentVars(accent);
  const idle = useIdle(IDLE_MS);
  const sp = useSendspin();
  // Wall displays: ?display=1 suppresses the listen controls (and their shortcuts) entirely.
  const controlsOff = params.get("display") === "1";
  const [settingsOpen, setSettingsOpen] = useState(false);
  const requestUrl = `${state?.public_url || location.origin}/request`;

  // The old persistent "Display only" switch is gone (not listening == display only).
  useEffect(() => {
    storage.set("encore.displayOnly", "");
  }, []);

  const [intro, setIntro] = useState(true);
  useEffect(() => {
    const t = window.setTimeout(() => setIntro(false), INTRO_MS);
    return () => window.clearTimeout(t);
  }, []);
  const controlsShown = !controlsOff && (!idle || intro || settingsOpen);
  const [armed, setArmed] = useState(controlsShown);

  useEffect(() => {
    if (!controlsShown) return setArmed(false);
    const t = window.setTimeout(() => setArmed(true), ARM_MS);
    return () => window.clearTimeout(t);
  }, [controlsShown]);

  const lastPointer = useRef("mouse");

  const toggleListen = () => {
    if (sp.status.listening) sp.stop();
    else sp.start();
  };

  // Remote / keyboard (works while the controls are hidden): Enter/Space toggles listening,
  // M mutes, ↑/↓ volume.
  const kb = useRef({ toggleListen, sp, controlsOff, settingsOpen });
  kb.current = { toggleListen, sp, controlsOff, settingsOpen };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const { toggleListen, sp, controlsOff, settingsOpen } = kb.current;
      if (settingsOpen || controlsOff || e.altKey || e.ctrlKey || e.metaKey) return;
      const t = e.target as HTMLElement | null;
      const tag = t?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t?.isContentEditable) return;
      // Let focused buttons/links handle their own Enter/Space.
      if ((e.key === "Enter" || e.key === " ") && (tag === "BUTTON" || tag === "A")) return;
      if (e.key === "Enter" || e.key === " " || e.key === "MediaPlayPause") {
        e.preventDefault();
        toggleListen();
      } else if (e.key === "m" || e.key === "M") {
        sp.setMuted(!sp.muted);
      } else if (e.key === "ArrowUp" || e.key === "AudioVolumeUp") {
        e.preventDefault();
        sp.setVolume(sp.volume + 5);
      } else if (e.key === "ArrowDown" || e.key === "AudioVolumeDown") {
        e.preventDefault();
        sp.setVolume(sp.volume - 5);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const party = state?.party_name || "Encore";
    document.title = np ? `${np.title} · ${np.artist} — ${party}` : party;
  }, [np?.title, np?.artist, state?.party_name]); // eslint-disable-line react-hooks/exhaustive-deps

  const upcoming = state?.upcoming ?? [];
  const shown = upcoming.slice(0, 4);
  const more = upcoming.length - shown.length;
  const bg = artworkAt(np?.artwork_url, 300);

  return (
    <div className={cx("tv", idle && "tv--idle", accent && "tv--accent")}>
      <div className="tv__backdrop" aria-hidden>
        {bg && <img key={bg} src={bg} alt="" className="tv__backdrop-img" />}
        <div className="tv__backdrop-shade" />
      </div>

      <header className="tv__header">
        <div className="brand">
          <span className="brand__mark" aria-hidden>
            <span />
            <span />
            <span />
          </span>
          <span className="brand__party">{state?.party_name || "Encore"}</span>
        </div>
        <div className="tv__header-right">
          {wsStatus !== "live" && (
            <span className={cx("chip", "chip--warn")} title="Live updates">
              <WifiOff size={18} />
              {wsStatus === "connecting" ? "Connecting…" : wsStatus === "polling" ? "Reconnecting…" : "Offline"}
            </span>
          )}
        </div>
      </header>

      <main className="tv__main">
        <div className="tv__art-wrap">
          <Artwork key={np?.artwork_url ?? "none"} url={np?.artwork_url} size={1000} alt={np ? `${np.album ?? np.title} artwork` : ""} className="tv__art" />
          {paused && np && <div className="tv__paused">Paused</div>}
        </div>

        <section className="tv__info" aria-live="polite">
          {np ? (
            <>
              <h1 className="tv__title">
                {np.title}
                {np.explicit && <SquareLetterE size={28} className="explicit" role="img" aria-label="Explicit" />}
              </h1>
              <div className="tv__artist">{np.artist}</div>
              <ProgressBar position={position} duration={np.duration} />
            </>
          ) : (
            <>
              <h1 className="tv__title">{state ? "Waiting for the first track" : "Tuning in…"}</h1>
              {state && <div className="tv__artist">Scan the code to request a song.</div>}
            </>
          )}

          <div className="upnext">
            <div className="upnext__heading">Up next</div>
            {shown.length > 0 ? (
              <ol className="upnext__list">
                {shown.map((q) => (
                  <li key={q.id} className="upnext__item">
                    <Artwork url={q.track.artwork_url} size={120} className="upnext__art" />
                    <div className="upnext__text">
                      <div className="upnext__title truncate">{q.track.title}</div>
                      <div className="upnext__artist truncate">{q.track.artist}</div>
                    </div>
                  </li>
                ))}
                {more > 0 && <li className="upnext__more">+{more} more</li>}
              </ol>
            ) : state?.up_next_fallback ? (
              <ol className="upnext__list upnext__list--fallback">
                <li className="upnext__item">
                  <span className="upnext__then">then</span>
                  <Artwork url={state.up_next_fallback.artwork_url} size={120} className="upnext__art" />
                  <div className="upnext__text">
                    <div className="upnext__title truncate">{state.up_next_fallback.title}</div>
                    <div className="upnext__artist truncate">{state.up_next_fallback.artist}</div>
                  </div>
                </li>
              </ol>
            ) : (
              <div className="upnext__empty">Nothing queued yet.</div>
            )}
          </div>
        </section>
      </main>

      <footer className="tv__footer">
        {!controlsOff ? (
          <div
            className={cx("tv-controls", controlsShown && "is-shown", armed && "is-armed")}
            onPointerDownCapture={(e) => {
              lastPointer.current = e.pointerType;
            }}
            onClickCapture={(e) => {
              // A touch on hidden controls only reveals them (useIdle handles that), so a tap
              // on an invisible button doesn't toggle anything. Mouse and keyboard go through.
              if (!armed && e.detail !== 0 && lastPointer.current !== "mouse") {
                e.preventDefault();
                e.stopPropagation();
              }
            }}
          >
            <button
              type="button"
              className={cx("tv-btn", "tv-btn--listen", sp.status.listening && "is-on")}
              onClick={toggleListen}
              aria-pressed={sp.status.listening}
              aria-label={sp.status.listening ? "Stop listening on this device" : "Listen on this device"}
              title={sp.status.listening ? "Listening — click to stop (Enter)" : "Listen on this device (Enter)"}
            >
              {sp.status.listening ? (
                <span className="eq" aria-hidden>
                  <span />
                  <span />
                  <span />
                  <span />
                </span>
              ) : (
                <Headphones size={24} />
              )}
            </button>
            {sp.status.listening && (
              <button
                type="button"
                className={cx("tv-btn", sp.muted && "is-muted")}
                onClick={() => sp.setMuted(!sp.muted)}
                aria-label={sp.muted ? "Unmute this device" : "Mute this device"}
                aria-pressed={sp.muted}
                title={sp.muted ? "Unmute (M)" : "Mute (M)"}
              >
                {sp.muted || sp.volume === 0 ? <VolumeX size={22} /> : <Volume2 size={22} />}
              </button>
            )}
            <SettingsPopover
              open={settingsOpen}
              setOpen={setSettingsOpen}
              deviceName={sp.deviceName}
              setDeviceName={sp.setDeviceName}
              volume={sp.volume}
              setVolume={sp.setVolume}
              muted={sp.muted}
              syncDelay={sp.syncDelay}
              setSyncDelay={sp.setSyncDelay}
              status={sp.status}
            />
            {!sp.status.listening && <span className="tv-controls__hint">Listen on this device</span>}
            <AudioAlert status={sp.status} />
          </div>
        ) : (
          <span />
        )}

        <Link to="/request" className="qr-card" aria-label="Request a song">
          <QrCode text={requestUrl} className="qr-card__code" label="QR code: request a song" />
          <div className="qr-card__text">
            <strong>{state && !state.requests_open ? "Requests are paused" : "Scan to request a song"}</strong>
            <span>{requestUrl.replace(/^https?:\/\//, "")}</span>
          </div>
        </Link>
        {/* Phones: the viewer is already on a phone, so a plain link replaces the QR code. */}
        {state?.requests_open !== false && (
          <Link to="/request" className="tv__request-link">
            Request a song
          </Link>
        )}
      </footer>
    </div>
  );
}

function syncLabel(status: SendspinStatus): string {
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

/** Tiny always-visible dot when this device is listening but not (yet) in sync. */
function AudioAlert({ status }: { status: SendspinStatus }) {
  const bad = status.sync === "error" || (status.listening && (status.sync === "connecting" || status.sync === "syncing"));
  if (!bad) return null;
  return (
    <span className={`tv-alert sync--${status.sync}`} role="status" title={syncLabel(status)}>
      <span className="sync__dot" aria-hidden />
      <span className="tv-alert__label">{status.sync === "error" ? "Audio error" : "Syncing…"}</span>
    </span>
  );
}

function SettingsPopover({
  open,
  setOpen,
  deviceName,
  setDeviceName,
  volume,
  setVolume,
  muted,
  syncDelay,
  setSyncDelay,
  status,
}: {
  open: boolean;
  setOpen: (v: boolean) => void;
  deviceName: string;
  setDeviceName: (v: string) => void;
  volume: number;
  setVolume: (v: number) => void;
  muted: boolean;
  syncDelay: number;
  setSyncDelay: (v: number) => void;
  status: SendspinStatus;
}) {
  const [name, setName] = useState(deviceName);
  const [delay, setDelay] = useState(String(syncDelay));
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (open) {
      setName(deviceName);
      setDelay(String(syncDelay));
    }
    // Only resync the fields when the popover opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Everything applies live. Renaming reconnects the player, so it's committed on blur / Enter /
  // close or after a longer pause in typing; the delay applies shortly after typing stops.
  const commitName = useRef(() => {});
  commitName.current = () => {
    if (name.trim() && name.trim() !== deviceName) setDeviceName(name);
  };
  useEffect(() => {
    if (!open) return;
    const t = window.setTimeout(() => commitName.current(), 1200);
    return () => window.clearTimeout(t);
  }, [name, open]);
  useEffect(() => {
    if (!open || delay === "" || Number(delay) === syncDelay) return;
    const t = window.setTimeout(() => setSyncDelay(Number(delay)), 400);
    return () => window.clearTimeout(t);
  }, [delay, open, syncDelay, setSyncDelay]);

  const close = useRef(setOpen);
  close.current = (v: boolean) => {
    if (!v) commitName.current();
    setOpen(v);
  };
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent | TouchEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close.current(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close.current(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("touchstart", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("touchstart", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="popover-anchor" ref={ref}>
      <button
        type="button"
        className={cx("tv-btn", open && "is-on")}
        aria-label="Device settings"
        title="Device settings"
        aria-expanded={open}
        onClick={() => close.current(!open)}
      >
        <Settings size={22} />
      </button>
      {open && (
        <div className="popover" role="dialog" aria-label="Device settings">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              commitName.current();
            }}
          >
            <div className="field">
              <span className="field__label" id="tv-vol-label">
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
                aria-labelledby="tv-vol-label"
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
            {status.listening && status.codec && <div className="field__hint">Stream: {status.codec}{status.outputLatencyMs !== null ? ` · output latency ${Math.round(status.outputLatencyMs)} ms` : ""}</div>}
            <div className="popover__actions">
              <a className="btn btn--ghost btn--sm" href="/admin">Host panel</a>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
