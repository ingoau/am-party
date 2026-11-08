import { useCallback, useEffect, useRef, useState } from "react";
import type { SendspinPlayer } from "@sendspin/sendspin-js";
import { lazyToast as toast } from "../components/lazyToast";
import { storage } from "../util";
import { installTrafficCounter, resetTraffic } from "./sendspinTraffic";

const K_NAME = "am-party.deviceName";
const K_VOL = "am-party.volume";
const K_MUTED = "am-party.muted";
const K_DELAY = "am-party.syncDelayMs";
const K_QUALITY = "am-party.opusKbps";

/**
 * Opus bitrates (kbps) a device can ask the server for; each must be in OPUS_BITRATES_KBPS
 * (sendspin_out.py). 64 kbps stereo Opus is close to transparent for party music at about two
 * thirds of libopus' automatic ~96 kbps, so it's the default.
 */
export const QUALITY_KBPS = [96, 64, 32] as const;
export type Quality = (typeof QUALITY_KBPS)[number];
export const DEFAULT_QUALITY: Quality = 64;

const readQuality = (): Quality => {
  const v = Number(storage.get(K_QUALITY));
  return (QUALITY_KBPS as readonly number[]).includes(v) ? (v as Quality) : DEFAULT_QUALITY;
};

/**
 * Why this browser can't get the (bitrate-adjustable) Opus stream, or null if it can.
 * Mirrors sendspin-js' getBrowserSupportedCodecs(), which drops Opus on Firefox and wherever
 * WebCodecs is missing (e.g. a page served over plain http://); Safari always gets Opus.
 */
export function opusUnavailableReason(): string | null {
  const ua = navigator.userAgent;
  if (/firefox/i.test(ua)) return "firefox";
  if (/^((?!chrome|android).)*safari/i.test(ua)) return null;
  if (typeof AudioDecoder !== "undefined") return null;
  return window.isSecureContext ? "unsupported" : "insecure";
}

type SendspinModule = typeof import("@sendspin/sendspin-js");
let sendspinMod: SendspinModule | null = null;
let sendspinLoad: Promise<SendspinModule> | null = null;

/**
 * Fetch the sendspin-js chunk (kept out of the TV page's initial bundle). Called when the page is
 * idle and on the first pointer / key interaction, so it's normally loaded before Listen is tapped:
 * the player has to be built and unlocked synchronously inside that tap for iOS / Chrome to let
 * its AudioContext play.
 */
export function preloadSendspin(): Promise<SendspinModule> {
  installTrafficCounter(); // must wrap WebSocket before sendspin-js opens its socket
  sendspinLoad ??= import("@sendspin/sendspin-js").then(
    (m) => (sendspinMod = m),
    (e) => {
      sendspinLoad = null; // allow a retry
      throw e;
    },
  );
  return sendspinLoad;
}

/** If the AudioContext can't be unlocked (no gesture left after a slow chunk load), give up after this. */
const UNLOCK_TIMEOUT_MS = 3000;

export function defaultDeviceName(): string {
  const ua = navigator.userAgent;
  const browser = /Edg\//.test(ua)
    ? "Edge"
    : /Firefox\//.test(ua)
      ? "Firefox"
      : /Chrome\//.test(ua)
        ? "Chrome"
        : /Safari\//.test(ua)
          ? "Safari"
          : "Browser";
  const device = /SMART-TV|SmartTV|Tizen|Web0S|webOS|BRAVIA|AFT|CrKey|GoogleTV|Android TV/i.test(ua)
    ? "TV"
    : /iPhone/.test(ua)
      ? "iPhone"
      : /iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)
        ? "iPad"
        : /Android/.test(ua)
          ? /Mobile/.test(ua)
            ? "Android phone"
            : "Android tablet"
          : /Mac OS X|Macintosh/.test(ua)
            ? "Mac"
            : /Windows/.test(ua)
              ? "Windows PC"
              : /CrOS/.test(ua)
                ? "Chromebook"
                : /Linux/.test(ua)
                  ? "Linux PC"
                  : "device";
  return `${browser} on ${device}`;
}

export type SyncState = "off" | "connecting" | "syncing" | "synced" | "error";

export type SendspinStatus = {
  listening: boolean;
  connected: boolean;
  sync: SyncState;
  syncErrorMs: number | null;
  outputLatencyMs: number | null;
  codec: string | null;
  error: string | null;
};

const OFF: SendspinStatus = { listening: false, connected: false, sync: "off", syncErrorMs: null, outputLatencyMs: null, codec: null, error: null };

const sameStatus = (a: SendspinStatus, b: SendspinStatus) =>
  (Object.keys(a) as (keyof SendspinStatus)[]).every((k) => a[k] === b[k]);

/**
 * Wraps a SendspinPlayer. A fresh player is created per listening session (disconnect closes
 * its AudioContext). Client identity (clientId) is persisted by sendspin-js in localStorage,
 * so this device keeps a stable player id across sessions.
 */
export function useSendspin() {
  const playerRef = useRef<SendspinPlayer | null>(null);
  /** Bumped by every start/stop so async work from an older session can tell it's stale. */
  const sessionRef = useRef(0);
  const [deviceName, setDeviceNameState] = useState(() => storage.get(K_NAME) || defaultDeviceName());
  /** Name the current listening session announced (clientName is fixed per connection). */
  const [activeName, setActiveName] = useState<string | null>(null);
  const [volume, setVolumeState] = useState(() => {
    const v = Number(storage.get(K_VOL));
    return storage.get(K_VOL) !== null && Number.isFinite(v) ? Math.max(0, Math.min(100, v)) : 80;
  });
  const [muted, setMutedState] = useState(() => storage.get(K_MUTED) === "1");
  const [status, setStatus] = useState<SendspinStatus>(OFF);

  const [syncDelay, setSyncDelayState] = useState(() => {
    const v = Number(storage.get(K_DELAY));
    return Number.isFinite(v) ? Math.max(0, Math.min(5000, Math.round(v))) : 0;
  });
  const syncDelayRef = useRef(syncDelay);
  const [quality, setQualityState] = useState<Quality>(readQuality);
  const qualityRef = useRef(quality);
  const volRef = useRef({ volume, muted });
  const nameRef = useRef(deviceName);
  useEffect(() => {
    syncDelayRef.current = syncDelay;
    qualityRef.current = quality;
    volRef.current = { volume, muted };
    nameRef.current = deviceName;
  });

  // Fetch the player code in the background: when idle, and on the first interaction.
  useEffect(() => {
    if (sendspinMod) return;
    const load = () => void preloadSendspin().catch(() => {});
    const ric = window.requestIdleCallback?.(load, { timeout: 4000 });
    const t = ric === undefined ? window.setTimeout(load, 1500) : undefined;
    const opts = { capture: true, once: true, passive: true } as const;
    window.addEventListener("pointerdown", load, opts);
    window.addEventListener("keydown", load, opts);
    return () => {
      if (ric !== undefined) window.cancelIdleCallback?.(ric);
      window.clearTimeout(t);
      window.removeEventListener("pointerdown", load, opts);
      window.removeEventListener("keydown", load, opts);
    };
  }, []);

  const teardown = () => {
    const p = playerRef.current;
    playerRef.current = null;
    sessionRef.current++;
    if (p) {
      try {
        p.disconnect("user_request");
      } catch {
        /* ignore */
      }
    }
  };

  const stop = useCallback(() => {
    teardown();
    setActiveName(null);
    setStatus(OFF);
  }, []);

  /** A failed session: drop the player and show why (status + toast). */
  const fail = useCallback((message: string) => {
    teardown();
    setActiveName(null);
    setStatus({ ...OFF, sync: "error", error: message });
    toast.error(message, { duration: 6000 });
  }, []);

  /**
   * Must be called synchronously from a user gesture (click / keydown): the player's AudioContext
   * is created and resumed inside it.
   */
  const start = useCallback(() => {
    if (playerRef.current) return;
    const session = ++sessionRef.current;
    const name = nameRef.current;
    installTrafficCounter();
    resetTraffic();
    setActiveName(name);
    setStatus({ ...OFF, listening: true, sync: "connecting" });

    const launch = (mod: SendspinModule, inGesture: boolean) => {
      if (sessionRef.current !== session) return;
      let player: SendspinPlayer;
      try {
        player = new mod.SendspinPlayer({
          // ws path /sendspin/<kbps>/sendspin picks the Opus bitrate (FLAC / PCM streams ignore it);
          // sendspin-js appends /sendspin and keeps the path on its own reconnects.
          baseUrl: `${location.origin}/sendspin/${qualityRef.current}`,
          clientName: name,
          productName: "am-party",
          // codecs left at default: the SDK filters out what this browser can't decode
          // (e.g. no opus without WebCodecs / a secure context).
          // Only override when the user set one here; otherwise keep any server-commanded delay the SDK persisted.
          syncDelay: storage.get(K_DELAY) !== null ? syncDelayRef.current : undefined,
          correctionMode: "sync",
          onStateChange: (s) => {
            if (playerRef.current !== player) return;
            setVolumeState((v) => (Math.round(s.volume) !== Math.round(v) ? s.volume : v));
            setMutedState(s.muted);
          },
          reconnect: {
            onReconnecting: () => {
              if (playerRef.current === player) setStatus((st) => (st.connected || st.sync !== "connecting" ? { ...st, connected: false, sync: "connecting" } : st));
            },
          },
        });
      } catch (e) {
        fail((e as Error)?.message || "Audio is not supported in this browser");
        return;
      }
      playerRef.current = player;
      // unlock() creates + resumes the AudioContext synchronously, so it must run in the gesture.
      // Outside one (the chunk was still loading) it may never resolve: time out and ask for a re-tap.
      const unlock = player.unlock();
      const unlocked = inGesture
        ? unlock
        : Promise.race([
            unlock,
            new Promise<never>((_, rej) => window.setTimeout(() => rej(new Error("Tap Listen again to start audio")), UNLOCK_TIMEOUT_MS)),
          ]);
      void (async () => {
        try {
          await unlocked;
          if (playerRef.current !== player) return;
          player.setVolume(volRef.current.volume);
          player.setMuted(volRef.current.muted);
          await player.connect();
          if (playerRef.current !== player) return;
          // Re-apply in case connect reset local state.
          player.setVolume(volRef.current.volume);
          player.setMuted(volRef.current.muted);
        } catch (e) {
          if (playerRef.current !== player) return;
          fail((e as Error)?.message || "Couldn't connect to the audio stream");
        }
      })();
    };

    if (sendspinMod) {
      launch(sendspinMod, true);
    } else {
      // Rare: tapped before the background preload finished.
      preloadSendspin().then(
        (m) => launch(m, false),
        () => sessionRef.current === session && fail("Couldn't load the audio player. Check your connection."),
      );
    }
  }, [fail]);

  // Poll sync status while listening; only re-render when something visible changed.
  useEffect(() => {
    if (!status.listening) return;
    const t = window.setInterval(() => {
      const p = playerRef.current;
      if (!p) return;
      const connected = p.isConnected;
      const ts = p.timeSyncInfo;
      const si = p.syncInfo;
      const fmt = p.currentFormat;
      let sync: SyncState;
      if (!connected) sync = "connecting";
      else if (p.playerState === "error") sync = "error";
      else if (!ts.synced) sync = "syncing";
      else sync = "synced";
      setStatus((st) => {
        const next: SendspinStatus = {
          ...st,
          connected,
          sync,
          syncErrorMs: p.isPlaying && Number.isFinite(si.syncErrorMs) ? Math.round(si.syncErrorMs) : null,
          outputLatencyMs: Number.isFinite(si.outputLatencyMs) ? Math.round(si.outputLatencyMs) : null,
          codec: fmt ? `${fmt.codec}${fmt.sample_rate ? ` · ${Math.round(fmt.sample_rate / 100) / 10} kHz` : ""}` : null,
          error: sync === "error" ? st.error : connected ? null : st.error,
        };
        return sameStatus(st, next) ? st : next;
      });
    }, 1000);
    return () => window.clearInterval(t);
  }, [status.listening]);

  useEffect(() => () => stop(), [stop]);

  const setVolume = useCallback((v: number) => {
    const val = Math.max(0, Math.min(100, Math.round(v)));
    setVolumeState(val);
    storage.set(K_VOL, String(val));
    playerRef.current?.setVolume(val);
    if (val > 0 && volRef.current.muted) {
      setMutedState(false);
      storage.set(K_MUTED, "0");
      playerRef.current?.setMuted(false);
    }
  }, []);

  const setMuted = useCallback((m: boolean) => {
    setMutedState(m);
    storage.set(K_MUTED, m ? "1" : "0");
    playerRef.current?.setMuted(m);
  }, []);

  /**
   * clientName is fixed when the player connects and sendspin-js has no way to change it live.
   * Reconnecting here would happen outside a user gesture (leaving the new AudioContext locked
   * on iOS / Chrome), so the new name is stored and used from the next time Listen is tapped.
   */
  const setDeviceName = useCallback((name: string) => {
    const n = name.trim() || defaultDeviceName();
    storage.set(K_NAME, n);
    nameRef.current = n;
    setDeviceNameState(n);
  }, []);

  /** Extra per-device delay compensation (ms, 0-5000) for external speakers / Bluetooth. */
  const setSyncDelay = useCallback((ms: number) => {
    const v = Math.max(0, Math.min(5000, Math.round(ms) || 0));
    setSyncDelayState(v);
    storage.set(K_DELAY, String(v));
    try {
      playerRef.current?.setSyncDelay(v);
    } catch {
      /* ignore */
    }
  }, []);

  /**
   * Pick the Opus bitrate. The server applies it per connection, so while listening this
   * reconnects right away: call it from a click so the new AudioContext unlocks.
   */
  const setQuality = useCallback(
    (kbps: Quality) => {
      if (kbps === qualityRef.current) return;
      qualityRef.current = kbps;
      setQualityState(kbps);
      storage.set(K_QUALITY, String(kbps));
      if (playerRef.current) {
        stop();
        start();
      }
    },
    [start, stop],
  );

  /** True while listening under an older name (the rename applies on the next Listen). */
  const renamePending = status.listening && activeName !== null && activeName !== deviceName;

  return { status, start, stop, volume, setVolume, muted, setMuted, deviceName, setDeviceName, renamePending, syncDelay, setSyncDelay, quality, setQuality };
}
