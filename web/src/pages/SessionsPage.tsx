import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ArrowLeft, MessageSquare, Cpu, Coins, Clock } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { SearchInput } from "@/components/shared/SearchInput";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useSessions, useMessages } from "@/api/hooks";
import { fmtTokens, fmtCost, fmtTime } from "@/api/client";

export default function SessionsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<string | null>(searchParams.get("session"));
  const { data, isLoading, isError } = useSessions(search);
  const { data: msgData, isLoading: msgLoading, isError: msgError } = useMessages(selected ?? "");

  const sessions = data?.sessions ?? [];
  const messages = msgData?.messages ?? [];

  if (selected) {
    const session = sessions.find((s) => s.id === selected);
    return (
      <>
        <div className="flex items-center gap-3 mb-6">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => {
              setSelected(null);
              setSearchParams({}, { replace: true });
            }}
          >
            <ArrowLeft className="size-4" />
          </Button>
          <div>
            <h1 className="text-xl font-semibold">{session?.title || selected.slice(0, 12) + "..."}</h1>
            <div className="flex gap-3 text-xs text-muted-foreground mt-1">
              {session?.model && <span className="flex items-center gap-1"><Cpu className="size-3" />{session.model.split("-").slice(0, 2).join("-")}</span>}
              {session?.started_at && <span className="flex items-center gap-1"><Clock className="size-3" />{fmtTime(session.started_at)}</span>}
              {(session?.estimated_cost_usd ?? 0) > 0 && <span className="flex items-center gap-1"><Coins className="size-3" />{fmtCost(session?.estimated_cost_usd ?? 0)}</span>}
            </div>
          </div>
        </div>

        <div className="space-y-3">
          {msgError ? (
            <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
          ) : msgLoading ? (
            Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-16 rounded-lg" />)
          ) : messages.map((m) => (
            <Card key={m.id} className="p-4">
              <div className="flex items-center gap-2 mb-2">
                <Badge variant={m.role === "user" ? "default" : "secondary"} className="text-xs">
                  {m.role}
                </Badge>
                {m.tool_name && <Badge variant="outline" className="text-xs">工具: {m.tool_name}</Badge>}
              </div>
              <div className="text-sm whitespace-pre-wrap">{m.content}</div>
              {((m.input_tokens ?? 0) > 0 || (m.output_tokens ?? 0) > 0) && (
                <div className="text-xs text-muted-foreground mt-2">
                  入: {fmtTokens(m.input_tokens ?? 0)} 出: {fmtTokens(m.output_tokens ?? 0)}
                </div>
              )}
            </Card>
          ))}
        </div>
      </>
    );
  }

  return (
    <>
      <PageHeader title="会话" description="对话历史记录" />
      <SearchInput value={search} onChange={setSearch} placeholder="搜索会话..." className="max-w-sm mb-4" />

      {isError ? (
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      ) : isLoading ? (
        <div className="space-y-3">{Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-14 rounded-lg" />)}</div>
      ) : sessions.length === 0 ? (
        <EmptyState title="暂无会话" description="从聊天页面开始一段对话" />
      ) : (
        <div className="grid gap-3">
          {sessions.map((s) => (
            <Card
              key={s.id}
              className="p-4 hover:shadow-sm transition-shadow cursor-pointer"
              onClick={() => {
                setSelected(s.id);
                setSearchParams({ session: s.id }, { replace: true });
              }}
            >
              <div className="flex items-center gap-3">
                <MessageSquare className="size-4 text-muted-foreground shrink-0" />
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-sm truncate">{s.title || s.id}</div>
                  <div className="text-xs text-muted-foreground flex flex-wrap gap-x-3 gap-y-1 mt-0.5">
                    {s.model && <span className="truncate min-w-0">{s.model}</span>}
                    <span className="shrink-0">{s.message_count ?? 0} 条消息</span>
                    {(s.input_tokens || s.output_tokens) && <span className="shrink-0">{fmtTokens((s.input_tokens ?? 0) + (s.output_tokens ?? 0))} 令牌</span>}
                    {s.estimated_cost_usd != null && s.estimated_cost_usd > 0 && <span className="shrink-0">{fmtCost(s.estimated_cost_usd)}</span>}
                  </div>
                </div>
                {s.started_at && <Badge variant="secondary" className="text-xs shrink-0">{fmtTime(s.started_at)}</Badge>}
              </div>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}