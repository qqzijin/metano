import { useState, useRef, useEffect } from "react";
import { Send, Bot, User, Trash2, History, X, Plus, Paperclip } from "lucide-react";
import { toast } from "sonner";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Markdown } from "@/components/Markdown";
import { useChatMutation, useSessions, useMessages, useUploadFile } from "@/api/hooks";
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
  // dirty = local state contains messages not (yet) confirmed persisted to DB.
  // Covers: failed sends (route_message threw -> _persist_chat never ran) and
  // in-flight requests. Survives page reloads via localStorage so disconnect
  // after a refresh still warns.
  const [dirty, setDirty] = useState<boolean>(loadDirty);
  const failedSendRef = useRef(false);
  const clearedRef = useRef(false);
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

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

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
    setConnectedSession(sessionId);
    clearedRef.current = false;
    failedSendRef.current = false;
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
    setDirty(false);
    setConnectedSession(null);
    setMessages([]);
    localStorage.removeItem(STORAGE_KEY);
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text) return;

    const userMsg: ChatMsg = { role: "user", content: text, ts: Date.now() };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    if (taRef.current) taRef.current.style.height = "auto";
    clearedRef.current = false;
    setDirty(true); // new user message is local-only until /api/chat persists it

    // Placeholder assistant message, updated via the SSE stream.
    const assistantMsg: ChatMsg = { role: "assistant", content: "", thinking: "", tool_calls: [], ts: Date.now() };
    setMessages((prev) => [...prev, assistantMsg]);

    const patchLast = (fn: (a: ChatMsg) => void) =>
      setMessages((prev) => {
        if (!prev.length) return prev;
        const next = [...prev];
        const last = { ...next[next.length - 1] };
        fn(last);
        next[next.length - 1] = last;
        return next;
      });

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          message: text,
          user_id: "web_user",
          session_id: connectedSession || undefined,
          context: messages.slice(-10).map((m) => ({ role: m.role, content: m.content })),
        }),
      });
      if (!res.ok || !res.body) throw new Error(`请求失败 (${res.status})`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
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
          try { ev = JSON.parse(line.slice(6)); } catch { continue; }
          if (ev.type === "thinking") {
            patchLast((a) => { a.thinking = (a.thinking ?? "") + ev.text; });
          } else if (ev.type === "text") {
            patchLast((a) => { a.content += ev.text; });
          } else if (ev.type === "tool_use") {
            // Tool use fires twice: once on content_block_start (empty input)
            // and again on stop (full args). Dedupe by id and update in place.
            patchLast((a) => {
              const calls = a.tool_calls ?? [];
              const idx = calls.findIndex((tc) => tc.id && tc.id === ev.id);
              const item = { id: ev.id || "", name: ev.name, input: JSON.stringify(ev.input ?? {}) };
              if (idx >= 0) {
                a.tool_calls = [...calls.slice(0, idx), { ...calls[idx], input: item.input }, ...calls.slice(idx + 1)];
              } else {
                a.tool_calls = [...calls, item];
              }
            });
          } else if (ev.type === "tool_result") {
            // Attach tool output to the matching tool_use by id.
            patchLast((a) => {
              a.tool_calls = (a.tool_calls ?? []).map((tc) =>
                tc.id && ev.id && tc.id === ev.id ? { ...tc, result: ev.content } : tc
              );
            });
          } else if (ev.type === "done") {
            setConnectedSession(ev.session_id || null);
            setDirty(failedSendRef.current);
          }
        }
      }
    } catch (err: any) {
      failedSendRef.current = true;
      setDirty(true);
      if (!clearedRef.current) {
        patchLast((a) => { a.content = `错误: ${err.message ?? "请求失败"}`; });
      }
    }
  };

  const handleClear = () => {
    if (dirty && !window.confirm("当前有未成功保存到历史的消息，清空后将丢失这些消息。仍要清空吗？")) return;
    clearedRef.current = true;
    failedSendRef.current = false;
    setDirty(false);
    setMessages([]);
    setConnectedSession(null);
    localStorage.removeItem(STORAGE_KEY);
  };

  const handleNewChat = () => {
    clearedRef.current = true;
    failedSendRef.current = false;
    loadedSessionRef.current = null;
    setShowSessionPicker(false);
    setDirty(false);
    setConnectedSession(null);
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
      <PageHeader title="对话" description="与 AI 助手对话，支持接入历史会话" />

      {/* Session Picker Modal */}
      {showSessionPicker && (
        <Card className="mb-4">
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

      {/* Native-app style chat: full-bleed, grey thread background, no card box */}
      <div className="-mx-4 flex h-[calc(100dvh-17rem)] flex-col overflow-hidden bg-muted/40 md:mx-0 md:h-[calc(100vh-12rem)] md:rounded-xl md:border md:border-border">
        {/* Header bar */}
        <div className="flex items-center justify-between bg-background px-4 py-2.5 border-b">
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground">{messages.length} 条消息</span>
            {connectedSession && (
              <Badge variant="outline" className="text-[10px] font-mono">
                已接入: {connectedSession.slice(0, 12)}
              </Badge>
            )}
          </div>
          <div className="flex gap-1">
            <Button size="sm" variant="ghost" onClick={handleNewChat}>
              <Plus className="size-3.5 mr-1" /> 新对话
            </Button>
            {!connectedSession && (
              <Button size="sm" variant="ghost" onClick={() => setShowSessionPicker(!showSessionPicker)}>
                <History className="size-3.5 mr-1" /> 接入历史
              </Button>
            )}
            {connectedSession && (
              <Button size="sm" variant="ghost" onClick={handleDisconnect}>
                <X className="size-3.5 mr-1" /> 断开
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={handleClear}>
              <Trash2 className="size-3.5 mr-1" /> 清空
            </Button>
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
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
                    <summary className="cursor-pointer select-none">💭 思考过程</summary>
                    <div className="mt-1 whitespace-pre-wrap border-t border-border/50 pt-1">{m.thinking}</div>
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
                  <span className="whitespace-pre-wrap">{m.content}</span>
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
          <div className="flex w-full min-w-0 items-end gap-1 rounded-[1.4rem] border bg-background py-1 pl-3 pr-1.5 shadow-sm transition-all focus-within:border-primary/50 focus-within:ring-2 focus-within:ring-primary/15">
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
              onClick={handleSend}
              disabled={chatMut.isPending || !input.trim()}
              size="icon"
              className="size-8 shrink-0 rounded-full md:size-9"
              aria-label="发送"
            >
              <Send className="size-4" />
            </Button>
          </div>
          <div className="mt-1 flex items-center gap-1.5">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-xs text-muted-foreground hover:text-foreground"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploadMut.isPending || chatMut.isPending}
            >
              <Paperclip className="mr-1 size-3.5" />
              {uploadMut.isPending ? "上传中…" : "附件"}
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