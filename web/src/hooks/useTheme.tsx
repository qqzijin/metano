import { createContext, useContext, useEffect, useState, useSyncExternalStore, type ReactNode } from "react";

type Theme = "light" | "dark" | "system";

interface ThemeCtx {
  theme: Theme;
  resolved: "light" | "dark";
  setTheme: (t: Theme) => void;
}

const ThemeContext = createContext<ThemeCtx>({
  theme: "system",
  resolved: "dark",
  setTheme: () => {},
});

// eslint-disable-next-line react-refresh/only-export-components -- context consumer hook must colocate with ThemeProvider
export function useTheme() {
  return useContext(ThemeContext);
}

function getSystemTheme(): "light" | "dark" {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function subscribeSystemTheme(onChange: () => void): () => void {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  mq.addEventListener("change", onChange);
  return () => mq.removeEventListener("change", onChange);
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem("theme") as Theme) || "system"
  );
  // `resolved` is derived state: the effective theme is the selected one, or
  // the live OS preference when "system". useSyncExternalStore keeps the OS
  // preference fresh without a synchronous setState-in-effect (the root cause
  // of cascading renders that react-hooks/set-state-in-effect flags).
  const systemTheme = useSyncExternalStore(subscribeSystemTheme, getSystemTheme, getSystemTheme);
  const resolved = theme === "system" ? systemTheme : theme;

  useEffect(() => {
    document.documentElement.classList.toggle("dark", resolved === "dark");
    localStorage.setItem("theme", theme);
  }, [resolved, theme]);

  return (
    <ThemeContext.Provider value={{ theme, resolved, setTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}
