import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
  AreaChart, Area, PieChart, Pie, Cell,
} from "recharts";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useAnalytics } from "@/api/hooks";
import { fmtTokens, fmtCost, fmtTime } from "@/api/client";
import { Activity, DollarSign, MessageSquare, Zap, Cpu, History, Layers } from "lucide-react";

const DAYS = [7, 30, 90] as const;
// Brand palette — kept intentionally: these hues read well in both themes.
const PIE_COLORS = ["#3b82f6", "#60a5fa", "#06b6d4", "#f59e0b", "#10b981"];

// Theme-aware chart styling. These resolve against the app's CSS variables
// (defined in index.css for both light & dark themes), so charts follow the
// active theme automatically without re-rendering.
const AXIS_TICK = { fontSize: 11, fill: "var(--muted-foreground)" };
const AXIS_STROKE = "var(--border)";
const GRID_STROKE = "var(--border)";
const TOOLTIP_STYLE = {
  background: "var(--popover)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  fontSize: 12,
  color: "var(--popover-foreground)",
};

interface MiniStatProps {
  icon: React.ReactNode;
  label: string;
  value: string | number;
  sub?: string;
  accent?: boolean;
}

function MiniStat({ icon, label, value, sub, accent }: MiniStatProps) {
  return (
    <Card className={accent ? "border-primary/30 bg-primary/5" : ""}>
      <CardContent className="p-4">
        <div className="flex items-center gap-3">
          <div className={`flex items-center justify-center size-9 rounded-lg shrink-0 ${accent ? "bg-primary/20 text-primary" : "bg-muted text-muted-foreground"}`}>
            {icon}
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-xs text-muted-foreground mb-0.5">{label}</div>
            <div className="text-xl font-semibold tracking-tight">{value}</div>
            {sub && <div className="text-xs text-muted-foreground mt-0.5">{sub}</div>}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export default function AnalyticsPage() {
  const [days, setDays] = useState<number>(7);
  const navigate = useNavigate();
  const { data, isLoading, isError } = useAnalytics(days);

  if (isError) return <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>;
  if (isLoading) return <div className="space-y-4">{Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-28 rounded-lg" />)}</div>;

  const total = data?.total ?? {};
  const daily = (data?.daily ?? []).map((d) => ({
    ...d,
    day: (d.day ?? "").slice(-5),
    input_tokens: d.input_tokens ?? 0,
    output_tokens: d.output_tokens ?? 0,
    total_tokens: (d.input_tokens ?? 0) + (d.output_tokens ?? 0),
    estimated_cost_usd: d.estimated_cost_usd ?? 0,
    session_count: d.session_count ?? 0,
  }));

  const byModel = (data?.by_model ?? [])
    .filter((m) => m.session_count > 0 && m.model && m.model !== "<synthetic>")
    .map((m) => ({
      ...m,
      name: m.model ?? "unknown",
      total_tokens: (m.input_tokens ?? 0) + (m.output_tokens ?? 0),
    }));

  // 单次对话 token 排行：每条会话（跨渠道，含本地 Claude Code 会话）
  const topSessions = (data?.sessions ?? []).slice(0, 10).map((s) => ({
    ...s,
    input_tokens: s.input_tokens ?? 0,
    output_tokens: s.output_tokens ?? 0,
    cache_read_tokens: s.cache_read_tokens ?? 0,
    total_tokens: (s.input_tokens ?? 0) + (s.output_tokens ?? 0) + (s.cache_read_tokens ?? 0),
  }));

  // 渠道/项目分布：区分网关渠道（feishu/web/wechat 等）与本地 Claude Code 会话
  const GATEWAY_PROJECTS = new Set(["feishu", "web", "wechat_ilink", "wechat", "qq", "telegram", "discord"]);
  const isGateway = (p: string) => GATEWAY_PROJECTS.has(p) || p.includes("metano");
  const byProject = (data?.by_project ?? []).map((p) => ({
    ...p,
    total_tokens: (p.input_tokens ?? 0) + (p.output_tokens ?? 0) + (p.cache_read_tokens ?? 0),
    gateway: isGateway(p.project ?? ""),
  }));

  // Compute averages for trend
  const avgCost = daily.length > 0 ? (total.estimated_cost_usd ?? 0) / daily.length : 0;
  const avgSessions = daily.length > 0 ? (total.session_count ?? 0) / daily.length : 0;

  return (
    <>
      <PageHeader title="数据统计" description="使用统计与趋势分析" />

      <div className="flex gap-2 mb-6">
        {DAYS.map((d) => (
          <Button key={d} size="sm" variant={days === d ? "default" : "outline"} onClick={() => setDays(d)}>
            近{d}天
          </Button>
        ))}
      </div>

      {/* Top stats row */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mb-6">
        <MiniStat icon={<Activity className="size-4" />} label="会话数" value={total.session_count ?? 0} sub={`日均 ${avgSessions.toFixed(1)}`} />
        <MiniStat icon={<MessageSquare className="size-4" />} label="消息数" value={(total.message_count ?? 0).toLocaleString()} />
        <MiniStat icon={<Zap className="size-4" />} label="工具调用" value={(total.tool_call_count ?? 0).toLocaleString()} />
        <MiniStat icon={<Cpu className="size-4" />} label="输入令牌" value={fmtTokens(total.input_tokens ?? 0)} />
        <MiniStat icon={<Cpu className="size-4" />} label="输出令牌" value={fmtTokens(total.output_tokens ?? 0)} />
        <MiniStat icon={<DollarSign className="size-4" />} label="总费用" value={fmtCost(total.estimated_cost_usd ?? 0)} sub={`日均 ${fmtCost(avgCost)}`} accent />
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
        {/* Cost trend */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <DollarSign className="size-4 text-primary" />
              每日费用趋势
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            {daily.length > 0 ? (
              <ResponsiveContainer width="100%" height={260}>
                <AreaChart data={daily}>
                  <defs>
                    <linearGradient id="costGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} opacity={0.5} />
                  <XAxis dataKey="day" tick={AXIS_TICK} stroke={AXIS_STROKE} />
                  <YAxis tickFormatter={(v: number) => fmtCost(v)} tick={AXIS_TICK} stroke={AXIS_STROKE} width={60} />
                  <Tooltip
                    formatter={(value: unknown) => [fmtCost(Number(value)), "费用"]}
                    labelFormatter={(label: unknown) => `${String(label)}`}
                    contentStyle={TOOLTIP_STYLE}
                  />
                  <Area type="monotone" dataKey="estimated_cost_usd" stroke="#3b82f6" fill="url(#costGrad)" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <div className="h-[260px] flex items-center justify-center text-sm text-muted-foreground">暂无数据</div>
            )}
          </CardContent>
        </Card>

        {/* Session count trend */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <Activity className="size-4 text-chart-2" />
              每日会话数
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            {daily.length > 0 ? (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={daily}>
                  <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} opacity={0.5} />
                  <XAxis dataKey="day" tick={AXIS_TICK} stroke={AXIS_STROKE} />
                  <YAxis tick={AXIS_TICK} stroke={AXIS_STROKE} width={40} />
                  <Tooltip
                    contentStyle={TOOLTIP_STYLE}
                  />
                  <Bar dataKey="session_count" name="会话数" fill="#3b82f6" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <div className="h-[260px] flex items-center justify-center text-sm text-muted-foreground">暂无数据</div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Token usage chart */}
      <Card className="mb-4">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm flex items-center gap-2">
            <Cpu className="size-4 text-chart-3" />
            每日总用量 · 令牌
          </CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          {daily.length > 0 ? (
            <ResponsiveContainer width="100%" height={280}>
              <BarChart data={daily}>
                <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} opacity={0.5} />
                <XAxis dataKey="day" tick={AXIS_TICK} stroke={AXIS_STROKE} />
                <YAxis tickFormatter={(v: number) => fmtTokens(v)} tick={AXIS_TICK} stroke={AXIS_STROKE} width={60} />
                <Tooltip
                  formatter={(value: unknown, name: unknown) => [fmtTokens(Number(value)), String(name)]}
                  contentStyle={TOOLTIP_STYLE}
                />
                <Bar dataKey="input_tokens" name="输入" fill="#3b82f6" radius={[4, 4, 0, 0]} />
                <Bar dataKey="output_tokens" name="输出" fill="#06b6d4" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-[280px] flex items-center justify-center text-sm text-muted-foreground">暂无数据</div>
          )}
        </CardContent>
      </Card>

      {/* Bottom row: model pie + model table */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Model pie chart */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">模型分布</CardTitle>
          </CardHeader>
          <CardContent className="pt-0 flex flex-col items-center">
            {byModel.length > 0 ? (
              <ResponsiveContainer width="100%" height={220}>
                <PieChart>
                  <Pie data={byModel} dataKey="session_count" nameKey="name" cx="50%" cy="50%" outerRadius={80} innerRadius={40} paddingAngle={2} strokeWidth={2} stroke="var(--card)">
                    {byModel.map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}
                  </Pie>
                  <Tooltip contentStyle={TOOLTIP_STYLE} />
                </PieChart>
              </ResponsiveContainer>
            ) : (
              <div className="h-[220px] flex items-center justify-center text-sm text-muted-foreground">暂无数据</div>
            )}
            <div className="flex flex-wrap gap-2 mt-2">
              {byModel.map((m, i) => (
                <Badge key={m.name} variant="secondary" className="text-xs">
                  <span className={`size-2 rounded-full shrink-0 mr-1`} style={{ backgroundColor: PIE_COLORS[i % PIE_COLORS.length] }} />
                  {m.name} ({m.session_count})
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>

        {/* Model detail table */}
        <Card className="lg:col-span-2">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">模型详细统计</CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            {byModel.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b text-left text-xs text-muted-foreground">
                      <th className="pb-2 pr-3">模型</th>
                      <th className="pb-2 pr-3 text-right">会话</th>
                      <th className="pb-2 pr-3 text-right">输入</th>
                      <th className="pb-2 pr-3 text-right">输出</th>
                      <th className="pb-2 pr-3 text-right">总令牌</th>
                      <th className="pb-2 text-right">费用</th>
                    </tr>
                  </thead>
                  <tbody>
                    {byModel.map((m) => (
                      <tr key={m.name} className="border-b last:border-0 hover:bg-muted/50">
                        <td className="py-2.5 pr-3 font-mono text-xs font-medium">{m.name}</td>
                        <td className="py-2.5 pr-3 text-right">{m.session_count}</td>
                        <td className="py-2.5 pr-3 text-right">{fmtTokens(m.input_tokens ?? 0)}</td>
                        <td className="py-2.5 pr-3 text-right">{fmtTokens(m.output_tokens ?? 0)}</td>
                        <td className="py-2.5 pr-3 text-right">{fmtTokens(m.total_tokens)}</td>
                        <td className="py-2.5 text-right font-medium">{fmtCost(m.estimated_cost_usd ?? 0)}</td>
                      </tr>
                    ))}
                    {/* Total row */}
                    <tr className="bg-muted/30 font-medium">
                      <td className="py-2.5 pr-3 text-xs">合计</td>
                      <td className="py-2.5 pr-3 text-right">{total.session_count ?? 0}</td>
                      <td className="py-2.5 pr-3 text-right">{fmtTokens(total.input_tokens ?? 0)}</td>
                      <td className="py-2.5 pr-3 text-right">{fmtTokens(total.output_tokens ?? 0)}</td>
                      <td className="py-2.5 pr-3 text-right">{fmtTokens((total.input_tokens ?? 0) + (total.output_tokens ?? 0))}</td>
                      <td className="py-2.5 text-right">{fmtCost(total.estimated_cost_usd ?? 0)}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="h-[200px] flex items-center justify-center text-sm text-muted-foreground">暂无数据</div>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}