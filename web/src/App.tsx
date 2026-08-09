import { useState } from "react";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ThemeProvider } from "@/hooks/useTheme";
import { Sidebar } from "@/components/layout/Sidebar";
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
  const { connected } = useWebSocket();

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem("sidebar-collapsed", String(next));
  };

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar
        collapsed={collapsed}
        onToggle={toggle}
        connected={connected}
      />
      <main className="flex-1 overflow-auto p-6">
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
            <Route path="/config" element={<ConfigPage />} />
            <Route path="/evolution" element={<EvolutionPage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/profiles" element={<ProfilesPage />} />
            <Route path="/cron" element={<CronPage />} />
            <Route path="/security" element={<SecurityPage />} />
            <Route path="/browser" element={<BrowserPage />} />
            <Route path="/voice" element={<VoicePage />} />
            <Route path="/home" element={<SmartHomePage />} />
            <Route path="/memory" element={<MemoryPage />} />
            <Route path="/mcp-tools" element={<McpToolsPage />} />
          </Routes>
          </ErrorBoundary>
        </AuthGuard>
      </main>
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