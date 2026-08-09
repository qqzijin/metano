import { Trash2, Pause, Play, FastForward } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCronJobs, useCronPause, useCronResume, useCronDelete, useCronTrigger } from "@/api/hooks";
import { toast } from "sonner";

export default function CronPage() {
  const { data, isLoading } = useCronJobs();
  const pauseMut = useCronPause();
  const resumeMut = useCronResume();
  const deleteMut = useCronDelete();
  const triggerMut = useCronTrigger();

  const jobs = data?.jobs ?? [];

  return (
    <>
      <PageHeader title="定时任务" description={`${jobs.length} 个定时任务`} />

      {isLoading ? (
        <div className="space-y-3">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-16 rounded-lg" />)}</div>
      ) : jobs.length === 0 ? (
        <EmptyState title="暂无定时任务" description="创建定时任务以自动执行周期性工作" />
      ) : (
        <div className="grid gap-3">
          {jobs.map((j) => (
            <Card key={j.id} className="p-4">
              <div className="flex items-center gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-sm">{j.name}</span>
                    <Badge variant={j.enabled ? "default" : "secondary"} className="text-[10px]">{j.enabled ? "已启用" : "已禁用"}</Badge>
                    {j.type && <Badge variant="outline" className="text-[10px]">{j.type}</Badge>}
                  </div>
                  <div className="text-xs text-muted-foreground mt-1 space-y-0.5">
                    <div>计划: <code className="bg-muted px-1 rounded">{j.schedule.expr}</code></div>
                    <div className="truncate">提示词: {j.prompt.slice(0, 100)}{j.prompt.length > 100 ? "..." : ""}</div>
                    {j.last_run_at && <div>上次运行: {new Date(j.last_run_at).toLocaleString()}</div>}
                    {j.last_error && <div className="text-destructive">错误: {j.last_error}</div>}
                  </div>
                </div>
                <div className="flex gap-1 shrink-0">
                  {j.enabled ? (
                    <Button size="icon" variant="ghost" className="size-8" onClick={() => pauseMut.mutate(j.id)}>
                      <Pause className="size-4" />
                    </Button>
                  ) : (
                    <Button size="icon" variant="ghost" className="size-8" onClick={() => resumeMut.mutate(j.id)}>
                      <Play className="size-4" />
                    </Button>
                  )}
                  <Button size="icon" variant="ghost" className="size-8" onClick={() => { triggerMut.mutate(j.id); toast.info(`已触发 ${j.name}`); }}>
                    <FastForward className="size-4" />
                  </Button>
                  <Button size="icon" variant="ghost" className="size-8 text-destructive" onClick={() => deleteMut.mutate(j.id)}>
                    <Trash2 className="size-4" />
                  </Button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}