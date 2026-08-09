import { Card, CardContent } from "@/components/ui/card";

interface StatCardProps {
  label: string;
  value: string | number;
  icon?: React.ReactNode;
  trend?: string;
}

export function StatCard({ label, value, icon, trend }: StatCardProps) {
  return (
    <Card>
      <CardContent className="flex items-center gap-4 p-4">
        {icon && (
          <div className="flex items-center justify-center size-10 rounded-lg bg-primary/10 text-primary shrink-0">
            {icon}
          </div>
        )}
        <div className="min-w-0">
          <div className="text-2xl font-semibold tracking-tight">{value}</div>
          <div className="text-sm text-muted-foreground truncate">{label}</div>
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