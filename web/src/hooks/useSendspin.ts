import { useCallback, useEffect, useRef, useState } from "react";
import { SendspinPlayer } from "@sendspin/sendspin-js";
import { storage } from "../util";

const K_NAME = "encore.deviceName";
const K_VOL = "encore.volume";
const K_MUTED = "encore.muted";
const K_DELAY = "encore.syncDelayMs";

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

/**
 * Wraps a SendspinPlayer. A fresh player is created per listening session (disconnect closes
 * its AudioContext). Client identity (clientId) is persisted by sendspin-js in localStorage,
 * so this device keeps a stable player id across sessions.
 */
export function useSendspin() {
  const playerRef = useRef<SendspinPlayer | null>(null);
  const [deviceName, setDeviceNameState] = useState(() => storage.get(K_NAME) || defaultDeviceName());
  const [volume, setVolumeState] = useState(() => {
    const v = Number(storage.get(K_VOL));
    return storage.get(K_VOL) !== null && Number.isFinite(v) ? Math.max(0, Math.min(100, v)) : 80;
  });
  const [muted, setMutedState] = useState(() => storage.get(K_MUTED) === "1");
  const [status, setStatus] = useState<SendspinStatus>({
    listening: false,
    connected: false,
    sync: "off",
    syncErrorMs: null,
    outputLatencyMs: null,
    codec: null,
    error: null,
  });

  const [syncDelay, setSyncDelayState] = useState(() => {
    const v = Number(storage.get(K_DELAY));
    return Number.isFinite(v) ? Math.max(0, Math.min(5000, Math.round(v))) : 0;
  });
  const syncDelayRef = useRef(syncDelay);
  syncDelayRef.current = syncDelay;

  const volRef = useRef({ volume, muted });
  volRef.current = { volume, muted };

  const stop = useCallback(() => {
    const p = playerRef.current;
    playerRef.current = null;
    if (p) {
      try {
        p.disconnect("user_request");
      } catch {
        /* ignore */
      }
    }
    setStatus({ listening: false, connected: false, sync: "off", syncErrorMs: null, outputLatencyMs: null, codec: null, error: null });
  }, []);

  /** Must be called synchronously from a user gesture (click / keydown). */
  const start = useCallback(
    (nameOverride?: string) => {
      if (playerRef.current) return;
      let player: SendspinPlayer;
      try {
        player = new SendspinPlayer({
          baseUrl: location.origin,
          clientName: nameOverride ?? deviceName,
          productName: "Encore",
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
              if (playerRef.current === player) setStatus((st) => ({ ...st, connected: false, sync: "connecting" }));
            },
          },
        });
      } catch (e) {
        setStatus((st) => ({ ...st, error: (e as Error).message || "Audio is not supported in this browser" }));
        return;
      }
      playerRef.current = player;
      setStatus({ listening: true, connected: false, sync: "connecting", syncErrorMs: null, outputLatencyMs: null, codec: null, error: null });
      // unlock() must be the first awaited call inside the gesture handler.
      const unlock = player.unlock();
      void (async () => {
        try {
          await unlock;
          player.setVolume(volRef.current.volume);
          player.setMuted(volRef.current.muted);
          await player.connect();
          // Re-apply in case connect reset local state.
          player.setVolume(volRef.current.volume);
          player.setMuted(volRef.current.muted);
        } catch (e) {
          if (playerRef.current !== player) return;
          setStatus((st) => ({ ...st, sync: "error", error: (e as Error)?.message || "Couldn't connect to the audio stream" }));
        }
      })();
    },
    [deviceName],
  );

  // Poll sync status while listening.
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
      setStatus((st) => ({
        ...st,
        connected,
        sync,
        syncErrorMs: p.isPlaying && Number.isFinite(si.syncErrorMs) ? si.syncErrorMs : null,
        outputLatencyMs: Number.isFinite(si.outputLatencyMs) ? si.outputLatencyMs : null,
        codec: fmt ? `${fmt.codec}${fmt.sample_rate ? ` · ${Math.round(fmt.sample_rate / 100) / 10} kHz` : ""}` : null,
        error: sync === "error" ? st.error : connected ? null : st.error,
      }));
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

  const setDeviceName = useCallback(
    (name: string) => {
      const n = name.trim() || defaultDeviceName();
      storage.set(K_NAME, n);
      setDeviceNameState(n);
      // clientName is fixed at construction: reconnect with the new name if listening.
      if (playerRef.current) {
        stop();
        start(n);
      }
    },
    [start, stop],
  );

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

  return { status, start, stop, volume, setVolume, muted, setMuted, deviceName, setDeviceName, syncDelay, setSyncDelay };
}
