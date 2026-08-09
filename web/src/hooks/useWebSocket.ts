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

  const connect = useCallback(() => {
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      retryRef.current = 0;
    };

    ws.onclose = () => {
      setConnected(false);
      const delay = Math.min(1000 * 2 ** retryRef.current, 30000);
      retryRef.current++;
      setTimeout(connect, delay);
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
    return () => wsRef.current?.close();
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