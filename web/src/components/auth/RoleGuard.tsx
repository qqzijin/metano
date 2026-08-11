import { useAuth } from "../../contexts/AuthContext";

export function RoleGuard({ role, children }: { role: string; children: React.ReactNode }) {
  const { user } = useAuth();
  if (!user || user.role !== role) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <div className="text-sm font-medium text-destructive">无权限</div>
        <div className="mt-1 text-xs text-muted-foreground">此页面仅管理员可访问</div>
      </div>
    );
  }
  return <>{children}</>;
}