import { useState } from "react";
import DOMPurify from "dompurify";
import { useNavigate } from "react-router-dom";
import { Search, FileText } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useSearch } from "@/api/hooks";
import { fmtTime } from "@/api/client";

const PAGE_SIZE = 20;

export default function SearchPage() {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState("");
  const [limit, setLimit] = useState(PAGE_SIZE);
  const { data, isLoading, isError } = useSearch(searched, limit);
  const results = data?.results ?? [];
  const total = data?.total ?? 0;
  const hasMore = total > results.length;

  const doSearch = () => {
    if (query.trim()) {
      setSearched(query.trim());
      setLimit(PAGE_SIZE);
    }
  };

  const loadMore = () => setLimit((n) => n + PAGE_SIZE);

  return (
    <>
      <PageHeader title="搜索" description="全文搜索会话内容" />
      <div className="flex gap-2 mb-4">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 size-4 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && doSearch()}
            placeholder="搜索对话..."
            className="pl-9"
          />
        </div>
        <Button onClick={doSearch} disabled={!query.trim()}>搜索</Button>
      </div>

      {isError ? (
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      ) : isLoading ? (
        <div className="space-y-3">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-20 rounded-lg" />)}</div>
      ) : searched && results.length === 0 ? (
        <EmptyState title="未找到结果" description="请尝试不同的关键词" />
      ) : !searched ? (
        <EmptyState title="搜索会话" description="输入关键词以搜索所有对话" icon={<FileText className="size-10" />} />
      ) : (
        <>
          <div className="text-xs text-muted-foreground mb-2">共 {total} 条结果</div>
          <div className="grid gap-3">
            {results.map((r, i) => (
              <Card
                key={i}
                role="link"
                tabIndex={0}
                className="p-4 cursor-pointer transition-shadow hover:shadow-sm"
                onClick={() => navigate(`/sessions?session=${encodeURIComponent(r.session_id)}`)}
                onKeyDown={(e) => { if (e.key === "Enter") navigate(`/sessions?session=${encodeURIComponent(r.session_id)}`); }}
              >
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mb-2">
                  <Badge variant="outline" className="text-[10px] font-mono">{r.session_id.slice(0, 12)}</Badge>
                  {r.title && <span className="text-sm font-medium truncate min-w-0 flex-1">{r.title}</span>}
                  <Badge variant={r.role === "user" ? "default" : "secondary"} className="text-[10px] ml-auto">{r.role}</Badge>
                  {r.timestamp && <span className="text-xs text-muted-foreground shrink-0">{fmtTime(r.timestamp)}</span>}
                </div>
                <div className="text-sm text-muted-foreground line-clamp-3 break-words" dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(r.snippet) }} />
              </Card>
            ))}
          </div>
          {hasMore && (
            <div className="flex justify-center mt-4">
              <Button variant="outline" onClick={loadMore} disabled={isLoading}>
                {isLoading ? "加载中..." : "加载更多"}
              </Button>
            </div>
          )}
        </>
      )}
    </>
  );
}