import { useCallback, useEffect, useState } from "react";
import { ClipboardList, Eye, FileText, Play, Plus, RefreshCw } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import { fmtCost, fmtTime } from "@/api/client";
import { toast } from "sonner";

/* ── types ─────────────────────────────────────────────────────────────── */

interface CollabTask {
  id: string;
  task_type: string;
  target: string;
  prompt: string;
  status: string;
  assigned_to?: string;
  created_by?: string;
  result?: string;
  cost?: number;
  error?: string;
  created_at?: number;
  updated_at?: number;
}

interface AuditEntry {
  timestamp?: number;
  action: string;
  user_id: string;
  details?: unknown;
}

const TASK_TYPES = [
  "general", "search", "web", "memory", "knowledge", "code", "file", "skill",
];

const STATUS_OPTIONS = ["pending", "running", "completed", "failed"];

const STATUS_LABELS: Record<string, string> = {
  pending: "待执行",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
};

/** 语义 token 状态色（随主题切换），badge 为 outline 变体时覆盖背景/前景。 */
const STATUS_BADGE: Record<string, string> = {
  pending: "bg-muted text-muted-foreground border-transparent",
  running: "bg-chart-1/10 text-chart-1 border-transparent",
  completed: "bg-chart-3/10 text-chart-3 border-transparent",
  failed: "bg-destructive/10 text-destructive border-transparent",
};

/* ── api helper（登录 cookie 复用现有 fetch 约定） ─────────────────────── */

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  const res = await fetch(`/api${path}`, { ...init, credentials: "include", headers });
  if (res.status === 401) {
    window.dispatchEvent(new Event("auth:unauthorized"));
    throw new Error("未登录");
  }
  if (!res.ok) {
    let detail = "";
    try {
      const data = await res.json();
      if (data && typeof data === "object" && "detail" in data) detail = String((data as { detail: unknown }).detail);
    } catch { /* ignore */ }
    throw new Error(detail || `请求失败 (${res.status})`);
  }
  return res.json();
}

/** 将审计 details 字段格式化为可读文本（JSON 美化，字符串原样）。 */
function formatDetails(value: unknown): string | null {
  if (value == null) return null;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return null;
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed && typeof parsed === "object") return JSON.stringify(parsed, null, 2);
      return trimmed;
    } catch {
      return trimmed;
    }
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/* ── page ──────────────────────────────────────────────────────────────── */

export default function CollabPage() {
  const [tasks, setTasks] = useState<CollabTask[]>([]);
  const [tasksLoading, setTasksLoading] = useState(true);
  const [tasksError, setTasksError] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");

  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [auditLoading, setAuditLoading] = useState(true);
  const [auditError, setAuditError] = useState(false);

  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ task_type: "general", target: "local", prompt: "", assigned_to: "" });
  const [creating, setCreating] = useState(false);
  const [executingId, setExecutingId] = useState<string | null>(null);
  const [detail, setDetail] = useState<CollabTask | null>(null);

  const loadTasks = useCallback(async () => {
    setTasksLoading(true);
    setTasksError(false);
    try {
      const q = statusFilter ? `?status=${encodeURIComponent(statusFilter)}&limit=100` : "?limit=100";
      const data = await api<{ items: CollabTask[] }>(`/collab/tasks${q}`);
      setTasks(data.items ?? []);
    } catch {
      setTasksError(true);
    } finally {
      setTasksLoading(false);
    }
  }, [statusFilter]);

  const loadAudit = useCallback(async () => {
    setAuditLoading(true);
    setAuditError(false);
    try {
      const data = await api<{ items: AuditEntry[] }>("/collab/audit?limit=50");
      setAudit(data.items ?? []);
    } catch {
      setAuditError(true);
    } finally {
      setAuditLoading(false);
    }
  }, []);

  const refresh = useCallback(() => {
    loadTasks();
    loadAudit();
  }, [loadTasks, loadAudit]);

  useEffect(() => { loadTasks(); }, [loadTasks]);
  useEffect(() => { loadAudit(); }, [loadAudit]);

  const handleCreate = async () => {
    if (!form.prompt.trim()) {
      toast.error("提示词必填");
      return;
    }
    setCreating(true);
    try {
      await api<CollabTask>("/collab/tasks", {
        method: "POST",
        body: JSON.stringify({
          task_type: form.task_type,
          target: form.target.trim() || "local",
          prompt: form.prompt.trim(),
          assigned_to: form.assigned_to.trim(),
        }),
      });
      toast.success("任务已创建");
      setShowForm(false);
      setForm({ task_type: "general", target: "local", prompt: "", assigned_to: "" });
      refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "创建失败");
    } finally {
      setCreating(false);
    }
  };

  const handleExecute = async (task: CollabTask) => {
    setExecutingId(task.id);
    try {
      const res = await api<{
        task: CollabTask;
        execution: { mode?: string; status?: string; note?: string; error?: string; duration_seconds?: number };
      }>(`/collab/tasks/${task.id}/execute`, {
        method: "POST",
        body: JSON.stringify({ timeout: 120 }),
      });
      const exec = res.execution;
      if (exec?.mode === "remote_placeholder") {
        toast.info(exec.note || "跨设备执行尚未接入");
      } else if (exec?.status === "failed") {
        toast.error(exec.error || "执行失败");
      } else {
        toast.success("执行完成");
      }
      refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "执行失败");
    } finally {
      setExecutingId(null);
    }
  };

  return (
    <>
      <PageHeader
        title="协作控制面"
        description={`${tasks.length} 个任务 · 跨设备协作管理`}
        actions={
          <>
            <Button size="sm" variant="outline" onClick={refresh} title="刷新">
              <RefreshCw className="size-3.5 mr-1" /> 刷新
            </Button>
            <Button size="sm" onClick={() => setShowForm(!showForm)}>
              <Plus className="size-3.5 mr-1" /> 新建任务
            </Button>
          </>
        }
      />

      {/* 创建任务表单 */}
      {showForm && (
        <Card className="p-4 mb-4 space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-xs text-muted-foreground">任务类型</label>
              <Select value={form.task_type} onValueChange={(v) => { if (v) setForm({ ...form, task_type: v }); }}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {TASK_TYPES.map((t) => (
                    <SelectItem key={t} value={t}>{t}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <label className="text-xs text-muted-foreground">目标</label>
              <Input
                placeholder="local 或 remote-HOST_REMOTE_PLACEHOLDER"
                value={form.target}
                onChange={(e) => setForm({ ...form, target: e.target.value })}
              />
            </div>
          </div>
          <div className="space-y-1.5">
            <label className="text-xs text-muted-foreground">
              提示词 <span className="text-destructive">*</span>
            </label>
            <Textarea
              placeholder="描述任务内容..."
              value={form.prompt}
              onChange={(e) => setForm({ ...form, prompt: e.target.value })}
              className="min-h-24"
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-xs text-muted-foreground">指派给（可选）</label>
            <Input
              placeholder="assigned_to，留空表示任何人"
              value={form.assigned_to}
              onChange={(e) => setForm({ ...form, assigned_to: e.target.value })}
            />
          </div>
          <div className="flex gap-2 flex-wrap">
            <Button size="sm" onClick={handleCreate} disabled={creating}>
              {creating ? "创建中..." : "创建"}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setShowForm(false)}>取消</Button>
          </div>
        </Card>
      )}

      {/* 状态筛选 */}
      <div className="flex flex-wrap gap-2 mb-4">
        <Badge
          variant={statusFilter === "" ? "default" : "outline"}
          className="cursor-pointer"
          onClick={() => setStatusFilter("")}
        >
          全部
        </Badge>
        {STATUS_OPTIONS.map((s) => (
          <Badge
            key={s}
            variant={statusFilter === s ? "default" : "outline"}
            className="cursor-pointer"
            onClick={() => setStatusFilter(s)}
          >
            {STATUS_LABELS[s] ?? s}
          </Badge>
        ))}
      </div>

      {/* 任务列表 */}
      <Card className="mb-6 overflow-hidden">
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2">
            <ClipboardList className="size-4" /> 任务列表
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {tasksError ? (
            <div className="px-4 py-6 text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
          ) : tasksLoading ? (
            <div className="space-y-3 p-4">
              {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-12 rounded-lg" />)}
            </div>
          ) : tasks.length === 0 ? (
            <EmptyState title="暂无任务" description="点击右上角「新建任务」创建协作任务" />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>类型</TableHead>
                  <TableHead>目标</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>创建时间</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {tasks.map((t) => (
                  <TableRow key={t.id}>
                    <TableCell><span className="font-mono text-xs">{t.id}</span></TableCell>
                    <TableCell><Badge variant="outline" className="text-[10px]">{t.task_type}</Badge></TableCell>
                    <TableCell><span className="text-xs whitespace-nowrap">{t.target || "local"}</span></TableCell>
                    <TableCell>
                      <Badge variant="outline" className={STATUS_BADGE[t.status] ?? ""}>
                        {STATUS_LABELS[t.status] ?? t.status}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <span className="text-xs text-muted-foreground whitespace-nowrap">{fmtTime(t.created_at)}</span>
                    </TableCell>
                    <TableCell>
                      <div className="flex justify-end gap-1">
                        <Button
                          size="icon-sm"
                          variant="outline"
                          title={t.status === "running" ? "正在执行" : "执行"}
                          disabled={executingId === t.id || t.status === "running"}
                          onClick={() => handleExecute(t)}
                        >
                          {executingId === t.id ? <RefreshCw className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
                        </Button>
                        <Button size="icon-sm" variant="outline" title="查看结果" onClick={() => setDetail(t)}>
                          <Eye className="size-3.5" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* 审计日志 */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2">
            <FileText className="size-4" /> 审计日志
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {auditError ? (
            <div className="px-4 py-6 text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
          ) : auditLoading ? (
            <div className="space-y-2 p-4">
              {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-9 rounded-lg" />)}
            </div>
          ) : audit.length === 0 ? (
            <EmptyState title="暂无审计记录" description="协作操作日志将在此显示" icon={<FileText className="size-10" />} />
          ) : (
            <div className="divide-y text-sm">
              {audit.map((entry, i) => {
                const detailText = formatDetails(entry.details);
                const time = entry.timestamp
                  ? new Date(entry.timestamp * 1000).toLocaleString()
                  : "";
                return (
                  <div key={i} className="flex items-start gap-3 px-4 py-2.5">
                    <Badge variant="secondary" className="text-[10px] shrink-0 mt-0.5">
                      {entry.action.replace(/^collab_/, "")}
                    </Badge>
                    <div className="flex-1 min-w-0">
                      <div className="text-xs text-muted-foreground mb-0.5">
                        {entry.user_id}
                        {time && <span className="ml-2">{time}</span>}
                      </div>
                      {detailText && (
                        <details className="mt-1">
                          <summary className="text-xs text-muted-foreground/80 cursor-pointer select-none hover:text-muted-foreground">
                            详情
                          </summary>
                          <pre className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground bg-muted/50 rounded p-2 whitespace-pre-wrap break-words">
                            {detailText}
                          </pre>
                        </details>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* 任务详情 */}
      <Dialog open={!!detail} onOpenChange={(open) => { if (!open) setDetail(null); }}>
        <DialogContent className="sm:max-w-lg max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>任务详情</DialogTitle>
            <DialogDescription>{detail ? `#${detail.id}` : ""}</DialogDescription>
          </DialogHeader>
          {detail && (
            <div className="space-y-3">
              <div className="flex items-center gap-2 flex-wrap">
                <Badge variant="outline" className={STATUS_BADGE[detail.status] ?? ""}>
                  {STATUS_LABELS[detail.status] ?? detail.status}
                </Badge>
                <Badge variant="outline" className="text-[10px]">{detail.task_type}</Badge>
                {detail.assigned_to && <Badge variant="secondary" className="text-[10px]">指派: {detail.assigned_to}</Badge>}
              </div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                <div className="text-muted-foreground">目标</div>
                <div className="break-words">{detail.target || "local"}</div>
                <div className="text-muted-foreground">创建者</div>
                <div>{detail.created_by || "-"}</div>
                <div className="text-muted-foreground">创建时间</div>
                <div>{detail.created_at ? new Date(detail.created_at * 1000).toLocaleString() : "-"}</div>
                <div className="text-muted-foreground">更新时间</div>
                <div>{detail.updated_at ? new Date(detail.updated_at * 1000).toLocaleString() : "-"}</div>
                <div className="text-muted-foreground">成本</div>
                <div>{fmtCost(detail.cost ?? 0)}</div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground mb-1">提示词</div>
                <pre className="text-xs leading-relaxed whitespace-pre-wrap break-words bg-muted/50 rounded-lg p-3">{detail.prompt}</pre>
              </div>
              {detail.error && (
                <div>
                  <div className="text-xs text-destructive mb-1">错误</div>
                  <pre className="text-xs leading-relaxed whitespace-pre-wrap break-words text-destructive bg-destructive/10 rounded-lg p-3">{detail.error}</pre>
                </div>
              )}
              {detail.result && (
                <div>
                  <div className="text-xs text-muted-foreground mb-1">结果</div>
                  <pre className="text-xs leading-relaxed whitespace-pre-wrap break-words bg-muted/50 rounded-lg p-3">{detail.result}</pre>
                </div>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
