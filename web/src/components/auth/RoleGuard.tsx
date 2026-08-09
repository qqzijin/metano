import { useAuth } from "../../contexts/AuthContext";

export function RoleGuard({ role, children }: { role: string; children: React.ReactNode }) {
  const { user } = useAuth();
  if (!user || user.role !== role) {
    return <div style={{ padding: 24, color: "#ff4d4f" }}>权限不足</div>;
  }
  return <>{children}</>;
}