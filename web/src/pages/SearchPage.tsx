import { useState } from "react";
import DOMPurify from "dompurify";
import { useNavigate } from "react-router-dom";
import { Search, FileText, BookOpen, Globe } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { useSearch, useKnowledgeSearch, useBrowserSearch } from "@/api/hooks";
import { fmtTime } from "@/api/client";

const PAGE_SIZE = 20;

/**
 * 统一搜索中心：会话全文 / 知识库 / 网页 三个入口合并到一个页面。
 * 此前三处各有搜索框（SearchPage 会话、KnowledgePage 知识库、BrowserPage 网页），
 * 统一后从一个入口完成全部搜索。
 */
export default function SearchPage() {
  const navigate = useNavigate();
  const [tab, setTab] = useState("chat");
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState("");
  const [limit, setLimit] = useState(PAGE_SIZE);
  const { data, isLoading, isError } = useSearch(searched, limit);
  const kbMut = useKnowledgeSearch();
  const webMut = useBrowserSearch();
  const results = data?.results ?? [];
  const total = data?.total ?? 0;
  const hasMore = total > results.length;

  const doSearch = () => {
    if (!query.trim()) return;
    const q = query.trim();
    setSearched(q);
    setLimit(PAGE_SIZE);
    if (tab === "knowledge") kbMut.mutate(q);
    else if (tab === "web") webMut.mutate(q);
  };

  const loadMore = () => setLimit((n) => n + PAGE_SIZE);

  const pending = (tab === "knowledge" && kbMut.isPending) || (tab === "web" && webMut.isPending);

  return (
    <>
      <PageHeader title="搜索" description="统一搜索：会话 · 知识库 · 网页" />
      <div className="flex gap-2 mb-4">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 size-4 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && doSearch()}
            placeholder="搜索对话 / 知识库 / 网页..."
            className="pl-9"
          />
        </div>
        <Button onClick={doSearch} disabled={!query.trim() || pending}>
          {pending ? "搜索中..." : "搜索"}
        </Button>
      </div>

      <Tabs value={tab} onValueChange={(v) => { setTab(v); if (v !== "chat") setSearched(""); }}>
        <TabsList className="mb-4">
          <TabsTrigger value="chat"><FileText className="size-3.5 mr-1" />会话</TabsTrigger>
          <TabsTrigger value="knowledge"><BookOpen className="size-3.5 mr-1" />知识库</TabsTrigger>
          <TabsTrigger value="web"><Globe className="size-3.5 mr-1" />网页</TabsTrigger>
        </TabsList>

        {/* ── 会话全文搜索 ── */}
        <TabsContent value="chat">
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
        </TabsContent>

        {/* ── 知识库搜索 ── */}
        <TabsContent value="knowledge">
          {kbMut.isError ? (
            <div className="text-sm text-destructive">知识库检索失败: {kbMut.error?.message || "请检查服务后重试"}</div>
          ) : kbMut.isPending ? (
            <div className="space-y-3">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-20 rounded-lg" />)}</div>
          ) : kbMut.data && (kbMut.data.results ?? []).length === 0 ? (
            <EmptyState title="未找到结果" description="请尝试不同的关键词" icon={<BookOpen className="size-10" />} />
          ) : !kbMut.data ? (
            <EmptyState title="搜索知识库" description="输入关键词检索已导入的文档分块" icon={<BookOpen className="size-10" />} />
          ) : (
            <div className="grid gap-3">
              {kbMut.data.results.map((r, i) => (
                <Card key={i} className="p-4">
                  <div className="text-sm font-medium mb-1 truncate">{r.title}</div>
                  <div className="text-sm text-muted-foreground line-clamp-3 break-words">{r.content}</div>
                  {r.score != null && (
                    <div className="text-xs text-muted-foreground mt-2">相关度: {r.score.toFixed(2)}</div>
                  )}
                </Card>
              ))}
            </div>
          )}
        </TabsContent>

        {/* ── 网页搜索 ── */}
        <TabsContent value="web">
          {webMut.isError ? (
            <div className="text-sm text-destructive">网页搜索失败: {webMut.error?.message || "请检查服务后重试"}</div>
          ) : webMut.isPending ? (
            <div className="space-y-3">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-20 rounded-lg" />)}</div>
          ) : webMut.data && (webMut.data.results ?? []).length === 0 ? (
            <EmptyState title="未找到结果" description="请尝试不同的关键词" icon={<Globe className="size-10" />} />
          ) : !webMut.data ? (
            <EmptyState title="搜索网页" description="输入关键词搜索网页内容" icon={<Globe className="size-10" />} />
          ) : (
            <div className="grid gap-3">
              {webMut.data.results.map((r, i) => (
                <Card key={i} className="p-4">
                  <a href={r.url} target="_blank" rel="noopener noreferrer" className="text-sm font-medium text-primary hover:underline break-words">{r.title}</a>
                  <p className="text-xs text-muted-foreground mt-1 line-clamp-2 break-words">{r.snippet}</p>
                  <p className="text-[10px] text-muted-foreground mt-1 truncate">{r.url}</p>
                </Card>
              ))}
            </div>
          )}
        </TabsContent>
      </Tabs>
    </>
  );
}
