/**
 * Module-level SSE chat stream manager.
 *
 * The streaming /api/chat request is started here (outside any component) so a
 * route change that unmounts ChatPage does NOT kill the in-flight stream. The
 * fetch/read loop keeps consuming events and accumulates the running message;
 * components subscribe to receive live events and re-sync from the accumulated
 * state when they re-mount (e.g. navigating back to /chat mid-generation).
 *
 * This solves: user starts a generation, clicks another page → ChatPage unmounts
 * → the old reader loop was tearing down and dropping the response. With the
 * loop owned by this module the backend keeps streaming, the message is persisted,
 * and returning to the chat page shows the finished reply.
 */
import { toast } from "sonner";

export interface ChatStreamEvent {
  type: "thinking" | "text" | "tool_use" | "tool_result" | "done" | "error";
  id?: string;
  name?: string;
  input?: unknown;
  content?: string;
  text?: string;
  session_id?: string;
  response?: string;
  message?: string;
}

/** A single assistant message being streamed (mirrors ChatPage's ChatMsg). */
export interface StreamMsg {
  role: "user" | "assistant";
  content: string;
  thinking?: string;
  tool_calls?: { id?: string; name: string; input: string; result?: string }[];
}

type Subscriber = (ev: ChatStreamEvent) => void;

const subscribers = new Set<Subscriber>();
let running = false;
// Accumulated messages for the in-flight stream, so a re-mount after a route
// switch can restore exactly where the generation is.
let streamMessages: StreamMsg[] = [];

export function subscribeChatStream(fn: Subscriber): () => void {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

export function isChatStreamRunning(): boolean {
  return running;
}

/** Snapshot of the in-flight stream messages (empty when idle). */
export function getStreamMessages(): StreamMsg[] {
  return streamMessages.map((m) => ({ ...m }));
}

function notify(ev: ChatStreamEvent) {
  subscribers.forEach((fn) => {
    try {
      fn(ev);
    } catch {
      /* subscriber errors must not kill the stream loop */
    }
  });
}

/**
 * Start (or reuse) a streaming chat request. Multiple pages/remounts calling
 * this while a stream is running just re-subscribe; they don't duplicate the
 * request.
 */
export async function startChatStream(body: {
  message: string;
  user_id: string;
  session_id?: string;
  reset?: boolean;
  context?: unknown[];
}): Promise<ChatStreamEvent | null> {
  if (running) {
    // A stream is already in flight; new subscribers receive its events.
    return null;
  }
  running = true;
  // Seed the accumulated state: the user message plus a placeholder assistant.
  streamMessages = [
    { role: "user", content: body.message },
    { role: "assistant", content: "", thinking: "", tool_calls: [] },
  ];
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) {
      streamMessages[streamMessages.length - 1].content = `请求失败 (${res.status})`;
      notify({ type: "error", message: `请求失败 (${res.status})` });
      return { type: "error", message: `请求失败 (${res.status})` };
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalEvent: ChatStreamEvent | null = null;
    const last = () => streamMessages[streamMessages.length - 1];
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";
      for (const part of parts) {
        const line = part.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        let ev: any;
        try {
          ev = JSON.parse(line.slice(6));
        } catch {
          continue;
        }
        if (ev.type === "thinking") {
          last().thinking = (last().thinking ?? "") + ev.text;
          notify({ type: "thinking", text: ev.text });
        } else if (ev.type === "text") {
          last().content += ev.text;
          notify({ type: "text", text: ev.text });
        } else if (ev.type === "tool_use") {
          const calls = last().tool_calls ?? [];
          const idx = calls.findIndex((tc) => tc.id && tc.id === ev.id);
          const item = { id: ev.id || "", name: ev.name, input: JSON.stringify(ev.input ?? {}) };
          if (idx >= 0) calls[idx] = { ...calls[idx], input: item.input };
          else calls.push(item);
          last().tool_calls = [...calls];
          notify({ type: "tool_use", id: ev.id, name: ev.name, input: ev.input });
        } else if (ev.type === "tool_result") {
          last().tool_calls = (last().tool_calls ?? []).map((tc) =>
            tc.id && ev.id && tc.id === ev.id ? { ...tc, result: ev.content } : tc
          );
          notify({ type: "tool_result", id: ev.id, content: ev.content });
        } else if (ev.type === "done") {
          finalEvent = { type: "done", session_id: ev.session_id, response: ev.response };
          notify(finalEvent);
        } else if (ev.type === "error") {
          finalEvent = { type: "error", message: ev.message };
          notify(finalEvent);
        }
      }
    }
    return finalEvent;
  } catch (err: any) {
    const msg = err?.message ?? "请求失败";
    streamMessages[streamMessages.length - 1].content = `错误: ${msg}`;
    notify({ type: "error", message: msg });
    return { type: "error", message: msg };
  } finally {
    running = false;
  }
}

void toast;
