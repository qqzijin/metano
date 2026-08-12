const BASE = "/api";

/** Parse a Response into T, handling error bodies and empty/204 responses. */
async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    // Prefer the backend's `detail` message (validation errors, etc.) over the
    // generic status text. M-01: the backend also returns `{error: {message}}`
    // for many errors — read that too so users see the actionable reason rather
    // than a bare `API 500`. Body may be empty or non-JSON — fall back quietly.
    let detail = "";
    try {
      const body = await res.json();
      if (body && typeof body === "object") {
        const b = body as { detail?: unknown; error?: unknown };
        if (b.detail) {
          detail = String(b.detail);
        } else if (b.error) {
          if (typeof b.error === "string") {
            detail = b.error;
          } else if (b.error && typeof b.error === "object") {
            const msg = (b.error as { message?: unknown }).message;
            if (msg) detail = String(msg);
          }
        }
      }
    } catch {
      /* empty / non-JSON error body */
    }
    throw new Error(detail || `API ${res.status}: ${res.statusText}`);
  }

  // Some endpoints return 204/empty bodies or text content — don't JSON.parse.
  const contentType = res.headers.get("content-type") ?? "";
  if (res.status === 204 || !contentType.includes("application/json")) {
    return undefined as T;
  }
  return res.json() as Promise<T>;
}

// Dedupe concurrent refresh calls: several requests may 401 at the same time
// right after the access token expires, but the refresh endpoint should be hit
// once and the new cookie reused by all of them.
let refreshPromise: Promise<boolean> | null = null;

/**
 * Silently renew the access token using the HttpOnly refresh_token cookie.
 *
 * The refresh token cookie is path-scoped to /api/auth/refresh, so the backend
 * middleware cannot auto-refresh on other API paths — the frontend must call
 * this endpoint explicitly. On success the response sets a fresh access_token
 * cookie (15 min) that subsequent requests send automatically.
 */
export async function refreshAuthSession(): Promise<boolean> {
  if (refreshPromise) return refreshPromise;
  refreshPromise = fetch(`${BASE}/auth/refresh`, {
    method: "POST",
    credentials: "include",
  })
    .then((res) => res.ok)
    .catch(() => false)
    .finally(() => {
      refreshPromise = null;
    });
  return refreshPromise;
}

export async function fetchAPI<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    // Let the browser set multipart Content-Type (with boundary) for FormData bodies.
    ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
    ...(init?.headers as Record<string, string> | undefined),
  };
  const doFetch = () =>
    fetch(`${BASE}${path}`, {
      ...init,
      credentials: "include",
      headers,
    });

  let res = await doFetch();

  if (res.status === 401) {
    // Access token may simply have expired (15 min) while the refresh token is
    // still valid — normal session expiry should NOT log the user out. Try a
    // silent refresh and retry once before giving up.
    const refreshed = await refreshAuthSession();
    if (refreshed) {
      res = await doFetch();
      if (res.status === 401) {
        // Retry failed even with a fresh token → refresh token is gone/invalid.
        window.dispatchEvent(new Event("auth:unauthorized"));
        throw new Error("未登录");
      }
    } else {
      // Refresh failed → no valid session at all.
      window.dispatchEvent(new Event("auth:unauthorized"));
      throw new Error("未登录");
    }
  }

  return handleResponse<T>(res);
}

/* ---- types ---- */

export interface Session {
  id: string;
  title?: string;
  model?: string;
  started_at?: string | number;
  last_active?: string | number;
  message_count?: number;
  input_tokens?: number;
  output_tokens?: number;
  estimated_cost_usd?: number;
  tool_call_count?: number;
}

export interface Message {
  id: string | number;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  tool_name?: string;
  tool_calls?: string;
  input_tokens?: number;
  output_tokens?: number;
  timestamp?: string | number;
  duration_ms?: number | null;
}

export interface AnalyticsData {
  total: {
    session_count?: number;
    message_count?: number;
    input_tokens?: number;
    output_tokens?: number;
    cache_read_tokens?: number;
    tool_call_count?: number;
    estimated_cost_usd?: number;
  };
  /** 每日总用量：按消息实际发生日聚合（跨日会话 token 拆到各自发生日） */
  daily: { day: string; session_count: number; input_tokens: number; output_tokens: number; estimated_cost_usd: number }[];
  by_model: {
    model: string;
    session_count: number;
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens?: number;
    estimated_cost_usd: number;
  }[];
  /** 渠道/项目分布：区分网关渠道与本地 Claude Code 会话 */
  by_project: {
    project: string;
    session_count: number;
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens?: number;
    estimated_cost_usd: number;
  }[];
  /** 单次对话 token 排行：每条会话的输入/输出/缓存 token 与费用 */
  sessions: {
    id: string;
    title?: string;
    project?: string;
    model?: string;
    message_count?: number;
    tool_call_count?: number;
    input_tokens?: number;
    output_tokens?: number;
    cache_read_tokens?: number;
    estimated_cost_usd?: number;
    started_at?: string | number;
    last_active?: string | number;
  }[];
}

export interface SearchResult {
  session_id: string;
  title?: string;
  role: string;
  snippet: string;
  timestamp: string | number;
}

export interface CronJob {
  id: string;
  name: string;
  schedule: { kind: string; expr: string };
  prompt: string;
  type?: string;
  enabled: boolean;
  last_run_at?: string | null;
  next_run_at?: string | null;
  last_error?: string | null;
}

export interface Skill {
  name: string;
  description: string;
  trigger: string;
  category: string;
  source: string;
}

export interface SkillDetail extends Skill {
  content: string;
}

export interface KnowledgeDoc {
  doc_id?: string;
  title?: string;
  source?: string;
  chunk_count?: number;
  updated_at?: number | string;
  doc_type?: string;
  [key: string]: unknown;
}

export interface GraphEntity {
  entity_id: string;
  name: string;
  entity_type: string;
  confidence: number;
}

export interface GraphRelationship {
  rel_id: string;
  source_id: string;
  target_id: string;
  rel_type: string;
  confidence: number;
  source_name: string;
  target_name: string;
}

export interface GraphQueryResult {
  entities: GraphEntity[];
  relationships: GraphRelationship[];
}

export interface GraphStats {
  entities: number;
  relationships: number;
  entity_types: Record<string, number>;
  relationship_types: Record<string, number>;
}

export interface CostCircuitState {
  state: "normal" | "warning" | "paused" | "stopped";
  daily_cost: number;
  warn_threshold: number;
  pause_threshold: number;
  stop_threshold: number;
  auto_resume_hours: number;
}

export interface EvolutionStatus {
  paused: boolean;
  profile_beliefs?: number;
  behavior_rules?: number;
  total_agent_rules?: number;
  by_stage?: Record<string, number>;
  pending_suggestions?: number;
  action_stats?: { total: number; by_outcome: Record<string, number>; by_type?: Record<string, number> };
  estimated_daily_cost?: number;
  cost_circuit?: CostCircuitState;
  error?: string;
}

export interface AgentRule {
  id: number;
  kind: string;
  content: string;
  scope: string;
  confidence: number;
  effectiveness: number;
  times_applied: number;
  times_succeeded: number;
  times_failed: number;
  source: string;
  active: number | boolean;
  created_at: number;
  updated_at: number;
  metadata?: Record<string, unknown>;
  strategy_score?: number;
}

export interface StrategyPattern {
  type: string;
  action_type?: string;
  rule_id?: string;
  rule_content?: string;
  success_rate?: number;
  sample_size?: number;
  pattern?: string;
  rule_suggestion?: string;
  confidence?: number;
  evidence?: string;
}

export interface KnowledgeGap {
  topic: string;
  description: string;
  failure_count: number;
  priority: string;
}

export interface ArchitectureSnapshot {
  model?: {
    timestamp: number;
    components: { name: string; size_bytes: number }[];
    mcp_tools: { name: string; description: string }[];
    cron_jobs: { id: string; name: string; schedule: object; enabled: boolean; last_error: string | null }[];
    routes: { path: string; methods: string[] }[];
    rules: { id: number; kind: string; content: string }[];
  };
  findings_count?: number;
  proposals_count?: number;
  created_at?: number;
  model_json?: string;
  id?: number;
}

export interface ActionLogEntry {
  id: number;
  session_id: string;
  action_type: string;
  action_detail: string;
  rule_ids_applied: number[] | string;
  outcome: string;
  timestamp: number;
}

export interface ExploreResult {
  status: string;
  topic: string;
  sources_found: number;
  findings: { title: string; summary: string; source_url: string; relevance: string }[];
  ingested: boolean;
}

export interface ApplyResult {
  status: string;
  proposal_id?: string;
  result?: object;
  error?: string;
}

export interface SemanticSearchResult {
  results: { file: string; content: string; score?: number }[];
  source: string;
  query?: string;
}

export interface EffectivenessData {
  rule_id: number | string;
  found: boolean;
  content?: string;
  effectiveness?: number;
  times_applied?: number;
  times_succeeded?: number;
  times_failed?: number;
  active?: boolean;
}

export interface Suggestion {
  id: string;
  type?: string;
  content?: string;
  suggestion?: string;
  belief_id?: string;
  status?: string;
  created_at?: number;
  [key: string]: unknown;
}

export interface ModelPrice {
  input?: number;
  output?: number;
  cache_read?: number;
}

export interface ModelProvider {
  name: string;
  model?: string;
  base_url?: string;
  max_tokens?: number;
  supports_vision?: boolean;
  is_default?: boolean;
  price?: ModelPrice;
  [key: string]: unknown;
}

export interface SystemStatus {
  status: string;
  sessions: number;
  messages: number;
  skills_count: number;
  active_services: number;
  evolution: EvolutionStatus;
  services: Record<string, string>;
}

export interface LogEntry {
  timestamp?: number;
  stage?: string;
  action?: string;
  [key: string]: unknown;
}

export interface BehaviorPattern {
  id: number;
  kind: string;
  content: string;
  confidence: number;
  effectiveness?: number;
  times_applied?: number;
  times_succeeded?: number;
  times_failed?: number;
  source?: string;
  active?: number | boolean;
  created_at?: number;
  updated_at?: number;
}

export interface BehaviorData {
  patterns: BehaviorPattern[];
  recent_corrections: Array<{ content: string; timestamp: number }>;
}

export interface LogData {
  evolution?: LogEntry[];
  audit?: LogEntry[];
  gateway?: LogEntry[];
}

export interface UserProfile {
  user?: { id: string; name?: string };
  belief_summary?: string;
  beliefs?: Array<{ id: string; category: string; content: string; confidence: number; stage: string; contradicted?: boolean; created_at?: number; updated_at?: number }>;
  recent_observations?: Array<{ id: string; content: string; category?: string; timestamp?: number }>;
  error?: string;
}

export interface ChatRequest {
  message: string;
  user_id?: string;
  platform?: string;
  session_id?: string;
  context?: unknown[];
  /** true = open a brand-new DB session (新对话); undefined/null = resume */
  reset?: boolean;
}

export interface Proposal {
  id: number;
  proposal_type: string;
  content: string;
  detail?: string;
  source: string;
  status: string;
  auto_applied: number;
  result?: string;
  created_at: number;
  approved_at?: number;
  applied_at?: number;
}

/* ---- formatters ---- */

export function fmtTokens(n: number): string {
  if (!n) return "0";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return n.toLocaleString();
}

export function fmtCost(n: number): string {
  if (!n) return "$0";
  if (n < 0.01) return "<$0.01";
  return "$" + n.toFixed(2);
}

export function fmtTime(s: string | number | undefined | null): string {
  if (!s) return "";
  let d: Date;
  if (typeof s === "number") {
    d = new Date(s * 1000);
  } else if (/^\d+\.?\d*$/.test(s)) {
    d = new Date(parseFloat(s) * 1000);
  } else {
    d = new Date(s);
  }
  if (isNaN(d.getTime())) return String(s);
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  if (diff < 0) return d.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return Math.floor(diff / 60_000) + "分钟前";
  if (diff < 86_400_000) return Math.floor(diff / 3_600_000) + "小时前";
  return d.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
}