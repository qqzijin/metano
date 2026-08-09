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
      <div className="w-20 h-2 bg-gray-700 rounded-full overflow-hidden">
        <div className={`h-full ${color} rounded-full`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-gray-400">{pct}% ({applied}次)</span>
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
    <span className={`px-1.5 py-0.5 rounded text-xs ${colors[kind] || 'bg-gray-700 text-gray-300'}`}>
      {labels[kind] || kind}
    </span>
  );
}

function PriorityBadge({ priority }: { priority: string }) {
  const colors: Record<string, string> = {
    high: 'bg-red-900 text-red-300',
    medium: 'bg-yellow-900 text-yellow-300',
    low: 'bg-gray-700 text-gray-300',
  };
  return (
    <span className={`px-1.5 py-0.5 rounded text-xs ${colors[priority] || 'bg-gray-700 text-gray-300'}`}>
      {priority}
    </span>
  );
}

function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-gray-800 rounded-lg p-4">
      <div className="text-2xl font-bold text-white">{value}</div>
      <div className="text-xs text-gray-400">{label}</div>
    </div>
  );
}

// ── Overview ──
function OverviewTab() {
  const { data: status, isLoading } = useEvolution();
  const { data: patterns } = useBehaviorPatterns();

  if (isLoading) return <div className="text-gray-400">加载中...</div>;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="用户画像" value={status?.profile_beliefs ?? 0} />
        <StatCard label="行为规则" value={status?.behavior_rules ?? 0} />
        <StatCard label="Agent 规则总数" value={status?.total_agent_rules ?? 0} />
        <StatCard label="待审批建议" value={status?.pending_suggestions ?? 0} />
      </div>

      {status?.by_stage && (
        <div className="bg-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-300 mb-2">Belief 阶段分布</h3>
          <div className="flex gap-4">
            {Object.entries(status.by_stage).map(([stage, count]) => (
              <div key={stage} className="text-center">
                <div className="text-lg font-bold text-white">{count}</div>
                <div className="text-xs text-gray-400">{stage}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {status?.action_stats && (
        <div className="bg-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-300 mb-2">操作统计</h3>
          <div className="flex gap-6">
            <div className="text-center">
              <div className="text-lg font-bold text-white">{status.action_stats.total}</div>
              <div className="text-xs text-gray-400">总操作</div>
            </div>
            {Object.entries(status.action_stats.by_outcome || {}).map(([outcome, count]) => (
              <div key={outcome} className="text-center">
                <div className={`text-lg font-bold ${outcome === 'success' ? 'text-green-400' : outcome === 'failure' ? 'text-red-400' : 'text-yellow-400'}`}>{count}</div>
                <div className="text-xs text-gray-400">{outcome}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="flex items-center gap-4 text-sm">
        {status?.paused && <span className="px-2 py-1 bg-red-900 text-red-300 rounded">已暂停</span>}
        <span className="text-gray-400">预估日成本: ${(status?.estimated_daily_cost ?? 0).toFixed(4)}</span>
      </div>

      {patterns?.recent_corrections && patterns.recent_corrections.length > 0 && (
        <div className="bg-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-300 mb-2">最近纠正</h3>
          <div className="space-y-2">
            {patterns.recent_corrections.map((c, i) => (
              <div key={i} className="text-sm text-gray-300 border-l-2 border-yellow-600 pl-3">
                {c.content?.slice(0, 100)}
                <span className="text-xs text-gray-500 ml-2">{fmtTime(c.timestamp)}</span>
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
  const { data: rulesData, isLoading } = useAgentRules();
  const toggle = useToggleRule();
  const [filter, setFilter] = useState<string>('all');

  if (isLoading) return <div className="text-gray-400">加载中...</div>;

  const rules = rulesData?.rules ?? [];
  const filtered = filter === 'all' ? rules : rules.filter((r: AgentRule) => r.kind === filter);
  const kinds = [...new Set(rules.map((r: AgentRule) => r.kind))];

  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        <button onClick={() => setFilter('all')} className={`px-3 py-1 rounded text-sm ${filter === 'all' ? 'bg-blue-600 text-white' : 'bg-gray-700 text-gray-300'}`}>
          全部 ({rules.length})
        </button>
        {kinds.map(k => (
          <button key={k} onClick={() => setFilter(k)} className={`px-3 py-1 rounded text-sm ${filter === k ? 'bg-blue-600 text-white' : 'bg-gray-700 text-gray-300'}`}>
            {k} ({rules.filter((r: AgentRule) => r.kind === k).length})
          </button>
        ))}
      </div>

      <div className="space-y-2">
        {filtered.map((rule: AgentRule) => (
          <div key={rule.id} className={`bg-gray-800 rounded-lg p-4 ${!rule.active ? 'opacity-50' : ''}`}>
            <div className="flex items-start justify-between">
              <div className="flex-1">
                <div className="flex items-center gap-2 mb-1">
                  <KindBadge kind={rule.kind} />
                  <span className="text-sm text-gray-300">{rule.content}</span>
                </div>
                <div className="flex items-center gap-4 mt-2">
                  <EffectivenessBar value={rule.effectiveness} applied={rule.times_applied} />
                  <span className="text-xs text-gray-500">置信度: {Math.round(rule.confidence * 100)}%</span>
                  <span className="text-xs text-gray-500">来源: {rule.source}</span>
                  <span className="text-xs text-gray-500">{rule.times_succeeded}成功 / {rule.times_failed}失败</span>
                </div>
              </div>
              <button
                onClick={() => toggle.mutate({ ruleId: rule.id, active: !rule.active })}
                className={`ml-3 px-2 py-1 rounded text-xs ${rule.active ? 'bg-green-900 text-green-300' : 'bg-gray-700 text-gray-400'}`}
              >
                {rule.active ? '启用' : '禁用'}
              </button>
            </div>
          </div>
        ))}
        {filtered.length === 0 && <div className="text-gray-500 text-sm">暂无规则</div>}
      </div>
    </div>
  );
}

// ── Strategy ──
function StrategyTab() {
  const [context, setContext] = useState('');
  const { data: strategyData, isLoading } = useStrategy(context || undefined);
  const detect = useDetectPatterns();
  const [detectedPatterns, setDetectedPatterns] = useState<any[]>([]);

  const handleDetect = async () => {
    const patterns = await detect.mutateAsync();
    setDetectedPatterns(patterns);
  };

  const strategies = strategyData?.strategies ?? [];

  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        <input
          type="text" value={context} onChange={e => setContext(e.target.value)}
          placeholder="输入上下文关键词（可选）"
          className="flex-1 bg-gray-700 rounded px-3 py-2 text-sm text-white placeholder-gray-400"
        />
        <button onClick={handleDetect} disabled={detect.isPending}
          className="px-4 py-2 bg-purple-600 text-white rounded text-sm hover:bg-purple-700 disabled:opacity-50">
          {detect.isPending ? '检测中...' : '检测策略模式'}
        </button>
      </div>

      <div className="bg-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-300 mb-3">策略推荐</h3>
        {isLoading ? <div className="text-gray-400 text-sm">加载中...</div> : (
          <div className="space-y-2">
            {strategies.map((s: AgentRule) => (
              <div key={s.id} className="flex items-center justify-between bg-gray-750 rounded p-3">
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <KindBadge kind={s.kind} />
                    <span className="text-sm text-gray-300">{s.content}</span>
                  </div>
                  <EffectivenessBar value={s.effectiveness} applied={s.times_applied} />
                </div>
                {s.strategy_score != null && (
                  <span className="text-xs text-gray-400 ml-2">评分: {Math.round(s.strategy_score * 100)}</span>
                )}
              </div>
            ))}
            {strategies.length === 0 && <div className="text-gray-500 text-sm">暂无策略推荐</div>}
          </div>
        )}
      </div>

      {detectedPatterns.length > 0 && (
        <div className="bg-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-300 mb-3">检测到的策略模式</h3>
          <div className="space-y-2">
            {detectedPatterns.map((p: any, i: number) => (
              <div key={i} className="border-l-2 border-purple-600 pl-3">
                <div className="text-sm text-gray-300">{p.pattern || p.rule_content || p.rule_suggestion}</div>
                <div className="flex items-center gap-2 mt-1">
                  <span className="text-xs text-gray-500">类型: {p.type}</span>
                  {p.success_rate != null && <span className="text-xs text-gray-500">成功率: {Math.round(p.success_rate * 100)}%</span>}
                  {p.confidence != null && <span className="text-xs text-gray-500">置信度: {Math.round(p.confidence * 100)}%</span>}
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
  const { data: gapsData, isLoading: gapsLoading } = useKnowledgeGaps();
  const [exploreResult, setExploreResult] = useState<any>(null);

  const handleExplore = async () => {
    if (!topic.trim()) return;
    const result = await explore.mutateAsync(topic);
    setExploreResult(result);
  };

  const gaps = gapsData?.gaps ?? [];

  return (
    <div className="space-y-4">
      <div className="bg-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-300 mb-3">主动探索</h3>
        <div className="flex gap-2">
          <input type="text" value={topic} onChange={e => setTopic(e.target.value)}
            placeholder="输入要探索的主题"
            className="flex-1 bg-gray-700 rounded px-3 py-2 text-sm text-white placeholder-gray-400"
            onKeyDown={e => e.key === 'Enter' && handleExplore()}
          />
          <button onClick={handleExplore} disabled={explore.isPending || !topic.trim()}
            className="px-4 py-2 bg-teal-600 text-white rounded text-sm hover:bg-teal-700 disabled:opacity-50">
            {explore.isPending ? '探索中...' : '探索'}
          </button>
        </div>

        {exploreResult && (
          <div className="mt-3 space-y-2">
            {(exploreResult.findings || []).map((f: any, i: number) => (
              <div key={i} className="border-l-2 border-teal-600 pl-3">
                <div className="text-sm font-medium text-gray-200">{f.title}</div>
                <div className="text-sm text-gray-400">{f.summary}</div>
                {f.source_url && <a href={f.source_url} className="text-xs text-teal-400 hover:underline" target="_blank" rel="noopener">{f.source_url}</a>}
              </div>
            ))}
            {!exploreResult.findings?.length && <div className="text-sm text-gray-500">未找到相关信息</div>}
          </div>
        )}
      </div>

      <div className="bg-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-300 mb-3">知识缺口</h3>
        {gapsLoading ? <div className="text-gray-400 text-sm">加载中...</div> : gaps.length ? (
          <div className="space-y-2">
            {gaps.map((g: KnowledgeGap, i: number) => (
              <div key={i} className="flex items-start justify-between bg-gray-750 rounded p-3">
                <div>
                  <div className="text-sm text-gray-300">{g.topic}</div>
                  <div className="text-xs text-gray-500">{g.description}</div>
                </div>
                <div className="flex items-center gap-2">
                  <PriorityBadge priority={g.priority} />
                  <span className="text-xs text-gray-500">{g.failure_count}次失败</span>
                </div>
              </div>
            ))}
          </div>
        ) : <div className="text-sm text-gray-500">暂无检测到的知识缺口</div>}
      </div>
    </div>
  );
}

// ── Architecture ──
function ArchitectureTab() {
  const { data: snapshot, isLoading } = useArchitecture();
  const model = snapshot?.model;

  if (isLoading) return <div className="text-gray-400">加载中...</div>;

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

          <div className="bg-gray-800 rounded-lg p-4">
            <h3 className="text-sm font-medium text-gray-300 mb-3">组件</h3>
            <div className="space-y-1">
              {model.components?.map(c => (
                <div key={c.name} className="flex items-center justify-between text-sm">
                  <span className="text-gray-300">{c.name}.py</span>
                  <span className="text-gray-500">{(c.size_bytes / 1024).toFixed(1)} KB</span>
                </div>
              ))}
            </div>
          </div>

          <div className="bg-gray-800 rounded-lg p-4">
            <h3 className="text-sm font-medium text-gray-300 mb-3">Cron 任务</h3>
            <div className="space-y-1">
              {model.cron_jobs?.map(j => (
                <div key={j.id} className="flex items-center justify-between text-sm">
                  <div className="flex items-center gap-2">
                    <span className={`w-2 h-2 rounded-full ${j.enabled ? 'bg-green-500' : 'bg-gray-500'}`} />
                    <span className="text-gray-300">{j.name || j.id}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-gray-500 text-xs">{(j.schedule as any)?.expr || JSON.stringify(j.schedule)}</span>
                    {j.last_error && <span className="text-red-400 text-xs">错误</span>}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="bg-gray-800 rounded-lg p-4">
            <h3 className="text-sm font-medium text-gray-300 mb-3">MCP 工具</h3>
            <div className="flex flex-wrap gap-2">
              {model.mcp_tools?.map(t => (
                <span key={t.name} className="px-2 py-1 bg-gray-700 rounded text-xs text-gray-300">{t.name}</span>
              ))}
            </div>
          </div>
        </>
      ) : <div className="text-gray-500 text-sm">暂无架构快照，运行 cron_architect 生成</div>}

      {snapshot?.created_at && (
        <div className="text-xs text-gray-500">
          快照时间: {fmtTime(snapshot.created_at)}
          {snapshot.findings_count != null && ` | 发现: ${snapshot.findings_count} | 提案: ${snapshot.proposals_count}`}
        </div>
      )}
    </div>
  );
}

// ── Actions ──
function ActionsTab() {
  const { data: actionsData, isLoading } = useActionLog(50);

  if (isLoading) return <div className="text-gray-400">加载中...</div>;

  const actions = actionsData?.actions ?? [];
  const outcomeColors: Record<string, string> = {
    success: 'text-green-400', failure: 'text-red-400', partial: 'text-yellow-400', pending: 'text-gray-400',
  };

  return (
    <div className="space-y-2">
      {actions.map((a: ActionLogEntry) => (
        <div key={a.id} className="bg-gray-800 rounded p-3 flex items-center justify-between">
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <span className="text-xs px-1.5 py-0.5 bg-gray-700 rounded">{a.action_type}</span>
              <span className="text-sm text-gray-300">{a.action_detail?.slice(0, 80)}</span>
            </div>
            {a.rule_ids_applied && <div className="text-xs text-gray-500 mt-1">规则: {a.rule_ids_applied}</div>}
          </div>
          <div className="flex items-center gap-3">
            <span className={`text-xs font-medium ${outcomeColors[a.outcome] || 'text-gray-400'}`}>{a.outcome}</span>
            <span className="text-xs text-gray-500">{fmtTime(a.timestamp)}</span>
          </div>
        </div>
      ))}
      {actions.length === 0 && <div className="text-gray-500 text-sm">暂无操作记录</div>}
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
  const { data, isLoading } = useProposals(statusFilter || undefined, typeFilter || undefined);
  const approve = useProposalApprove();
  const reject = useProposalReject();
  const apply = useProposalApply();
  const applyAll = useProposalsApplyApproved();

  if (isLoading) return <div className="text-gray-400">加载中...</div>;

  const proposals = data?.items ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2 items-center">
        <span className="text-sm text-gray-400">状态:</span>
        {['', 'pending', 'approved', 'rejected', 'applied', 'failed'].map(s => (
          <button key={s} onClick={() => setStatusFilter(s)}
            className={`px-2.5 py-1 rounded text-xs ${statusFilter === s ? 'bg-blue-600 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'}`}>
            {s ? STATUS_LABELS[s] : '全部'}
          </button>
        ))}
        <span className="text-sm text-gray-400 ml-4">类型:</span>
        {['', 'behavior_improvement', 'config_change', 'rule_add', 'claude_md_inject'].map(t => (
          <button key={t} onClick={() => setTypeFilter(t)}
            className={`px-2.5 py-1 rounded text-xs ${typeFilter === t ? 'bg-purple-600 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'}`}>
            {t ? PROPOSAL_TYPES[t] || t : '全部'}
          </button>
        ))}
        {proposals.some(p => p.status === 'approved') && (
          <button onClick={() => applyAll.mutate()}
            disabled={applyAll.isPending}
            className="ml-auto px-3 py-1.5 bg-green-700 text-green-200 rounded text-xs hover:bg-green-600 disabled:opacity-50">
            {applyAll.isPending ? '执行中...' : '批量应用已批准'}
          </button>
        )}
      </div>

      <div className="space-y-2">
        {proposals.map((p: Proposal) => (
          <div key={p.id} className="bg-gray-800 rounded-lg p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs font-mono text-gray-500">#{p.id}</span>
                  <span className={`px-1.5 py-0.5 rounded text-xs ${STATUS_COLORS[p.status] || 'bg-gray-700 text-gray-300'}`}>
                    {STATUS_LABELS[p.status] || p.status}
                  </span>
                  <span className="px-1.5 py-0.5 rounded text-xs bg-gray-700 text-gray-300">
                    {PROPOSAL_TYPES[p.proposal_type] || p.proposal_type}
                  </span>
                  <span className="text-xs text-gray-500">{fmtTime(p.created_at)}</span>
                </div>
                <div className="text-sm text-gray-300">{p.content}</div>
                {p.detail && <div className="text-xs text-gray-500 mt-1">{p.detail.slice(0, 200)}</div>}
                {p.result && <div className="text-xs text-gray-500 mt-1 border-l-2 border-gray-600 pl-2">结果: {p.result.slice(0, 200)}</div>}
                <div className="text-xs text-gray-500 mt-1">来源: {p.source}</div>
              </div>
              <div className="flex gap-1 shrink-0">
                {p.status === 'pending' && (
                  <>
                    <button onClick={() => approve.mutate(p.id)} disabled={approve.isPending}
                      className="px-2 py-1 bg-blue-700 text-blue-200 rounded text-xs hover:bg-blue-600 disabled:opacity-50">批准</button>
                    <button onClick={() => reject.mutate(p.id)} disabled={reject.isPending}
                      className="px-2 py-1 bg-red-700 text-red-200 rounded text-xs hover:bg-red-600 disabled:opacity-50">拒绝</button>
                  </>
                )}
                {p.status === 'approved' && (
                  <button onClick={() => apply.mutate(p.id)} disabled={apply.isPending}
                    className="px-2 py-1 bg-green-700 text-green-200 rounded text-xs hover:bg-green-600 disabled:opacity-50">
                    {apply.isPending ? '执行中...' : '应用'}
                  </button>
                )}
              </div>
            </div>
          </div>
        ))}
        {proposals.length === 0 && <div className="text-gray-500 text-sm">暂无提案</div>}
      </div>
    </div>
  );
}

// ── Main ──
export default function EvolutionPage() {
  const [activeTab, setActiveTab] = useState<TabKey>('overview');

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <h1 className="text-2xl font-bold text-white mb-6">进化系统</h1>

      <div className="flex gap-1 mb-6 border-b border-gray-700">
        {tabs.map(tab => (
          <button key={tab.key} onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              activeTab === tab.key ? 'text-blue-400 border-b-2 border-blue-400' : 'text-gray-400 hover:text-gray-200'
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
