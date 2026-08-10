import { useState, type FormEvent } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useNavigate, useLocation } from "react-router-dom";

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: { pathname: string } })?.from?.pathname || "/";

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      await login(username, password);
      navigate(from, { replace: true });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{
      minHeight: "100vh",
      background: "#0a0a0f",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      padding: 16,
    }}>
      <div style={{
        width: "min(380px, 100%)",
        boxSizing: "border-box",
        background: "#111118",
        borderRadius: 16,
        border: "1px solid rgba(170,59,255,0.2)",
        boxShadow: "0 0 60px rgba(170,59,255,0.08)",
        padding: "32px 24px",
      }}>
        <div style={{ textAlign: "center", marginBottom: 32 }}>
          <div style={{
            width: 48, height: 48, borderRadius: 12,
            background: "rgba(170,59,255,0.15)",
            display: "inline-flex", alignItems: "center", justifyContent: "center",
            marginBottom: 16,
          }}>
            <span style={{ fontSize: 24, fontWeight: 700, color: "#aa3bff" }}>C</span>
          </div>
          <h1 style={{ color: "#e4e4e7", fontSize: 22, fontWeight: 600, margin: 0 }}>
            CC Bridge
          </h1>
          <p style={{ color: "#71717a", fontSize: 14, marginTop: 8 }}>
            登录以继续访问
          </p>
        </div>

        {error && (
          <div style={{
            background: "rgba(255,77,79,0.1)",
            border: "1px solid rgba(255,77,79,0.3)",
            borderRadius: 8, padding: "10px 14px",
            color: "#ff4d4f", fontSize: 13, marginBottom: 20,
          }}>
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <div style={{ marginBottom: 20 }}>
            <label style={{ color: "#a1a1aa", fontSize: 13, display: "block", marginBottom: 6 }}>
              用户名
            </label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              disabled={loading}
              style={{
                width: "100%", padding: "10px 14px",
                background: "#1a1a2e", border: "1px solid #2a2a3e",
                borderRadius: 8, color: "#e4e4e7", fontSize: 14,
                outline: "none",
                boxSizing: "border-box",
              }}
            />
          </div>
          <div style={{ marginBottom: 24 }}>
            <label style={{ color: "#a1a1aa", fontSize: 13, display: "block", marginBottom: 6 }}>
              密码
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={loading}
              style={{
                width: "100%", padding: "10px 14px",
                background: "#1a1a2e", border: "1px solid #2a2a3e",
                borderRadius: 8, color: "#e4e4e7", fontSize: 14,
                outline: "none",
                boxSizing: "border-box",
              }}
            />
          </div>
          <button
            type="submit"
            disabled={loading}
            style={{
              width: "100%", padding: "12px 0",
              background: loading ? "#555" : "linear-gradient(135deg, #7c3aed, #aa3bff)",
              border: "none", borderRadius: 8,
              color: "#fff", fontSize: 15, fontWeight: 600,
              cursor: loading ? "not-allowed" : "pointer",
              boxShadow: loading ? "none" : "0 4px 20px rgba(170,59,255,0.3)",
            }}
          >
            {loading ? "登录中..." : "登录"}
          </button>
        </form>

        <p style={{
          textAlign: "center", color: "#52525b", fontSize: 12,
          marginTop: 28,
        }}>
          Powered by Claude Code
        </p>
      </div>
    </div>
  );
}