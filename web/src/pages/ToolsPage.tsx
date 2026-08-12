import { useState } from "react";
import { Zap, Wrench, Search, Globe, Flame, Snowflake, Pencil, Trash2 } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { useSkills, useSkill, useSkillUsage, useSkillUpdate, useSkillDelete, useMcpTools, useWebSearch } from "@/api/hooks";
import { toast } from "sonner";

/**
 * 工具中心：技能 + MCP 工具 合并为一个页面（此前是两个独立入口）。
 */
export default function ToolsPage() {
  return (
    <>
      <PageHeader title="工具" description="技能与 MCP 工具统一管理" />
      <Tabs defaultValue="skills">
        <TabsList className="mb-4">
          <TabsTrigger value="skills"><Zap className="size-3.5 mr-1" />技能</TabsTrigger>
          <TabsTrigger value="mcp"><Wrench className="size-3.5 mr-1" />MCP 工具</TabsTrigger>
        </TabsList>
        <TabsContent value="skills"><SkillsView /></TabsContent>
        <TabsContent value="mcp"><McpToolsView /></TabsContent>
      </Tabs>
    </>
  );
}

/* ── 技能视图（原 SkillsPage） ── */
function SkillsView() {
  const [category, setCategory] = useState("");
  const [detailName, setDetailName] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const { data, isLoading, isError } = useSkills();
  const { data: detail } = useSkill(detailName ?? "");
  const { data: usage } = useSkillUsage(30);
  const updateMut = useSkillUpdate();
  const deleteMut = useSkillDelete();

  const skills = data?.skills ?? [];
  const categories = [...new Set(skills.map((s) => s.category).filter(Boolean))];

  const usageMap = new Map<string, number>();
  for (const u of usage?.recent ?? []) usageMap.set(u.skill_name, u.uses);
  const usedSkills = new Set(usageMap.keys());
  const neverUsed = skills.filter((s) => !usedSkills.has(s.name));
  const hotSkills = (usage?.recent ?? []).slice(0, 5);

  return (
    <>
      {hotSkills.length > 0 && (
        <Card className="mb-4">
          <CardContent className="p-3">
            <div className="text-xs font-medium text-muted-foreground flex items-center gap-1.5 mb-2">
              <Flame className="size-3.5 text-orange-500" /> 近30天热门技能
            </div>
            <div className="flex flex-wrap gap-2">
              {hotSkills.map((u) => (
                <Badge key={u.skill_name} variant="secondary" className="text-[10px]">
                  {u.skill_name} × {u.uses}
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
      {neverUsed.length > 0 && (
        <div className="flex items-center gap-2 mb-4 text-xs text-muted-foreground">
          <Snowflake className="size-3.5" />
          <span>{neverUsed.length} 项技能近30天未使用</span>
        </div>
      )}

      <div className="flex gap-2 mb-4 flex-wrap">
        <Button variant={category === "" ? "default" : "outline"} size="sm" onClick={() => setCategory("")}>全部</Button>
        {categories.map((c) => (
          <Button key={c} variant={category === c ? "default" : "outline"} size="sm" onClick={() => setCategory(c)}>
            {c}
          </Button>
        ))}
      </div>

      {isError ? (
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      ) : isLoading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-28 rounded-lg" />)}
        </div>
      ) : skills.length === 0 ? (
        <EmptyState title="暂无技能" description="注册技能以扩展系统能力" icon={<Zap className="size-10" />} />
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {skills.map((s) => (
            <Card key={s.name} className="cursor-pointer hover:shadow-sm transition-shadow" onClick={() => setDetailName(s.name)}>
              <CardContent className="p-4">
                <div className="font-medium text-sm truncate">{s.name}</div>
                <div className="text-xs text-muted-foreground mt-1 line-clamp-2 break-words">{s.description}</div>
                <div className="flex gap-2 mt-2 flex-wrap">
                  <Badge variant="secondary" className="text-[10px]">{s.trigger}</Badge>
                  <Badge variant="outline" className="text-[10px]">{s.category}</Badge>
                  <Badge
                    variant={usageMap.get(s.name) ? "default" : "outline"}
                    className={`text-[10px] ${usageMap.get(s.name) ? "" : "text-muted-foreground"}`}
                    title="近30天使用次数"
                  >
                    {usageMap.get(s.name) ? `${usageMap.get(s.name)}次` : "未使用"}
                  </Badge>
                  {s.source && <Badge variant="outline" className="text-[10px]">{s.source}</Badge>}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <Dialog open={!!detailName} onOpenChange={() => { setDetailName(null); setEditing(false); }}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{detail?.name}</DialogTitle>
          </DialogHeader>
          {detail && (
            <div className="space-y-3">
              <div className="flex gap-2 flex-wrap">
                <Badge variant="secondary">{detail.trigger}</Badge>
                <Badge variant="outline">{detail.category}</Badge>
                {detail.source && <Badge variant="outline">{detail.source}</Badge>}
              </div>
              {editing ? (
                <>
                  <Textarea
                    value={editContent}
                    onChange={(e) => setEditContent(e.target.value)}
                    spellCheck={false}
                    className="font-mono text-xs min-h-[220px] max-h-72 resize-y"
                  />
                  <div className="flex justify-end gap-2">
                    <Button size="sm" variant="outline" onClick={() => setEditing(false)}>取消</Button>
                    <Button size="sm" disabled={updateMut.isPending} onClick={async () => {
                      try {
                        await updateMut.mutateAsync({ name: detail.name, content: editContent });
                        toast.success("技能已更新");
                        setEditing(false);
                      } catch { toast.error("更新失败（内置技能可能受保护）"); }
                    }}>
                      {updateMut.isPending ? "保存中..." : "保存"}
                    </Button>
                  </div>
                </>
              ) : (
                <>
                  <p className="text-sm text-muted-foreground break-words">{detail.description}</p>
                  <pre className="text-xs bg-muted p-3 rounded-md overflow-auto max-h-60 whitespace-pre-wrap break-words">{detail.content}</pre>
                  <div className="flex justify-end gap-2 pt-1">
                    <Button size="sm" variant="outline" onClick={() => { setEditContent(detail.content); setEditing(true); }}>
                      <Pencil className="size-3.5 mr-1" /> 编辑
                    </Button>
                    <Button size="sm" variant="destructive" disabled={deleteMut.isPending} onClick={async () => {
                      if (!window.confirm(`确定删除技能「${detail.name}」？内置技能无法删除。`)) return;
                      try {
                        await deleteMut.mutateAsync(detail.name);
                        toast.success("技能已删除");
                        setDetailName(null);
                      } catch { toast.error("删除失败（内置技能受保护）"); }
                    }}>
                      <Trash2 className="size-3.5 mr-1" /> 删除
                    </Button>
                  </div>
                </>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

/* ── MCP 工具视图（原 McpToolsPage） ── */
function McpToolsView() {
  const { data, isLoading, isError } = useMcpTools();
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResult, setSearchResult] = useState<any>(null);
  const webSearchMut = useWebSearch();

  const tools = data?.tools ?? [];

  const handleWebSearch = async () => {
    if (!searchQuery.trim()) return;
    try {
      const result = await webSearchMut.mutateAsync(searchQuery);
      setSearchResult(result);
      toast.success("搜索完成");
    } catch {
      toast.error("搜索失败");
    }
  };

  return (
    <>
      {/* Tavily 网页搜索 */}
      <Card className="mb-4">
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2">
            <Globe className="size-4" /> Tavily 网页搜索
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap gap-2">
            <Input
              placeholder="搜索关键词..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="flex-1 min-w-0"
              onKeyDown={(e) => e.key === "Enter" && handleWebSearch()}
            />
            <Button onClick={handleWebSearch} disabled={webSearchMut.isPending} className="shrink-0">
              <Search className="size-4 mr-1" /> 搜索
            </Button>
          </div>

          {searchResult && (
            <div className="space-y-3">
              {searchResult.answer && (
                <div className="bg-primary/5 border border-primary/25 rounded-lg p-3">
                  <p className="text-sm font-medium mb-1">摘要</p>
                  <p className="text-sm">{searchResult.answer}</p>
                </div>
              )}
              {(searchResult.results ?? []).map((r: any, i: number) => (
                <div key={i} className="bg-muted/50 rounded-lg p-3 min-w-0">
                  <div className="font-medium text-sm mb-1 break-words">{r.title}</div>
                  <p className="text-xs text-muted-foreground mb-1 break-words">{r.snippet}</p>
                  <a href={r.url} target="_blank" rel="noopener noreferrer" className="text-xs text-primary hover:underline break-all">
                    {r.url}
                  </a>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Tool Registry */}
      {isError ? (
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      ) : isLoading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-24 rounded-lg" />)}
        </div>
      ) : tools.length === 0 ? (
        <EmptyState title="暂无MCP工具" description="启动MCP服务器后工具将自动注册" icon={<Wrench className="size-10" />} />
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {tools.map((t) => (
            <Card key={t.name}>
              <CardContent>
                <div className="flex items-center gap-2 mb-2 min-w-0">
                  <Wrench className="size-4 text-muted-foreground shrink-0" />
                  <span className="font-medium text-sm font-mono truncate min-w-0 flex-1">{t.name}</span>
                  <Badge variant={t.source === "internal" ? "default" : "secondary"} className="text-xs shrink-0">
                    {t.source === "internal" ? "内置" : "外部"}
                  </Badge>
                </div>
                <p className="text-xs text-muted-foreground line-clamp-2 break-words">{t.description || "无描述"}</p>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}
