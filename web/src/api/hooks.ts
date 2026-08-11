import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { startChatStream } from "@/lib/chatStream";
import {
  fetchAPI,
  type Session,
  type Message,
  type AnalyticsData,
  type SearchResult,
  type CronJob,
  type Skill,
  type SkillDetail,
  type KnowledgeDoc,
  type EvolutionStatus,
  type CostCircuitState,
  type Suggestion,
  type ModelProvider,
  type SystemStatus,
  type LogData,
  type UserProfile,
  type ChatRequest,
  type BehaviorData,
  type AgentRule,
  type StrategyPattern,
  type KnowledgeGap,
  type ActionLogEntry,
  type ArchitectureSnapshot,
  type ExploreResult,
  type ApplyResult,
  type Proposal,
  type EffectivenessData,
  type GraphStats,
  type GraphQueryResult,
} from "./client";

/* ---- query key factory ---- */

export const qk = {
  status: ["status"] as const,
  sessions: (p?: { search?: string; limit?: number }) => ["sessions", p] as const,
  session: (id: string) => ["session", id] as const,
  messages: (id: string) => ["messages", id] as const,
  analytics: (days?: number) => ["analytics", days] as const,
  search: (q: string, limit = 20, offset = 0) => ["search", q, limit, offset] as const,
  cron: ["cron"] as const,
  skills: ["skills"] as const,
  skill: (n: string) => ["skill", n] as const,
  knowledge: ["knowledge"] as const,
  evolution: ["evolution"] as const,
  suggestions: ["suggestions"] as const,
  models: ["models"] as const,
  logs: (source?: string) => ["logs", source] as const,
  profiles: (uid?: string) => ["profiles", uid] as const,
  config: ["config"] as const,
  browserSearch: (q: string) => ["browser", "search", q] as const,
  voices: (lang?: string) => ["voices", lang] as const,
  homeStatus: ["home", "status"] as const,
  security: (uid: string) => ["security", uid] as const,
  audit: (uid: string) => ["security", uid, "audit"] as const,
  securityUsers: ["security", "users"] as const,
};

/* ---- query hooks ---- */

export function useStatus() {
  return useQuery<SystemStatus>({
    queryKey: qk.status,
    queryFn: () => fetchAPI("/status"),
    refetchInterval: 15000,
  });
}

export function useSessions(search?: string, limit = 20, refetchInterval: number | false = 10000) {
  return useQuery<{ sessions: Session[] }>({
    queryKey: qk.sessions({ search, limit }),
    queryFn: () => fetchAPI(`/sessions?limit=${limit}${search ? `&search=${encodeURIComponent(search)}` : ""}`).then((d: any) => ({ sessions: d.items ?? [] })),
    refetchInterval,
  });
}

export function useSession(id: string) {
  return useQuery<Session>({
    queryKey: qk.session(id),
    queryFn: () => fetchAPI(`/sessions/${id}`),
    enabled: !!id,
  });
}

export function useMessages(sessionId: string) {
  return useQuery<{ messages: Message[] }>({
    queryKey: qk.messages(sessionId),
    queryFn: () => fetchAPI(`/sessions/${sessionId}/messages`).then((d: any) => ({ messages: d.items ?? [] })),
    enabled: !!sessionId,
  });
}

export function useAnalytics(days = 7) {
  return useQuery<AnalyticsData>({
    queryKey: qk.analytics(days),
    queryFn: () => fetchAPI(`/analytics?days=${days}`),
  });
}

export function useSearch(query: string, limit = 20, offset = 0) {
  return useQuery<{ results: SearchResult[]; total: number }>({
    queryKey: qk.search(query, limit, offset),
    queryFn: () =>
      fetchAPI(`/search?q=${encodeURIComponent(query)}&limit=${limit}&offset=${offset}`),
    enabled: !!query,
  });
}

export function useCronJobs() {
  return useQuery<{ jobs: CronJob[] }>({
    queryKey: qk.cron,
    queryFn: () => fetchAPI("/cron/jobs").then((d: any) => ({ jobs: Array.isArray(d) ? d : d.jobs ?? d.items ?? [] })),
  });
}

export function useSkills() {
  return useQuery<{ skills: Skill[] }>({
    queryKey: qk.skills,
    queryFn: () => fetchAPI("/skills").then((d: any) => ({ skills: d.items ?? [] })),
  });
}

export function useSkill(name: string) {
  return useQuery<SkillDetail>({
    queryKey: qk.skill(name),
    queryFn: () => fetchAPI(`/skills/${name}`),
    enabled: !!name,
  });
}

export function useSkillUsage(days = 30) {
  return useQuery<{ days: number; recent: { skill_name: string; uses: number; last_used: number }[]; all_time: { skill_name: string; uses: number; last_used: number }[] }>({
    queryKey: ["skill-usage", days],
    queryFn: () => fetchAPI(`/skills/usage?days=${days}`),
    staleTime: 60000,
  });
}

export function useKnowledge() {
  return useQuery<{ documents: KnowledgeDoc[] }>({
    queryKey: qk.knowledge,
    queryFn: () => fetchAPI("/knowledge").then((d: any) => ({ documents: d.items ?? [] })),
  });
}

export function useEvolution() {
  return useQuery<EvolutionStatus>({
    queryKey: qk.evolution,
    queryFn: () => fetchAPI("/evolution"),
    refetchInterval: 10000,
  });
}

export function useModels() {
  return useQuery<{ providers: ModelProvider[] }>({
    queryKey: qk.models,
    queryFn: () => fetchAPI("/models").then((d: any) => ({ providers: d.items ?? [] })),
  });
}

export function useLogs(source?: string) {
  return useQuery<LogData>({
    queryKey: qk.logs(source),
    queryFn: () => fetchAPI(`/logs${source ? `?source=${source}` : ""}`),
    refetchInterval: 5000,
  });
}

export function useProfile(userId = "default") {
  return useQuery<UserProfile>({
    queryKey: qk.profiles(userId),
    queryFn: () => fetchAPI(`/profiles/${userId}`),
  });
}

export function useConfig() {
  return useQuery<Record<string, unknown>>({
    queryKey: qk.config,
    queryFn: () => fetchAPI("/config").then((d: any) => d.config ?? d),
  });
}

/* ---- mutation hooks ---- */

export function useChatMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (req: ChatRequest) =>
      // Streamed via the module-level chatStream manager so the reply keeps
      // flowing even if the page is unmounted mid-generation (route switch).
      startChatStream({
        message: req.message,
        user_id: req.user_id ?? "web_user",
        session_id: req.session_id,
        reset: req.reset,
        context: req.context,
      }).then(() => undefined),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.sessions() }),
  });
}

export function useUploadFile() {
  return useMutation<{ path: string; name: string; size: number }, Error, File>({
    mutationFn: (file) => {
      const fd = new FormData();
      fd.append("file", file);
      return fetchAPI("/upload", { method: "POST", body: fd });
    },
  });
}

export function useCronCreate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (job: Partial<CronJob>) =>
      fetchAPI("/cron/jobs", { method: "POST", body: JSON.stringify(job) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.cron }),
  });
}

export function useCronDelete() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchAPI(`/cron/jobs/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.cron }),
  });
}

export function useCronPause() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchAPI(`/cron/jobs/${id}/pause`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.cron }),
  });
}

export function useCronResume() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchAPI(`/cron/jobs/${id}/resume`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.cron }),
  });
}

export function useCronTrigger() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchAPI(`/cron/jobs/${id}/trigger`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.cron }),
  });
}

export function useEvolutionRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (stage: string) =>
      fetchAPI("/evolution/run", { method: "POST", body: JSON.stringify({ stage }) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.evolution });
      qc.invalidateQueries({ queryKey: qk.suggestions });
    },
  });
}

export function useEvolutionPause() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => fetchAPI("/evolution/pause", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.evolution }),
  });
}

export function useEvolutionResume() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => fetchAPI("/evolution/resume", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.evolution }),
  });
}

export function useKnowledgeIngest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: { path: string; title?: string }) =>
      fetchAPI("/knowledge/ingest", { method: "POST", body: JSON.stringify(data) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.knowledge }),
  });
}

export function useKnowledgeDelete() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (docId: string) =>
      fetchAPI(`/knowledge/${docId}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.knowledge }),
  });
}

export function useKnowledgeSearch() {
  return useMutation({
    mutationFn: (query: string) =>
      fetchAPI<{ results: Array<{ content: string; score: number; title: string }> }>(
        "/knowledge/search",
        { method: "POST", body: JSON.stringify({ query }) }
      ),
  });
}

export function useModelSetDefault() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) =>
      fetchAPI(`/models/${name}/default`, { method: "PUT" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.models }),
  });
}

export function useBrowserSearch() {
  return useMutation({
    mutationFn: (query: string) =>
      fetchAPI<{ results: Array<{ title: string; url: string; snippet: string }> }>(
        "/browser/search",
        { method: "POST", body: JSON.stringify({ query }) }
      ),
  });
}

export function useBrowserBrowse() {
  return useMutation({
    mutationFn: (url: string) =>
      fetchAPI<{ content: string; title: string }>("/browser/browse", {
        method: "POST",
        body: JSON.stringify({ url }),
      }),
  });
}

export function useVoiceTTS() {
  return useMutation({
    mutationFn: (data: { text: string; voice?: string; rate?: string }) =>
      fetchAPI<{ path: string; status: string }>("/voice/tts", {
        method: "POST",
        body: JSON.stringify(data),
      }),
  });
}

export function useSecuritySetTier() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, tier }: { userId: string; tier: string }) =>
      fetchAPI(`/security/${userId}/tier`, {
        method: "PUT",
        body: JSON.stringify({ tier }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.securityUsers }),
  });
}

export function useSecurityUsers() {
  return useQuery<{
    users: Array<{
      user_id: string;
      tier: string;
      rate_limit_remaining?: number;
      blocked_count?: number;
    }>;
  }>({
    queryKey: qk.securityUsers,
    queryFn: () => fetchAPI("/security/users").then((d: any) => ({ users: Array.isArray(d) ? d : d.users ?? d.items ?? [] })),
  });
}

/* ---- new: web search via Tavily ---- */

export function useWebSearch() {
  return useMutation({
    mutationFn: (query: string) =>
      fetchAPI<{ answer: string; results: Array<{ title: string; url: string; snippet: string }> }>(
        "/search/web",
        { method: "POST", body: JSON.stringify({ query }) }
      ),
  });
}

/* ---- new: MCP tools ---- */

export function useMcpTools() {
  return useQuery<{ tools: Array<{ name: string; source: string; description: string }> }>({
    queryKey: ["mcp", "tools"],
    queryFn: () => fetchAPI("/mcp/tools").then((d: any) => ({ tools: d.tools ?? [] })),
  });
}

/* ---- new: memory system ---- */

export function useMemoryStats() {
  return useQuery({
    queryKey: ["memory", "stats"],
    queryFn: () => fetchAPI("/memory/stats"),
  });
}

export function useMemorySearch(q: string) {
  return useQuery<{ results: Array<{ id: number; content: string; category: string; importance: number }> }>({
    queryKey: ["memory", "search", q],
    queryFn: () => fetchAPI(`/memory/search?q=${encodeURIComponent(q)}`),
    enabled: !!q,
  });
}

export function useMemoryCompress() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => fetchAPI("/memory/compress", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["memory", "stats"] }),
  });
}

/* ---- new: memory export/import/seed ---- */

export function useMemoryExport() {
  return useMutation({
    mutationFn: () => fetchAPI("/memory/export"),
  });
}

export function useMemorySeed() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => fetchAPI("/memory/seed", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["memory", "stats"] }),
  });
}

/* ---- new: evolution correction ---- */

export function useEvolutionCorrect() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: { correction: string; category?: string }) =>
      fetchAPI("/evolution/correct", { method: "POST", body: JSON.stringify(data) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.evolution });
      qc.invalidateQueries({ queryKey: qk.suggestions });
    },
  });
}

/* ---- new: proxy providers ---- */

export function useProxyProviders() {
  return useQuery<{ providers: ModelProvider[] }>({
    queryKey: ["proxy", "providers"],
    queryFn: () => fetchAPI("/proxy/providers").then((d: any) => ({ providers: d.providers ?? d.items ?? [] })),
  });
}

/* ---- new: browser screenshot ---- */

export function useBrowserScreenshot() {
  return useMutation({
    mutationFn: (url: string) =>
      fetchAPI<{ path?: string; image?: string; url?: string; status?: string; error?: string }>(
        "/browser/screenshot",
        { method: "POST", body: JSON.stringify({ url }) }
      ),
  });
}

/* ---- behavior patterns ---- */

export function useBehaviorPatterns() {
  return useQuery<BehaviorData>({
    queryKey: ["evolution", "behaviors"],
    queryFn: () => fetchAPI("/evolution/behaviors"),
  });
}

export function useBehaviorAnalyze() {
  return useMutation({
    mutationFn: (days: number = 7) =>
      fetchAPI("/evolution/analyze", { method: "POST", body: JSON.stringify({ days }) }),
  });
}

export function useBehaviorApprove() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchAPI(`/evolution/behavior-approve/${id}`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["evolution", "behaviors"] });
      qc.invalidateQueries({ queryKey: qk.suggestions });
    },
  });
}

export function useBehaviorReject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchAPI(`/evolution/behavior-reject/${id}`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["evolution", "behaviors"] });
      qc.invalidateQueries({ queryKey: qk.suggestions });
    },
  });
}

/* ---- agent rules ---- */

export function useAgentRules() {
  return useQuery<{ rules: AgentRule[] }>({
    queryKey: ["agentRules"],
    queryFn: () => fetchAPI("/evolution/rules"),
  });
}

export function useToggleRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ ruleId, active }: { ruleId: number; active: boolean }) =>
      fetchAPI(`/evolution/rules/${ruleId}/toggle`, { method: "POST", body: JSON.stringify({ active }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agentRules"] }),
  });
}

/* ---- strategy ---- */

export function useStrategy(context?: string) {
  return useQuery<{ strategies: AgentRule[] }>({
    queryKey: ["strategy", context],
    queryFn: () => fetchAPI(`/evolution/strategy${context ? `?context=${encodeURIComponent(context)}` : ""}`),
  });
}

export function useDetectPatterns() {
  return useMutation<StrategyPattern[], Error, void>({
    mutationFn: async () => {
      const d = await fetchAPI("/evolution/strategy-detect", { method: "POST" });
      return (d as any).patterns ?? [];
    },
  });
}

/* ---- architecture ---- */

export function useArchitecture() {
  return useQuery<ArchitectureSnapshot>({
    queryKey: ["architecture"],
    queryFn: () => fetchAPI("/evolution/architecture"),
  });
}

export function useApplyRestructure() {
  const qc = useQueryClient();
  return useMutation<ApplyResult, Error, string>({
    mutationFn: (proposalId: string) =>
      fetchAPI(`/evolution/restructure-apply/${proposalId}`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["architecture"] }),
  });
}

/* ---- knowledge exploration ---- */

export function useExploreKnowledge() {
  return useMutation<ExploreResult, Error, string>({
    mutationFn: (topic: string) =>
      fetchAPI("/knowledge/explore", { method: "POST", body: JSON.stringify({ topic }) }),
  });
}

export function useKnowledgeGaps() {
  return useQuery<{ gaps: KnowledgeGap[] }>({
    queryKey: ["knowledgeGaps"],
    queryFn: () => fetchAPI("/knowledge/gaps"),
  });
}

/* ---- action log ---- */

export function useActionLog(limit = 20) {
  return useQuery<{ actions: ActionLogEntry[] }>({
    queryKey: ["actionLog", limit],
    queryFn: () => fetchAPI(`/evolution/action-log?limit=${limit}`),
  });
}

/* ---- proposals ---- */

export function useProposals(status?: string, type?: string) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (type) params.set("proposal_type", type);
  const qs = params.toString();
  return useQuery<{ items: Proposal[] }>({
    queryKey: ["proposals", status, type],
    queryFn: () => fetchAPI(`/evolution/proposals${qs ? "?" + qs : ""}`),
  });
}

export function useProposalApprove() {
  const qc = useQueryClient();
  return useMutation<{ status: string }, Error, number>({
    mutationFn: (id: number) => fetchAPI(`/evolution/proposals/${id}/approve`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposals"] }),
  });
}

export function useProposalReject() {
  const qc = useQueryClient();
  return useMutation<{ status: string }, Error, number>({
    mutationFn: (id: number) => fetchAPI(`/evolution/proposals/${id}/reject`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposals"] }),
  });
}

export function useProposalApply() {
  const qc = useQueryClient();
  return useMutation<{ status: string }, Error, number>({
    mutationFn: (id: number) => fetchAPI(`/evolution/proposals/${id}/apply`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposals"] }),
  });
}

export function useProposalsApplyApproved() {
  const qc = useQueryClient();
  return useMutation<{ applied: number }, Error, void>({
    mutationFn: () => fetchAPI("/evolution/proposals/apply-approved", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposals"] }),
  });
}

/* ---- knowledge graph ---- */

export function useGraphStats() {
  return useQuery<GraphStats>({
    queryKey: ["knowledge", "graph", "stats"],
    queryFn: () => fetchAPI("/knowledge/graph/stats"),
    refetchInterval: 15000,
  });
}

export function useGraphQuery(entity: string, entityType: string, limit = 100) {
  const params = new URLSearchParams();
  if (entity) params.set("entity", entity);
  if (entityType) params.set("entity_type", entityType);
  params.set("limit", String(limit));
  const qs = params.toString();
  return useQuery<GraphQueryResult>({
    queryKey: ["knowledge", "graph", "query", entity, entityType],
    queryFn: () => fetchAPI(`/knowledge/graph?${qs}`),
  });
}

export function useGraphExtract() {
  const qc = useQueryClient();
  return useMutation<GraphStats, Error, { doc_id?: string; limit?: number; replace?: boolean }>({
    mutationFn: (data) =>
      fetchAPI("/knowledge/graph/extract", { method: "POST", body: JSON.stringify(data) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge", "graph", "stats"] });
      qc.invalidateQueries({ queryKey: ["knowledge", "graph", "query"] });
    },
  });
}

/* ---- knowledge semantic search ---- */

export function useKnowledgeSemanticSearch() {
  return useMutation<{ results: Array<{ file: string; content: string; score: number }> }, Error, string>({
    mutationFn: (query) =>
      fetchAPI("/knowledge/semantic-search", { method: "POST", body: JSON.stringify({ query }) }),
  });
}

/* ---- knowledge synthesize ---- */

export function useCostCircuit() {
  return useQuery<CostCircuitState>({
    queryKey: ["evolution", "cost-circuit"],
    queryFn: () => fetchAPI("/evolution/cost-circuit"),
    refetchInterval: 30000,
  });
}

export function useUpdateCostCircuitConfig() {
  const qc = useQueryClient();
  return useMutation<CostCircuitState, Error, { warn?: number; pause?: number; stop?: number; auto_resume_hours?: number }>({
    mutationFn: (config) =>
      fetchAPI("/evolution/cost-circuit/config", { method: "POST", body: JSON.stringify(config) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["evolution", "cost-circuit"] });
      qc.invalidateQueries({ queryKey: qk.evolution });
    },
  });
}

/* ---- home entity detail ---- */

export function useProxyAdd() {
  const qc = useQueryClient();
  return useMutation<{ status: string }, Error, { name: string; base_url: string; api_key?: string; model?: string }>({
    mutationFn: (data) =>
      fetchAPI("/proxy/add", { method: "POST", body: JSON.stringify(data) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proxy", "providers"] }),
  });
}

export function useProxyUpdate() {
  const qc = useQueryClient();
  return useMutation<{ status: string }, Error, { name: string; body: Record<string, unknown> }>({
    mutationFn: ({ name, body }) =>
      fetchAPI(`/proxy/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["proxy", "providers"] });
      qc.invalidateQueries({ queryKey: qk.models });
    },
  });
}
