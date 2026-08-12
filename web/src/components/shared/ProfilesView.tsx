import { User } from "lucide-react";
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

interface Belief {
  id: string;
  category?: string;
  content: string;
  confidence: number;
  stage?: string;
  contradicted?: boolean;
  reinforcement_count?: number;
  timestamp?: number;
  created_at?: number;
  updated_at?: number;
}

function computeStage(b: Belief): string {
  if (b.stage) return b.stage;
  const c = b.confidence ?? 0;
  const r = b.reinforcement_count ?? 0;
  if (c >= 0.8 && r >= 5) return "core";
  if (c >= 0.6 && r >= 2) return "established";
  return "draft";
}

/**
 * 用户画像（honcho 信念 + 观察）展示。
 * 从 ProfilesPage 抽取，供 /profiles 与记忆中心共用。
 */
export function ProfilesView() {
  const { data: profile, isLoading, isError } = useProfile();

  if (isError) return <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>;
  if (isLoading) return <div className="space-y-4">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-28 rounded-lg" />)}</div>;
  if (profile?.error) return <EmptyState title="用户画像不可用" description={profile.error} icon={<User className="size-10" />} />;

  const rawBeliefs: Belief[] = profile?.beliefs ?? [];
  const beliefs = rawBeliefs.filter((b) => !b.contradicted).map((b) => ({ ...b, stage: computeStage(b) }));

  const byStage: Record<string, Belief[]> = {};
  for (const b of beliefs) {
    const stage = b.stage;
    (byStage[stage] = byStage[stage] || []).push(b);
  }

  const stageOrder = ["core", "established", "draft"];

  return (
    <>
      {/* 说明：百分比是置信度而非成熟度。stage 由 置信度 + 强化次数 共同决定，
          所以会出现"established 的置信度低于 draft"——draft 可能单次置信高但强化次数不足。 */}
      <p className="text-xs text-muted-foreground mb-4 leading-relaxed">
        成熟度（核心/已建立/草稿）由 <code className="font-mono">置信度 × 强化次数</code> 共同决定，
        卡片右上角百分比是<strong className="text-foreground">置信度</strong>（该信念被证据支持的程度），不是完成度。
        一条信念可以置信度高但强化次数少而停留在草稿；置信度会随时间衰减，被强化后回升。
      </p>

      {profile?.belief_summary && (
        <Card className="mb-4">
          <CardContent className="pt-4">
            <div className="text-sm whitespace-pre-wrap break-words">{profile.belief_summary}</div>
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
                {byStage[stage].map((b) => (
                  <Card key={b.id}>
                    <CardContent className="p-4">
                      <div className="text-sm break-words">{b.content}</div>
                      <div className="flex gap-2 flex-wrap mt-2">
                        <Badge variant="secondary" className="text-[10px]">{STAGE_NAMES[b.stage ?? ""] ?? b.stage}</Badge>
                        {b.category && <Badge variant="outline" className="text-[10px]">{b.category}</Badge>}
                        <Badge variant="outline" className="text-[10px] ml-auto" title="置信度：该信念被证据支持的程度">置信度 {((b.confidence ?? 0) * 100).toFixed(0)}%</Badge>
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
            {profile.recent_observations.map((o) => (
              <Card key={o.id}>
                <CardContent className="p-3 flex items-center gap-3">
                  <div className="text-sm text-muted-foreground flex-1 min-w-0 break-words">{o.content}</div>
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

export default ProfilesView;
