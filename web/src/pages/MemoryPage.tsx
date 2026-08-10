import { useState, useEffect } from "react";
import { Search, Zap, Download, Sparkles, ChevronRight, ChevronDown } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useMemoryStats, useMemorySearch, useMemoryCompress, useMemorySeed, useMemoryExport } from "@/api/hooks";
import { toast } from "sonner";

export default function MemoryPage() {
  const [query, setQuery] = useState("");
  const [allMemories, setAllMemories] = useState<any[]>([]);
  const [loadingAll, setLoadingAll] = useState(true);
  const [expandedCats, setExpandedCats] = useState<Record<string, boolean>>({});
  const { data: stats, isLoading: statsLoading, isError: statsError, refetch } = useMemoryStats();
  const { data: searchResult, isLoading: searchLoading, isError: searchError } = useMemorySearch(query);
  const compressMut = useMemoryCompress();
  const seedMut = useMemorySeed();
  const exportMut = useMemoryExport();

  const searchResults = searchResult?.results ?? [];
  const memStats = stats as Record<string, unknown> | undefined;
  const total = (memStats?.total_memories as number) ?? 0;
  const byCategory = (memStats?.by_category as Record<string, number>) ?? {};
  const avgImportance = (memStats?.avg_importance as number) ?? 0;

  // Fetch all memories on mount
  useEffect(() => {
    setLoadingAll(true);
    fetch("/api/memory/export")
      .then((r) => r.json())
      .then((d) => {
        setAllMemories(d.memories ?? []);
      })
      .catch(() => setAllMemories([]))
      .finally(() => setLoadingAll(false));
  }, [total]);

  const handleSeed = async () => {
    const result = await seedMut.mutateAsync(undefined as any);
    toast.success(`导入种子数据: ${(result as any)?.imported ?? 0} 条`);
    refetch();
  };

  const handleExport = async () => {
    const result = await exportMut.mutateAsync(undefined as any);
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `memory-export-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast.success("导出完成");
  };

  const toggleCat = (cat: string) => setExpandedCats((prev) => ({ ...prev, [cat]: !prev[cat] }));

  // Group memories by category
  const grouped: Record<string, any[]> = {};
  for (const m of allMemories) {
    const cat = m.category || "general";
    if (!grouped[cat]) grouped[cat] = [];
    grouped[cat].push(m);
  }

  return (
    <>
      <PageHeader title="记忆系统" description="跨会话持久记忆，语义压缩存储" />

      {/* Stats */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 mb-6">
        {statsError ? (
          <div className="col-span-full text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
        ) : statsLoading ? (
          Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-20 rounded-lg" />)
        ) : (
          <>
            <Card>
              <CardContent className="p-4 text-center">
                <div className="text-2xl font-bold">{total}</div>
                <div className="text-xs text-muted-foreground">总记忆数</div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4 text-center">
                <div className="text-2xl font-bold">{Object.keys(byCategory).length}</div>
                <div className="text-xs text-muted-foreground">分类数</div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4 text-center">
                <div className="text-2xl font-bold">{avgImportance.toFixed(2)}</div>
                <div className="text-xs text-muted-foreground">平均重要度</div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4 flex items-center justify-center">
                <Button size="sm" variant="outline" onClick={handleSeed} disabled={seedMut.isPending}>
                  <Sparkles className="size-4 mr-1" /> 导入种子
                </Button>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4 flex items-center justify-center gap-2">
                <Button size="sm" variant="outline" onClick={handleExport} disabled={exportMut.isPending}>
                  <Download className="size-4 mr-1" /> 导出
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={async () => {
                    try {
                      await compressMut.mutateAsync(undefined as any);
                      toast.success("压缩完成");
                    } catch {
                      toast.error("压缩失败");
                    }
                  }}
                  disabled={compressMut.isPending}
                >
                  <Zap className="size-4 mr-1" /> 压缩
                </Button>
              </CardContent>
            </Card>
          </>
        )}
      </div>

      {/* Search */}
      <Card className="mb-4">
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2">
            <Search className="size-4" /> 搜索记忆
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Input
            placeholder="搜索记忆内容..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="flex-1"
          />
          {query && searchLoading && <Skeleton className="h-20 mt-3 rounded-md" />}
          {query && searchError && (
            <p className="text-sm text-destructive mt-3">加载失败，请检查服务或刷新重试</p>
          )}
          {query && !searchLoading && searchResults.length === 0 && (
            <p className="text-sm text-muted-foreground mt-3">未找到相关记忆</p>
          )}
          {query && searchResults.length > 0 && (
            <div className="space-y-2 mt-3">
              {searchResults.map((r: any) => (
                <div key={r.id} className="bg-muted rounded-md p-3">
                  <div className="flex items-center gap-2 mb-1">
                    <Badge variant="outline" className="text-[10px]">{r.category}</Badge>
                    <span className="text-xs text-muted-foreground">重要度: {r.importance?.toFixed(2)}</span>
                  </div>
                  <p className="text-sm whitespace-pre-wrap">{r.content}</p>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* All Memories by Category */}
      {!query && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">全部记忆</CardTitle>
          </CardHeader>
          <CardContent>
            {loadingAll ? (
              <div className="space-y-3">
                {Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-16 rounded-md" />)}
              </div>
            ) : allMemories.length === 0 ? (
              <p className="text-sm text-muted-foreground">暂无记忆数据。点击"导入种子"从Claude Code记忆和知识库导入。</p>
            ) : (
              <div className="space-y-2">
                {Object.entries(grouped)
                  .sort(([a], [b]) => (grouped[b].length - grouped[a].length))
                  .map(([cat, items]) => (
                    <div key={cat}>
                      <button
                        className="flex items-center gap-2 w-full text-left py-1.5 hover:bg-muted/50 rounded px-2"
                        onClick={() => toggleCat(cat)}
                      >
                        {expandedCats[cat] ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
                        <Badge variant="secondary">{cat}</Badge>
                        <span className="text-xs text-muted-foreground">{items.length} 条</span>
                      </button>
                      {expandedCats[cat] && (
                        <div className="ml-6 space-y-1.5 mt-1">
                          {items.map((m) => (
                            <div key={m.id} className="bg-muted/50 rounded-md p-3 text-sm">
                              <div className="flex items-center gap-2 mb-1">
                                <span className="text-[10px] text-muted-foreground">#{m.id}</span>
                                <span className="text-[10px] text-muted-foreground">重要度: {m.importance?.toFixed(2)}</span>
                                {m.created_at && <span className="text-[10px] text-muted-foreground">{m.created_at?.slice(0, 10)}</span>}
                              </div>
                              <p className="whitespace-pre-wrap">{m.content}</p>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </>
  );
}