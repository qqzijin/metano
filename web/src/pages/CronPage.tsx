import { useState } from "react";
import { Trash2, Pause, Play, FastForward, Plus } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";
import { useCronJobs, useCronPause, useCronResume, useCronDelete, useCronTrigger, useCronCreate } from "@/api/hooks";
import { RoleGuard } from "@/components/auth/RoleGuard";
import { toast } from "sonner";

export default function CronPage() {
  const { data, isLoading, isError } = useCronJobs();
  const pauseMut = useCronPause();
  const resumeMut = useCronResume();
  const deleteMut = useCronDelete();
  const triggerMut = useCronTrigger();
  const createMut = useCronCreate();

  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: "", expr: "0 9 * * *", prompt: "" });

  const jobs = data?.jobs ?? [];

  const handleCreate = async () => {
    if (!form.name.trim() || !form.prompt.trim()) {
      toast.error("名称和提示词必填");
      return;
    }
    try {
      await createMut.mutateAsync({ name: form.name, schedule: { kind: "cron", expr: form.expr }, prompt: form.prompt });
      toast.success("定时任务已创建");
      setShowForm(false);
      setForm({ name: "", expr: "0 9 * * *", prompt: "" });
    } catch {
      toast.error("创建失败");
    }
  };

  const handleToggle = async (j: any) => {
    try {
      if (j.enabled) await pauseMut.mutateAsync(j.id);
      else await resumeMut.mutateAsync(j.id);
    } catch {
      toast.error("操作失败");
    }
  };

  const handleTrigger = async (j: any) => {
    try {
      await triggerMut.mutateAsync(j.id);
      toast.info(`已触发 ${j.name}`);
    } catch {
      toast.error("触发失败");
    }
  };

  const handleDelete = async (j: any) => {
    if (!window.confirm(`确定删除定时任务 "${j.name}"？`)) return;
    try {
      await deleteMut.mutateAsync(j.id);
      toast.success("已删除");
    } catch {
      toast.error("删除失败");
    }
  };

  return (
    <RoleGuard role="admin">
      <>
      <PageHeader
        title="定时任务"
        description={`${jobs.length} 个定时任务`}
        actions={
          <Button size="sm" onClick={() => setShowForm(!showForm)}>
            <Plus className="size-3.5 mr-1" /> 新建任务
          </Button>
        }
      />

      {showForm && (
        <Card className="p-4 mb-4 space-y-3">
          <Input placeholder="任务名称" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <Input placeholder="计划表达式 (cron，如 0 9 * * *)" value={form.expr} onChange={(e) => setForm({ ...form, expr: e.target.value })} />
          <Textarea placeholder="任务提示词" value={form.prompt} onChange={(e) => setForm({ ...form, prompt: e.target.value })} className="min-h-20" />
          <div className="flex gap-2 flex-wrap">
            <Button size="sm" onClick={handleCreate} disabled={createMut.isPending}>{createMut.isPending ? "创建中..." : "创建"}</Button>
            <Button size="sm" variant="ghost" onClick={() => setShowForm(false)}>取消</Button>
          </div>
        </Card>
      )}

      {isError ? (
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      ) : isLoading ? (
        <div className="space-y-3">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-16 rounded-lg" />)}</div>
      ) : jobs.length === 0 ? (
        <EmptyState title="暂无定时任务" description="点击右上角「新建任务」创建定时任务" />
      ) : (
        <div className="grid gap-3">
          {jobs.map((j) => (
            <Card key={j.id} className="p-4">
              <div className="flex items-center gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-medium text-sm break-words">{j.name}</span>
                    <Badge variant={j.enabled ? "default" : "secondary"} className="text-[10px]">{j.enabled ? "已启用" : "已禁用"}</Badge>
                    {j.type && <Badge variant="outline" className="text-[10px]">{j.type}</Badge>}
                  </div>
                  <div className="text-xs text-muted-foreground mt-1 space-y-0.5">
                    <div>计划: <code className="bg-muted px-1 rounded break-all">{j.schedule.expr}</code></div>
                    <div className="truncate">提示词: {(j.prompt ?? "").slice(0, 100)}{(j.prompt ?? "").length > 100 ? "..." : ""}</div>
                    {j.last_run_at && <div>上次运行: {new Date(j.last_run_at).toLocaleString()}</div>}
                    {j.last_error && <div className="text-destructive">错误: {j.last_error}</div>}
                  </div>
                </div>
                <div className="flex gap-1 shrink-0">
                  <Button size="icon" variant="ghost" className="size-8" onClick={() => handleToggle(j)} title={j.enabled ? "暂停" : "恢复"}>
                    {j.enabled ? <Pause className="size-4" /> : <Play className="size-4" />}
                  </Button>
                  <Button size="icon" variant="ghost" className="size-8" onClick={() => handleTrigger(j)} title="触发">
                    <FastForward className="size-4" />
                  </Button>
                  <Button size="icon" variant="ghost" className="size-8 text-destructive" onClick={() => handleDelete(j)} title="删除">
                    <Trash2 className="size-4" />
                  </Button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
      </>
    </RoleGuard>
  );
}
