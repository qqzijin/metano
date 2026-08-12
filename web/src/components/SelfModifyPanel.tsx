import { useCallback, useEffect, useState } from "react";
import { RefreshCw, Undo2, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { toast } from "sonner";

interface SelfModifyEvent {
  id: number;
  issue: string;
  file: string;
  diff: string;
  verify_result: string;
  applied_at?: number;
  commit_hash?: string;
  status: string;
  created_at: number;
}

const STATUS_BADGE: Record<string, { label: string; cls: string }> = {
  candidate: { label: "候选", cls: "bg-muted text-muted-foreground" },
  verified: { label: "已验证", cls: "bg-chart-2/10 text-chart-2" },
  applied: { label: "已应用", cls: "bg-chart-3/10 text-chart-3" },
  rejected: { label: "已淘汰", cls: "bg-destructive/10 text-destructive" },
  reverted: { label: "已回滚", cls: "bg-muted text-muted-foreground" },
};

/**
 * 自我修改面板（进化系统第 8 个 tab）。
 * 内容原为独立页面 SelfModifyPage —— 自我修改是进化系统的核心能力，
 * 故并入进化系统页，此处为纯内容，无 PageHeader / RoleGuard 包裹。
 */
export function SelfModifyPanel() {
  const [events, setEvents] = useState<SelfModifyEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/self-modify/events?limit=50", { credentials: "include" });
      if (res.status === 401) { window.dispatchEvent(new Event("auth:unauthorized")); return; }
      // M-04: don't treat a 403/500 as an empty list — surface the failure.
      if (!res.ok) {
        setLoadError(`加载失败 (HTTP ${res.status})`);
        setEvents([]);
        return;
      }
      const data = await res.json();
      setEvents(data.items ?? []);
      setLoadError(null);
    } catch {
      setLoadError("加载失败，请检查服务或刷新重试");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    (async () => { await load(); })();
  }, [load]);

  const handleRun = async (dry: boolean) => {
    setRunning(true);
    setLoading(true);
    setLoadError(null);
    try {
      const res = await fetch(`/api/self-modify/run?dry_run=${dry}`, {
        method: "POST", credentials: "include",
      });
      if (res.status === 401) { window.dispatchEvent(new Event("auth:unauthorized")); return; }
      const data = await res.json();
      if (!res.ok) {
        setLoadError(data?.detail || data?.error?.message || `运行失败 (HTTP ${res.status})`);
        load();
        return;
      }
      toast.success(`完成: 扫描 ${data.scanned ?? 0} 问题, 应用 ${data.applied ?? 0}`);
      load();
    } catch {
      toast.error("触发失败");
      setLoadError("触发失败");
    } finally {
      setRunning(false);
    }
  };

  const handleRevert = async (id: number) => {
    if (!window.confirm("确认回滚这次自我修改？将通过 git revert 还原代码。")) return;
    setLoading(true);
    try {
      const res = await fetch(`/api/self-modify/revert/${id}`, {
        method: "POST", credentials: "include",
      });
      if (res.status === 401) { window.dispatchEvent(new Event("auth:unauthorized")); return; }
      const data = await res.json();
      if (!res.ok) {
        toast.error(data?.detail || data?.error?.message || `回滚失败 (HTTP ${res.status})`);
        load();
        return;
      }
      toast.success(data.status === "reverted" ? "已回滚" : (data.reason || "操作失败"));
      load();
    } catch {
      toast.error("回滚失败");
    }
  };

  const fmtTime = (ts?: number) => ts ? new Date(ts * 1000).toLocaleString() : "-";
  const shortHash = (h?: string) => h ? h.slice(0, 10) : "-";

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">变异记录（self_modify_events）</CardTitle>
          <CardAction>
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={() => handleRun(true)} disabled={running}>
                <Play className="size-3.5 mr-1" /> 扫描(仅候选)
              </Button>
              <Button size="sm" onClick={() => handleRun(false)} disabled={running}>
                <RefreshCw className="size-3.5 mr-1" /> 运行自举
              </Button>
            </div>
          </CardAction>
        </CardHeader>
        <CardContent>
          {loadError ? (
            <div className="text-sm text-destructive">{loadError}</div>
          ) : loading ? (
            <div className="text-sm text-muted-foreground">加载中…</div>
          ) : events.length === 0 ? (
            <div className="text-sm text-muted-foreground">
              暂无自我修改记录。可点击上方「运行自举」触发第一次扫描。
            </div>
          ) : (
            <div className="space-y-2">
              {events.map((ev) => {
                const badge = STATUS_BADGE[ev.status] ?? STATUS_BADGE.candidate;
                return (
                  <div key={ev.id} className="border rounded-lg overflow-hidden">
                    <div
                      className="flex items-center gap-2 p-3 cursor-pointer hover:bg-muted/50"
                      onClick={() => setExpanded(expanded === ev.id ? null : ev.id)}
                    >
                      <span className="text-xs text-muted-foreground font-mono">#{ev.id}</span>
                      <span className="flex-1 text-sm truncate">{ev.issue || ev.file}</span>
                      <Badge variant="outline" className={`text-[10px] ${badge.cls}`}>{badge.label}</Badge>
                      {ev.status === "applied" && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={(e) => { e.stopPropagation(); handleRevert(ev.id); }}
                          title="git revert 回滚"
                        >
                          <Undo2 className="size-3.5" />
                        </Button>
                      )}
                    </div>
                    {expanded === ev.id && (
                      <div className="px-3 pb-3 border-t text-xs space-y-1">
                        <div className="flex flex-wrap gap-x-4 text-muted-foreground pt-2">
                          <span>文件: <code className="font-mono">{ev.file}</code></span>
                          <span>验证: {ev.verify_result}</span>
                          <span>提交: <code className="font-mono">{shortHash(ev.commit_hash)}</code></span>
                          <span>应用: {fmtTime(ev.applied_at)}</span>
                          <span>创建: {fmtTime(ev.created_at)}</span>
                        </div>
                        <pre className="mt-2 max-h-60 overflow-auto rounded bg-muted/40 p-2 font-mono text-[11px] whitespace-pre-wrap break-all">
                          {ev.diff}
                        </pre>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export default SelfModifyPanel;
