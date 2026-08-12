import { useState } from "react";
import { Save, Settings, Shield } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { useConfig, useSecurityUsers, useSecuritySetTier } from "@/api/hooks";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchAPI } from "@/api/client";
import { RoleGuard } from "@/components/auth/RoleGuard";
import { toast } from "sonner";

/**
 * 设置中心：系统配置 + 安全管理 合并为一个页面（此前是两个独立入口）。
 */
export default function SettingsPage() {
  return (
    <RoleGuard role="admin">
      <>
        <PageHeader title="设置" description="系统配置与访问控制" />
        <Tabs defaultValue="config">
          <TabsList className="mb-4">
            <TabsTrigger value="config"><Settings className="size-3.5 mr-1" />系统配置</TabsTrigger>
            <TabsTrigger value="security"><Shield className="size-3.5 mr-1" />安全管理</TabsTrigger>
          </TabsList>
          <TabsContent value="config"><ConfigView /></TabsContent>
          <TabsContent value="security"><SecurityView /></TabsContent>
        </Tabs>
      </>
    </RoleGuard>
  );
}

/* ── 配置视图（原 ConfigPage） ── */
function ConfigView() {
  const { data, isLoading, isError } = useConfig();
  const qc = useQueryClient();
  const [edited, setEdited] = useState<string | null>(null);

  const configText = edited ?? (data ? JSON.stringify(data, null, 2) : "{}");
  const savedText = data ? JSON.stringify(data, null, 2) : "{}";
  const changed = edited !== null && edited !== savedText;

  const saveMut = useMutation({
    mutationFn: () => {
      const parsed = JSON.parse(edited!);
      return fetchAPI("/config", { method: "PUT", body: JSON.stringify({ config: parsed }) });
    },
    onSuccess: () => {
      toast.success("配置已保存");
      setEdited(null);
      qc.invalidateQueries({ queryKey: ["config"] });
    },
    onError: (e) => toast.error(`保存失败: ${e.message}`),
  });

  return (
    <>
      {isError ? (
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      ) : isLoading ? (
        <Skeleton className="h-96 rounded-lg" />
      ) : (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2">
              <Settings className="size-4 text-primary" /> 系统配置
            </CardTitle>
            <CardDescription>JSON 格式，修改后点击保存</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <Textarea
              value={configText}
              onChange={(e) => setEdited(e.target.value)}
              spellCheck={false}
              className="font-mono text-xs min-h-[300px] sm:min-h-[400px] resize-y"
            />
            <Button size="sm" disabled={!changed || saveMut.isPending} onClick={() => saveMut.mutate()}>
              <Save className="size-4 mr-1" /> {saveMut.isPending ? "保存中..." : changed ? "保存更改" : "已保存"}
            </Button>
          </CardContent>
        </Card>
      )}
    </>
  );
}

/* ── 安全视图（原 SecurityPage） ── */
function SecurityView() {
  const { data, isLoading, isError } = useSecurityUsers();
  const setTierMut = useSecuritySetTier();

  const users = data?.users ?? [];

  return (
    <>
      {isError ? (
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      ) : isLoading ? (
        <div className="space-y-3">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-14 rounded-lg" />)}</div>
      ) : users.length === 0 ? (
        <EmptyState title="暂无用户" description="用户与网关交互后将自动出现" />
      ) : (
        <div className="grid gap-3">
          {users.map((u: { user_id: string; tier: string; rate_limit_remaining?: number; blocked_count?: number }) => (
            <Card key={u.user_id}>
              <CardContent>
                <div className="flex flex-col sm:flex-row sm:items-center gap-3">
                  <div className="flex items-center gap-3 min-w-0">
                    <div className="size-9 rounded-full bg-primary/10 text-primary flex items-center justify-center shrink-0">
                      <Shield className="size-4.5" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="font-medium text-sm font-mono break-all">{u.user_id}</div>
                      <div className="text-xs text-muted-foreground mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5">
                        {u.rate_limit_remaining != null && <span>速率限制: {u.rate_limit_remaining}</span>}
                        {u.blocked_count != null && u.blocked_count > 0 && <span className="text-destructive">已阻止: {u.blocked_count}</span>}
                      </div>
                    </div>
                  </div>
                  <div className="flex gap-1 flex-wrap sm:justify-end shrink-0">
                    {["admin", "user", "guest"].map((tier) => (
                      <Button
                        key={tier}
                        size="sm"
                        variant={u.tier === tier ? "default" : "outline"}
                        className="text-xs h-8"
                        onClick={async () => {
                          try {
                            await setTierMut.mutateAsync({ userId: u.user_id, tier });
                            toast.success(`${u.user_id} → ${tier}`);
                          } catch {
                            toast.error(`设置失败: ${u.user_id}`);
                          }
                        }}
                      >
                        {tier}
                      </Button>
                    ))}
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}
