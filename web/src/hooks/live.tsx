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
    // Best (lowest-uncertainty) clock offset estimate seen recently.
    let bestOffset: { offset: number; err: number; at: number } | null = null;

    const applyState = (s: State, localT0: number, localT1: number) => {
      const err = (localT1 - localT0) / 2000;
      const offset = s.server_time - (localT0 + localT1) / 2000;
      const now = Date.now();
      // Prefer low-latency samples, but let old estimates expire after a minute.
      if (!bestOffset || err <= bestOffset.err + 0.005 || now - bestOffset.at > 60_000) {
        bestOffset = { offset, err, at: now };
        setClockOffset(offset);
      }
      setState(s);
      setReceivedAt(now);
    };

    const emit = (ev: LiveEvent) => listeners.current.get(ev)?.forEach((fn) => fn());

    const poll = async () => {
      const t0 = Date.now();
      try {
        const s = await api.state();
        if (!disposed) {
          applyState(s, t0, Date.now());
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
        pingTimer = window.setInterval(() => {
          if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
        }, 25_000);
      };
      ws.onmessage = (ev) => {
        const t = Date.now();
        let msg: WsMessage;
        try {
          msg = JSON.parse(ev.data as string) as WsMessage;
        } catch {
          return;
        }
        if (msg.type === "state") applyState(msg.data, t, t);
        else if (msg.type === "requests_changed" || msg.type === "users_changed") emit(msg.type);
      };
      ws.onclose = () => {
        if (pingTimer !== undefined) window.clearInterval(pingTimer);
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
