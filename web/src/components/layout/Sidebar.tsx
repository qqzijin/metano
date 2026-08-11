import { useState } from "react";
import { NavLink } from "react-router-dom";
import {
  BarChart3, BookOpen, Bot, Brain, Clock, Cpu, Dna,
  FileText, Globe, Home, LayoutDashboard, MessageSquare,
  Mic, Network, Puzzle, Search, Settings, Shield, User, Users, Zap,
  X, ChevronsLeft, ChevronsRight, Sun, Moon, Monitor, LogOut, KeyRound,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useTheme } from "@/hooks/useTheme";
import { VERSION } from "@/version";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Logo } from "@/components/Logo";
import { useAuth } from "@/contexts/AuthContext";

const NAV_GROUPS = [
  {
    label: "概览",
    items: [
      { to: "/", icon: LayoutDashboard, label: "仪表盘", end: true },
      { to: "/chat", icon: MessageSquare, label: "聊天" },
      { to: "/sessions", icon: Bot, label: "会话" },
    ],
  },
  {
    label: "能力",
    items: [
      { to: "/skills", icon: Zap, label: "技能" },
      { to: "/models", icon: Cpu, label: "模型" },
      { to: "/knowledge", icon: BookOpen, label: "知识库" },
      { to: "/knowledge-graph", icon: Network, label: "知识图谱" },
      { to: "/evolution", icon: Dna, label: "进化系统" },
      { to: "/browser", icon: Globe, label: "浏览器" },
      { to: "/voice", icon: Mic, label: "语音" },
      { to: "/home", icon: Home, label: "智能家居" },
      { to: "/memory", icon: Brain, label: "记忆系统" },
      { to: "/mcp-tools", icon: Puzzle, label: "MCP工具" },
    ],
  },
  {
    label: "系统",
    items: [
      { to: "/security", icon: Shield, label: "安全管理" },
      { to: "/profiles", icon: User, label: "用户画像" },
      { to: "/cron", icon: Clock, label: "定时任务" },
      { to: "/config", icon: Settings, label: "配置" },
    ],
  },
  {
    label: "数据",
    items: [
      { to: "/analytics", icon: BarChart3, label: "数据统计" },
      { to: "/logs", icon: FileText, label: "日志" },
      { to: "/search", icon: Search, label: "搜索" },
      { to: "/collab", icon: Users, label: "协作" },
    ],
  },
];

interface SidebarProps {
  collapsed: boolean;
  onToggle: () => void;
  connected: boolean;
  /** When set, the sidebar renders as a non-collapsible drawer (mobile sheet). */
  onNavigate?: () => void;
  /** Mobile only — render a close button in the brand row. */
  onClose?: () => void;
}

function ChangePasswordDialog({ onClose }: { onClose: () => void }) {
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (newPassword.length < 6) {
      setError("新密码长度至少6位");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const res = await fetch("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
        credentials: "include",
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({ detail: "修改失败" }));
        throw new Error(data.detail || "修改失败");
      }
      setSuccess(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "修改失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>修改密码</DialogTitle>
        </DialogHeader>

        {success ? (
          <div className="py-2 text-center">
            <p className="text-sm font-medium text-emerald-500">密码修改成功</p>
            <Button className="mt-4" onClick={onClose}>关闭</Button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            {error && (
              <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                {error}
              </div>
            )}
            <div className="space-y-1.5">
              <label className="text-xs text-muted-foreground">原密码</label>
              <Input
                type="password"
                value={oldPassword}
                onChange={(e) => setOldPassword(e.target.value)}
                disabled={loading}
                required
                autoComplete="current-password"
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs text-muted-foreground">新密码</label>
              <Input
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                disabled={loading}
                required
                minLength={6}
                placeholder="至少6位"
                autoComplete="new-password"
              />
            </div>
            <DialogFooter className="flex gap-2 sm:justify-end">
              <Button type="button" variant="outline" onClick={onClose} disabled={loading}>
                取消
              </Button>
              <Button type="submit" disabled={loading}>
                {loading ? "提交中..." : "确认修改"}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function Sidebar({ collapsed, onToggle, connected, onNavigate, onClose }: SidebarProps) {
  const { theme, setTheme } = useTheme();
  const { user, logout } = useAuth();
  const [showChangePwd, setShowChangePwd] = useState(false);
  const mobile = !!onNavigate;

  return (
    <>
      {showChangePwd && <ChangePasswordDialog onClose={() => setShowChangePwd(false)} />}
      <aside
        className={cn(
          "flex flex-col border-r border-border bg-sidebar h-full transition-all duration-200",
          mobile ? "w-full" : collapsed ? "w-16" : "w-56"
        )}
      >
        {/* Brand */}
        <div className="flex items-center gap-2 px-4 h-14 shrink-0">
          <Logo className="size-5 shrink-0" />
          {!collapsed && (
            <div className="min-w-0 flex-1 leading-none">
              <span className="font-semibold text-sm">metano</span>
              <span className="text-xs text-muted-foreground ml-1">v{VERSION}</span>
            </div>
          )}
          {onClose && (
            <Button variant="ghost" size="icon" className="size-8 shrink-0 rounded-full" onClick={onClose} aria-label="关闭菜单">
              <X className="size-4" />
            </Button>
          )}
        </div>

        <Separator />

        {/* Nav */}
        <nav className="flex-1 overflow-y-auto py-2">
          {NAV_GROUPS.map((group) => (
            <div key={group.label} className="mb-2">
              {!collapsed && (
                <div className="px-4 mb-1 text-xs font-medium text-muted-foreground uppercase tracking-wider">
                  {group.label}
                </div>
              )}
              {group.items.map(({ to, icon: Icon, label, end }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={end}
                  onClick={onNavigate}
                  className={({ isActive }) =>
                    cn(
                      "flex items-center gap-3 px-4 h-9 text-sm rounded-md mx-1 transition-colors",
                      isActive
                        ? "bg-sidebar-accent text-sidebar-accent-foreground font-medium"
                        : "text-sidebar-foreground hover:bg-sidebar-accent/50",
                      collapsed && "justify-center px-0"
                    )
                  }
                >
                  <Icon className="size-4 shrink-0" />
                  {!collapsed && <span>{label}</span>}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        <Separator />

        {/* Footer */}
        <div className="px-3 py-2 shrink-0">
          {!collapsed && user && (
            <div className="flex items-center justify-between mb-2 px-1">
              <div className="flex items-center gap-2">
                <div className="flex size-7 items-center justify-center rounded-full bg-primary/15">
                  <span className="text-xs font-medium text-primary">
                    {user.username.charAt(0).toUpperCase()}
                  </span>
                </div>
                <div className="leading-none">
                  <p className="text-xs text-foreground">{user.username}</p>
                  <p className="text-xs text-muted-foreground">{user.role}</p>
                </div>
              </div>
              <div className="flex gap-1">
                <Button variant="ghost" size="icon" className="size-7" onClick={() => setShowChangePwd(true)} title="修改密码" aria-label="修改密码">
                  <KeyRound className="size-3.5 text-muted-foreground" />
                </Button>
                <Button variant="ghost" size="icon" className="size-7" onClick={logout} title="登出" aria-label="登出">
                  <LogOut className="size-3.5 text-muted-foreground" />
                </Button>
              </div>
            </div>
          )}
          <div className="flex items-center gap-2 px-1">
            {/* Theme toggle */}
            <Button
              variant="ghost"
              size="icon"
              className="size-7"
              aria-label="切换主题"
              onClick={() => {
                const next = theme === "dark" ? "light" : theme === "light" ? "system" : "dark";
                setTheme(next);
              }}
            >
              {theme === "dark" ? <Moon className="size-3.5" /> : theme === "light" ? <Sun className="size-3.5" /> : <Monitor className="size-3.5" />}
            </Button>

            {/* Connection status */}
            {!collapsed && (
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <span className={cn("size-2 rounded-full", connected ? "bg-emerald-500" : "bg-destructive")} />
                {connected ? "已连接" : "已断开"}
              </div>
            )}

            {/* Collapse toggle (desktop only) */}
            {!mobile && (
              <Button variant="ghost" size="icon" className="size-7 ml-auto" onClick={onToggle} aria-label={collapsed ? "展开侧边栏" : "折叠侧边栏"}>
                {collapsed ? <ChevronsRight className="size-3.5" /> : <ChevronsLeft className="size-3.5" />}
              </Button>
            )}
          </div>
        </div>
      </aside>
    </>
  );
}