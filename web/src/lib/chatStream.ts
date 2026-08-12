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

import { refreshAuthSession } from "@/api/client";

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

// F-13/M-07: a generation counter + AbortController let a new conversation
// (or a logout / user switch) cancel the in-flight stream and ignore the late
// events of a superseded generation instead of writing them back to a stale
// session.
let generation = 0;
let controller: AbortController | null = null;

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
 * Abort the in-flight stream (if any) and drop the accumulated state. Used by
 * 新对话/清空/断开 so an old reply cannot re-attach to a new session, and by
 * logout to stop any still-generating response from being written back.
 */
export function cancelChatStream(): void {
  generation++;
  if (controller) {
    controller.abort();
    controller = null;
  }
  running = false;
  streamMessages = [];
}

/** Full teardown used on logout: cancel the stream and drop all subscribers. */
export function resetChatStreamState(): void {
  cancelChatStream();
  subscribers.clear();
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
  const myGen = ++generation;
  const ctrl = new AbortController();
  controller = ctrl;
  running = true;
  // Seed the accumulated state: the user message plus a placeholder assistant.
  streamMessages = [
    { role: "user", content: body.message },
    { role: "assistant", content: "", thinking: "", tool_calls: [] },
  ];
  try {
    let res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    if (res.status === 401) {
      // Access token may have expired (15 min) while the refresh token is still
      // valid. This raw fetch bypasses fetchAPI's 401-retry, so renew explicitly
      // and retry once before reporting failure.
      const refreshed = await refreshAuthSession();
      if (refreshed) {
        if (myGen !== generation) return null; // cancelled while refreshing
        res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify(body),
          signal: ctrl.signal,
        });
      }
      if (res.status === 401) {
        // Refresh failed or retry still 401 → session is genuinely dead.
        window.dispatchEvent(new Event("auth:unauthorized"));
      }
    }
    if (!res.ok || !res.body) {
      if (myGen !== generation) return null; // cancelled — ignore stale result
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
        // Fields mirror ChatStreamEvent — parsed from untrusted SSE payload, so
        // no `any`; absent fields are simply undefined.
        let ev: {
          type?: string;
          text?: string;
          id?: string;
          name?: string;
          input?: unknown;
          content?: string;
          session_id?: string;
          response?: string;
          message?: string;
        };
        try {
          ev = JSON.parse(line.slice(6)) as typeof ev;
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
          const item = { id: ev.id || "", name: ev.name || "", input: JSON.stringify(ev.input ?? {}) };
          if (idx >= 0) calls[idx] = { ...calls[idx], input: item.input };
          else calls.push(item);
          last().tool_calls = [...calls];
          notify({ type: "tool_use", id: ev.id, name: ev.name || "", input: ev.input });
        } else if (ev.type === "tool_result") {
          last().tool_calls = (last().tool_calls ?? []).map((tc) =>
            tc.id && ev.id && tc.id === ev.id ? { ...tc, result: ev.content } : tc
          );
          notify({ type: "tool_result", id: ev.id, content: ev.content });
        } else if (ev.type === "done") {
          // F-12: command-style replies can stream zero `text` events and only
          // carry the final answer in the done event. Fill the placeholder with
          // it so the message is never left as a blank "思考中…".
          if (ev.response && !last().content) {
            last().content = ev.response;
          }
          finalEvent = { type: "done", session_id: ev.session_id, response: ev.response };
          notify(finalEvent);
        } else if (ev.type === "error") {
          finalEvent = { type: "error", message: ev.message };
          notify(finalEvent);
        }
      }
    }
    return finalEvent;
  } catch (err) {
    if (myGen !== generation) return null; // superseded/aborted — stay silent
    const msg = err instanceof Error ? err.message : "请求失败";
    streamMessages[streamMessages.length - 1].content = `错误: ${msg}`;
    notify({ type: "error", message: msg });
    return { type: "error", message: msg };
  } finally {
    if (myGen === generation) {
      running = false;
      controller = null;
    }
  }
}
