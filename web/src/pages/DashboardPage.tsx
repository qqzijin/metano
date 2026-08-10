import { Bot, Cpu, DollarSign, MessageSquare, Zap, Activity, Eye, Brain } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useStatus, useEvolution, useAnalytics } from "@/api/hooks";
import { fmtCost, fmtTokens } from "@/api/client";

/** 服务名 -> 对应功能页面（点击“服务状态”列表项跳转） */
const SERVICE_PAGE: Record<string, string> = {
  gateway: "/logs",
  evolution: "/evolution",
  rag: "/knowledge",
  tts: "/voice",
  browser: "/browser",
  home: "/home",
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
    <Card className={cn("shadow-sm", accent && "border-primary/25 bg-primary/5")}>
      <CardContent className="p-4">
        <div className="flex items-center gap-3">
          <div className={cn(
            "flex items-center justify-center size-9 rounded-lg shrink-0",
            accent ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"
          )}>
            {icon}
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-xs text-muted-foreground mb-0.5">{label}</div>
            <div className={cn("text-xl font-semibold tracking-tight", accent && "text-primary")}>{value}</div>
            {sub && <div className="text-xs text-muted-foreground mt-0.5">{sub}</div>}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export default function DashboardPage() {
  const navigate = useNavigate();
  const { data: status, isError: statusError } = useStatus();
  const { data: evo } = useEvolution();
  const { data: analytics } = useAnalytics(7);

  const services = status?.services ?? {};
  const serviceEntries = Object.entries(services);
  const daily = analytics?.daily ?? [];
  // daily 的 day 字段是 YYYY-MM-DD（按 last_active 分组），用实际今天日期匹配，
  // 不能取最后一项（最后一项是"最近活跃的一天"，今天没会话时会误显示昨天的费用）。
  const todayKey = (() => {
    const d = new Date();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${d.getFullYear()}-${m}-${day}`;
  })();
  const todayCost = daily.find((d) => d.day === todayKey)?.estimated_cost_usd ?? 0;
  const totalCost7d = analytics?.total?.estimated_cost_usd ?? 0;

  if (statusError) {
    return (
      <>
        <PageHeader title="仪表盘" description="系统概览与核心指标" />
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      </>
    );
  }

  return (
    <>
      <PageHeader title="仪表盘" description="系统概览与核心指标" />

      {/* Primary metrics */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mb-6">
        <MiniStat icon={<Bot className="size-4" />} label="总会话" value={status?.sessions ?? 0} />
        <MiniStat icon={<MessageSquare className="size-4" />} label="总消息" value={(status?.messages ?? 0).toLocaleString()} />
        <MiniStat icon={<Zap className="size-4" />} label="技能数" value={status?.skills_count ?? 0} />
        <MiniStat icon={<Brain className="size-4" />} label="信念数" value={evo?.profile_beliefs ?? 0} />
        <MiniStat icon={<Cpu className="size-4" />} label="行为规则" value={evo?.behavior_rules ?? 0} />
        <MiniStat icon={<DollarSign className="size-4" />} label="7日费用" value={fmtCost(totalCost7d)} sub={`今日 ${fmtCost(todayCost)}`} accent />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Services */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm flex items-center gap-2">
              <Activity className="size-4 text-primary" />
              服务状态
              <Badge variant="secondary" className="ml-auto text-[10px]">{serviceEntries.filter(([, s]) => s === "active").length}/{serviceEntries.length}</Badge>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {serviceEntries.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4 text-center">暂无服务</p>
            ) : (
              <div className="space-y-2">
                {serviceEntries.map(([name, state]) => {
                  const target = SERVICE_PAGE[name];
                  return (
                    <div
                      key={name}
                      role={target ? "link" : undefined}
                      tabIndex={target ? 0 : undefined}
                      onClick={target ? () => navigate(target) : undefined}
                      onKeyDown={target ? (e) => { if (e.key === "Enter") navigate(target); } : undefined}
                      className={cn(
                        "flex items-center gap-2 text-sm py-1.5 px-2 rounded-md hover:bg-muted/50",
                        target && "cursor-pointer"
                      )}
                    >
                      <span className={`size-2 rounded-full shrink-0 ${state === "active" ? "bg-primary" : "bg-destructive"}`} />
                      <span className="capitalize flex-1 min-w-0 truncate">{name.replace("metano-", "")}</span>
                      <Badge variant={state === "active" ? "default" : "destructive"} className="text-[10px]">{state === "active" ? "运行" : "停止"}</Badge>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Evolution status */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm flex items-center gap-2">
              <Cpu className="size-4 text-primary" />
              进化引擎
              <Badge variant={evo?.paused ? "destructive" : "default"} className="ml-auto text-[10px]">{evo?.paused ? "暂停" : "运行中"}</Badge>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div className="text-center p-2 rounded-lg bg-muted/50">
                  <div className="text-lg font-semibold">{evo?.profile_beliefs ?? 0}</div>
                  <div className="text-xs text-muted-foreground">信念</div>
                </div>
                <div className="text-center p-2 rounded-lg bg-muted/50">
                  <div className="text-lg font-semibold">{evo?.behavior_rules ?? 0}</div>
                  <div className="text-xs text-muted-foreground">规则</div>
                </div>
              </div>
              {evo?.by_stage && (
                <div className="flex flex-wrap gap-2">
                  {Object.entries(evo.by_stage).map(([stage, count]) => (
                    <div key={stage} className="flex-1 text-center p-1.5 rounded bg-muted/30">
                      <div className="text-sm font-medium">{count}</div>
                      <div className="text-[10px] text-muted-foreground">{stage}</div>
                    </div>
                  ))}
                </div>
              )}
              <div
                role="link"
                tabIndex={0}
                onClick={() => navigate("/evolution")}
                onKeyDown={(e) => { if (e.key === "Enter") navigate("/evolution"); }}
                className="flex items-center justify-between text-sm pt-1 cursor-pointer rounded-md px-1 -mx-1 hover:bg-muted/50"
              >
                <span className="text-muted-foreground">待审批</span>
                <Badge variant={evo?.pending_suggestions ? "default" : "secondary"}>{evo?.pending_suggestions ?? 0}</Badge>
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Quick summary */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm flex items-center gap-2">
              <Eye className="size-4 text-primary" />
              近7日概览
            </CardTitle>
          </CardHeader>
          <CardContent>
            {daily.length > 0 ? (
              <div className="space-y-2">
                {daily.slice().reverse().slice(0, 7).map((d) => (
                  <div
                    key={d.day ?? ""}
                    role="link"
                    tabIndex={0}
                    onClick={() => navigate("/analytics")}
                    onKeyDown={(e) => { if (e.key === "Enter") navigate("/analytics"); }}
                    className="flex items-center gap-2 text-sm py-1.5 px-2 rounded-md cursor-pointer hover:bg-muted/50"
                  >
                    <span className="text-xs font-mono text-muted-foreground shrink-0 w-8">{(d.day ?? "").slice(-5)}</span>
                    <div className="flex-1 min-w-0 flex items-center gap-3">
                      <Badge variant="secondary" className="text-[10px]">{d.session_count ?? 0} 会话</Badge>
                      <span className="text-xs text-muted-foreground">{fmtTokens(d.input_tokens ?? 0)} 输入</span>
                    </div>
                    <span className="font-medium text-xs shrink-0">{fmtCost(d.estimated_cost_usd ?? 0)}</span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground py-4 text-center">暂无数据</p>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}