import { Shield } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useSecurityUsers, useSecuritySetTier } from "@/api/hooks";
import { RoleGuard } from "@/components/auth/RoleGuard";
import { toast } from "sonner";

export default function SecurityPage() {
  const { data, isLoading, isError } = useSecurityUsers();
  const setTierMut = useSecuritySetTier();

  const users = data?.users ?? [];

  return (
    <RoleGuard role="admin">
      <>
      <PageHeader title="安全管理" description="访问控制与审计" />

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
    </RoleGuard>
  );
}
