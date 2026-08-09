import { useState, useRef, useEffect } from "react";
import { Send, Bot, User, Trash2, History, X } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useChatMutation, useSessions, useMessages } from "@/api/hooks";
import { fmtTime } from "@/api/client";

interface ChatMsg {
  role: "user" | "assistant" | "system";
  content: string;
  ts: number;
}

const STORAGE_KEY = "cc-hermes-chat-history";

function loadHistory(): ChatMsg[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch {}
  return [];
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
  const bottomRef = useRef<HTMLDivElement>(null);
  const chatMut = useChatMutation();
  const { data: sessionsData } = useSessions("", 20);
  const { data: msgData, isLoading: msgLoading } = useMessages(connectedSession ?? "");

  const sessions = sessionsData?.sessions ?? [];

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    saveHistory(messages);
  }, [messages]);

  const handleConnectSession = (sessionId: string) => {
    setShowSessionPicker(false);
    setConnectedSession(sessionId);
    // When messages data arrives, merge them into chat
  };

  // Load session messages when data arrives
  useEffect(() => {
    if (connectedSession && msgData && !msgLoading) {
      const sessionMsgs = msgData.messages.map((m: any) => ({
        role: m.role as "user" | "assistant" | "system",
        content: m.content ?? "",
        ts: typeof m.timestamp === "number" ? m.timestamp * 1000 : Date.now(),
      }));
      setMessages(sessionMsgs);
    }
  }, [connectedSession, msgData, msgLoading]);

  const handleDisconnect = () => {
    setConnectedSession(null);
    setMessages([]);
    localStorage.removeItem(STORAGE_KEY);
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text || chatMut.isPending) return;

    const userMsg: ChatMsg = { role: "user", content: text, ts: Date.now() };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");

    try {
      const res = await chatMut.mutateAsync({
        message: text,
        user_id: "web_user",
        session_id: connectedSession || undefined,
        context: messages.slice(-10).map((m) => ({ role: m.role, content: m.content })),
      });
      const content = typeof res === "string" ? res : res?.response ?? JSON.stringify(res);
      setMessages((prev) => [...prev, { role: "assistant", content, ts: Date.now() }]);
    } catch (err: any) {
      setMessages((prev) => [...prev, { role: "assistant", content: `错误: ${err.message ?? "请求失败"}`, ts: Date.now() }]);
    }
  };

  const handleClear = () => {
    setMessages([]);
    setConnectedSession(null);
    localStorage.removeItem(STORAGE_KEY);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
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
            {sessions.length === 0 ? (
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

      <Card className="flex flex-col h-[calc(100vh-12rem)]">
        {/* Header bar */}
        <div className="flex items-center justify-between px-4 pt-3 pb-1 border-b">
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground">{messages.length} 条消息</span>
            {connectedSession && (
              <Badge variant="outline" className="text-[10px] font-mono">
                已接入: {connectedSession.slice(0, 12)}
              </Badge>
            )}
          </div>
          <div className="flex gap-1">
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
          {messages.map((m, i) => (
            <div key={i} className={`flex gap-3 ${m.role === "user" ? "justify-end" : ""}`}>
              {m.role === "assistant" && (
                <div className="size-8 rounded-full bg-primary/10 flex items-center justify-center shrink-0">
                  <Bot className="size-4 text-primary" />
                </div>
              )}
              <div
                className={`rounded-xl px-4 py-2.5 max-w-[80%] whitespace-pre-wrap text-sm ${
                  m.role === "user" ? "bg-primary text-primary-foreground" : "bg-muted"
                }`}
              >
                {m.content}
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

        {/* Input */}
        <div className="p-3 border-t flex gap-2">
          <Input
            placeholder="输入消息..."
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={chatMut.isPending}
          />
          <Button onClick={handleSend} disabled={chatMut.isPending || !input.trim()}>
            <Send className="size-4" />
          </Button>
        </div>
      </Card>
    </>
  );
}