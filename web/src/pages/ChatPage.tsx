import { useState, useRef, useEffect } from "react";
import { Send, Bot, User, Trash2, History, X, Plus, Paperclip } from "lucide-react";
import { toast } from "sonner";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Markdown } from "@/components/Markdown";
import { useChatMutation, useSessions, useMessages, useUploadFile } from "@/api/hooks";
import { subscribeChatStream, getStreamMessages, isChatStreamRunning } from "@/lib/chatStream";
import { fmtTime } from "@/api/client";

interface ToolCall {
  id?: string;
  name: string;
  input: string;
  result?: string;
}

interface ChatMsg {
  role: "user" | "assistant" | "system";
  content: string;
  ts: number;
  thinking?: string;
  tool_calls?: ToolCall[];
}

const STORAGE_KEY = "metano-chat-history";
const DIRTY_KEY = "metano-chat-dirty";

function loadHistory(): ChatMsg[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch {}
  return [];
}

function loadDirty(): boolean {
  try {
    return localStorage.getItem(DIRTY_KEY) === "1";
  } catch {}
  return false;
}

function saveHistory(msgs: ChatMsg[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(msgs.slice(-200)));
  } catch {}
}

export default function ChatPage() {
  const [messages, setMessages] = useState<ChatMsg[]>(loadHistory);
  const [input, setInput] = useState("");
  const [showSessionPicker, setShowSessionPicker] = useState(false);
  const [connectedSession, setConnectedSession] = useState<string | null>(null);
  // Ref mirror of connectedSession. handleSend reads this instead of the state
  // value: setConnectedSession is async, so a send fired right after selecting
  // a history session could otherwise capture the stale (null/old) id and open
  // a brand-new DB session instead of resuming the picked one.
  const connectedSessionRef = useRef<string | null>(null);
  const setSession = (sid: string | null) => {
    connectedSessionRef.current = sid;
    setConnectedSession(sid);
  };
  // dirty = local state contains messages not (yet) confirmed persisted to DB.
  // Covers: failed sends (route_message threw -> _persist_chat never ran) and
  // in-flight requests. Survives page reloads via localStorage so disconnect
  // after a refresh still warns.
  const [dirty, setDirty] = useState<boolean>(loadDirty);
  const failedSendRef = useRef(false);
  const clearedRef = useRef(false);
  // True after 新对话/清空/断开: the next message must open a brand-new DB
  // session (reset:true) instead of continuing whatever session the backend
  // still has pinned.
  const freshRef = useRef(false);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  const autoGrow = () => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
  };
  const chatMut = useChatMutation();
  const uploadMut = useUploadFile();
  // Only poll the session list while the picker is open; when closed, stop polling
  // (the initial fetch on mount is still performed by useQuery).
  const { data: sessionsData, isError: sessionsError } = useSessions("", 20, showSessionPicker ? 10000 : false);
  const { data: msgData, isLoading: msgLoading, isError: msgError } = useMessages(connectedSession ?? "");

  const sessions = sessionsData?.sessions ?? [];

  // Keep streaming responses pinned only while the user is already at the
  // bottom. A smooth scroll on every token fights manual scrolling (notably
  // when expanding the thinking details) and makes the viewport jerk.
  const autoScrollRef = useRef(true);
  useEffect(() => {
    if (!autoScrollRef.current) return;
    bottomRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
  }, [messages]);

  const handleMessagesScroll = () => {
    const container = messagesContainerRef.current;
    if (!container) return;
    autoScrollRef.current = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
  };

  useEffect(() => {
    saveHistory(messages);
  }, [messages]);

  useEffect(() => {
    try {
      if (dirty) localStorage.setItem(DIRTY_KEY, "1");
      else localStorage.removeItem(DIRTY_KEY);
    } catch {}
  }, [dirty]);

  const handleConnectSession = (sessionId: string) => {
    setShowSessionPicker(false);
    setSession(sessionId);
    clearedRef.current = false;
    failedSendRef.current = false;
    freshRef.current = false; // explicitly resumed a past session, not a fresh one
    setDirty(false); // replacing local state with persisted DB messages
    // When messages data arrives, merge them into chat
  };

  // Only load session messages once per connected session, so later refetches
  // (e.g. refetchOnWindowFocus) don't overwrite messages sent locally this turn.
  const loadedSessionRef = useRef<string | null>(null);
  useEffect(() => {
    if (connectedSession && msgData && !msgLoading && loadedSessionRef.current !== connectedSession) {
      loadedSessionRef.current = connectedSession;
      const sessionMsgs = msgData.messages.map((m: any) => ({
        role: m.role as "user" | "assistant" | "system",
        content: m.content ?? "",
        ts: typeof m.timestamp === "number" ? m.timestamp * 1000 : Date.now(),
      }));
      setMessages(sessionMsgs);
    }
  }, [connectedSession, msgData, msgLoading]);

  const handleDisconnect = () => {
    if (dirty && !window.confirm("当前有未成功保存到历史的消息，断开后将丢失这些消息。仍要断开吗？")) return;
    clearedRef.current = true;
    failedSendRef.current = false;
    freshRef.current = true;
    setDirty(false);
    setSession(null);
    setMessages([]);
    localStorage.removeItem(STORAGE_KEY);
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text) return;
    if (chatMut.isPending) return; // a stream is already running

    const userMsg: ChatMsg = { role: "user", content: text, ts: Date.now() };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    if (taRef.current) taRef.current.style.height = "auto";
    clearedRef.current = false;
    setDirty(true); // new user message is local-only until /api/chat persists it

    // Placeholder assistant message, updated via the stream subscription.
    const assistantMsg: ChatMsg = { role: "assistant", content: "", thinking: "", tool_calls: [], ts: Date.now() };
    setMessages((prev) => [...prev, assistantMsg]);

    // The SSE stream is owned by the module-level chatStream manager, so
    // navigating away (unmounting ChatPage) does NOT kill the in-flight reply.
    chatMut.mutate({
      message: text,
      session_id: connectedSessionRef.current || undefined,
      reset: !connectedSessionRef.current && freshRef.current ? true : undefined,
      context: messages.slice(-10).map((m) => ({ role: m.role, content: m.content })),
    });
  };

  // Consume the module-level chat stream. The subscription lives outside the
  // component lifecycle: navigating away unmounts ChatPage but the stream keeps
  // running (chatStream.ts), and re-mounting re-subscribes and restores the
  // in-flight reply from getStreamMessages().
  const patchLast = (fn: (a: ChatMsg) => void) =>
    setMessages((prev) => {
      if (!prev.length) return prev;
      const next = [...prev];
      const last = { ...next[next.length - 1] };
      fn(last);
      next[next.length - 1] = last;
      return next;
    });
  useEffect(() => {
    // Re-mount mid-stream: restore the accumulated user+assistant messages so
    // the partially-generated reply is not lost when navigating back.
    if (isChatStreamRunning() && getStreamMessages().length) {
      const acc = getStreamMessages();
      setMessages((prev) => {
        // Only seed when we have nothing newer (e.g. no picked history loaded).
        if (prev.length === 0) {
          return acc.map((m) => ({ role: m.role, content: m.content, thinking: m.thinking, tool_calls: m.tool_calls, ts: Date.now() }));
        }
        return prev;
      });
    }
    const unsub = subscribeChatStream((ev) => {
      if (ev.type === "thinking") {
        patchLast((a) => { a.thinking = (a.thinking ?? "") + (ev.text ?? ""); });
      } else if (ev.type === "text") {
        patchLast((a) => { a.content += ev.text ?? ""; });
      } else if (ev.type === "tool_use") {
        patchLast((a) => {
          const calls = a.tool_calls ?? [];
          const idx = calls.findIndex((tc) => tc.id && tc.id === ev.id);
          const item = { id: ev.id || "", name: ev.name || "", input: JSON.stringify(ev.input ?? {}) };
          if (idx >= 0) a.tool_calls = [...calls.slice(0, idx), { ...calls[idx], input: item.input }, ...calls.slice(idx + 1)];
          else a.tool_calls = [...calls, item];
        });
      } else if (ev.type === "tool_result") {
        patchLast((a) => {
          a.tool_calls = (a.tool_calls ?? []).map((tc) =>
            tc.id && ev.id && tc.id === ev.id ? { ...tc, result: ev.content } : tc
          );
        });
      } else if (ev.type === "done") {
        setSession(ev.session_id || null);
        freshRef.current = false;
        setDirty(failedSendRef.current);
      } else if (ev.type === "error") {
        failedSendRef.current = true;
        setDirty(true);
        if (!clearedRef.current) {
          patchLast((a) => { a.content = `错误: ${ev.message ?? "请求失败"}`; });
        }
      }
    });
    return unsub;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleClear = () => {
    if (dirty && !window.confirm("当前有未成功保存到历史的消息，清空后将丢失这些消息。仍要清空吗？")) return;
    clearedRef.current = true;
    failedSendRef.current = false;
    freshRef.current = true;
    setDirty(false);
    setMessages([]);
    setSession(null);
    localStorage.removeItem(STORAGE_KEY);
  };

  const handleNewChat = () => {
    clearedRef.current = true;
    failedSendRef.current = false;
    freshRef.current = true;
    loadedSessionRef.current = null;
    setShowSessionPicker(false);
    setDirty(false);
    setSession(null);
    setMessages([]);
    localStorage.removeItem(STORAGE_KEY);
    toast.success("已开启新对话");
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleFileSelected = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file
    if (!file) return;
    uploadMut.mutate(file, {
      onSuccess: ({ path }) => {
        setInput((prev) => (prev ? prev + " " : "") + `[附件: ${path}]`);
      },
      onError: () => toast.error("上传失败"),
    });
  };

  return (
    <>
      {/* Native app chat has no page header — the chat fills the whole
          viewport below the mobile top bar. */}

      {/* Session Picker Modal */}
      {showSessionPicker && (
        <Card className="relative z-10 mb-4">
          <div className="flex items-center justify-between p-4 pb-2">
            <span className="font-medium text-sm">选择历史会话继续对话</span>
            <Button size="sm" variant="ghost" onClick={() => setShowSessionPicker(false)}>
              <X className="size-4" />
            </Button>
          </div>
          <div className="px-4 pb-4 space-y-2 max-h-[300px] overflow-y-auto">
            {sessionsError ? (
              <p className="text-sm text-destructive">加载失败，请检查服务或刷新重试</p>
            ) : sessions.length === 0 ? (
              <p className="text-sm text-muted-foreground">暂无历史会话</p>
            ) : sessions.map((s) => (
              <button
                key={s.id}
                className="w-full text-left p-3 rounded-lg hover:bg-muted/80 transition-colors flex items-center gap-3"
                onClick={() => handleConnectSession(s.id)}
              >
                <History className="size-4 text-muted-foreground shrink-0" />
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-sm truncate">{s.title || s.id.slice(0, 16)}</div>
                  <div className="text-xs text-muted-foreground flex gap-2 mt-0.5">
                    <span>{s.message_count ?? 0} 条消息</span>
                    {s.started_at && <span>{fmtTime(s.started_at)}</span>}
                    {s.model && <span>{s.model}</span>}
                  </div>
                </div>
              </button>
            ))}
          </div>
        </Card>
      )}

      {/* Native-app style chat: absolutely fills the <main> area on mobile
          (top bar → screen bottom, edge to edge, no gaps). Desktop keeps a
          normal-flow card with margins. */}
      <div className="absolute inset-x-0 bottom-0 top-0 z-0 flex min-h-0 min-w-0 flex-col overflow-hidden bg-muted/40 md:static md:z-auto md:h-[calc(100dvh-12rem)] md:rounded-xl md:border md:border-border">
        {/* Header bar — blends into the grey thread on mobile (icon buttons),
            full labels + white bar on desktop. */}
        <div className="flex items-center justify-between bg-muted/40 px-3 py-2 border-b md:bg-background md:px-4 md:py-2.5">
          <div className="flex min-w-0 items-center gap-2">
            <span className="text-xs text-muted-foreground md:text-sm">{messages.length} 条消息</span>
            {connectedSession && (
              <Badge variant="outline" className="text-[10px] font-mono">
                已接入: {connectedSession.slice(0, 12)}
              </Badge>
            )}
          </div>
          <div className="flex items-center gap-0.5 md:gap-1">
            <Button variant="ghost" className="size-8 rounded-full md:h-auto md:w-auto md:rounded-lg md:px-3 md:py-1.5" title="新对话" onClick={handleNewChat}>
              <Plus className="size-4" />
              <span className="hidden md:inline md:ml-1.5 md:text-sm">新对话</span>
            </Button>
            {!connectedSession && (
              <Button variant="ghost" className="size-8 rounded-full md:h-auto md:w-auto md:rounded-lg md:px-3 md:py-1.5" title="接入历史" onClick={() => setShowSessionPicker(!showSessionPicker)}>
                <History className="size-4" />
                <span className="hidden md:inline md:ml-1.5 md:text-sm">接入历史</span>
              </Button>
            )}
            {connectedSession && (
              <Button variant="ghost" className="size-8 rounded-full md:h-auto md:w-auto md:rounded-lg md:px-3 md:py-1.5" title="断开" onClick={handleDisconnect}>
                <X className="size-4" />
                <span className="hidden md:inline md:ml-1.5 md:text-sm">断开</span>
              </Button>
            )}
            <Button variant="ghost" className="size-8 rounded-full md:h-auto md:w-auto md:rounded-lg md:px-3 md:py-1.5" title="清空" onClick={handleClear}>
              <Trash2 className="size-4" />
              <span className="hidden md:inline md:ml-1.5 md:text-sm">清空</span>
            </Button>
          </div>
        </div>

        {/* Messages */}
        <div
          ref={messagesContainerRef}
          onScroll={handleMessagesScroll}
          className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain p-4 space-y-4"
        >
          {messages.length === 0 && !msgLoading && (
            <div className="flex flex-col items-center justify-center h-full gap-3">
              <p className="text-sm text-muted-foreground">输入消息开始对话</p>
              <Button variant="outline" size="sm" onClick={() => setShowSessionPicker(true)}>
                <History className="size-4 mr-1" /> 接入历史会话继续对话
              </Button>
            </div>
          )}
          {msgLoading && connectedSession && (
            <div className="space-y-3">{Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-12 rounded-lg" />)}</div>
          )}
          {msgError && connectedSession && (
            <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`flex gap-3 ${m.role === "user" ? "justify-end" : ""}`}>
              {m.role === "assistant" && (
                <div className="size-8 rounded-full bg-primary/10 flex items-center justify-center shrink-0">
                  <Bot className="size-4 text-primary" />
                </div>
              )}
              <div
                className={`w-fit max-w-[85%] px-4 py-2.5 text-sm ${
                  m.role === "user"
                    ? "rounded-2xl rounded-br-md bg-primary text-primary-foreground whitespace-pre-wrap"
                    : "rounded-2xl rounded-bl-md border border-border/60 bg-card text-foreground"
                }`}
              >
                {m.role === "assistant" && m.thinking ? (
                  <details className="mb-1.5 text-xs text-muted-foreground">
                    <summary
                      className="cursor-pointer select-none"
                      onClick={() => { autoScrollRef.current = false; }}
                    >
                      💭 思考过程
                    </summary>
                    <div className="mt-1 whitespace-pre-wrap break-all border-t border-border/50 pt-1">{m.thinking}</div>
                  </details>
                ) : null}
                {m.role === "assistant" && m.tool_calls && m.tool_calls.length > 0 ? (
                  <div className="mb-1.5 space-y-1.5">
                    {m.tool_calls.map((tc, ti) => (
                      <div key={ti} className="overflow-hidden rounded-lg border bg-background/60 text-xs font-mono">
                        <div className="flex items-center justify-between border-b border-border/50 px-2.5 py-1.5">
                          <span className="font-semibold text-primary">🔧 {tc.name}</span>
                          <span
                            className={
                              tc.result !== undefined
                                ? "text-[10px] text-emerald-500"
                                : "text-[10px] text-muted-foreground"
                            }
                          >
                            {tc.result !== undefined ? "✓ 完成" : "⏳ 执行中"}
                          </span>
                        </div>
                        <details className="px-2.5 py-1.5">
                          <summary className="cursor-pointer select-none text-muted-foreground">参数</summary>
                          <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all text-muted-foreground">
                            {tc.input}
                          </pre>
                        </details>
                        {tc.result !== undefined && (
                          <details className="border-t border-border/50 px-2.5 py-1.5">
                            <summary className="cursor-pointer select-none text-muted-foreground">结果</summary>
                            <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-all text-muted-foreground">
                              {tc.result}
                            </pre>
                          </details>
                        )}
                      </div>
                    ))}
                  </div>
                ) : null}
                {m.role === "assistant" ? (
                  m.content ? (
                    <Markdown>{m.content}</Markdown>
                  ) : (
                    <span className="text-muted-foreground">思考中…</span>
                  )
                ) : (
                  <span className="whitespace-pre-wrap break-words">{m.content}</span>
                )}
              </div>
              {m.role === "user" && (
                <div className="size-8 rounded-full bg-secondary flex items-center justify-center shrink-0">
                  <User className="size-4" />
                </div>
              )}
            </div>
          ))}
          {chatMut.isPending && (
            <div className="flex gap-3">
              <div className="size-8 rounded-full bg-primary/10 flex items-center justify-center shrink-0">
                <Bot className="size-4 text-primary" />
              </div>
              <div className="bg-muted rounded-xl px-4 py-2.5 text-sm text-muted-foreground">思考中...</div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* Composer — capsule textarea, auto-growing, keyboard-safe.
            Buttons live OUTSIDE the input row so the textarea takes full width. */}
        <div className="bg-background p-2.5 pb-safe border-t">
          <div className="flex w-full min-w-0 items-end gap-0.5 rounded-[1.4rem] border bg-background py-1 pl-3 pr-1.5 shadow-sm transition-all focus-within:border-primary/50 focus-within:ring-2 focus-within:ring-primary/15">
            <textarea
              ref={taRef}
              rows={1}
              value={input}
              placeholder="输入消息..."
              onChange={(e) => { setInput(e.target.value); autoGrow(); }}
              onKeyDown={handleKeyDown}
              disabled={chatMut.isPending}
              className="min-w-0 flex-1 resize-none bg-transparent px-1 py-2 text-[16px] leading-relaxed outline-none placeholder:text-muted-foreground max-h-40"
            />
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-8 shrink-0 rounded-full md:size-9"
              title="上传附件"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploadMut.isPending || chatMut.isPending}
            >
              {uploadMut.isPending ? <span className="text-xs">上传中</span> : <Paperclip className="size-4.5" />}
            </Button>
            <Button
              onClick={handleSend}
              disabled={chatMut.isPending || !input.trim()}
              size="icon"
              className="size-8 shrink-0 rounded-full md:size-9"
              aria-label="发送"
            >
              <Send className="size-4" />
            </Button>
          </div>
          <input
            ref={fileInputRef}
            type="file"
            hidden
            accept=".txt,.md,.pdf,.png,.jpg,.jpeg,.gif,.webp,.csv,.json,.py,.js,.ts,.html,.docx"
            onChange={handleFileSelected}
          />
        </div>
      </div>
    </>
  );
}