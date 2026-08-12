import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { Card, CardContent } from "@/components/ui/card";

interface StatCardProps {
  label: string;
  value: string | number;
  icon?: React.ReactNode;
  trend?: string;
  /** 副文本（如"今日 $x"） */
  sub?: string;
  /** 高亮强调 */
  accent?: boolean;
  /** 可选跳转目标：有值时可点击导航到对应页面 */
  to?: string;
}

export function StatCard({ label, value, icon, trend, sub, accent, to }: StatCardProps) {
  const navigate = useNavigate();
  return (
    <Card
      className={cn(
        "shadow-sm",
        accent && "border-primary/25 bg-primary/5",
        to && "cursor-pointer transition-colors hover:bg-muted/40"
      )}
      role={to ? "link" : undefined}
      tabIndex={to ? 0 : undefined}
      onClick={to ? () => navigate(to) : undefined}
      onKeyDown={to ? (e) => { if (e.key === "Enter") navigate(to); } : undefined}
    >
      <CardContent className="flex items-center gap-4 p-4">
        {icon && (
          <div className={cn(
            "flex items-center justify-center size-10 rounded-lg shrink-0",
            accent ? "bg-primary/10 text-primary" : "bg-primary/10 text-primary"
          )}>
            {icon}
          </div>
        )}
        <div className="min-w-0">
          <div className="text-2xl font-semibold tracking-tight">{value}</div>
          <div className="text-sm text-muted-foreground truncate">{label}</div>
          {sub && <div className="text-xs text-muted-foreground truncate">{sub}</div>}
        </div>
        {trend && (
          <div className="ml-auto text-xs text-emerald-600 dark:text-emerald-400 shrink-0">
            {trend}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
