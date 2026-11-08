/**
 * Counts the bytes the Sendspin audio WebSocket receives, so the TV page can show how much data
 * listening uses. sendspin-js opens (and on reconnect, reopens) its own socket and has no traffic
 * stats or hook for it, so the global WebSocket is wrapped once with a subclass that counts
 * incoming messages on `/sendspin` sockets. Other sockets pass through untouched.
 *
 * Counted: WebSocket message payloads (audio frames, artwork, control/time-sync JSON). Not counted:
 * WebSocket framing, TLS and HTTP overhead, so the real network figure is slightly higher.
 */

import { useEffect, useState } from "react";

let received = 0;
let installed = false;

const isSendspinUrl = (url: string | URL) => {
  try {
    return new URL(url, location.href).pathname.endsWith("/sendspin");
  } catch {
    return false;
  }
};

const payloadBytes = (data: unknown): number => {
  if (data instanceof ArrayBuffer) return data.byteLength;
  if (ArrayBuffer.isView(data)) return data.byteLength;
  if (typeof data === "string") return data.length; // JSON control messages: ASCII, so chars ≈ bytes
  if (data instanceof Blob) return data.size;
  return 0;
};

/** Install the counting WebSocket wrapper (idempotent). Must run before sendspin-js connects. */
export function installTrafficCounter(): void {
  if (installed || typeof window === "undefined" || typeof window.WebSocket !== "function") return;
  installed = true;
  const Native = window.WebSocket;
  class CountingWebSocket extends Native {
    constructor(url: string | URL, protocols?: string | string[]) {
      super(url, protocols);
      if (isSendspinUrl(url)) {
        this.addEventListener("message", (e: MessageEvent) => {
          received += payloadBytes(e.data);
        });
      }
    }
  }
  window.WebSocket = CountingWebSocket;
}

/** Bytes received on Sendspin sockets since the last reset. */
export const trafficReceived = () => received;

/** Start counting from zero (a new listening session). */
export const resetTraffic = () => {
  received = 0;
};

/** "1.2 MB", "340 KB" (decimal units, like network meters). */
export function fmtBytes(n: number): string {
  if (n < 1000) return `${Math.round(n)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1000;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i++;
  }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/** Bits per second as "320 kb/s" / "1.4 Mb/s". */
export function fmtBitrate(bytesPerSec: number): string {
  const kbps = (bytesPerSec * 8) / 1000;
  if (kbps < 1000) return `${Math.round(kbps)} kb/s`;
  return `${(kbps / 1000).toFixed(1)} Mb/s`;
}

/** Window for the "current rate" figure. Audio arrives in bursts (the server streams ahead), so smooth it. */
const RATE_WINDOW_MS = 5000;

export type Traffic = { bytes: number; bytesPerSec: number | null };

/** Poll the counter once a second while `active` (e.g. the settings popover is open while listening). */
export function useSendspinTraffic(active: boolean): Traffic {
  const [traffic, setTraffic] = useState<Traffic>(() => ({ bytes: received, bytesPerSec: null }));
  useEffect(() => {
    if (!active) return;
    const samples: { t: number; bytes: number }[] = [];
    const tick = () => {
      const now = performance.now();
      const bytes = received;
      // A reset (new session) makes the counter go backwards: start the window again.
      if (samples.length && bytes < samples[samples.length - 1].bytes) samples.length = 0;
      samples.push({ t: now, bytes });
      while (samples.length > 2 && now - samples[1].t >= RATE_WINDOW_MS) samples.shift();
      const first = samples[0];
      const dt = (now - first.t) / 1000;
      setTraffic({ bytes, bytesPerSec: dt >= 0.5 ? (bytes - first.bytes) / dt : null });
    };
    tick();
    const t = window.setInterval(tick, 1000);
    return () => window.clearInterval(t);
  }, [active]);
  return traffic;
}
