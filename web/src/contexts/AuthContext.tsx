import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { refreshAuthSession } from "@/api/client";
import { cancelChatStream, resetChatStreamState } from "@/lib/chatStream";

interface User {
  username: string;
  role: string;
}

interface AuthContextType {
  user: User | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

// Backend ACCESS_TOKEN_EXPIRE_MINUTES = 15; renew well before it lapses so an
// open page never crosses the expiry boundary mid-use.
const ACCESS_REFRESH_INTERVAL_MS = 9 * 60 * 1000;

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // F-12: an open page may sit across the 15-min access-token expiry. Try a
  // silent refresh BEFORE trusting a negative `/api/auth/me` so a merely
  // expired token does not boot the user to the login screen on reload.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      await refreshAuthSession();
      if (cancelled) return;
      try {
        const res = await fetch("/api/auth/me", { credentials: "include" });
        if (cancelled) return;
        setUser(res.ok ? await res.json() : null);
      } catch {
        if (!cancelled) setUser(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Proactively renew the access token before it expires. The refresh token
  // cookie is HttpOnly + path-scoped to /api/auth/refresh, so this endpoint is
  // the only way the frontend can keep the session alive without a 401 mid-use.
  useEffect(() => {
    if (!user) return;
    // Immediately top up once (covers a session left open across the expiry),
    // then keep it fresh on a timer for as long as the user is signed in.
    refreshAuthSession();
    const id = window.setInterval(() => {
      refreshAuthSession();
    }, ACCESS_REFRESH_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [user]);

  useEffect(() => {
    const handler = () => {
      // Session died mid-use (401 even after refresh): purge per-user caches and
      // stop any in-flight stream so the next account cannot see this user's data.
      queryClient.clear();
      cancelChatStream();
      resetChatStreamState();
      setUser(null);
      navigate("/login");
    };
    window.addEventListener("auth:unauthorized", handler);
    return () => window.removeEventListener("auth:unauthorized", handler);
  }, [navigate, queryClient]);

  const login = useCallback(async (username: string, password: string) => {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
      credentials: "include",
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ detail: "登录失败" }));
      throw new Error(data.detail || "登录失败");
    }
    const data = await res.json();
    // M-07: an account switch must never inherit the previous account's cached
    // queries or module-level stream state.
    queryClient.clear();
    cancelChatStream();
    resetChatStreamState();
    setUser(data);
  }, [queryClient]);

  const logout = useCallback(async () => {
    await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
    queryClient.clear();
    cancelChatStream();
    resetChatStreamState();
    setUser(null);
    navigate("/login");
  }, [navigate, queryClient]);

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components -- context consumer hook must colocate with AuthProvider
export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}