import { useEffect, useRef, useCallback, useState } from "react";

type WSEvent = { type: string; data?: unknown };

interface UseWebSocketReturn {
  connected: boolean;
  subscribe: (type: string, cb: (data: unknown) => void) => () => void;
  send: (data: unknown) => void;
}

const WS_URL = `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws`;

export function useWebSocket(): UseWebSocketReturn {
  const wsRef = useRef<WebSocket | null>(null);
  const subsRef = useRef<Map<string, Set<(data: unknown) => void>>>(new Map());
  const [connected, setConnected] = useState(false);
  const retryRef = useRef(0);
  // Track the pending reconnect timer so it can be cleared on unmount and
  // before a new connection is opened (avoiding reconnects after teardown).
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    // A previous close may have scheduled a reconnect; drop it so we don't
    // end up with two sockets / reconnect loops.
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      retryRef.current = 0;
    };

    ws.onclose = () => {
      setConnected(false);
      // Only schedule a reconnect if this closing socket is still the active
      // one. After unmount (or a newer connect) wsRef.current changes, and a
      // stale socket's onclose must not arm a timer.
      if (wsRef.current !== ws) return;
      const delay = Math.min(1000 * 2 ** retryRef.current, 30000);
      retryRef.current++;
      retryTimerRef.current = setTimeout(connect, delay);
    };

    ws.onmessage = (e) => {
      try {
        const event: WSEvent = JSON.parse(e.data);
        const handlers = subsRef.current.get(event.type);
        if (handlers) handlers.forEach((cb) => cb(event.data));
      } catch {}
    };

    ws.onerror = () => ws.close();
  }, []);

  useEffect(() => {
    connect();
    return () => {
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