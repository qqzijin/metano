import { useState } from "react";
import { Zap } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useSkills, useSkill, useSkillUsage } from "@/api/hooks";
import { Flame, Snowflake } from "lucide-react";

export default function SkillsPage() {
  const [category, setCategory] = useState("");
  const [detailName, setDetailName] = useState<string | null>(null);
  const { data, isLoading, isError } = useSkills();
  const { data: detail } = useSkill(detailName ?? "");
  const { data: usage } = useSkillUsage(30);

  const skills = data?.skills ?? [];
  const categories = [...new Set(skills.map((s) => s.category).filter(Boolean))];

  // usage map: skill_name -> uses (last 30 days) for badges
  const usageMap = new Map<string, number>();
  for (const u of usage?.recent ?? []) usageMap.set(u.skill_name, u.uses);
  // Skills with zero recorded usage in the window.
  const usedSkills = new Set(usageMap.keys());
  const neverUsed = skills.filter((s) => !usedSkills.has(s.name));
  const hotSkills = (usage?.recent ?? []).slice(0, 5);

  return (
    <>
      <PageHeader title="技能" description={`已注册 ${skills.length} 项`} />

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

      <Dialog open={!!detailName} onOpenChange={() => setDetailName(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{detail?.name}</DialogTitle>
          </DialogHeader>
          {detail && (
            <div className="space-y-3">
              <div className="flex gap-2 flex-wrap">
                <Badge variant="secondary">{detail.trigger}</Badge>
                <Badge variant="outline">{detail.category}</Badge>
              </div>
              <p className="text-sm text-muted-foreground break-words">{detail.description}</p>
              <pre className="text-xs bg-muted p-3 rounded-md overflow-auto max-h-72 whitespace-pre-wrap break-words">{detail.content}</pre>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
