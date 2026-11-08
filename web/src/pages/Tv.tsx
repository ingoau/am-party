import { Suspense, memo, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";
import { useLive } from "../hooks/live";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import { useAccentVars, useArtworkColor } from "../hooks/useArtworkColor";
import { useIdle } from "../hooks/useIdle";
import { useSendspin, type SendspinStatus } from "../hooks/useSendspin";
import { LiveProgress } from "../components/ProgressBar";
import { LazyToaster } from "../components/lazyToast";
import { QrCode } from "../components/QrCode";
import { Headphones, Music, Pause, Settings, Volume2, VolumeX, WifiOff } from "lucide-react";
import type { QueueItem, Track } from "../api/types";
import { TV_TEXT_DEFAULTS, artworkAt, cx } from "../util";
import "../styles/tv.css";

/** How long the controls stay up after the last mouse move / touch / key press. */
const IDLE_MS = 3000;
/** Controls are shown for this long on first load so people know they exist. */
const INTRO_MS = 6000;
/** Taps on hidden controls only reveal them; they arm this long after appearing. */
const ARM_MS = 300;
/** Crossfade length between tracks (artwork + backdrop). Keep in sync with tv.css. */
const FADE_MS = 600;
/** Phone / portrait: the stacked layout (keep in sync with the media query in tv.css). */
const STACKED_MQ = "(max-width: 699px), (orientation: portrait)";
/** Most rows the "Coming up" list ever shows (TV); phones show at most this many too. */
const MAX_ROWS = 8;
const MAX_ROWS_STACKED = 6;

export function TvPage() {
  const { state, clockOffset, status: wsStatus } = useLive();
  const [params] = useSearchParams();
  const np = state?.now_playing ?? null;
  const paused = state?.paused ?? false;
  const bg = artworkAt(np?.artwork_url, 300);
  // Sample the accent from the backdrop image the page loads anyway (no extra artwork fetch).
  const accent = useArtworkColor(bg);
  useAccentVars(accent);
  const idle = useIdle(IDLE_MS);
  const sp = useSendspin();
  // Wall displays: ?display=1 suppresses the listen controls (and their shortcuts) entirely.
  const controlsOff = params.get("display") === "1";
  const [settingsOpen, setSettingsOpen] = useState(false);
  const base = (state?.public_url || location.origin).replace(/\/+$/, "");
  const host = hostOf(base);
  const requestsOpen = state?.requests_open !== false;

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

  // The party name lives in the browser title only (the screen itself stays uncluttered).
  const party = state?.party_name || "am-party";
  useDocumentTitle(np ? `${np.title} · ${np.artist} · ${party}` : party);

  const ctaText = state?.tv_cta_text || TV_TEXT_DEFAULTS.cta;
  const pausedText = state?.tv_paused_text || TV_TEXT_DEFAULTS.paused;
  const emptyText = state?.tv_empty_text || TV_TEXT_DEFAULTS.empty;

  const stacked = useMediaQuery(STACKED_MQ);
  const longTitle = !!np && np.title.length > 18;

  return (
    <div className={cx("tv", idle && "tv--idle", paused && np && "tv--paused", !np && "tv--empty")}>
      <div className="tv__backdrop" aria-hidden>
        <Crossfade
          url={bg}
          render={(src, onReady) =>
            src ? <img src={src} alt="" className="tv__backdrop-img" onLoad={onReady} onError={onReady} /> : <ReadyNow onReady={onReady} />
          }
        />
        <div className="tv__backdrop-shade" />
      </div>

      {wsStatus !== "live" && (
        <span className="chip chip--warn tv__conn" role="status" title="Live updates">
          <WifiOff aria-hidden />
          {wsStatus === "connecting" ? "Connecting…" : wsStatus === "polling" ? "Reconnecting…" : "Offline"}
        </span>
      )}

      {np ? (
        <main className="tv__stage">
          {/* Announce track changes only, not the ticking progress. */}
          <p className="sr-only" aria-live="polite" aria-atomic="true">
            {`${np.title} by ${np.artist}`}
          </p>
          <section className="tv__now" aria-label="Now playing">
            <div className="tv__art-wrap">
              <Crossfade
                url={artworkAt(np.artwork_url, 1000)}
                render={(src, onReady) => <ArtImg src={src} alt={`${np.album ?? np.title} artwork`} onReady={onReady} />}
              />
              {paused && (
                <div className="tv__paused" role="status">
                  <span className="tv__paused-icon" aria-hidden>
                    <Pause />
                  </span>
                  Paused
                </div>
              )}
            </div>
            <h1 className={cx("tv__title", longTitle && "tv__title--long")}>
              {np.title}
            </h1>
            <div className="tv__artist">{np.artist}</div>
            <LiveProgress np={np} clockOffset={clockOffset} paused={paused} />
          </section>

          <aside className="tv__panel" aria-label="Coming up">
            <RequestCta url={base} host={host} open={requestsOpen} text={ctaText} pausedText={pausedText} />
            <div className="tv__divider" aria-hidden />
            <ComingUp upcoming={state!.upcoming} fallback={state!.up_next_fallback} stacked={stacked} emptyText={emptyText} />
          </aside>
        </main>
      ) : (
        <main className="tv__idle-state">
          <div className="tv__idle-card">
            <h1 className="tv__idle-title">{state ? "Nothing's playing yet" : "Tuning in…"}</h1>
            {state && (
              <>
                <p className="tv__idle-sub">
                  {requestsOpen ? "Scan the code to pick the first song." : pausedText}
                </p>
                {requestsOpen && (
                  <Link to="/" className="tv__idle-qr" aria-label="Request a song">
                    <QrCode text={base} className="tv__qr-code" label="QR code: request a song" />
                    <span className="tv__host">{host}</span>
                  </Link>
                )}
                {requestsOpen && (
                  <Link to="/" className="tv__request-btn">
                    Request a song
                  </Link>
                )}
              </>
            )}
          </div>
        </main>
      )}

      <footer className="tv__dock">
        {!controlsOff && (
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
              title={sp.status.listening ? "Stop listening (Enter)" : "Listen on this device (Enter)"}
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
              renamePending={sp.renamePending}
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
        )}
      </footer>
      <Suspense fallback={null}>
        <LazyToaster />
      </Suspense>
    </div>
  );
}

/** QR + "Pick the next song" (customisable) on TVs; a plain button on phones (the viewer is already on one). */
function RequestCta({
  url,
  host,
  open,
  text,
  pausedText,
}: {
  url: string;
  host: string;
  open: boolean;
  text: string;
  pausedText: string;
}) {
  if (!open) {
    return (
      <div className="tv__cta tv__cta--closed">
        <div className="tv__cta-text">
          <strong>{pausedText}</strong>
        </div>
      </div>
    );
  }
  return (
    <>
      <Link to="/" className="tv__cta" aria-label="Request a song">
        <QrCode text={url} className="tv__qr-code" label="QR code: request a song" />
        <div className="tv__cta-text">
          <strong>{text}</strong>
          <span className="tv__host">{host}</span>
        </div>
      </Link>
      <Link to="/" className="tv__request-btn">
        Request a song
      </Link>
    </>
  );
}

/** "Coming up": as many whole rows as fit (no half rows), then "+N more". */
const ComingUp = memo(function ComingUp({
  upcoming,
  fallback,
  stacked,
  emptyText,
}: {
  upcoming: QueueItem[];
  fallback: Track | null;
  stacked: boolean;
  emptyText: string;
}) {
  const listRef = useRef<HTMLOListElement>(null);
  const probeRef = useRef<HTMLLIElement>(null);
  const moreRef = useRef<HTMLLIElement>(null);
  const n = Math.min(upcoming.length, stacked ? MAX_ROWS_STACKED : MAX_ROWS);
  const [fit, setFit] = useState(n);

  useLayoutEffect(() => {
    const list = listRef.current;
    const probe = probeRef.current;
    const more = moreRef.current;
    if (!list || !probe || !more) return;
    const measure = () => {
      if (stacked) return setFit(n);
      const avail = list.clientHeight;
      const rowH = probe.offsetHeight;
      const gap = parseFloat(getComputedStyle(list).rowGap) || 0;
      if (!rowH) return setFit(n);
      // Everything fits (the "+N more" line is only needed when truncating).
      if (n * rowH + Math.max(0, n - 1) * gap <= avail + 0.5 && n === upcoming.length) return setFit(n);
      setFit(Math.max(0, Math.min(n, Math.floor((avail - more.offsetHeight + 0.5) / (rowH + gap)))));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(list);
    ro.observe(probe);
    return () => ro.disconnect();
  }, [n, stacked, upcoming.length]);

  const shown = upcoming.slice(0, Math.min(fit, n));
  const more = upcoming.length - shown.length;

  return (
    <div className="cu">
      <h2 className="cu__heading">Coming up</h2>
      <ol className={cx("cu__list", upcoming.length === 0 && "cu__list--short")} ref={listRef}>
        {/* Invisible row used to measure how many rows fit. */}
        <li className="cu__row cu__probe" ref={probeRef} aria-hidden>
          <span className="cu__thumb" />
          <span className="cu__text">
            <span className="cu__title">Probe</span>
            <span className="cu__artist">Probe</span>
          </span>
        </li>
        {shown.map((q) => (
          <Row key={q.id} track={q.track} />
        ))}
        {upcoming.length === 0 &&
          (fallback ? (
            <Row track={fallback} dim />
          ) : (
            <li className="cu__empty">{emptyText}</li>
          ))}
        <li className={cx("cu__more", more <= 0 && "is-hidden")} ref={moreRef} aria-hidden={more <= 0}>
          +{Math.max(1, more)} more
        </li>
      </ol>
      {upcoming.length === 0 && fallback && <p className="cu__hint">{emptyText}</p>}
    </div>
  );
});

const Row = memo(function Row({ track, dim }: { track: Track; dim?: boolean }) {
  const [failed, setFailed] = useState(false);
  const src = artworkAt(track.artwork_url, 160);
  return (
    <li className={cx("cu__row", dim && "cu__row--dim")}>
      {src && !failed ? (
        <img className="cu__thumb" src={src} alt="" loading="lazy" decoding="async" draggable={false} onError={() => setFailed(true)} />
      ) : (
        <span className="cu__thumb cu__thumb--empty" aria-hidden>
          <Music />
        </span>
      )}
      <span className="cu__text">
        <span className="cu__title">{track.title}</span>
        <span className="cu__artist">{track.artist}</span>
      </span>
    </li>
  );
},
// Every state push brings new track objects; only re-render when what's shown changed.
(a, b) =>
  a.dim === b.dim &&
  a.track.id === b.track.id &&
  a.track.title === b.track.title &&
  a.track.artist === b.track.artist &&
  a.track.artwork_url === b.track.artwork_url);

/**
 * Stacks one layer per distinct `url`; a new layer fades in over the old one once it has loaded
 * (onReady), then the older layers are dropped.
 */
function Crossfade({ url, render }: { url: string | null; render: (src: string | null, onReady: () => void) => ReactNode }) {
  const [layers, setLayers] = useState<{ id: number; url: string | null; ready: boolean }[]>(() => [{ id: 0, url, ready: false }]);
  useEffect(() => {
    setLayers((ls) => (ls[ls.length - 1].url === url ? ls : [...ls.slice(-2), { id: ls[ls.length - 1].id + 1, url, ready: false }]));
  }, [url]);
  const top = layers[layers.length - 1];
  const prune = (id: number) => setLayers((ls) => (ls.length > 1 ? ls.filter((l) => l.id >= id) : ls));
  // Older layers go once the new one has finished fading in (animationend); the timer is a
  // fallback for when no transition runs (e.g. a background tab).
  useEffect(() => {
    if (!top.ready || layers.length < 2) return;
    const t = window.setTimeout(() => prune(top.id), FADE_MS * 3);
    return () => window.clearTimeout(t);
  }, [top.id, top.ready, layers.length]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <>
      {layers.map((l) => (
        <div
          key={l.id}
          className={cx("fade-layer", l.ready && "is-ready")}
          onAnimationEnd={(e) => {
            if (e.target === e.currentTarget && l.ready) prune(l.id);
          }}
        >
          {render(l.url, () => setLayers((ls) => ls.map((x) => (x.id === l.id && !x.ready ? { ...x, ready: true } : x))))}
        </div>
      ))}
    </>
  );
}

function ReadyNow({ onReady }: { onReady: () => void }) {
  useEffect(onReady, []); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

function ArtImg({ src, alt, onReady }: { src: string | null; alt: string; onReady: () => void }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) {
    return (
      <div className="tv__art tv__art--empty" role="img" aria-label={alt || "No artwork"}>
        <Music aria-hidden />
        <ReadyNow onReady={onReady} />
      </div>
    );
  }
  return (
    <img
      className="tv__art"
      src={src}
      alt={alt}
      decoding="async"
      draggable={false}
      onLoad={onReady}
      onError={() => {
        setFailed(true);
      }}
    />
  );
}

function useMediaQuery(q: string): boolean {
  const [m, setM] = useState(() => window.matchMedia(q).matches);
  useEffect(() => {
    const mq = window.matchMedia(q);
    const on = () => setM(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [q]);
  return m;
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url.replace(/^https?:\/\//, "");
  }
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
  renamePending,
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
  renamePending: boolean;
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
  const gearRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (open) {
      setName(deviceName);
      setDelay(String(syncDelay));
    }
    // Only resync the fields when the popover opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Volume and delay apply live (the delay shortly after typing stops). The name is committed on
  // Enter / blur / close only, and takes effect the next time Listen is tapped.
  const commitName = useRef(() => {});
  useEffect(() => {
    commitName.current = () => {
      if (name.trim() && name.trim() !== deviceName) setDeviceName(name);
    };
  });
  useEffect(() => {
    if (!open || delay === "" || Number(delay) === syncDelay) return;
    const t = window.setTimeout(() => setSyncDelay(Number(delay)), 400);
    return () => window.clearTimeout(t);
  }, [delay, open, syncDelay, setSyncDelay]);

  const close = useRef(setOpen);
  useEffect(() => {
    close.current = (v: boolean) => {
      if (!v) commitName.current();
      setOpen(v);
    };
  });

  // Dialog focus: move in on open; on close, return to the gear unless the user clicked elsewhere.
  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    dialog?.querySelector<HTMLElement>("input, button, a[href]")?.focus();
    return () => {
      const a = document.activeElement;
      if (!a || a === document.body || dialog?.contains(a) || !a.isConnected) gearRef.current?.focus();
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent | TouchEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close.current(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      close.current(false);
    };
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
        ref={gearRef}
        type="button"
        className={cx("tv-btn", open && "is-on")}
        aria-label="Device settings"
        aria-haspopup="dialog"
        title="Device settings"
        aria-expanded={open}
        onClick={() => close.current(!open)}
      >
        <Settings size={22} />
      </button>
      {open && (
        <div className="popover" role="dialog" aria-label="Device settings" ref={dialogRef}>
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
            {status.listening && status.codec && <div className="field__hint">Stream: {status.codec}{status.outputLatencyMs !== null ? ` · output latency ${Math.round(status.outputLatencyMs)} ms` : ""}</div>}
          </form>
        </div>
      )}
    </div>
  );
}
