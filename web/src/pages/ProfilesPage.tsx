import { User } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useProfile } from "@/api/hooks";
import { fmtTime } from "@/api/client";

const STAGE_NAMES: Record<string, string> = {
  core: "核心信念",
  established: "已建立",
  draft: "草稿",
};

function computeStage(b: any): string {
  if (b.stage) return b.stage;
  const c = b.confidence ?? 0;
  const r = b.reinforcement_count ?? 0;
  if (c >= 0.8 && r >= 5) return "core";
  if (c >= 0.6 && r >= 2) return "established";
  return "draft";
}

export default function ProfilesPage() {
  const { data: profile, isLoading } = useProfile();

  if (isLoading) return <div className="space-y-4">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-28 rounded-lg" />)}</div>;
  if (profile?.error) return <EmptyState title="用户画像不可用" description={profile.error} icon={<User className="size-10" />} />;

  const rawBeliefs: any[] = (profile as any)?.beliefs ?? [];
  const beliefs = rawBeliefs.filter((b: any) => !b.contradicted).map((b: any) => ({ ...b, stage: computeStage(b) }));

  const byStage: Record<string, any[]> = {};
  for (const b of beliefs) {
    const stage = b.stage;
    (byStage[stage] = byStage[stage] || []).push(b);
  }

  const stageOrder = ["core", "established", "draft"];

  return (
    <>
      <PageHeader title="用户画像" description={`${beliefs.length} 条信念`} />

      {profile?.belief_summary && (
        <Card className="mb-4">
          <CardContent className="p-4">
            <div className="text-xs text-muted-foreground mb-1">信念摘要</div>
            <div className="text-sm whitespace-pre-wrap">{profile.belief_summary}</div>
          </CardContent>
        </Card>
      )}

      {beliefs.length === 0 ? (
        <EmptyState title="暂无信念" description="进化引擎将随时间积累信念" />
      ) : (
        stageOrder
          .filter((s) => byStage[s])
          .map((stage) => (
            <div key={stage} className="mb-6">
              <div className="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-2">
                {STAGE_NAMES[stage] ?? stage} ({byStage[stage].length})
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {byStage[stage].map((b: any) => (
                  <Card key={b.id}>
                    <CardContent className="p-4">
                      <div className="text-sm mb-2">{b.content}</div>
                      <div className="flex gap-2 flex-wrap">
                        <Badge variant="secondary" className="text-[10px]">{STAGE_NAMES[b.stage] ?? b.stage}</Badge>
                        {b.category && <Badge variant="outline" className="text-[10px]">{b.category}</Badge>}
                        <Badge variant="outline" className="text-[10px] ml-auto">{((b.confidence ?? 0) * 100).toFixed(0)}%</Badge>
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            </div>
          ))
      )}

      {profile?.recent_observations && profile.recent_observations.length > 0 && (
        <div className="mt-6">
          <div className="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-2">
            最近观察 ({profile.recent_observations.length})
          </div>
          <div className="space-y-2">
            {profile.recent_observations.map((o: any) => (
              <Card key={o.id}>
                <CardContent className="p-3 flex items-center gap-3">
                  <div className="text-sm text-muted-foreground flex-1 min-w-0">{o.content}</div>
                  {o.timestamp != null && <span className="text-xs text-muted-foreground shrink-0">{fmtTime(o.timestamp)}</span>}
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      )}
    </>
  );
}