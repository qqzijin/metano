import { useState } from "react";
import { BookOpen, Upload, Search, Trash2 } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useKnowledge, useKnowledgeSearch, useKnowledgeIngest, useKnowledgeDelete } from "@/api/hooks";
import { fmtTime } from "@/api/client";
import { toast } from "sonner";

export default function KnowledgePage() {
  const [searchQuery, setSearchQuery] = useState("");
  const [ingestPath, setIngestPath] = useState("");
  const [showSearch, setShowSearch] = useState(false);
  const { data, isLoading, isError } = useKnowledge();
  const searchMut = useKnowledgeSearch();
  const ingestMut = useKnowledgeIngest();
  const deleteMut = useKnowledgeDelete();

  const docs = data?.documents ?? [];

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
    searchMut.mutate(searchQuery);
  };

  return (
    <>
      <PageHeader title="知识库" description={`${docs.length} 份文档`} />

      <div className="flex gap-3 mb-4 flex-wrap">
        <div className="flex gap-2 flex-1 min-w-[200px]">
          <Input placeholder="输入文件路径或 URL 导入..." value={ingestPath} onChange={(e) => setIngestPath(e.target.value)} />
          <Button onClick={handleIngest} disabled={!ingestPath.trim() || ingestMut.isPending} size="sm">
            <Upload className="size-4 mr-1" /> 导入
          </Button>
        </div>
        <div className="flex gap-2 flex-1 min-w-[200px]">
          <Input placeholder="搜索知识库..." value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && handleSearch()} />
          <Button onClick={handleSearch} disabled={!searchQuery.trim()} variant="outline" size="sm">
            <Search className="size-4 mr-1" /> 搜索
          </Button>
        </div>
      </div>

      {showSearch && searchMut.data && (
        <Card className="mb-4">
          <CardContent className="p-4">
            <div className="text-sm font-medium mb-2">搜索结果</div>
            {(searchMut.data.results ?? []).length === 0 ? (
              <p className="text-sm text-muted-foreground">未找到结果</p>
            ) : (
              <div className="space-y-2">
                {searchMut.data.results.map((r, i) => (
                  <div key={i} className="text-sm border-l-2 border-primary pl-3">
                    <div className="font-medium text-xs text-muted-foreground">{r.title} (相关度: {r.score?.toFixed(2) ?? "N/A"})</div>
                    <div className="line-clamp-2">{r.content}</div>
                  </div>
                ))}
              </div>
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
                  <BookOpen className="size-4 text-muted-foreground shrink-0" />
                  <div className="min-w-0 flex-1">
                    <div className="font-medium text-sm truncate">{title}</div>
                    <div className="text-xs text-muted-foreground flex gap-3 mt-0.5 min-w-0">
                      {d.source && <span className="truncate min-w-0">{d.source}</span>}
                      {chunks != null && <span className="shrink-0">{chunks} 分块</span>}
                      {updatedAt != null && <span className="shrink-0">{fmtTime(updatedAt)}</span>}
                    </div>
                  </div>
                  {id && (
                    <Button variant="ghost" size="icon" className="size-8 text-muted-foreground hover:text-destructive" onClick={() => handleDelete(id)}>
                      <Trash2 className="size-4" />
                    </Button>
                  )}
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </>
  );
}