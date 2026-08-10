import { useState } from "react";
import { FileText } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useLogs } from "@/api/hooks";
import type { LogEntry } from "@/api/client";

type LogSource = "evolution" | "audit" | "gateway";

export default function LogsPage() {
  const [source, setSource] = useState<LogSource>("evolution");
  const { data, isLoading, isError } = useLogs(source);

  const entries = data?.[source] ?? [];

  return (
    <>
      <PageHeader title="日志" description={`${entries.length} 条记录`} />

      <div className="flex gap-2 mb-4">
        {(["evolution", "audit", "gateway"] as LogSource[]).map((s) => (
          <Badge
            key={s}
            variant={source === s ? "default" : "outline"}
            className="cursor-pointer capitalize"
            onClick={() => setSource(s)}
          >
            {s}
          </Badge>
        ))}
      </div>

      {isError ? (
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      ) : isLoading ? (
        <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-10 rounded" />)}</div>
      ) : entries.length === 0 ? (
        <EmptyState title="暂无日志" description={`${source} 事件将在此显示`} icon={<FileText className="size-10" />} />
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="divide-y text-sm">
              {entries.slice().reverse().map((entry: LogEntry, i: number) => {
                const level = entry.error ? "error" : entry.action === "observe" ? "observe" : "info";
                const content = entry.error || entry.content || entry.action || JSON.stringify(entry).slice(0, 120);
                return (
                  <div key={i} className="flex items-start gap-3 px-4 py-2.5">
                    <Badge variant={level === "error" ? "destructive" : "secondary"} className="text-[10px] shrink-0 mt-0.5">
                      {level}
                    </Badge>
                    <div className="flex-1 min-w-0">
                      <div className="text-xs text-muted-foreground mb-0.5">
                        {entry.timestamp ? new Date(entry.timestamp * 1000).toLocaleString() : ""}
                        {entry.stage && <span className="ml-2">[{entry.stage}]</span>}
                      </div>
                      <div className="break-words">{String(content)}</div>
                    </div>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>
      )}
    </>
  );
}