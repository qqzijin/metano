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

/**
 * 将日志条目的 detail/details 字段格式化为可读 JSON 文本。
 * - 值为对象 → 直接 JSON.stringify(detail, null, 2)
 * - 值为字符串 → 尝试 JSON.parse，成功且为对象则格式化；否则原样返回字符串
 * - 值为空/undefined → 返回 null（不渲染详情区）
 */
function parseDetailText(value: unknown): string | null {
  if (value == null) return null;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return null;
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed && typeof parsed === "object") {
        return JSON.stringify(parsed, null, 2);
      }
      return trimmed;
    } catch {
      return trimmed;
    }
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

export default function LogsPage() {
  const [source, setSource] = useState<LogSource>("evolution");
  const { data, isLoading, isError } = useLogs(source);

  const entries = data?.[source] ?? [];

  return (
    <>
      <PageHeader title="日志" description={`${entries.length} 条记录`} />

      <div className="flex flex-wrap gap-2 mb-4">
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
        <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-10 rounded-lg" />)}</div>
      ) : entries.length === 0 ? (
        <EmptyState title="暂无日志" description={`${source} 事件将在此显示`} icon={<FileText className="size-10" />} />
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="divide-y text-sm">
              {entries.slice().reverse().map((entry: LogEntry, i: number) => {
                const level = entry.error ? "error" : entry.action === "observe" ? "observe" : "info";
                const content = entry.error || entry.content || entry.action || JSON.stringify(entry).slice(0, 120);
                // evolution 源用 detail（JSON 字符串），audit/gateway 源用 details（JSON 对象）
                const detailText = parseDetailText(entry.details ?? entry.detail);
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
                      {detailText && (
                        <details className="mt-1.5">
                          <summary className="text-xs text-muted-foreground/80 cursor-pointer select-none hover:text-muted-foreground">
                            详情
                          </summary>
                          <pre className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground bg-muted/50 rounded p-2 whitespace-pre-wrap break-words">
                            {detailText}
                          </pre>
                        </details>
                      )}
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