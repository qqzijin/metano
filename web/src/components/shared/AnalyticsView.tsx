import { useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
  AreaChart, Area, PieChart, Pie, Cell,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useAnalytics } from "@/api/hooks";
import { fmtTokens, fmtCost } from "@/api/client";
import {
  Activity, DollarSign, MessageSquare, Zap, Cpu, Database,
  TrendingUp, Layers, CircleDollarSign, Clock,
} from "lucide-react";

const DAYS = [7, 30, 90] as const;

// Theme-derived styling — everything resolves against CSS variables so charts
// follow the active light/dark theme automatically.
const AXIS_TICK = { fontSize: 11, fill: "var(--muted-foreground)" };
const AXIS_STROKE = "var(--border)";
const GRID_STROKE = "var(--border)";
const TOOLTIP_STYLE = {
  background: "var(--popover)",
  border: "1px solid var(--border)",
  borderRadius: 10,
  fontSize: 12,
  color: "var(--popover-foreground)",
  boxShadow: "0 8px 24px rgba(0,0,0,0.12)",
};
const PRIMARY = "var(--primary)";
const CHART = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)"];

type Tone = "primary" | "c2" | "c3" | "c4" | "rose";
const TONE_CLS: Record<Tone, string> = {
  primary: "bg-primary/10 text-primary",
  c2: "bg-chart-2/10 text-chart-2",
  c3: "bg-chart-3/10 text-chart-3",
  c4: "bg-chart-4/10 text-chart-4",
  rose: "bg-destructive/10 text-destructive",
};

interface StatCardProps {
  icon: React.ReactNode;
  label: string;
  value: string | number;
  sub?: string;
  tone?: Tone;
}

function StatCard({ icon, label, value, sub, tone = "primary" }: StatCardProps) {
  return (
    <Card>
      <CardContent className="flex items-center gap-3 p-4">
        <div className={`flex size-10 shrink-0 items-center justify-center rounded-xl ${TONE_CLS[tone]}`}>
          {icon}
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-xs text-muted-foreground">{label}</div>
          <div className="truncate text-lg font-bold tracking-tight">{value}</div>
          {sub && <div className="mt-0.5 text-[11px] text-muted-foreground">{sub}</div>}
        </div>
      </CardContent>
    </Card>
  );
}

export function AnalyticsView() {
  const [days, setDays] = useState<number>(7);
  const { data, isLoading, isError } = useAnalytics(days);

  if (isError) return <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>;
  if (isLoading) return <div className="space-y-4">{Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-24 rounded-xl" />)}</div>;

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

  const GATEWAY_PROJECTS = new Set(["feishu", "web", "wechat_ilink", "wechat", "qq", "telegram", "discord"]);
  const isGateway = (p: string) => GATEWAY_PROJECTS.has(p) || p.includes("metano");
  const byProject = (data?.by_project ?? [])
    .map((p) => ({
      ...p,
      total_tokens: (p.input_tokens ?? 0) + (p.output_tokens ?? 0) + (p.cache_read_tokens ?? 0),
      gateway: isGateway(p.project ?? ""),
    }))
    .filter((p) => p.total_tokens > 0)
    .sort((a, b) => b.total_tokens - a.total_tokens);
  const projectMax = Math.max(1, ...byProject.map((p) => p.total_tokens));

  const topSessions = (data?.sessions ?? []).slice(0, 8).map((s) => ({
    ...s,
    input_tokens: s.input_tokens ?? 0,
    output_tokens: s.output_tokens ?? 0,
    cache_read_tokens: s.cache_read_tokens ?? 0,
    total_tokens: (s.input_tokens ?? 0) + (s.output_tokens ?? 0) + (s.cache_read_tokens ?? 0),
  }));

  const avgCost = daily.length > 0 ? (total.estimated_cost_usd ?? 0) / daily.length : 0;
  const avgSessions = daily.length > 0 ? (total.session_count ?? 0) / daily.length : 0;
  const cacheRead = total.cache_read_tokens ?? 0;

  return (
    <>
      {/* Period segmented control */}
      <div className="mb-5 inline-flex rounded-full border bg-muted/40 p-0.5">
        {DAYS.map((d) => (
          <button
            key={d}
            onClick={() => setDays(d)}
            className={`rounded-full px-3.5 py-1 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 ${
              days === d ? "bg-primary text-primary-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            近{d}天
          </button>
        ))}
      </div>

      {/* Hero + stat cards */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-6 lg:gap-4">
        <Card className="col-span-2 overflow-hidden border-primary/25 bg-gradient-to-br from-primary/10 via-primary/5 to-transparent lg:col-span-2">
          <CardContent className="p-4 lg:p-5">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <DollarSign className="size-3.5" /> 总费用 · 近 {days} 天
                </div>
                <div className="mt-1.5 text-2xl font-bold tracking-tight lg:text-3xl">{fmtCost(total.estimated_cost_usd ?? 0)}</div>
                <div className="mt-1 flex flex-wrap gap-x-3 text-[11px] text-muted-foreground">
                  <span className="inline-flex items-center gap-1"><Clock className="size-3" />日均 {fmtCost(avgCost)}</span>
                  <span className="inline-flex items-center gap-1"><Activity className="size-3" />{total.session_count ?? 0} 会话</span>
                </div>
              </div>
              <div className="flex size-11 shrink-0 items-center justify-center rounded-2xl bg-primary/15 text-primary">
                <CircleDollarSign className="size-5.5" />
              </div>
            </div>
          </CardContent>
        </Card>

        <StatCard icon={<MessageSquare className="size-4.5" />} label="消息数" value={(total.message_count ?? 0).toLocaleString()} tone="c2" />
        <StatCard icon={<Zap className="size-4.5" />} label="工具调用" value={(total.tool_call_count ?? 0).toLocaleString()} tone="c3" />
        <StatCard icon={<Cpu className="size-4.5" />} label="输入令牌" value={fmtTokens(total.input_tokens ?? 0)} tone="primary" />
        <StatCard icon={<Database className="size-4.5" />} label="缓存读取" value={fmtTokens(cacheRead)} sub="prompt 缓存命中" tone="c4" />
      </div>

      {/* Charts row */}
      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <TrendingUp className="size-4 text-primary" /> 每日费用趋势
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            {daily.length > 0 ? (
              <ResponsiveContainer width="100%" height={240}>
                <AreaChart data={daily} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="costGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={PRIMARY} stopOpacity={0.22} />
                      <stop offset="100%" stopColor={PRIMARY} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <XAxis dataKey="day" tick={AXIS_TICK} axisLine={false} tickLine={false} dy={6} />
                  <YAxis tickFormatter={(v: number) => fmtCost(v)} tick={AXIS_TICK} axisLine={false} tickLine={false} width={52} />
                  <Tooltip
                    formatter={(value: unknown) => [fmtCost(Number(value)), "费用"]}
                    labelFormatter={(label: unknown) => `${String(label)}`}
                    contentStyle={TOOLTIP_STYLE}
                    cursor={{ stroke: PRIMARY, strokeDasharray: "4 4", strokeOpacity: 0.35 }}
                  />
                  <Area
                    type="monotone"
                    dataKey="estimated_cost_usd"
                    stroke={PRIMARY}
                    strokeWidth={2.5}
                    fill="url(#costGrad)"
                    dot={false}
                    activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--card)" }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-[240px] items-center justify-center text-sm text-muted-foreground">暂无数据</div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Layers className="size-4 text-chart-3" /> 每日令牌用量
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            {daily.length > 0 ? (
              <ResponsiveContainer width="100%" height={240}>
                <BarChart data={daily} margin={{ top: 8, right: 8, left: 0, bottom: 0 }} barGap={3}>
                  <XAxis dataKey="day" tick={AXIS_TICK} axisLine={false} tickLine={false} dy={6} />
                  <YAxis tickFormatter={(v: number) => fmtTokens(v)} tick={AXIS_TICK} axisLine={false} tickLine={false} width={52} />
                  <Tooltip
                    formatter={(value: unknown, name: unknown) => [fmtTokens(Number(value)), String(name)]}
                    contentStyle={TOOLTIP_STYLE}
                    cursor={{ fill: "var(--muted)", fillOpacity: 0.35 }}
                  />
                  <Bar dataKey="input_tokens" name="输入" fill={PRIMARY} radius={[5, 5, 0, 0]} maxBarSize={26} />
                  <Bar dataKey="output_tokens" name="输出" fill="var(--chart-2)" radius={[5, 5, 0, 0]} maxBarSize={26} />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-[240px] items-center justify-center text-sm text-muted-foreground">暂无数据</div>
            )}
            {daily.length > 0 && (
              <div className="mt-1.5 flex items-center justify-center gap-4 text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1.5">
                  <span className="size-2 rounded-full" style={{ background: PRIMARY }} /> 输入
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <span className="size-2 rounded-full" style={{ background: "var(--chart-2)" }} /> 输出
                </span>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Distribution row: channels + models */}
      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Activity className="size-4 text-chart-2" /> 渠道 / 项目分布
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            {byProject.length > 0 ? (
              <div className="space-y-3.5">
                {byProject.slice(0, 7).map((p, i) => (
                  <div key={p.project}>
                    <div className="mb-1 flex items-center justify-between text-xs">
                      <span className="flex min-w-0 items-center gap-1.5">
                        <span className="size-2 shrink-0 rounded-full" style={{ background: CHART[i % CHART.length] }} />
                        <span className="truncate font-medium">{p.project}</span>
                        <Badge variant="outline" className="ml-0.5 px-1 py-0 text-[10px]">
                          {p.gateway ? "网关" : "本地"}
                        </Badge>
                      </span>
                      <span className="shrink-0 text-muted-foreground">{fmtTokens(p.total_tokens)}</span>
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full transition-all"
                        style={{ width: `${(p.total_tokens / projectMax) * 100}%`, background: CHART[i % CHART.length] }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="flex h-[220px] items-center justify-center text-sm text-muted-foreground">暂无数据</div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">模型分布</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col items-center pt-0">
            {byModel.length > 0 ? (
              <>
                <ResponsiveContainer width="100%" height={180}>
                  <PieChart>
                    <Pie data={byModel} dataKey="session_count" nameKey="name" cx="50%" cy="50%" outerRadius={70} innerRadius={38} paddingAngle={2} strokeWidth={2} stroke="var(--card)">
                      {byModel.map((_, i) => <Cell key={i} fill={CHART[i % CHART.length]} />)}
                    </Pie>
                    <Tooltip contentStyle={TOOLTIP_STYLE} />
                  </PieChart>
                </ResponsiveContainer>
                <div className="mt-2 flex flex-wrap justify-center gap-1.5">
                  {byModel.map((m, i) => (
                    <Badge key={m.name} variant="secondary" className="text-[11px]">
                      <span className="mr-1 size-1.5 rounded-full" style={{ background: CHART[i % CHART.length] }} />
                      {m.name} ({m.session_count})
                    </Badge>
                  ))}
                </div>
              </>
            ) : (
              <div className="flex h-[180px] items-center justify-center text-sm text-muted-foreground">暂无数据</div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <MessageSquare className="size-4 text-chart-4" /> 高消耗会话
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            {topSessions.length > 0 ? (
              <div className="space-y-2">
                {topSessions.map((s) => (
                  <div key={s.id} className="flex items-center justify-between gap-2 rounded-lg border border-border/60 px-3 py-2">
                    <div className="min-w-0">
                      <div className="break-all font-mono text-[11px] text-muted-foreground">{s.id}</div>
                      <div className="truncate text-xs font-medium">{s.project ?? "—"}</div>
                    </div>
                    <div className="shrink-0 text-right">
                      <div className="text-xs font-semibold">{fmtTokens(s.total_tokens)}</div>
                      <div className="text-[10px] text-muted-foreground">{fmtCost(s.estimated_cost_usd ?? 0)}</div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="flex h-[180px] items-center justify-center text-sm text-muted-foreground">暂无数据</div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Model detail table */}
      <Card className="mt-4">
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
            <div className="flex h-[200px] items-center justify-center text-sm text-muted-foreground">暂无数据</div>
          )}
        </CardContent>
      </Card>
    </>
  );
}
