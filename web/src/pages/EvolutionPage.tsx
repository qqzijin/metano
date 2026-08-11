import { useState } from 'react';
import {
  useEvolution,
  useBehaviorPatterns,
  useAgentRules,
  useToggleRule,
  useStrategy,
  useDetectPatterns,
  useArchitecture,
  useExploreKnowledge,
  useKnowledgeGaps,
  useActionLog,
  useProposals,
  useProposalApprove,
  useProposalReject,
  useProposalApply,
  useProposalsApplyApproved,
} from '../api/hooks';
import type { AgentRule, KnowledgeGap, ActionLogEntry, Proposal } from '../api/client';
import { PageHeader } from '@/components/layout/PageHeader';
import { RoleGuard } from '@/components/auth/RoleGuard';
import { StatCard } from '@/components/shared/StatCard';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Loader2, Zap, Compass, User, ListChecks, ShieldCheck, Inbox,
  Boxes, Puzzle, Clock, Route,
} from 'lucide-react';
import { toast } from 'sonner';

type TabKey = 'overview' | 'proposals' | 'rules' | 'strategy' | 'knowledge' | 'architecture' | 'actions';

const tabs: { key: TabKey; label: string }[] = [
  { key: 'overview', label: '概览' },
  { key: 'proposals', label: '提案' },
  { key: 'rules', label: 'Agent 规则' },
  { key: 'strategy', label: '策略优化' },
  { key: 'knowledge', label: '知识探索' },
  { key: 'architecture', label: '架构感知' },
  { key: 'actions', label: '操作日志' },
];

function fmtTime(ts: number) {
  if (!ts) return '-';
  return new Date(ts * 1000).toLocaleString('zh-CN');
}

function LoadingBlock() {
  return (
    <div className="space-y-3">
      {Array.from({ length: 3 }).map((_, i) => (
        <Skeleton key={i} className="h-16 rounded-lg" />
      ))}
    </div>
  );
}

function EffectivenessBar({ value, applied }: { value: number; applied: number }) {
  const pct = Math.round(value * 100);
  const color = pct >= 70 ? 'bg-emerald-500' : pct >= 40 ? 'bg-amber-500' : 'bg-rose-500';
  return (
    <div className="flex items-center gap-2">
      <div className="w-20 h-2 bg-muted rounded-full overflow-hidden">
        <div className={`h-full ${color} rounded-full`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-muted-foreground">{pct}% ({applied}次)</span>
    </div>
  );
}

const KIND_STYLES: Record<string, string> = {
  behavior: 'bg-chart-1/10 text-chart-1',
  strategy: 'bg-chart-2/10 text-chart-2',
  knowledge_pattern: 'bg-chart-3/10 text-chart-3',
};

function KindBadge({ kind }: { kind: string }) {
  const labels: Record<string, string> = { behavior: '行为', strategy: '策略', knowledge_pattern: '知识' };
  return (
    <span className={`inline-flex shrink-0 items-center rounded-md px-1.5 py-0.5 text-xs font-medium ${KIND_STYLES[kind] || 'bg-muted text-muted-foreground'}`}>
      {labels[kind] || kind}
    </span>
  );
}

const PRIORITY_STYLES: Record<string, string> = {
  high: 'bg-destructive/10 text-destructive',
  medium: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  low: 'bg-muted text-muted-foreground',
};

function PriorityBadge({ priority }: { priority: string }) {
  return (
    <span className={`inline-flex shrink-0 items-center rounded-md px-1.5 py-0.5 text-xs font-medium ${PRIORITY_STYLES[priority] || 'bg-muted text-muted-foreground'}`}>
      {priority}
    </span>
  );
}

// ── Overview ──
function OverviewTab() {
  const { data: status, isLoading, isError } = useEvolution();
  const { data: patterns } = useBehaviorPatterns();

  if (isError) return <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>;
  if (isLoading) return <LoadingBlock />;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard icon={<User className="size-4.5" />} label="用户画像" value={status?.profile_beliefs ?? 0} />
        <StatCard icon={<ListChecks className="size-4.5" />} label="行为规则" value={status?.behavior_rules ?? 0} />
        <StatCard icon={<ShieldCheck className="size-4.5" />} label="Agent 规则总数" value={status?.total_agent_rules ?? 0} />
        <StatCard icon={<Inbox className="size-4.5" />} label="待审批建议" value={status?.pending_suggestions ?? 0} />
      </div>

      {status?.by_stage && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Belief 阶段分布</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-x-6 gap-y-3 pt-0">
            {Object.entries(status.by_stage).map(([stage, count]) => (
              <div key={stage} className="text-center">
                <div className="text-lg font-bold text-foreground">{count}</div>
                <div className="text-xs text-muted-foreground">{stage}</div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {status?.action_stats && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">操作统计</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-x-6 gap-y-3 pt-0">
            <div className="text-center">
              <div className="text-lg font-bold text-foreground">{status.action_stats.total}</div>
              <div className="text-xs text-muted-foreground">总操作</div>
            </div>
            {Object.entries(status.action_stats.by_outcome || {}).map(([outcome, count]) => (
              <div key={outcome} className="text-center">
                <div className={`text-lg font-bold ${outcome === 'success' ? 'text-emerald-600 dark:text-emerald-400' : outcome === 'failure' ? 'text-destructive' : 'text-amber-600 dark:text-amber-400'}`}>{count}</div>
                <div className="text-xs text-muted-foreground">{outcome}</div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <div className="flex flex-wrap items-center gap-4 text-sm">
        {status?.paused && <span className="rounded-md bg-destructive/10 px-2 py-1 text-xs font-medium text-destructive">已暂停</span>}
        <span className="text-muted-foreground">预估日成本: ${(status?.estimated_daily_cost ?? 0).toFixed(4)}</span>
      </div>

      {patterns?.recent_corrections && patterns.recent_corrections.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">最近纠正</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 pt-0">
            {patterns.recent_corrections.map((c, i) => (
              <div key={i} className="text-sm text-foreground border-l-2 border-amber-500/60 pl-3">
                <span className="break-words">{c.content?.slice(0, 100)}</span>
                <span className="text-xs text-muted-foreground ml-2">{fmtTime(c.timestamp)}</span>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

// ── Rules ──
function RulesTab() {
  const { data: rulesData, isLoading, isError } = useAgentRules();
  const toggle = useToggleRule();
  const [filter, setFilter] = useState<string>('all');

  if (isError) return <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>;
  if (isLoading) return <LoadingBlock />;

  const rules = rulesData?.rules ?? [];
  const filtered = filter === 'all' ? rules : rules.filter((r: AgentRule) => r.kind === filter);
  const kinds = [...new Set(rules.map((r: AgentRule) => r.kind))];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant={filter === 'all' ? 'default' : 'outline'} onClick={() => setFilter('all')}>
          全部 ({rules.length})
        </Button>
        {kinds.map(k => (
          <Button key={k} size="sm" variant={filter === k ? 'default' : 'outline'} onClick={() => setFilter(k)}>
            {k} ({rules.filter((r: AgentRule) => r.kind === k).length})
          </Button>
        ))}
      </div>

      <div className="space-y-2">
        {filtered.map((rule: AgentRule) => (
          <Card key={rule.id} size="sm" className={!rule.active ? 'opacity-60' : ''}>
            <CardContent className="flex flex-col gap-2 p-3 sm:flex-row sm:items-start sm:justify-between">
              <div className="flex-1 min-w-0">
                <div className="flex items-start gap-2 min-w-0">
                  <KindBadge kind={rule.kind} />
                  <span className="text-sm text-foreground break-words">{rule.content}</span>
                </div>
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-2">
                  <EffectivenessBar value={rule.effectiveness} applied={rule.times_applied} />
                  <span className="text-xs text-muted-foreground">置信度: {Math.round(rule.confidence * 100)}%</span>
                  <span className="text-xs text-muted-foreground">来源: {rule.source}</span>
                  <span className="text-xs text-muted-foreground">{rule.times_succeeded}成功 / {rule.times_failed}失败</span>
                </div>
              </div>
              <Button
                size="xs"
                variant={rule.active ? 'default' : 'outline'}
                className="shrink-0 self-start sm:mt-0.5"
                onClick={async () => {
                  const newActive = !rule.active;
                  try {
                    await toggle.mutateAsync({ ruleId: rule.id, active: newActive });
                    toast.success(newActive ? '规则已启用' : '规则已禁用');
                  } catch {
                    toast.error('切换规则失败');
                  }
                }}
              >
                {rule.active ? '启用' : '禁用'}
              </Button>
            </CardContent>
          </Card>
        ))}
        {filtered.length === 0 && <div className="text-muted-foreground text-sm">暂无规则</div>}
      </div>
    </div>
  );
}

// ── Strategy ──
function StrategyTab() {
  const [context, setContext] = useState('');
  const { data: strategyData, isLoading, isError } = useStrategy(context || undefined);
  const detect = useDetectPatterns();
  const [detectedPatterns, setDetectedPatterns] = useState<any[]>([]);

  const handleDetect = async () => {
    const patterns = await detect.mutateAsync();
    setDetectedPatterns(patterns);
  };

  const strategies = strategyData?.strategies ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row">
        <Input
          type="text"
          value={context}
          onChange={e => setContext(e.target.value)}
          placeholder="输入上下文关键词（可选）"
          className="flex-1 min-w-0"
          onKeyDown={e => e.key === 'Enter' && handleDetect()}
        />
        <Button onClick={handleDetect} disabled={detect.isPending} className="shrink-0">
          {detect.isPending ? <Loader2 className="size-4 mr-1 animate-spin" /> : <Zap className="size-4 mr-1" />}
          {detect.isPending ? '检测中...' : '检测策略模式'}
        </Button>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">策略推荐</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 pt-0">
          {isError ? (
            <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
          ) : isLoading ? (
            <LoadingBlock />
          ) : (
            <>
              {strategies.map((s: AgentRule) => (
                <div key={s.id} className="flex items-center justify-between gap-2 rounded-lg bg-muted/50 p-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 min-w-0">
                      <KindBadge kind={s.kind} />
                      <span className="text-sm text-foreground break-words">{s.content}</span>
                    </div>
                    <div className="mt-1.5">
                      <EffectivenessBar value={s.effectiveness} applied={s.times_applied} />
                    </div>
                  </div>
                  {s.strategy_score != null && (
                    <span className="text-xs text-muted-foreground ml-2 shrink-0">评分: {Math.round(s.strategy_score * 100)}</span>
                  )}
                </div>
              ))}
              {strategies.length === 0 && <div className="text-muted-foreground text-sm">暂无策略推荐</div>}
            </>
          )}
        </CardContent>
      </Card>

      {detectedPatterns.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">检测到的策略模式</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 pt-0">
            {detectedPatterns.map((p: any, i: number) => (
              <div key={i} className="border-l-2 border-primary/40 pl-3">
                <div className="text-sm text-foreground break-words">{p.pattern || p.rule_content || p.rule_suggestion}</div>
                <div className="flex flex-wrap items-center gap-2 mt-1">
                  <span className="text-xs text-muted-foreground">类型: {p.type}</span>
                  {p.success_rate != null && <span className="text-xs text-muted-foreground">成功率: {Math.round(p.success_rate * 100)}%</span>}
                  {p.confidence != null && <span className="text-xs text-muted-foreground">置信度: {Math.round(p.confidence * 100)}%</span>}
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

// ── Knowledge ──
function KnowledgeTab() {
  const [topic, setTopic] = useState('');
  const explore = useExploreKnowledge();
  const { data: gapsData, isLoading: gapsLoading, isError: gapsError } = useKnowledgeGaps();
  const [exploreResult, setExploreResult] = useState<any>(null);

  const handleExplore = async () => {
    if (!topic.trim()) return;
    const result = await explore.mutateAsync(topic);
    setExploreResult(result);
  };

  const gaps = gapsData?.gaps ?? [];

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">主动探索</CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          <div className="flex flex-col gap-2 sm:flex-row">
            <Input
              type="text"
              value={topic}
              onChange={e => setTopic(e.target.value)}
              placeholder="输入要探索的主题"
              className="flex-1 min-w-0"
              onKeyDown={e => e.key === 'Enter' && handleExplore()}
            />
            <Button onClick={handleExplore} disabled={explore.isPending || !topic.trim()} className="shrink-0">
              {explore.isPending ? <Loader2 className="size-4 mr-1 animate-spin" /> : <Compass className="size-4 mr-1" />}
              {explore.isPending ? '探索中...' : '探索'}
            </Button>
          </div>

          {exploreResult && (
            <div className="mt-3 space-y-2">
              {(exploreResult.findings || []).map((f: any, i: number) => (
                <div key={i} className="border-l-2 border-primary/40 pl-3">
                  <div className="text-sm font-medium text-foreground">{f.title}</div>
                  <div className="text-sm text-muted-foreground break-words">{f.summary}</div>
                  {f.source_url && (
                    <a href={f.source_url} className="text-xs text-primary hover:underline break-all" target="_blank" rel="noopener">
                      {f.source_url}
                    </a>
                  )}
                </div>
              ))}
              {!exploreResult.findings?.length && <div className="text-sm text-muted-foreground">未找到相关信息</div>}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">知识缺口</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 pt-0">
          {gapsError ? (
            <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
          ) : gapsLoading ? (
            <LoadingBlock />
          ) : gaps.length ? (
            <>
              {gaps.map((g: KnowledgeGap, i: number) => (
                <div key={i} className="flex flex-col gap-2 rounded-lg bg-muted/50 p-3 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <div className="text-sm text-foreground break-words">{g.topic}</div>
                    <div className="text-xs text-muted-foreground break-words">{g.description}</div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <PriorityBadge priority={g.priority} />
                    <span className="text-xs text-muted-foreground">{g.failure_count}次失败</span>
                  </div>
                </div>
              ))}
            </>
          ) : <div className="text-sm text-muted-foreground">暂无检测到的知识缺口</div>}
        </CardContent>
      </Card>
    </div>
  );
}

// ── Architecture ──
function ArchitectureTab() {
  const { data: snapshot, isLoading, isError } = useArchitecture();
  const model = snapshot?.model;

  if (isError) return <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>;
  if (isLoading) return <LoadingBlock />;

  return (
    <div className="space-y-4">
      {model ? (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
            <StatCard icon={<Boxes className="size-4.5" />} label="组件" value={model.components?.length || 0} />
            <StatCard icon={<Puzzle className="size-4.5" />} label="MCP 工具" value={model.mcp_tools?.length || 0} />
            <StatCard icon={<Clock className="size-4.5" />} label="Cron 任务" value={model.cron_jobs?.length || 0} />
            <StatCard icon={<Route className="size-4.5" />} label="API 路由" value={model.routes?.length || 0} />
            <StatCard icon={<ShieldCheck className="size-4.5" />} label="Agent 规则" value={model.rules?.length || 0} />
          </div>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">组件</CardTitle>
            </CardHeader>
            <CardContent className="space-y-1 pt-0">
              {model.components?.map(c => (
                <div key={c.name} className="flex items-center justify-between gap-2 text-sm">
                  <span className="truncate font-mono text-xs text-foreground">{c.name}.py</span>
                  <span className="shrink-0 text-muted-foreground">{(c.size_bytes / 1024).toFixed(1)} KB</span>
                </div>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Cron 任务</CardTitle>
            </CardHeader>
            <CardContent className="space-y-1.5 pt-0">
              {model.cron_jobs?.map(j => (
                <div key={j.id} className="flex flex-col gap-1 rounded-lg bg-muted/50 p-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className={`size-2 shrink-0 rounded-full ${j.enabled ? 'bg-emerald-500' : 'bg-muted-foreground'}`} />
                    <span className="truncate text-sm text-foreground">{j.name || j.id}</span>
                  </div>
                  <div className="flex items-center gap-2 sm:shrink-0 sm:pl-4">
                    <span className="truncate text-xs text-muted-foreground">{(j.schedule as any)?.expr || JSON.stringify(j.schedule)}</span>
                    {j.last_error && <span className="shrink-0 text-xs text-destructive">错误</span>}
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">MCP 工具</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-wrap gap-2 pt-0">
              {model.mcp_tools?.map(t => (
                <span key={t.name} className="rounded-md bg-muted px-2 py-1 text-xs text-foreground">{t.name}</span>
              ))}
            </CardContent>
          </Card>
        </>
      ) : <div className="text-muted-foreground text-sm">暂无架构快照，运行 cron_architect 生成</div>}

      {snapshot?.created_at && (
        <div className="text-xs text-muted-foreground">
          快照时间: {fmtTime(snapshot.created_at)}
          {snapshot.findings_count != null && ` | 发现: ${snapshot.findings_count} | 提案: ${snapshot.proposals_count}`}
        </div>
      )}
    </div>
  );
}

// ── Actions ──
function ActionsTab() {
  const { data: actionsData, isLoading, isError } = useActionLog(50);

  if (isError) return <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>;
  if (isLoading) return <LoadingBlock />;

  const actions = actionsData?.actions ?? [];
  const outcomeColors: Record<string, string> = {
    success: 'text-emerald-600 dark:text-emerald-400', failure: 'text-destructive', partial: 'text-amber-600 dark:text-amber-400', pending: 'text-muted-foreground',
  };

  return (
    <div className="space-y-2">
      {actions.map((a: ActionLogEntry) => (
        <Card key={a.id} size="sm">
          <CardContent className="flex items-center justify-between gap-3 p-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 min-w-0">
                <span className="shrink-0 rounded-md bg-muted px-1.5 py-0.5 text-xs">{a.action_type}</span>
                <span className="truncate text-sm text-foreground">{a.action_detail?.slice(0, 80)}</span>
              </div>
              {a.rule_ids_applied && <div className="mt-1 truncate text-xs text-muted-foreground">规则: {a.rule_ids_applied}</div>}
            </div>
            <div className="flex shrink-0 flex-col items-end gap-0.5 sm:flex-row sm:items-center sm:gap-3">
              <span className={`text-xs font-medium ${outcomeColors[a.outcome] || 'text-muted-foreground'}`}>{a.outcome}</span>
              <span className="text-xs text-muted-foreground">{fmtTime(a.timestamp)}</span>
            </div>
          </CardContent>
        </Card>
      ))}
      {actions.length === 0 && <div className="text-muted-foreground text-sm">暂无操作记录</div>}
    </div>
  );
}

// ── Proposals ──
const PROPOSAL_TYPES: Record<string, string> = {
  behavior_improvement: '行为改进',
  config_change: '配置变更',
  rule_add: '规则新增',
  claude_md_inject: 'CLAUDE.md注入',
};

const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  approved: 'bg-chart-2/10 text-chart-2',
  rejected: 'bg-destructive/10 text-destructive',
  applied: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  failed: 'bg-destructive/10 text-destructive',
};

const STATUS_LABELS: Record<string, string> = {
  pending: '待审批',
  approved: '已批准',
  rejected: '已拒绝',
  applied: '已应用',
  failed: '已失败',
};

function ProposalsTab() {
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [typeFilter, setTypeFilter] = useState<string>('');
  const { data, isLoading, isError } = useProposals(statusFilter || undefined, typeFilter || undefined);
  const approve = useProposalApprove();
  const reject = useProposalReject();
  const apply = useProposalApply();
  const applyAll = useProposalsApplyApproved();

  if (isError) return <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>;
  if (isLoading) return <LoadingBlock />;

  const proposals = data?.items ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2 items-center">
        <span className="text-sm text-muted-foreground">状态:</span>
        {['', 'pending', 'approved', 'rejected', 'applied', 'failed'].map(s => (
          <button key={s} onClick={() => setStatusFilter(s)}
            className={`px-2.5 py-1 rounded-md text-xs transition-colors ${statusFilter === s ? 'bg-primary text-primary-foreground shadow-sm' : 'bg-muted text-foreground hover:bg-muted/80'}`}>
            {s ? STATUS_LABELS[s] : '全部'}
          </button>
        ))}
        <span className="text-sm text-muted-foreground ml-1">类型:</span>
        {['', 'behavior_improvement', 'config_change', 'rule_add', 'claude_md_inject'].map(t => (
          <button key={t} onClick={() => setTypeFilter(t)}
            className={`px-2.5 py-1 rounded-md text-xs transition-colors ${typeFilter === t ? 'bg-primary/10 text-primary' : 'bg-muted text-foreground hover:bg-muted/80'}`}>
            {t ? PROPOSAL_TYPES[t] || t : '全部'}
          </button>
        ))}
        {proposals.some(p => p.status === 'approved') && (
          <Button
            size="sm"
            className="ml-auto"
            onClick={async () => {
              try {
                const res = await applyAll.mutateAsync();
                toast.success(`已应用 ${res.applied} 个提案`);
              } catch {
                toast.error('批量应用失败');
              }
            }}
            disabled={applyAll.isPending}
          >
            {applyAll.isPending ? '执行中...' : '批量应用已批准'}
          </Button>
        )}
      </div>

      <div className="space-y-2">
        {proposals.map((p: Proposal) => (
          <Card key={p.id}>
            <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2 mb-1.5">
                  <span className="text-xs font-mono text-muted-foreground">#{p.id}</span>
                  <span className={`rounded-md px-1.5 py-0.5 text-xs font-medium ${STATUS_COLORS[p.status] || 'bg-muted text-foreground'}`}>
                    {STATUS_LABELS[p.status] || p.status}
                  </span>
                  <span className="rounded-md bg-muted px-1.5 py-0.5 text-xs text-foreground">
                    {PROPOSAL_TYPES[p.proposal_type] || p.proposal_type}
                  </span>
                  <span className="text-xs text-muted-foreground">{fmtTime(p.created_at)}</span>
                </div>
                <div className="text-sm text-foreground break-words">{p.content}</div>
                {p.detail && <div className="mt-1 text-xs text-muted-foreground break-words">{p.detail.slice(0, 200)}</div>}
                {p.result && <div className="mt-1 border-l-2 border-border pl-2 text-xs text-muted-foreground break-words">结果: {p.result.slice(0, 200)}</div>}
                <div className="mt-1 text-xs text-muted-foreground break-words">来源: {p.source}</div>
              </div>
              <div className="flex shrink-0 gap-1.5">
                {p.status === 'pending' && (
                  <>
                    <Button size="xs" variant="default" onClick={async () => {
                        try {
                          await approve.mutateAsync(p.id);
                          toast.success('提案已批准');
                        } catch {
                          toast.error('批准失败');
                        }
                      }} disabled={approve.isPending}>
                      批准
                    </Button>
                    <Button size="xs" variant="destructive" onClick={async () => {
                        try {
                          await reject.mutateAsync(p.id);
                          toast.success('提案已拒绝');
                        } catch {
                          toast.error('拒绝失败');
                        }
                      }} disabled={reject.isPending}>
                      拒绝
                    </Button>
                  </>
                )}
                {p.status === 'approved' && (
                  <Button size="xs" variant="default" onClick={async () => {
                      try {
                        await apply.mutateAsync(p.id);
                        toast.success('提案已应用');
                      } catch {
                        toast.error('应用失败');
                      }
                    }} disabled={apply.isPending}>
                    {apply.isPending ? '执行中...' : '应用'}
                  </Button>
                )}
              </div>
            </CardContent>
          </Card>
        ))}
        {proposals.length === 0 && <div className="text-muted-foreground text-sm">暂无提案</div>}
      </div>
    </div>
  );
}

// ── Main ──
export default function EvolutionPage() {
  const [activeTab, setActiveTab] = useState<TabKey>('overview');

  return (
    <RoleGuard role="admin">
      <PageHeader title="进化系统" description="自我进化、规则与策略管理" />
      <div className="flex gap-1 mb-6 border-b border-border overflow-x-auto">
        {tabs.map(tab => (
          <button key={tab.key} onClick={() => setActiveTab(tab.key)}
            className={`shrink-0 whitespace-nowrap px-4 py-2 text-sm font-medium transition-colors border-b-2 ${
              activeTab === tab.key ? 'border-primary text-primary' : 'border-transparent text-muted-foreground hover:text-foreground'
            }`}>
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'overview' && <OverviewTab />}
      {activeTab === 'proposals' && <ProposalsTab />}
      {activeTab === 'rules' && <RulesTab />}
      {activeTab === 'strategy' && <StrategyTab />}
      {activeTab === 'knowledge' && <KnowledgeTab />}
      {activeTab === 'architecture' && <ArchitectureTab />}
      {activeTab === 'actions' && <ActionsTab />}
    </RoleGuard>
  );
}
