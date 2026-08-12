import { useEffect, useRef, useCallback, useState } from "react";

type WSEvent = { type: string; data?: unknown };

interface UseWebSocketReturn {
  connected: boolean;
  subscribe: (type: string, cb: (data: unknown) => void) => () => void;
  send: (data: unknown) => void;
}

const WS_URL = `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws`;

// L-03: the backend authenticates the first WS frame (or the handshake cookie).
// The access token is HttpOnly so the frontend cannot read it directly; instead
// it asks for a short-lived WS ticket from /api/auth/ws-ticket. When that
// endpoint is absent the backend is expected to authenticate via the handshake
// cookie, in which case a plain `new WebSocket()` is enough.
const MAX_RETRIES = 5;

async function fetchWsTicket(): Promise<string | null> {
  try {
    const res = await fetch("/api/auth/ws-ticket", { method: "POST", credentials: "include" });
    if (!res.ok) return null;
    const data = await res.json();
    return typeof data?.ticket === "string" ? data.ticket : null;
  } catch {
    return null;
  }
}

export function useWebSocket(): UseWebSocketReturn {
  const wsRef = useRef<WebSocket | null>(null);
  const subsRef = useRef<Map<string, Set<(data: unknown) => void>>>(new Map());
  const [connected, setConnected] = useState(false);
  const retryRef = useRef(0);
  // Track the pending reconnect timer so it can be cleared on unmount and
  // before a new connection is opened (avoiding reconnects after teardown).
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const disposedRef = useRef(false);
  // Holds the latest `connect` so the onclose handler can schedule a reconnect
  // without self-referencing `connect` inside its own useCallback body.
  const connectRef = useRef<() => void>(() => {});

  const connect = useCallback(() => {
    // A previous close may have scheduled a reconnect; drop it so we don't
    // end up with two sockets / reconnect loops.
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    let ws: WebSocket | null = null;
    let cancelled = false;

    fetchWsTicket().then((ticket) => {
      if (disposedRef.current || cancelled) return;
      ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        retryRef.current = 0;
        // First frame carries the ticket; the backend rejects (4001) without it
        // unless the handshake cookie already authenticated the connection.
        if (ticket) ws?.send(JSON.stringify({ token: ticket }));
      };

      ws.onmessage = (e) => {
        try {
          const event: WSEvent = JSON.parse(e.data as string);
          const handlers = subsRef.current.get(event.type);
          if (handlers) handlers.forEach((cb) => cb(event.data));
        } catch {
          /* ignore malformed frames */
        }
      };

      ws.onerror = () => ws?.close();

      ws.onclose = (ev) => {
        setConnected(false);
        cancelled = true;
        // Only schedule a reconnect if this closing socket is still the active
        // one. After unmount (or a newer connect) wsRef.current changes, and a
        // stale socket's onclose must not arm a timer.
        if (wsRef.current !== ws) return;
        // 4001 = authentication rejected: no token / no ticket / wrong cookie.
        // Retrying won't help, and an infinite loop just hammers the server.
        if (ev.code === 4001) return;
        if (retryRef.current >= MAX_RETRIES) return;
        const delay = Math.min(1000 * 2 ** retryRef.current, 30000);
        retryRef.current++;
        retryTimerRef.current = setTimeout(() => connectRef.current(), delay);
      };
    });
  }, []);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  useEffect(() => {
    disposedRef.current = false;
    connect();
    return () => {
      disposedRef.current = true;
      // Clear any pending reconnect so an unmounted hook never reconnects.
      if (retryTimerRef.current) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect]);

  const subscribe = useCallback((type: string, cb: (data: unknown) => void) => {
    if (!subsRef.current.has(type)) subsRef.current.set(type, new Set());
    subsRef.current.get(type)!.add(cb);
    return () => subsRef.current.get(type)?.delete(cb);
  }, []);

  const send = useCallback((data: unknown) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    }
  }, []);

  return { connected, subscribe, send };
}
