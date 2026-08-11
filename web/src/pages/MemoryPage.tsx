import { useState, useEffect, useMemo } from "react";
import { Search, Zap, Download, Sparkles, ChevronRight, ChevronDown, Brain, Layers, User, ListChecks } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useMemoryStats, useMemorySearch, useMemoryCompress, useMemorySeed, useMemoryExport, useProfile, useAgentRules } from "@/api/hooks";
import { RoleGuard } from "@/components/auth/RoleGuard";
import { toast } from "sonner";

// Belief lifecycle stage (same derivation as ProfilesPage / honcho.belief_stage).
interface BeliefLike {
  stage?: string;
  confidence?: number;
  reinforcement_count?: number;
}
function computeStage(b: BeliefLike): string {
  if (b.stage) return b.stage;
  const c = b.confidence ?? 0;
  const r = b.reinforcement_count ?? 0;
  if (c >= 0.8 && r >= 5) return "core";
  if (c >= 0.6 && r >= 2) return "established";
  return "draft";
}

const BELIEF_STAGE_NAMES: Record<string, string> = {
  core: "核心",
  established: "已建立",
  draft: "草稿",
};

const BELIEF_STAGE_COLORS: Record<string, string> = {
  core: "bg-chart-1/15 text-chart-1",
  established: "bg-chart-2/15 text-chart-2",
  draft: "bg-muted text-muted-foreground",
};

const RULE_KIND_NAMES: Record<string, string> = {
  behavior: "行为",
  strategy: "策略",
  knowledge_pattern: "知识",
};

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

  // 记忆体系总览数据源：honcho 信念 + evo 规则
  const { data: profile } = useProfile();
  const { data: rulesData } = useAgentRules();

  const beliefs = useMemo(() => {
    const raw = profile?.beliefs ?? [];
    return raw
      .filter((b) => !b.contradicted)
      .map((b) => ({ ...b, stage: computeStage(b) }));
  }, [profile]);
  const beliefsByStage = useMemo(() => {
    const m: Record<string, number> = {};
    for (const b of beliefs) m[b.stage] = (m[b.stage] ?? 0) + 1;
    return m;
  }, [beliefs]);

  const rules = useMemo(() => rulesData?.rules ?? [], [rulesData]);
  const rulesByKind = useMemo(() => {
    const m: Record<string, number> = {};
    for (const r of rules) m[r.kind] = (m[r.kind] ?? 0) + 1;
    return m;
  }, [rules]);

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
    <RoleGuard role="admin">
      <>
      <PageHeader title="记忆系统" description="统一记忆体系：跨会话记忆 + 用户画像 + 行为规则" />

      {/* 记忆体系总览 */}
      <Card className="mb-6">
        <CardHeader className="pb-2">
          <CardTitle className="text-base flex items-center gap-2">
            <Layers className="size-4" /> 记忆体系总览
          </CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          <p className="text-xs text-muted-foreground mb-4">
            统一记忆 = 跨会话记忆（memory.db） + 用户画像（honcho 信念） + 行为规则（evo）
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {/* 跨会话记忆 */}
            <Card className="bg-gradient-to-br from-primary/10 via-primary/5 to-transparent">
              <CardContent className="p-4">
                <div className="flex items-center gap-3">
                  <div className="flex size-10 items-center justify-center rounded-lg bg-primary/10 text-primary shrink-0">
                    <Brain className="size-5" />
                  </div>
                  <div className="min-w-0">
                    <div className="text-2xl font-semibold leading-none">
                      {statsLoading ? "…" : statsError ? "-" : total}
                    </div>
                    <div className="text-xs text-muted-foreground mt-1">跨会话记忆 · memory.db</div>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1.5 mt-3">
                  {Object.entries(byCategory)
                    .sort((a, b) => b[1] - a[1])
                    .slice(0, 5)
                    .map(([cat, n]) => (
                      <Badge key={cat} variant="secondary" className="text-[10px] gap-1">
                        {cat} <span className="text-muted-foreground">{n}</span>
                      </Badge>
                    ))}
                  {!statsLoading && !statsError && Object.keys(byCategory).length === 0 && (
                    <span className="text-[11px] text-muted-foreground">暂无记忆</span>
                  )}
                </div>
              </CardContent>
            </Card>

            {/* 用户画像 · honcho 信念 */}
            <Card>
              <CardContent className="p-4">
                <div className="flex items-center gap-3">
                  <div className="flex size-10 items-center justify-center rounded-lg bg-chart-2/15 text-chart-2 shrink-0">
                    <User className="size-5" />
                  </div>
                  <div className="min-w-0">
                    <div className="text-2xl font-semibold leading-none">{beliefs.length}</div>
                    <div className="text-xs text-muted-foreground mt-1">用户画像 · honcho 信念</div>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1.5 mt-3">
                  {["core", "established", "draft"]
                    .filter((s) => beliefsByStage[s])
                    .map((s) => (
                      <Badge key={s} variant="outline" className={`text-[10px] gap-1 ${BELIEF_STAGE_COLORS[s]}`}>
                        {BELIEF_STAGE_NAMES[s]} <span className="opacity-70">{beliefsByStage[s]}</span>
                      </Badge>
                    ))}
                  {beliefs.length === 0 && <span className="text-[11px] text-muted-foreground">暂无信念</span>}
                </div>
              </CardContent>
            </Card>

            {/* 行为规则 · evo */}
            <Card>
              <CardContent className="p-4">
                <div className="flex items-center gap-3">
                  <div className="flex size-10 items-center justify-center rounded-lg bg-chart-3/15 text-chart-3 shrink-0">
                    <ListChecks className="size-5" />
                  </div>
                  <div className="min-w-0">
                    <div className="text-2xl font-semibold leading-none">{rules.length}</div>
                    <div className="text-xs text-muted-foreground mt-1">行为规则 · evo</div>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1.5 mt-3">
                  {Object.entries(rulesByKind).map(([kind, n]) => (
                    <Badge key={kind} variant="secondary" className="text-[10px] gap-1">
                      {RULE_KIND_NAMES[kind] ?? kind} <span className="text-muted-foreground">{n}</span>
                    </Badge>
                  ))}
                  {rules.length === 0 && <span className="text-[11px] text-muted-foreground">暂无规则</span>}
                </div>
              </CardContent>
            </Card>
          </div>
        </CardContent>
      </Card>

      {/* 跨会话记忆明细 */}
      <div className="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-3">
        跨会话记忆明细（memory.db）
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 mb-6">
        {statsError ? (
          <div className="col-span-full text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
        ) : statsLoading ? (
          Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-20 rounded-lg" />)
        ) : (
          <>
            <Card className="col-span-2 sm:col-span-1 bg-gradient-to-br from-primary/10 via-primary/5 to-transparent">
              <CardContent className="text-center">
                <div className="text-2xl font-bold">{total}</div>
                <div className="text-xs text-muted-foreground">总记忆数</div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="text-center">
                <div className="text-2xl font-bold">{Object.keys(byCategory).length}</div>
                <div className="text-xs text-muted-foreground">分类数</div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="text-center">
                <div className="text-2xl font-bold">{avgImportance.toFixed(2)}</div>
                <div className="text-xs text-muted-foreground">平均重要度</div>
              </CardContent>
            </Card>
            <Card className="col-span-2 sm:col-span-1">
              <CardContent className="flex items-center justify-center">
                <Button size="sm" variant="outline" onClick={handleSeed} disabled={seedMut.isPending}>
                  <Sparkles className="size-4 mr-1" /> 导入种子
                </Button>
              </CardContent>
            </Card>
            <Card className="col-span-2 sm:col-span-1">
              <CardContent className="flex flex-wrap items-center justify-center gap-2">
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
                <div key={r.id} className="bg-muted/50 rounded-lg p-3 min-w-0">
                  <div className="flex items-center gap-2 mb-1 flex-wrap">
                    <Badge variant="outline" className="text-xs">{r.category}</Badge>
                    <span className="text-xs text-muted-foreground">重要度: {r.importance?.toFixed(2)}</span>
                  </div>
                  <p className="text-sm whitespace-pre-wrap break-words">{r.content}</p>
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
                        className="flex items-center gap-2 w-full text-left py-1.5 hover:bg-muted/50 rounded-lg px-2 min-w-0"
                        onClick={() => toggleCat(cat)}
                      >
                        {expandedCats[cat] ? <ChevronDown className="size-4 shrink-0" /> : <ChevronRight className="size-4 shrink-0" />}
                        <Badge variant="secondary" className="shrink-0">{cat}</Badge>
                        <span className="text-xs text-muted-foreground">{items.length} 条</span>
                      </button>
                      {expandedCats[cat] && (
                        <div className="ml-6 space-y-1.5 mt-1">
                          {items.map((m) => (
                            <div key={m.id} className="bg-muted/50 rounded-lg p-3 text-sm min-w-0">
                              <div className="flex items-center gap-2 mb-1 flex-wrap">
                                <span className="text-xs text-muted-foreground">#{m.id}</span>
                                <span className="text-xs text-muted-foreground">重要度: {m.importance?.toFixed(2)}</span>
                                {m.created_at && <span className="text-xs text-muted-foreground">{m.created_at?.slice(0, 10)}</span>}
                              </div>
                              <p className="whitespace-pre-wrap break-words">{m.content}</p>
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
    </RoleGuard>
  );
}
