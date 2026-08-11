import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { refreshAuthSession } from "@/api/client";

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

  useEffect(() => {
    fetch("/api/auth/me", { credentials: "include" })
      .then((res) => res.ok ? res.json() : null)
      .then((data) => setUser(data))
      .catch(() => setUser(null))
      .finally(() => setLoading(false));
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
      setUser(null);
      navigate("/login");
    };
    window.addEventListener("auth:unauthorized", handler);
    return () => window.removeEventListener("auth:unauthorized", handler);
  }, [navigate]);

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
    setUser(data);
  }, []);

  const logout = useCallback(async () => {
    await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
    setUser(null);
    navigate("/login");
  }, [navigate]);

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}