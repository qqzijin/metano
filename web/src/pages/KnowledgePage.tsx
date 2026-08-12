import { useState } from "react";
import { BookOpen, Upload, Search, Trash2, Loader2, Eye, Sparkles } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Markdown } from "@/components/Markdown";
import { useKnowledge, useKnowledgeSearch, useKnowledgeSemanticSearch, useKnowledgeIngest, useKnowledgeDelete } from "@/api/hooks";
import { fmtTime } from "@/api/client";
import { toast } from "sonner";

export default function KnowledgePage() {
  const [searchQuery, setSearchQuery] = useState("");
  const [ingestPath, setIngestPath] = useState("");
  const [showSearch, setShowSearch] = useState(false);
  const [viewDoc, setViewDoc] = useState<{ title: string; content: string; doc_type?: string } | null>(null);
  const [viewLoading, setViewLoading] = useState(false);
  const { data, isLoading, isError } = useKnowledge();
  const searchMut = useKnowledgeSearch();
  const semanticMut = useKnowledgeSemanticSearch();
  const ingestMut = useKnowledgeIngest();
  const deleteMut = useKnowledgeDelete();

  const docs = data?.documents ?? [];

  // View a knowledge document's full content (the viewer that was missing).
  const handleView = async (id: string, title: string) => {
    if (!id) return;
    setViewLoading(true);
    try {
      const res = await fetch(`/api/knowledge/${id}`, { credentials: "include" });
      if (res.status === 401) { window.dispatchEvent(new Event("auth:unauthorized")); return; }
      const d = await res.json();
      if (d.error) { toast.error(d.error?.message || "加载失败"); return; }
      setViewDoc({ title: d.title || title, content: d.content || "(无内容)", doc_type: d.doc_type });
    } catch {
      toast.error("加载文档内容失败");
    } finally {
      setViewLoading(false);
    }
  };

  const handleIngest = async () => {
    if (!ingestPath.trim()) return;
    try {
      await ingestMut.mutateAsync({ path: ingestPath });
      toast.success("文档已导入");
      setIngestPath("");
    } catch (e) {
      toast.error("导入失败");
    }
  };

  const handleDelete = async (docId: string) => {
    if (!window.confirm("确定删除该文档？")) return;
    try {
      await deleteMut.mutateAsync(docId);
      toast.success("文档已删除");
    } catch {
      toast.error("删除失败");
    }
  };

  const handleSearch = async () => {
    if (!searchQuery.trim()) return;
    setShowSearch(true);
    try {
      await searchMut.mutateAsync(searchQuery);
    } catch {
      toast.error("搜索失败");
    }
  };

  const handleSemanticSearch = async () => {
    if (!searchQuery.trim()) return;
    setShowSearch(true);
    try {
      await semanticMut.mutateAsync(searchQuery);
    } catch {
      toast.error("语义搜索失败");
    }
  };

  return (
    <>
      <PageHeader title="知识库" description={`${docs.length} 份文档`} />

      <div className="flex flex-col gap-3 mb-4 lg:flex-row">
        <div className="flex gap-2 flex-1 min-w-0">
          <Input placeholder="输入文件路径或 URL 导入..." value={ingestPath} onChange={(e) => setIngestPath(e.target.value)} className="flex-1 min-w-0" />
          <Button onClick={handleIngest} disabled={!ingestPath.trim() || ingestMut.isPending} size="sm" className="shrink-0">
            <Upload className="size-4 mr-1" /> 导入
          </Button>
        </div>
        <div className="flex gap-2 flex-1 min-w-0">
          <Input placeholder="搜索知识库..." value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && handleSearch()} className="flex-1 min-w-0" />
          <Button onClick={handleSearch} disabled={!searchQuery.trim() || searchMut.isPending} variant="outline" size="sm" className="shrink-0">
            {searchMut.isPending ? <Loader2 className="size-4 mr-1 animate-spin" /> : <Search className="size-4 mr-1" />}
            {searchMut.isPending ? "搜索中..." : "搜索"}
          </Button>
          <Button onClick={handleSemanticSearch} disabled={!searchQuery.trim() || semanticMut.isPending} variant="outline" size="sm" className="shrink-0" title="CocoIndex 代码语义搜索">
            {semanticMut.isPending ? <Loader2 className="size-4 mr-1 animate-spin" /> : <Sparkles className="size-4 mr-1" />}
            {semanticMut.isPending ? "语义搜索中..." : "语义搜索"}
          </Button>
        </div>
      </div>

      {showSearch && searchMut.isPending && (
        <Card className="mb-4">
          <CardContent className="p-4 flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> 搜索中...
          </CardContent>
        </Card>
      )}

      {showSearch && searchMut.isError && (
        <Card className="mb-4">
          <CardContent className="p-4">
            <div className="text-sm text-destructive">搜索失败，请检查服务后重试</div>
          </CardContent>
        </Card>
      )}

      {showSearch && !searchMut.isPending && !searchMut.isError && searchMut.data && (
        <Card className="mb-4">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">搜索结果</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 pt-0">
            {(searchMut.data.results ?? []).length === 0 ? (
              <p className="text-sm text-muted-foreground">未找到结果</p>
            ) : (
              <>
                {searchMut.data.results.map((r, i) => (
                  <div key={i} className="text-sm border-l-2 border-primary pl-3">
                    <div className="font-medium text-xs text-muted-foreground">{r.title} (相关度: {r.score?.toFixed(2) ?? "N/A"})</div>
                    <div className="line-clamp-2 break-words">{r.content}</div>
                  </div>
                ))}
              </>
            )}
          </CardContent>
        </Card>
      )}

      {/* 语义搜索结果（CocoIndex 代码库） */}
      {semanticMut.data && !semanticMut.isPending && (
        <Card className="mb-4">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">语义搜索结果（代码库）</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 pt-0">
            {(semanticMut.data.results ?? []).length === 0 ? (
              <p className="text-sm text-muted-foreground">未找到语义相关代码</p>
            ) : (
              (semanticMut.data.results ?? []).map((r, i) => (
                <div key={i} className="text-sm border-l-2 border-chart-4 pl-3">
                  <div className="font-medium text-xs text-muted-foreground">{r.file} (相关度: {r.score?.toFixed(2) ?? "N/A"})</div>
                  <div className="line-clamp-2 break-words">{r.content}</div>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      )}

      {isError ? (
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      ) : isLoading ? (
        <div className="space-y-3">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-14 rounded-lg" />)}</div>
      ) : docs.length === 0 ? (
        <EmptyState title="暂无文档" description="导入文件以启用 RAG 检索" icon={<BookOpen className="size-10" />} />
      ) : (
        <div className="grid gap-3">
          {docs.map((d, i) => {
            const id = (d as any).doc_id ?? (d as any).id;
            const title = d.title || id || `文档 ${i + 1}`;
            const chunks = (d as any).chunk_count ?? (d as any).chunks;
            const updatedAt = (d as any).updated_at ?? (d as any).indexed_at;
            return (
              <Card key={id ?? i} className="p-4">
                <div className="flex items-center gap-3">
                  <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                    <BookOpen className="size-4" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="font-medium text-sm truncate">{title}</div>
                    <div className="text-xs text-muted-foreground flex flex-wrap gap-x-3 gap-y-0.5 mt-0.5 min-w-0">
                      {d.source && <span className="truncate min-w-0">{d.source}</span>}
                      {chunks != null && <span className="shrink-0">{chunks} 分块</span>}
                      {updatedAt != null && <span className="shrink-0">{fmtTime(updatedAt)}</span>}
                    </div>
                  </div>
                  {id && (
                    <div className="flex items-center gap-1 shrink-0">
                      <Button variant="ghost" size="sm" className="size-8 px-2 text-muted-foreground hover:text-primary" onClick={() => handleView(id, title)} title="查看文档内容">
                        <Eye className="size-4" />
                        <span className="ml-1 text-xs">查看</span>
                      </Button>
                      <Button variant="ghost" size="icon" className="size-8 text-muted-foreground hover:text-destructive" onClick={() => handleDelete(id)}>
                        <Trash2 className="size-4" />
                      </Button>
                    </div>
                  )}
                </div>
              </Card>
            );
          })}
        </div>
      )}

      <Dialog open={!!viewDoc || viewLoading} onOpenChange={(o) => { if (!o) setViewDoc(null); }}>
        <DialogContent className="sm:max-w-2xl max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle>{viewDoc?.title ?? "加载中…"}</DialogTitle>
          </DialogHeader>
          {viewLoading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground py-8 justify-center">
              <Loader2 className="size-4 animate-spin" /> 加载文档内容…
            </div>
          ) : viewDoc ? (
            viewDoc.doc_type === "markdown" ? (
              <div className="overflow-y-auto text-sm">
                <Markdown>{viewDoc.content}</Markdown>
              </div>
            ) : (
              <div className="overflow-y-auto text-sm whitespace-pre-wrap break-words">
                {viewDoc.content}
              </div>
            )
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
