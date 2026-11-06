import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { State, WsMessage } from "../api/types";

export type ConnStatus = "connecting" | "live" | "polling" | "offline";

type LiveEvent = "requests_changed" | "users_changed";
type Listener = () => void;

type LiveCtx = {
  state: State | null;
  /** serverTime - localTime, seconds */
  clockOffset: number;
  status: ConnStatus;
  /** Local timestamp (ms) at which `state` was received. */
  receivedAt: number;
  subscribe: (ev: LiveEvent, fn: Listener) => () => void;
  refresh: () => void;
};

const Ctx = createContext<LiveCtx | null>(null);

const POLL_MS = 5000;
const PING_MS = 25_000;
/** A clock-offset sample older than this is replaced by the next one, whatever its round trip. */
const OFFSET_MAX_AGE_MS = 5 * 60_000;

/**
 * Single shared connection to /api/ws for the whole app.
 * Reconnects with exponential backoff; while the socket is down it polls /api/state.
 */
export function LiveProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State | null>(null);
  const [clockOffset, setClockOffset] = useState(0);
  const [receivedAt, setReceivedAt] = useState(0);
  const [status, setStatus] = useState<ConnStatus>("connecting");
  const listeners = useRef(new Map<LiveEvent, Set<Listener>>());
  const refreshRef = useRef<() => void>(() => {});

  useEffect(() => {
    let ws: WebSocket | null = null;
    let disposed = false;
    let attempt = 0;
    let reconnectTimer: number | undefined;
    let pollTimer: number | undefined;
    let pingTimer: number | undefined;
    // Best clock offset sample: the one with the lowest round trip (least uncertainty). It is
    // re-estimated once it gets old, so slow drift between the clocks is still followed.
    let best: { offset: number; rtt: number; at: number } | null = null;
    // server_time of the last state applied; anything not newer is a stale response.
    let lastServerTime = -Infinity;
    let pingSentAt: number | null = null;
    let pingSeqTimers: number[] = [];

    /** One round-trip sample: request sent at tSend (ms), answer stamped serverTime (s), received after rtt ms. */
    const sampleClock = (serverTime: number, tSend: number, rtt: number) => {
      const offset = serverTime - (tSend + rtt / 2) / 1000;
      const now = Date.now();
      if (!best || rtt <= best.rtt || now - best.at > OFFSET_MAX_AGE_MS) {
        best = { offset, rtt, at: now };
        setClockOffset(offset);
      }
    };

    const applyState = (s: State) => {
      // Ignore out-of-order responses (a slow poll landing after a newer socket push). A big
      // backwards jump means the server restarted / its clock was reset, so start over.
      if (s.server_time <= lastServerTime && lastServerTime - s.server_time < 30) return;
      lastServerTime = s.server_time;
      // Pushes carry no round-trip info: use one only until a real sample exists.
      if (!best) setClockOffset(s.server_time - Date.now() / 1000);
      setState(s);
      setReceivedAt(Date.now());
    };

    const sendPing = () => {
      if (ws?.readyState !== WebSocket.OPEN) return;
      if (pingSentAt !== null && Date.now() - pingSentAt < 10_000) return; // one ping in flight at a time
      pingSentAt = Date.now();
      ws.send(JSON.stringify({ type: "ping" }));
    };

    const emit = (ev: LiveEvent) => listeners.current.get(ev)?.forEach((fn) => fn());

    const poll = async () => {
      const t0 = Date.now();
      try {
        const s = await api.state();
        if (!disposed) {
          sampleClock(s.server_time, t0, Date.now() - t0);
          applyState(s);
          setStatus((st) => (st === "live" ? st : "polling"));
        }
      } catch {
        if (!disposed) setStatus((st) => (st === "live" ? st : "offline"));
      }
    };

    const startPolling = () => {
      if (pollTimer !== undefined) return;
      void poll();
      pollTimer = window.setInterval(poll, POLL_MS);
    };
    const stopPolling = () => {
      if (pollTimer !== undefined) window.clearInterval(pollTimer);
      pollTimer = undefined;
    };

    const connect = () => {
      if (disposed) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      let opened = false;
      try {
        ws = new WebSocket(`${proto}//${location.host}/api/ws`);
      } catch {
        scheduleReconnect();
        return;
      }
      ws.onopen = () => {
        opened = true;
        attempt = 0;
        setStatus("live");
        stopPolling();
        pingSentAt = null;
        // A few quick pings to get a good clock offset early, then one per interval.
        sendPing();
        pingSeqTimers = [1000, 2500, 5000].map((d) => window.setTimeout(sendPing, d));
        pingTimer = window.setInterval(sendPing, PING_MS);
      };
      ws.onmessage = (ev) => {
        const t = Date.now();
        let msg: WsMessage;
        try {
          msg = JSON.parse(ev.data as string) as WsMessage;
        } catch {
          return;
        }
        if (msg.type === "state") applyState(msg.data);
        else if (msg.type === "pong") {
          if (pingSentAt !== null && typeof msg.server_time === "number") sampleClock(msg.server_time, pingSentAt, t - pingSentAt);
          pingSentAt = null;
        }
        else if (msg.type === "requests_changed" || msg.type === "users_changed") emit(msg.type);
      };
      ws.onclose = () => {
        if (pingTimer !== undefined) window.clearInterval(pingTimer);
        pingSeqTimers.forEach((x) => window.clearTimeout(x));
        pingSentAt = null;
        ws = null;
        if (disposed) return;
        setStatus(opened ? "polling" : (s) => (s === "offline" ? s : "polling"));
        startPolling();
        scheduleReconnect();
      };
      ws.onerror = () => {
        /* onclose follows */
      };
    };

    const scheduleReconnect = () => {
      if (disposed) return;
      const delay = Math.min(30_000, 1000 * 2 ** attempt) * (0.75 + Math.random() * 0.5);
      attempt++;
      reconnectTimer = window.setTimeout(connect, delay);
    };

    const onVisible = () => {
      if (document.visibilityState === "visible" && !ws) {
        window.clearTimeout(reconnectTimer);
        attempt = 0;
        connect();
      }
    };

    refreshRef.current = () => void poll();
    // Fetch immediately so first paint doesn't wait on the socket.
    void poll();
    connect();
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", onVisible);
      window.clearTimeout(reconnectTimer);
      if (pingTimer !== undefined) window.clearInterval(pingTimer);
      pingSeqTimers.forEach((x) => window.clearTimeout(x));
      stopPolling();
      ws?.close();
    };
  }, []);

  const subscribe = useCallback((ev: LiveEvent, fn: Listener) => {
    let set = listeners.current.get(ev);
    if (!set) listeners.current.set(ev, (set = new Set()));
    set.add(fn);
    return () => {
      set!.delete(fn);
    };
  }, []);
  const refresh = useCallback(() => refreshRef.current(), []);

  const value = useMemo<LiveCtx>(
    () => ({ state, clockOffset, status, receivedAt, subscribe, refresh }),
    [state, clockOffset, status, receivedAt, subscribe, refresh],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useLive(): LiveCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useLive outside LiveProvider");
  return c;
}

/** Run `fn` whenever the server pushes `ev` over the socket. */
export function useLiveEvent(ev: LiveEvent, fn: () => void) {
  const { subscribe } = useLive();
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => subscribe(ev, () => ref.current()), [subscribe, ev]);
}
