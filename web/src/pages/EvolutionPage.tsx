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

function EffectivenessBar({ value, applied }: { value: number; applied: number }) {
  const pct = Math.round(value * 100);
  const color = pct >= 70 ? 'bg-green-500' : pct >= 40 ? 'bg-yellow-500' : 'bg-red-500';
  return (
    <div className="flex items-center gap-2">
      <div className="w-20 h-2 bg-muted rounded-full overflow-hidden">
        <div className={`h-full ${color} rounded-full`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-muted-foreground">{pct}% ({applied}次)</span>
    </div>
  );
}

function KindBadge({ kind }: { kind: string }) {
  const colors: Record<string, string> = {
    behavior: 'bg-blue-900 text-blue-300',
    strategy: 'bg-purple-900 text-purple-300',
    knowledge_pattern: 'bg-teal-900 text-teal-300',
  };
  const labels: Record<string, string> = { behavior: '行为', strategy: '策略', knowledge_pattern: '知识' };
  return (
    <span className={`px-1.5 py-0.5 rounded text-xs ${colors[kind] || 'bg-muted text-foreground'}`}>
      {labels[kind] || kind}
    </span>
  );
}

function PriorityBadge({ priority }: { priority: string }) {
  const colors: Record<string, string> = {
    high: 'bg-red-900 text-red-300',
    medium: 'bg-yellow-900 text-yellow-300',
    low: 'bg-muted text-foreground',
  };
  return (
    <span className={`px-1.5 py-0.5 rounded text-xs ${colors[priority] || 'bg-muted text-foreground'}`}>
      {priority}
    </span>
  );
}

function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-card rounded-lg p-4">
      <div className="text-2xl font-bold text-foreground">{value}</div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  );
}

// ── Overview ──
function OverviewTab() {
  const { data: status, isLoading, isError } = useEvolution();
  const { data: patterns } = useBehaviorPatterns();

  if (isError) return <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>;
  if (isLoading) return <div className="text-muted-foreground">加载中...</div>;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="用户画像" value={status?.profile_beliefs ?? 0} />
        <StatCard label="行为规则" value={status?.behavior_rules ?? 0} />
        <StatCard label="Agent 规则总数" value={status?.total_agent_rules ?? 0} />
        <StatCard label="待审批建议" value={status?.pending_suggestions ?? 0} />
      </div>

      {status?.by_stage && (
        <div className="bg-card rounded-lg p-4">
          <h3 className="text-sm font-medium text-foreground mb-2">Belief 阶段分布</h3>
          <div className="flex gap-4">
            {Object.entries(status.by_stage).map(([stage, count]) => (
              <div key={stage} className="text-center">
                <div className="text-lg font-bold text-foreground">{count}</div>
                <div className="text-xs text-muted-foreground">{stage}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {status?.action_stats && (
        <div className="bg-card rounded-lg p-4">
          <h3 className="text-sm font-medium text-foreground mb-2">操作统计</h3>
          <div className="flex gap-6">
            <div className="text-center">
              <div className="text-lg font-bold text-foreground">{status.action_stats.total}</div>
              <div className="text-xs text-muted-foreground">总操作</div>
            </div>
            {Object.entries(status.action_stats.by_outcome || {}).map(([outcome, count]) => (
              <div key={outcome} className="text-center">
                <div className={`text-lg font-bold ${outcome === 'success' ? 'text-green-400' : outcome === 'failure' ? 'text-red-400' : 'text-yellow-400'}`}>{count}</div>
                <div className="text-xs text-muted-foreground">{outcome}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="flex items-center gap-4 text-sm">
        {status?.paused && <span className="px-2 py-1 bg-red-900 text-red-300 rounded">已暂停</span>}
        <span className="text-muted-foreground">预估日成本: ${(status?.estimated_daily_cost ?? 0).toFixed(4)}</span>
      </div>

      {patterns?.recent_corrections && patterns.recent_corrections.length > 0 && (
        <div className="bg-card rounded-lg p-4">
          <h3 className="text-sm font-medium text-foreground mb-2">最近纠正</h3>
          <div className="space-y-2">
            {patterns.recent_corrections.map((c, i) => (
              <div key={i} className="text-sm text-foreground border-l-2 border-yellow-600 pl-3">
                {c.content?.slice(0, 100)}
                <span className="text-xs text-muted-foreground ml-2">{fmtTime(c.timestamp)}</span>
              </div>
            ))}
          </div>
        </div>
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
  if (isLoading) return <div className="text-muted-foreground">加载中...</div>;

  const rules = rulesData?.rules ?? [];
  const filtered = filter === 'all' ? rules : rules.filter((r: AgentRule) => r.kind === filter);
  const kinds = [...new Set(rules.map((r: AgentRule) => r.kind))];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <button onClick={() => setFilter('all')} className={`px-3 py-1 rounded text-sm ${filter === 'all' ? 'bg-blue-600 text-white' : 'bg-muted text-foreground'}`}>
          全部 ({rules.length})
        </button>
        {kinds.map(k => (
          <button key={k} onClick={() => setFilter(k)} className={`px-3 py-1 rounded text-sm ${filter === k ? 'bg-blue-600 text-white' : 'bg-muted text-foreground'}`}>
            {k} ({rules.filter((r: AgentRule) => r.kind === k).length})
          </button>
        ))}
      </div>

      <div className="space-y-2">
        {filtered.map((rule: AgentRule) => (
          <div key={rule.id} className={`bg-card rounded-lg p-4 ${!rule.active ? 'opacity-50' : ''}`}>
            <div className="flex items-start justify-between">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1 min-w-0">
                  <KindBadge kind={rule.kind} />
                  <span className="text-sm text-foreground truncate">{rule.content}</span>
                </div>
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-2">
                  <EffectivenessBar value={rule.effectiveness} applied={rule.times_applied} />
                  <span className="text-xs text-muted-foreground">置信度: {Math.round(rule.confidence * 100)}%</span>
                  <span className="text-xs text-muted-foreground">来源: {rule.source}</span>
                  <span className="text-xs text-muted-foreground">{rule.times_succeeded}成功 / {rule.times_failed}失败</span>
                </div>
              </div>
              <button
                onClick={async () => {
                  const newActive = !rule.active;
                  try {
                    await toggle.mutateAsync({ ruleId: rule.id, active: newActive });
                    toast.success(newActive ? '规则已启用' : '规则已禁用');
                  } catch {
                    toast.error('切换规则失败');
                  }
                }}
                className={`ml-3 px-2 py-1 rounded text-xs ${rule.active ? 'bg-green-900 text-green-300' : 'bg-muted text-muted-foreground'}`}
              >
                {rule.active ? '启用' : '禁用'}
              </button>
            </div>
          </div>
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
      <div className="flex flex-wrap gap-2">
        <input
          type="text" value={context} onChange={e => setContext(e.target.value)}
          placeholder="输入上下文关键词（可选）"
          className="flex-1 min-w-[160px] bg-muted rounded px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground"
        />
        <button onClick={handleDetect} disabled={detect.isPending}
          className="shrink-0 px-4 py-2 bg-purple-600 text-white rounded text-sm hover:bg-purple-700 disabled:opacity-50">
          {detect.isPending ? '检测中...' : '检测策略模式'}
        </button>
      </div>

      <div className="bg-card rounded-lg p-4">
        <h3 className="text-sm font-medium text-foreground mb-3">策略推荐</h3>
        {isError ? <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div> : isLoading ? <div className="text-muted-foreground text-sm">加载中...</div> : (
          <div className="space-y-2">
            {strategies.map((s: AgentRule) => (
              <div key={s.id} className="flex items-center justify-between bg-muted/50 rounded p-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <KindBadge kind={s.kind} />
                    <span className="text-sm text-foreground truncate">{s.content}</span>
                  </div>
                  <EffectivenessBar value={s.effectiveness} applied={s.times_applied} />
                </div>
                {s.strategy_score != null && (
                  <span className="text-xs text-muted-foreground ml-2 shrink-0">评分: {Math.round(s.strategy_score * 100)}</span>
                )}
              </div>
            ))}
            {strategies.length === 0 && <div className="text-muted-foreground text-sm">暂无策略推荐</div>}
          </div>
        )}
      </div>

      {detectedPatterns.length > 0 && (
        <div className="bg-card rounded-lg p-4">
          <h3 className="text-sm font-medium text-foreground mb-3">检测到的策略模式</h3>
          <div className="space-y-2">
            {detectedPatterns.map((p: any, i: number) => (
              <div key={i} className="border-l-2 border-purple-600 pl-3">
                <div className="text-sm text-foreground">{p.pattern || p.rule_content || p.rule_suggestion}</div>
                <div className="flex items-center gap-2 mt-1">
                  <span className="text-xs text-muted-foreground">类型: {p.type}</span>
                  {p.success_rate != null && <span className="text-xs text-muted-foreground">成功率: {Math.round(p.success_rate * 100)}%</span>}
                  {p.confidence != null && <span className="text-xs text-muted-foreground">置信度: {Math.round(p.confidence * 100)}%</span>}
                </div>
              </div>
            ))}
          </div>
        </div>
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
      <div className="bg-card rounded-lg p-4">
        <h3 className="text-sm font-medium text-foreground mb-3">主动探索</h3>
        <div className="flex flex-wrap gap-2">
          <input type="text" value={topic} onChange={e => setTopic(e.target.value)}
            placeholder="输入要探索的主题"
            className="flex-1 min-w-[160px] bg-muted rounded px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground"
            onKeyDown={e => e.key === 'Enter' && handleExplore()}
          />
          <button onClick={handleExplore} disabled={explore.isPending || !topic.trim()}
            className="shrink-0 px-4 py-2 bg-teal-600 text-white rounded text-sm hover:bg-teal-700 disabled:opacity-50">
            {explore.isPending ? '探索中...' : '探索'}
          </button>
        </div>

        {exploreResult && (
          <div className="mt-3 space-y-2">
            {(exploreResult.findings || []).map((f: any, i: number) => (
              <div key={i} className="border-l-2 border-teal-600 pl-3">
                <div className="text-sm font-medium text-foreground">{f.title}</div>
                <div className="text-sm text-muted-foreground">{f.summary}</div>
                {f.source_url && <a href={f.source_url} className="text-xs text-teal-400 hover:underline" target="_blank" rel="noopener">{f.source_url}</a>}
              </div>
            ))}
            {!exploreResult.findings?.length && <div className="text-sm text-muted-foreground">未找到相关信息</div>}
          </div>
        )}
      </div>

      <div className="bg-card rounded-lg p-4">
        <h3 className="text-sm font-medium text-foreground mb-3">知识缺口</h3>
        {gapsError ? <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div> : gapsLoading ? <div className="text-muted-foreground text-sm">加载中...</div> : gaps.length ? (
          <div className="space-y-2">
            {gaps.map((g: KnowledgeGap, i: number) => (
              <div key={i} className="flex items-start justify-between bg-muted/50 rounded p-3">
                <div>
                  <div className="text-sm text-foreground">{g.topic}</div>
                  <div className="text-xs text-muted-foreground">{g.description}</div>
                </div>
                <div className="flex items-center gap-2">
                  <PriorityBadge priority={g.priority} />
                  <span className="text-xs text-muted-foreground">{g.failure_count}次失败</span>
                </div>
              </div>
            ))}
          </div>
        ) : <div className="text-sm text-muted-foreground">暂无检测到的知识缺口</div>}
      </div>
    </div>
  );
}

// ── Architecture ──
function ArchitectureTab() {
  const { data: snapshot, isLoading, isError } = useArchitecture();
  const model = snapshot?.model;

  if (isError) return <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>;
  if (isLoading) return <div className="text-muted-foreground">加载中...</div>;

  return (
    <div className="space-y-4">
      {model ? (
        <>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
            <StatCard label="组件" value={model.components?.length || 0} />
            <StatCard label="MCP 工具" value={model.mcp_tools?.length || 0} />
            <StatCard label="Cron 任务" value={model.cron_jobs?.length || 0} />
            <StatCard label="API 路由" value={model.routes?.length || 0} />
            <StatCard label="Agent 规则" value={model.rules?.length || 0} />
          </div>

          <div className="bg-card rounded-lg p-4">
            <h3 className="text-sm font-medium text-foreground mb-3">组件</h3>
            <div className="space-y-1">
              {model.components?.map(c => (
                <div key={c.name} className="flex items-center justify-between text-sm">
                  <span className="text-foreground">{c.name}.py</span>
                  <span className="text-muted-foreground">{(c.size_bytes / 1024).toFixed(1)} KB</span>
                </div>
              ))}
            </div>
          </div>

          <div className="bg-card rounded-lg p-4">
            <h3 className="text-sm font-medium text-foreground mb-3">Cron 任务</h3>
            <div className="space-y-1">
              {model.cron_jobs?.map(j => (
                <div key={j.id} className="flex items-center justify-between text-sm">
                  <div className="flex items-center gap-2">
                    <span className={`w-2 h-2 rounded-full ${j.enabled ? 'bg-green-500' : 'bg-muted-foreground'}`} />
                    <span className="text-foreground">{j.name || j.id}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-muted-foreground text-xs">{(j.schedule as any)?.expr || JSON.stringify(j.schedule)}</span>
                    {j.last_error && <span className="text-red-400 text-xs">错误</span>}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="bg-card rounded-lg p-4">
            <h3 className="text-sm font-medium text-foreground mb-3">MCP 工具</h3>
            <div className="flex flex-wrap gap-2">
              {model.mcp_tools?.map(t => (
                <span key={t.name} className="px-2 py-1 bg-muted rounded text-xs text-foreground">{t.name}</span>
              ))}
            </div>
          </div>
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
  if (isLoading) return <div className="text-muted-foreground">加载中...</div>;

  const actions = actionsData?.actions ?? [];
  const outcomeColors: Record<string, string> = {
    success: 'text-green-400', failure: 'text-red-400', partial: 'text-yellow-400', pending: 'text-muted-foreground',
  };

  return (
    <div className="space-y-2">
      {actions.map((a: ActionLogEntry) => (
        <div key={a.id} className="bg-card rounded p-3 flex items-center justify-between">
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <span className="text-xs px-1.5 py-0.5 bg-muted rounded">{a.action_type}</span>
              <span className="text-sm text-foreground">{a.action_detail?.slice(0, 80)}</span>
            </div>
            {a.rule_ids_applied && <div className="text-xs text-muted-foreground mt-1">规则: {a.rule_ids_applied}</div>}
          </div>
          <div className="flex items-center gap-3">
            <span className={`text-xs font-medium ${outcomeColors[a.outcome] || 'text-muted-foreground'}`}>{a.outcome}</span>
            <span className="text-xs text-muted-foreground">{fmtTime(a.timestamp)}</span>
          </div>
        </div>
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
  pending: 'bg-yellow-900 text-yellow-300',
  approved: 'bg-blue-900 text-blue-300',
  rejected: 'bg-red-900 text-red-300',
  applied: 'bg-green-900 text-green-300',
  failed: 'bg-red-900 text-red-300',
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
  if (isLoading) return <div className="text-muted-foreground">加载中...</div>;

  const proposals = data?.items ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2 items-center">
        <span className="text-sm text-muted-foreground">状态:</span>
        {['', 'pending', 'approved', 'rejected', 'applied', 'failed'].map(s => (
          <button key={s} onClick={() => setStatusFilter(s)}
            className={`px-2.5 py-1 rounded text-xs ${statusFilter === s ? 'bg-blue-600 text-white' : 'bg-muted text-foreground hover:bg-accent'}`}>
            {s ? STATUS_LABELS[s] : '全部'}
          </button>
        ))}
        <span className="text-sm text-muted-foreground ml-4">类型:</span>
        {['', 'behavior_improvement', 'config_change', 'rule_add', 'claude_md_inject'].map(t => (
          <button key={t} onClick={() => setTypeFilter(t)}
            className={`px-2.5 py-1 rounded text-xs ${typeFilter === t ? 'bg-purple-600 text-white' : 'bg-muted text-foreground hover:bg-accent'}`}>
            {t ? PROPOSAL_TYPES[t] || t : '全部'}
          </button>
        ))}
        {proposals.some(p => p.status === 'approved') && (
          <button onClick={async () => {
              try {
                const res = await applyAll.mutateAsync();
                toast.success(`已应用 ${res.applied} 个提案`);
              } catch {
                toast.error('批量应用失败');
              }
            }}
            disabled={applyAll.isPending}
            className="ml-auto px-3 py-1.5 bg-green-700 text-green-200 rounded text-xs hover:bg-green-600 disabled:opacity-50">
            {applyAll.isPending ? '执行中...' : '批量应用已批准'}
          </button>
        )}
      </div>

      <div className="space-y-2">
        {proposals.map((p: Proposal) => (
          <div key={p.id} className="bg-card rounded-lg p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs font-mono text-muted-foreground">#{p.id}</span>
                  <span className={`px-1.5 py-0.5 rounded text-xs ${STATUS_COLORS[p.status] || 'bg-muted text-foreground'}`}>
                    {STATUS_LABELS[p.status] || p.status}
                  </span>
                  <span className="px-1.5 py-0.5 rounded text-xs bg-muted text-foreground">
                    {PROPOSAL_TYPES[p.proposal_type] || p.proposal_type}
                  </span>
                  <span className="text-xs text-muted-foreground">{fmtTime(p.created_at)}</span>
                </div>
                <div className="text-sm text-foreground">{p.content}</div>
                {p.detail && <div className="text-xs text-muted-foreground mt-1">{p.detail.slice(0, 200)}</div>}
                {p.result && <div className="text-xs text-muted-foreground mt-1 border-l-2 border-border pl-2">结果: {p.result.slice(0, 200)}</div>}
                <div className="text-xs text-muted-foreground mt-1">来源: {p.source}</div>
              </div>
              <div className="flex gap-1 shrink-0">
                {p.status === 'pending' && (
                  <>
                    <button onClick={async () => {
                        try {
                          await approve.mutateAsync(p.id);
                          toast.success('提案已批准');
                        } catch {
                          toast.error('批准失败');
                        }
                      }} disabled={approve.isPending}
                      className="px-2 py-1 bg-blue-700 text-blue-200 rounded text-xs hover:bg-blue-600 disabled:opacity-50">批准</button>
                    <button onClick={async () => {
                        try {
                          await reject.mutateAsync(p.id);
                          toast.success('提案已拒绝');
                        } catch {
                          toast.error('拒绝失败');
                        }
                      }} disabled={reject.isPending}
                      className="px-2 py-1 bg-red-700 text-red-200 rounded text-xs hover:bg-red-600 disabled:opacity-50">拒绝</button>
                  </>
                )}
                {p.status === 'approved' && (
                  <button onClick={async () => {
                      try {
                        await apply.mutateAsync(p.id);
                        toast.success('提案已应用');
                      } catch {
                        toast.error('应用失败');
                      }
                    }} disabled={apply.isPending}
                    className="px-2 py-1 bg-green-700 text-green-200 rounded text-xs hover:bg-green-600 disabled:opacity-50">
                    {apply.isPending ? '执行中...' : '应用'}
                  </button>
                )}
              </div>
            </div>
          </div>
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
    <div className="p-6 max-w-6xl mx-auto">
      <h1 className="text-2xl font-bold text-foreground mb-6">进化系统</h1>

      <div className="flex gap-1 mb-6 border-b border-border overflow-x-auto">
        {tabs.map(tab => (
          <button key={tab.key} onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2 text-sm font-medium transition-colors shrink-0 whitespace-nowrap ${
              activeTab === tab.key ? 'text-primary border-b-2 border-primary' : 'text-muted-foreground hover:text-foreground'
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
    </div>
  );
}
