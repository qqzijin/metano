import { useMemo, useState } from "react";
import { Boxes, Share2, RefreshCw, Search, Network, Loader2 } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import {
  useGraphStats, useGraphQuery, useGraphExtract,
} from "@/api/hooks";
import type { GraphEntity, GraphRelationship } from "@/api/client";
import { useAuth } from "@/contexts/AuthContext";
import { toast } from "sonner";

const ENTITY_TYPES = ["technology", "concept", "module", "file"];

const ENTITY_TYPE_LABELS: Record<string, string> = {
  technology: "技术",
  concept: "概念",
  module: "模块",
  file: "文件",
};

// Theme-aware tinted chips — resolve against chart/indigo tokens so they
// follow the active light/dark theme automatically.
const ENTITY_TYPE_COLORS: Record<string, string> = {
  technology: "bg-chart-1/15 text-chart-1",
  concept: "bg-chart-2/15 text-chart-2",
  module: "bg-chart-4/15 text-chart-4",
  file: "bg-chart-3/15 text-chart-3",
};

const REL_TYPE_LABELS: Record<string, string> = {
  related_to: "相关",
  imports: "导入",
  implements: "实现",
  implemented_by: "被实现",
  co_occurs_with: "共现",
};

function entityBadgeClass(type: string): string {
  return ENTITY_TYPE_COLORS[type] ?? "bg-muted text-muted-foreground";
}

export default function KnowledgeGraphPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  const [search, setSearch] = useState("");
  const [entityType, setEntityType] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedName, setSelectedName] = useState("");

  const stats = useGraphStats();
  const { data, isLoading, isError, refetch } = useGraphQuery(search, entityType, 100);
  const extractMut = useGraphExtract();

  const entities = data?.entities ?? [];
  const relationships = data?.relationships ?? [];

  const handleSelect = (e: GraphEntity) => {
    setSelectedId(e.entity_id);
    setSelectedName(e.name);
    setSearch(e.name);
  };

  const handleClearSelected = () => {
    setSelectedId(null);
    setSelectedName("");
  };

  const selectedRels = useMemo(() => {
    if (!selectedId) return [];
    return relationships.filter(
      (r) => r.source_id === selectedId || r.target_id === selectedId
    );
  }, [selectedId, relationships]);

  const handleRebuild = async () => {
    if (!window.confirm("重新构建知识图谱？将清空现有实体和关系后从全部文档重建。")) return;
    try {
      await extractMut.mutateAsync({ limit: 0, replace: true });
      toast.success("知识图谱已重建");
      handleClearSelected();
    } catch {
      toast.error("重建失败，请查看服务日志");
    }
  };

  const statsCards = useMemo(() => {
    const s = stats.data;
    if (!s) return [];
    return [
      { label: "实体", value: s.entities, icon: <Boxes className="size-5" /> },
      { label: "关系", value: s.relationships, icon: <Share2 className="size-5" /> },
      { label: "文档分块", value: s.entities > 0 ? "已建图" : "—", icon: <Network className="size-5" /> },
    ];
  }, [stats.data]);

  const typeBreakdown = stats.data?.entity_types ?? {};

  return (
    <>
      <PageHeader
        title="知识图谱"
        description={
          stats.data
            ? `${stats.data.entities} 个实体 · ${stats.data.relationships} 条关系 · 手动或访问时自动构建`
            : "实体与关系图谱"
        }
        actions={
          isAdmin ? (
            <Button
              size="sm"
              variant="outline"
              onClick={handleRebuild}
              disabled={extractMut.isPending}
            >
              {extractMut.isPending ? (
                <Loader2 className="size-4 mr-1 animate-spin" />
              ) : (
                <RefreshCw className="size-4 mr-1" />
              )}
              重建图谱
            </Button>
          ) : undefined
        }
      />

      {/* Search / filter bar */}
      <div className="flex flex-col gap-2 mb-4 sm:flex-row">
        <div className="flex gap-2 flex-1 min-w-0">
          <Input
            placeholder="搜索实体（如 Python、web_server、react）..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                setSelectedId(null);
                setSelectedName("");
                refetch();
              }
            }}
            className="flex-1 min-w-0"
          />
          <Button
            size="sm"
            variant="outline"
            className="shrink-0"
            onClick={() => {
              setSelectedId(null);
              setSelectedName("");
              refetch();
            }}
          >
            <Search className="size-4 mr-1" /> 搜索
          </Button>
        </div>
        <div className="flex gap-2">
          <Select
            value={entityType}
            onValueChange={(v) => {
              setEntityType(v ?? "");
              setSelectedId(null);
              setSelectedName("");
            }}
          >
            <SelectTrigger className="w-full sm:w-36">
              <SelectValue placeholder="全部类型" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="">全部类型</SelectItem>
              {ENTITY_TYPES.map((t) => (
                <SelectItem key={t} value={t}>
                  {ENTITY_TYPE_LABELS[t] ?? t}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* Stats cards */}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mb-4">
        {stats.isLoading ? (
          Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-20 rounded-lg" />
          ))
        ) : (
          statsCards.map((c) => (
            <Card key={c.label}>
              <CardContent className="flex items-center gap-3 p-4">
                <div className="flex items-center justify-center size-10 rounded-lg bg-primary/10 text-primary shrink-0">
                  {c.icon}
                </div>
                <div className="min-w-0">
                  <div className="text-2xl font-semibold tracking-tight truncate">{c.value}</div>
                  <div className="text-sm text-muted-foreground truncate">{c.label}</div>
                </div>
              </CardContent>
            </Card>
          ))
        )}
      </div>

      {/* Entity type distribution */}
      {Object.keys(typeBreakdown).length > 0 && (
        <div className="flex flex-wrap items-center gap-2 mb-4">
          <span className="text-xs text-muted-foreground">类型分布：</span>
          {Object.entries(typeBreakdown)
            .sort((a, b) => b[1] - a[1])
            .map(([t, n]) => (
              <Badge key={t} variant="outline" className="gap-1">
                {ENTITY_TYPE_LABELS[t] ?? t}
                <span className="text-muted-foreground">{n}</span>
              </Badge>
            ))}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-4">
        {/* Entity list */}
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="text-sm">
              实体列表
              <span className="text-muted-foreground font-normal ml-2">
                {isLoading ? "" : `(${entities.length})`}
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {isError ? (
              <div className="p-6 text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
            ) : isLoading ? (
              <div className="space-y-2 p-3">
                {Array.from({ length: 6 }).map((_, i) => (
                  <Skeleton key={i} className="h-9 rounded-md" />
                ))}
              </div>
            ) : entities.length === 0 ? (
              <EmptyState
                title={search ? "未找到匹配实体" : "暂无实体"}
                description={
                  search
                    ? "换个关键词试试，或点击右上角重建图谱"
                    : "导入文档后访问本页会自动构建图谱"
                }
                icon={<Boxes className="size-10" />}
              />
            ) : (
              <div className="max-h-[55vh] overflow-y-auto">
                {entities.map((e) => {
                  const active = e.entity_id === selectedId;
                  return (
                    <button
                      key={e.entity_id}
                      onClick={() => handleSelect(e)}
                      className={`flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm transition-colors border-l-2 ${
                        active
                          ? "bg-sidebar-accent border-primary"
                          : "border-transparent hover:bg-muted/60"
                      }`}
                    >
                      <span className="min-w-0 flex-1 truncate font-medium">{e.name}</span>
                      <Badge className={`shrink-0 ${entityBadgeClass(e.entity_type)}`}>
                        {ENTITY_TYPE_LABELS[e.entity_type] ?? e.entity_type}
                      </Badge>
                    </button>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Relationship detail */}
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="text-sm flex items-center gap-2">
              关系
              {selectedName && (
                <span className="truncate text-muted-foreground font-normal">
                  与 <span className="text-foreground font-medium">{selectedName}</span> 相关
                </span>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {!selectedId ? (
              <EmptyState
                title="选择一个实体查看关系"
                description="点击左侧列表中的实体，显示它的出入关系"
                icon={<Share2 className="size-10" />}
              />
            ) : selectedRels.length === 0 ? (
              <EmptyState
                title="暂无关系"
                description="该实体未提取到关系，可能只出现在单个分块中"
                icon={<Network className="size-10" />}
              />
            ) : (
              <div className="max-h-[55vh] overflow-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>来源</TableHead>
                      <TableHead>关系</TableHead>
                      <TableHead>目标</TableHead>
                      <TableHead className="text-right">置信度</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {selectedRels.map((r: GraphRelationship) => (
                      <TableRow key={r.rel_id}>
                        <TableCell className="max-w-[120px]">
                          <span className="block truncate">{r.source_name}</span>
                        </TableCell>
                        <TableCell>
                          <Badge variant="secondary" className="shrink-0">
                            {REL_TYPE_LABELS[r.rel_type] ?? r.rel_type}
                          </Badge>
                        </TableCell>
                        <TableCell className="max-w-[120px]">
                          <span className="block truncate">{r.target_name}</span>
                        </TableCell>
                        <TableCell className="text-right text-muted-foreground">
                          {r.confidence.toFixed(2)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}
