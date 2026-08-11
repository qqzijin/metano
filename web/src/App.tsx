import { useState } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Menu } from "lucide-react";
import { cn } from "@/lib/utils";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ThemeProvider } from "@/hooks/useTheme";
import { Button } from "@/components/ui/button";
import { Logo } from "@/components/Logo";
import { Sidebar } from "@/components/layout/Sidebar";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import { useWebSocket } from "@/hooks/useWebSocket";
import { AuthProvider } from "@/contexts/AuthContext";
import { AuthGuard } from "@/components/auth/AuthGuard";
import { ErrorBoundary } from "@/components/ErrorBoundary";

import DashboardPage from "@/pages/DashboardPage";
import SessionsPage from "@/pages/SessionsPage";
import ChatPage from "@/pages/ChatPage";
import SearchPage from "@/pages/SearchPage";
import AnalyticsPage from "@/pages/AnalyticsPage";
import SkillsPage from "@/pages/SkillsPage";
import ModelsPage from "@/pages/ModelsPage";
import KnowledgePage from "@/pages/KnowledgePage";
import KnowledgeGraphPage from "@/pages/KnowledgeGraphPage";
import ConfigPage from "@/pages/ConfigPage";
import EvolutionPage from "@/pages/EvolutionPage";
import LogsPage from "@/pages/LogsPage";
import ProfilesPage from "@/pages/ProfilesPage";
import CronPage from "@/pages/CronPage";
import SecurityPage from "@/pages/SecurityPage";
import BrowserPage from "@/pages/BrowserPage";
import VoicePage from "@/pages/VoicePage";
import SmartHomePage from "@/pages/SmartHomePage";
import MemoryPage from "@/pages/MemoryPage";
import McpToolsPage from "@/pages/McpToolsPage";
import CollabPage from "@/pages/CollabPage";
import LoginPage from "@/pages/LoginPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10000,
      retry: 1,
    },
  },
});

function MainLayout() {
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("sidebar-collapsed") === "true"
  );
  const [mobileOpen, setMobileOpen] = useState(false);
  const { connected } = useWebSocket();

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem("sidebar-collapsed", String(next));
  };

  return (
    <div className="flex h-dvh overflow-hidden">
      {/* Desktop sidebar */}
      <div className="hidden md:block shrink-0">
        <Sidebar
          collapsed={collapsed}
          onToggle={toggle}
          connected={connected}
        />
      </div>

      {/* Mobile drawer */}
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="w-[280px] p-0 gap-0" showCloseButton={false}>
          <Sidebar
            collapsed={false}
            onToggle={() => {}}
            connected={connected}
            onNavigate={() => setMobileOpen(false)}
            onClose={() => setMobileOpen(false)}
          />
        </SheetContent>
      </Sheet>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {/* Mobile top bar */}
        <header className="md:hidden flex items-center gap-2.5 h-14 px-3 border-b border-border bg-sidebar shrink-0">
          <Button
            variant="ghost"
            size="icon"
            className="size-9"
            onClick={() => setMobileOpen(true)}
            aria-label="打开菜单"
          >
            <Menu className="size-5" />
          </Button>
          <Logo className="size-5 shrink-0" />
          <span className="font-semibold text-sm truncate">metano</span>
          <div className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground shrink-0">
            <span className={cn("size-2 rounded-full", connected ? "bg-emerald-500" : "bg-destructive")} />
            <span className="hidden sm:inline">{connected ? "已连接" : "已断开"}</span>
          </div>
        </header>

      <main className="relative min-h-0 min-w-0 flex-1 overflow-auto p-4 md:p-6">
        <AuthGuard>
          <ErrorBoundary>
            <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/sessions" element={<SessionsPage />} />
            <Route path="/search" element={<SearchPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/skills" element={<SkillsPage />} />
            <Route path="/models" element={<ModelsPage />} />
            <Route path="/knowledge" element={<KnowledgePage />} />
            <Route path="/knowledge-graph" element={<KnowledgeGraphPage />} />
            <Route path="/config" element={<ConfigPage />} />
            <Route path="/evolution" element={<EvolutionPage />} />
            <Route path="/self-modify" element={<Navigate to="/evolution" replace />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/profiles" element={<ProfilesPage />} />
            <Route path="/cron" element={<CronPage />} />
            <Route path="/security" element={<SecurityPage />} />
            <Route path="/browser" element={<BrowserPage />} />
            <Route path="/voice" element={<VoicePage />} />
            <Route path="/home" element={<SmartHomePage />} />
            <Route path="/memory" element={<MemoryPage />} />
            <Route path="/mcp-tools" element={<McpToolsPage />} />
            <Route path="/collab" element={<CollabPage />} />
          </Routes>
          </ErrorBoundary>
        </AuthGuard>
      </main>
      </div>
    </div>
  );
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <BrowserRouter>
          <AuthProvider>
            <TooltipProvider>
              <Routes>
                <Route path="/login" element={<LoginPage />} />
                <Route path="/*" element={<MainLayout />} />
              </Routes>
              <Toaster richColors position="top-right" />
            </TooltipProvider>
          </AuthProvider>
        </BrowserRouter>
      </ThemeProvider>
    </QueryClientProvider>
  );
}

export default App;