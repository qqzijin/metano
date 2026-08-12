"""Evolution data models: agent_rules, action_log, evolution_meta — separate from user modeling (Honcho)."""

import json
import re
import sqlite3
import time
from typing import Optional

from .paths import EVO_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'behavior',        -- behavior / strategy / knowledge_pattern / memory_pattern
    content TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',          -- global / project / skill
    confidence REAL NOT NULL DEFAULT 0.5,
    effectiveness REAL NOT NULL DEFAULT 0.0,
    times_applied INTEGER NOT NULL DEFAULT 0,
    times_succeeded INTEGER NOT NULL DEFAULT 0,
    times_failed INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'correction',     -- correction / observation / synthesis / strategy / network_explore
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    metadata TEXT DEFAULT '{}',
    -- 新增：基于网络探索的Agent Memory改进
    temporal_tag TEXT DEFAULT '',                  -- 时间标签：rising/declining/stable/new
    recall_rate REAL DEFAULT 0.0,                -- 记忆召回率
    last_recalled_at REAL DEFAULT 0               -- 上次召回时间
);

CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    action_type TEXT NOT NULL,
    action_detail TEXT,
    rule_ids_applied TEXT DEFAULT '[]',
    outcome TEXT,                                  -- success / failure / partial / fallback_used
    timestamp REAL NOT NULL,
    -- 新增：基于网络探索的工具回退机制
    fallback_level INTEGER DEFAULT 0,            -- 0=主工具, 1=替代工具, 2=模拟工具, 3=用户协助
    tool_name TEXT DEFAULT '',                   -- 使用的工具名称
    error_type TEXT DEFAULT ''                   -- 错误类型
);

CREATE TABLE IF NOT EXISTS evolution_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS architecture_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_json TEXT NOT NULL,
    findings_count INTEGER NOT NULL DEFAULT 0,
    proposals_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_type TEXT NOT NULL,            -- behavior_improvement / config_change / rule_add / claude_md_inject
    content TEXT NOT NULL,
    detail TEXT DEFAULT '',
    source TEXT NOT NULL DEFAULT 'evolution',  -- evolution / introspector / strategy / reflection
    status TEXT NOT NULL DEFAULT 'pending',    -- pending / approved / rejected / applied / failed
    auto_applied INTEGER NOT NULL DEFAULT 0,
    result TEXT DEFAULT '',
    created_at REAL NOT NULL,
    approved_at REAL,
    applied_at REAL
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_type ON proposals(proposal_type);

CREATE TABLE IF NOT EXISTS effect_baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL,
    baseline_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_effect_baseline_proposal ON effect_baselines(proposal_id);
CREATE INDEX IF NOT EXISTS idx_arch_snapshots_time ON architecture_snapshots(created_at);

CREATE INDEX IF NOT EXISTS idx_agent_rules_kind ON agent_rules(kind);
CREATE INDEX IF NOT EXISTS idx_agent_rules_active ON agent_rules(active);
CREATE INDEX IF NOT EXISTS idx_action_log_session ON action_log(session_id);
CREATE INDEX IF NOT EXISTS idx_action_log_type ON action_log(action_type);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phase TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    detail TEXT DEFAULT '',
    cost REAL DEFAULT 0,
    model TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    timestamp REAL NOT NULL DEFAULT (strftime('%s','now')),
    -- 新增：基于网络探索的成本控制和挫败感检测
    frustration_detected INTEGER DEFAULT 0,      -- 是否检测到用户挫败感
    frustration_signals TEXT DEFAULT '',         -- 挫败感信号JSON
    cost_optimized INTEGER DEFAULT 0             -- 是否经过成本优化
);

CREATE INDEX IF NOT EXISTS idx_audit_phase ON audit_log(phase);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp);

CREATE TABLE IF NOT EXISTS cron_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT '',
    schedule_kind TEXT DEFAULT 'cron',
    schedule_expr TEXT DEFAULT '0 0 * * *',
    enabled INTEGER DEFAULT 1,
    prompt TEXT DEFAULT '',
    last_run_at REAL,
    next_run_at REAL,
    last_error TEXT
);

-- Self-modification events: every code mutation the evolution system proposes /
-- applies is recorded here (the "mutation log") so any change can be inspected
-- and reverted via git. This is the species-level safety net: a bad mutation is
-- recorded, rejected, and can be reverted — the system always survives.
CREATE TABLE IF NOT EXISTS self_modify_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue TEXT NOT NULL,                 -- what problem the mutation addresses
    file TEXT NOT NULL,                  -- file the diff touches
    diff TEXT NOT NULL,                  -- full unified diff
    verify_result TEXT DEFAULT 'pending',-- pending / verified / failed
    applied_at REAL,                     -- when applied to runtime (epoch)
    commit_hash TEXT,                    -- git commit hash (rollback point)
    status TEXT DEFAULT 'candidate',     -- candidate/verified/applied/rejected/reverted
    created_at REAL
);

-- Skill usage tracking: every time a skill is activated (via /trigger), one
-- row is recorded so the system knows which skills are hot and which are dead
-- (feeds skill pruning and the "which skills to keep" decision).
CREATE TABLE IF NOT EXISTS skill_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    platform TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    used_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_usage_name ON skill_usage(skill_name);
CREATE INDEX IF NOT EXISTS idx_skill_usage_time ON skill_usage(used_at);
"""


def _get_conn() -> sqlite3.Connection:
    EVO_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(EVO_DB_PATH))
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = _get_conn()
    conn.executescript(SCHEMA)
    conn.close()


# ── agent_rules CRUD ──

def add_rule(kind: str, content: str, scope: str = "global",
             confidence: float = 0.5, source: str = "correction",
             metadata: dict = None) -> int:
    now = time.time()
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO agent_rules (kind, content, scope, confidence, source, created_at, updated_at, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, content, scope, confidence, source, now, now, json.dumps(metadata or {}))
    )
    rule_id = cur.lastrowid
    conn.commit()
    conn.close()
    return rule_id


def get_rules(kind: str = None, active_only: bool = True) -> list[dict]:
    conn = _get_conn()
    sql = "SELECT * FROM agent_rules"
    conds, params = [], []
    if kind:
        conds.append("kind = ?")
        params.append(kind)
    if active_only:
        conds.append("active = 1")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY effectiveness DESC, confidence DESC"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    for r in rows:
        r["metadata"] = json.loads(r.get("metadata") or "{}")
    return rows


def get_rule(rule_id: int) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM agent_rules WHERE id = ?", (rule_id,)).fetchone()
    conn.close()
    if not row:
        return None
    r = dict(row)
    r["metadata"] = json.loads(r.get("metadata") or "{}")
    return r


def update_rule_effectiveness(rule_id: int, success: bool = None,
                               effectiveness: float = None,
                               times_applied: int = None,
                               times_succeeded: int = None,
                               times_failed: int = None):
    conn = _get_conn()
    if effectiveness is not None and times_applied is not None:
        conn.execute(
            "UPDATE agent_rules SET times_applied = ?, times_succeeded = ?, "
            "times_failed = ?, effectiveness = ?, updated_at = ? WHERE id = ?",
            (times_applied, times_succeeded or 0, times_failed or 0,
             effectiveness, time.time(), rule_id)
        )
    elif success is not None:
        conn.execute(
            "UPDATE agent_rules SET times_applied = times_applied + 1, "
            "times_succeeded = times_succeeded + ?, times_failed = times_failed + ?, "
            "effectiveness = (CAST(times_succeeded AS REAL) + ?) / (CAST(times_applied AS REAL) + 1), "
            "updated_at = ? WHERE id = ?",
            (1 if success else 0, 0 if success else 1,
             1 if success else 0, time.time(), rule_id)
        )
    conn.commit()
    conn.close()


def toggle_rule(rule_id: int, active: bool):
    conn = _get_conn()
    conn.execute("UPDATE agent_rules SET active = ?, updated_at = ? WHERE id = ?",
                 (1 if active else 0, time.time(), rule_id))
    conn.commit()
    conn.close()




def rule_count(kind: str = None) -> int:
    conn = _get_conn()
    sql = "SELECT COUNT(*) FROM agent_rules"
    params = []
    if kind:
        sql += " WHERE kind = ?"
        params.append(kind)
    count = conn.execute(sql, params).fetchone()[0]
    conn.close()
    return count


# ── action_log CRUD ──

def log_action(session_id: str, action_type: str, action_detail: str = "",
               rule_ids_applied: str = "", outcome: str = "pending") -> int:
    """Log an action to the action_log table. Returns the action ID."""
    conn = _get_conn()
    cursor = conn.execute(
        "INSERT INTO action_log (session_id, action_type, action_detail, rule_ids_applied, outcome, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, action_type, action_detail, rule_ids_applied, outcome, time.time())
    )
    conn.commit()
    action_id = cursor.lastrowid
    conn.close()
    return action_id


def parse_rule_ids(raw) -> list:
    """Parse a ``rule_ids_applied`` cell into a list of rule id strings.

    F-08: historical rows may store a JSON array (``'["1","2"]'``) or a legacy
    comma-separated string (``'1,2'``); both must parse so Strategy can read the
    full history instead of erroring on one format.
    """
    if raw is None:
        return []
    s = str(raw).strip()
    if not s:
        return []
    if s.startswith('['):
        try:
            val = json.loads(s)
            if isinstance(val, list):
                return [str(x).strip() for x in val if str(x).strip()]
        except (json.JSONDecodeError, ValueError):
            pass
    return [x.strip() for x in s.split(',') if x.strip()]


def get_recent_actions(limit: int = 50, action_type: str = None) -> list[dict]:
    conn = _get_conn()
    sql = "SELECT * FROM action_log"
    params = []
    if action_type:
        sql += " WHERE action_type = ?"
        params.append(action_type)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    for r in rows:
        r["rule_ids_applied"] = parse_rule_ids(r.get("rule_ids_applied"))
    return rows


def get_action_stats() -> dict:
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
    by_outcome = {}
    for row in conn.execute("SELECT outcome, COUNT(*) as cnt FROM action_log GROUP BY outcome"):
        by_outcome[row["outcome"] or "unknown"] = row["cnt"]
    by_type = {}
    for row in conn.execute("SELECT action_type, COUNT(*) as cnt FROM action_log GROUP BY action_type"):
        by_type[row["action_type"]] = row["cnt"]
    conn.close()
    return {"total": total, "by_outcome": by_outcome, "by_type": by_type}


# ── evolution_meta CRUD ──

def get_meta(key: str, default: str = None) -> Optional[str]:
    conn = _get_conn()
    row = conn.execute("SELECT value FROM evolution_meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_meta(key: str, value):
    conn = _get_conn()
    conn.execute("INSERT OR REPLACE INTO evolution_meta (key, value) VALUES (?, ?)", (key, json.dumps(value) if not isinstance(value, str) else value))
    conn.commit()
    conn.close()


def save_architecture_snapshot(model_json: str, findings_count: int = 0, proposals_count: int = 0) -> int:
    conn = _get_conn()
    cursor = conn.execute(
        "INSERT INTO architecture_snapshots (model_json, findings_count, proposals_count, created_at) "
        "VALUES (?, ?, ?, ?)",
        (model_json, findings_count, proposals_count, time.time()),
    )
    conn.commit()
    snap_id = cursor.lastrowid
    conn.close()
    return snap_id


def get_latest_architecture_snapshot() -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM architecture_snapshots ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return None
    r = dict(row)
    try:
        r["model"] = json.loads(r["model_json"])
    except (json.JSONDecodeError, TypeError):
        r["model"] = None
    return r


# ── Proposals CRUD ──

_PROPOSAL_PREFIXES = ('add a rule: ', 'promote observation to belief: ', 'fix ')


def normalize_proposal_content(text: str) -> str:
    """Normalize proposal content for near-duplicate detection.

    Strips framing prefixes, source-file line numbers (memory.py:99 -> memory.py),
    collapses whitespace and trims trailing punctuation, so e.g.
    'Fix sql-concat: memory.py:99 — ...' and 'Fix sql-concat: memory.py:43 — ...'
    (the same finding, line shifted by later edits) collapse to one key.
    """
    s = (text or '').strip().lower()
    for prefix in _PROPOSAL_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # Drop line numbers right after source file paths (introspector findings).
    s = re.sub(r'([\w./\\-]+\.(?:py|tsx|jsx)):\d+', r'\1', s)
    s = re.sub(r'\s+', ' ', s).strip(' \t\n.。！!?？')
    return s


def add_proposal(proposal_type: str, content: str, detail: str = "",
                 source: str = "evolution") -> int:
    """Insert a new pending proposal.

    Quality gate: returns 0 (skipped) when content is empty or is a
    near-duplicate (by normalized content) of an existing proposal in any
    status, so re-runs don't re-queue the same finding after line shifts or
    wording tweaks. Callers must treat 0 as 'not created'.
    """
    if not content or not content.strip():
        return 0
    key = normalize_proposal_content(content)
    if not key:
        return 0
    conn = _get_conn()
    try:
        for r in conn.execute("SELECT proposal_type, content FROM proposals"):
            if r["proposal_type"] == proposal_type and normalize_proposal_content(r["content"]) == key:
                return 0
        now = time.time()
        cur = conn.execute(
            "INSERT INTO proposals (proposal_type, content, detail, source, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (proposal_type, content, detail, source, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_proposals(status: str = None, proposal_type: str = None) -> list[dict]:
    conn = _get_conn()
    sql = "SELECT * FROM proposals"
    conds, params = [], []
    if status:
        conds.append("status = ?")
        params.append(status)
    if proposal_type:
        conds.append("proposal_type = ?")
        params.append(proposal_type)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY created_at DESC"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def update_proposal_status(proposal_id: int, status: str, result: str = ""):
    now = time.time()
    conn = _get_conn()
    ptype = ""
    if status == "approved":
        conn.execute("UPDATE proposals SET status=?, approved_at=? WHERE id=?",
                     (status, now, proposal_id))
        row = conn.execute(
            "SELECT proposal_type FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        ptype = row["proposal_type"] if row else ""
    elif status == "applied" or status == "failed":
        conn.execute("UPDATE proposals SET status=?, applied_at=?, result=? WHERE id=?",
                     (status, now, result, proposal_id))
    else:
        conn.execute("UPDATE proposals SET status=? WHERE id=?", (status, proposal_id))
    conn.commit()
    conn.close()
    # A8: close the approved→applied state machine for idempotent rule types
    # regardless of which path approved the proposal (web UI + MCP both call
    # this helper). apply is idempotent (see adapter._apply_behavior_improvement
    # / _apply_rule_add), so re-approval never duplicates a rule. Non-rule types
    # (config_change / claude_md_inject / skill) stay approved for explicit apply.
    if status == "approved" and ptype in ("behavior_improvement", "rule_add"):
        try:
            from .adapter import apply_proposal
            apply_proposal(proposal_id)
        except Exception:
            pass


# ── Migration from Honcho ──

def migrate_from_honcho():
    """Copy behavior_pattern beliefs from honcho.db to agent_rules."""
    honcho_path = EVO_DB_PATH.parent / "honcho_data" / "honcho.db"
    if not honcho_path.exists():
        return 0

    hconn = sqlite3.connect(str(honcho_path))
    hconn.row_factory = sqlite3.Row
    beliefs = hconn.execute(
        "SELECT * FROM beliefs WHERE category = 'behavior_pattern' AND COALESCE(contradicted, 0) = 0"
    ).fetchall()
    hconn.close()

    if not beliefs:
        return 0

    existing = {r["content"] for r in get_rules(kind="behavior")}
    migrated = 0
    for b in beliefs:
        bd = dict(b)
        content = bd["content"]
        if content in existing:
            continue
        add_rule(
            kind="behavior",
            content=content,
            scope="global",
            confidence=bd.get("confidence", 0.5),
            source="migration",
            metadata={"honcho_id": bd["id"], "original_category": bd.get("category")}
        )
        migrated += 1
    return migrated


# ── Audit Log ──

def add_audit(phase: str, action: str, detail: str = '', cost: float = 0,
              model: str = '', session_id: str = '') -> int:
    now = time.time()
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO audit_log (phase, action, detail, cost, model, session_id, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (phase, action, detail, cost, model, session_id, now)
    )
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    return aid


def get_audit(limit: int = 100, phase: str = '', since: float = 0) -> list[dict]:
    conn = _get_conn()
    sql = "SELECT * FROM audit_log"
    conds, params = [], []
    if phase:
        conds.append("phase = ?")
        params.append(phase)
    if since:
        conds.append("timestamp >= ?")
        params.append(since)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def get_daily_cost(days: int = 1) -> float:
    cutoff = time.time() - days * 86400
    conn = _get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost), 0) FROM audit_log WHERE timestamp >= ?",
        (cutoff,)
    ).fetchone()
    conn.close()
    return row[0] if row else 0.0


def get_audit_cost_since(cutoff: float, phases: tuple = ()) -> float:
    """Direct SQL SUM of audit_log.cost since cutoff (optionally filtered by phase).

    S4：与 get_audit(limit=100) 不同，这里用聚合查询一次性求和，不会因日调用量
    超过 100 条而被截断，从而避免成本被低估、熔断永不触发。
    """
    conn = _get_conn()
    try:
        if phases:
            placeholders = ','.join('?' * len(phases))
            row = conn.execute(
                f"SELECT COALESCE(SUM(cost), 0) FROM audit_log "
                f"WHERE timestamp >= ? AND phase IN ({placeholders})",
                (cutoff,) + tuple(phases)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost), 0) FROM audit_log WHERE timestamp >= ?",
                (cutoff,)
            ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0.0


# ── Cron Jobs ──



def get_cron_jobs() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM cron_jobs ORDER BY name").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d['enabled'] = bool(d.get('enabled', 0))
        d['schedule'] = {'kind': d.get('schedule_kind', 'cron'), 'expr': d.get('schedule_expr', '0 0 * * *')}
        # Remove internal fields
        for k in ('schedule_kind', 'schedule_expr'):
            d.pop(k, None)
        result.append(d)
    conn.close()
    return result




# ── self_modify_events CRUD ──

def add_self_modify_event(issue: str, file: str, diff: str) -> int:
    """Record a new self-modification candidate (mutation) into the log."""
    conn = _get_conn()
    now = time.time()
    cur = conn.execute(
        "INSERT INTO self_modify_events (issue, file, diff, status, created_at) "
        "VALUES (?, ?, ?, 'candidate', ?)",
        (issue, file, diff, now),
    )
    conn.commit()
    eid = cur.lastrowid
    conn.close()
    return eid


def update_self_modify_event(event_id: int, **fields) -> None:
    """Update a self-modification event (verify_result / status / commit_hash /
    applied_at). Field names are validated against a whitelist."""
    allowed = {'verify_result', 'status', 'commit_hash', 'applied_at'}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    conn = _get_conn()
    conn.execute(f"UPDATE self_modify_events SET {', '.join(sets)} WHERE id = ?", vals + [event_id])
    conn.commit()
    conn.close()


def get_self_modify_events(limit: int = 50, status: str = None) -> list[dict]:
    """List self-modification events, newest first."""
    conn = _get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM self_modify_events WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM self_modify_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return result


def get_self_modify_event(event_id: int) -> dict | None:
    """Fetch a single self-modification event by id."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM self_modify_events WHERE id = ?", (event_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── skill usage tracking ──

def record_skill_usage(skill_name: str, platform: str = '', user_id: str = '') -> None:
    """Record that a skill was activated (used) at this moment."""
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO skill_usage (skill_name, platform, user_id, used_at) VALUES (?, ?, ?, ?)",
            (skill_name, platform, user_id, time.time()),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception('record_skill_usage failed')


def get_skill_usage(days: int = 30) -> list[dict]:
    """Aggregate skill usage frequency over the last ``days`` days, hottest first."""
    conn = _get_conn()
    cutoff = time.time() - days * 86400
    rows = conn.execute(
        "SELECT skill_name, COUNT(*) AS uses, MAX(used_at) AS last_used "
        "FROM skill_usage WHERE used_at >= ? GROUP BY skill_name "
        "ORDER BY uses DESC, last_used DESC",
        (cutoff,),
    ).fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return result


def get_skill_usage_all_time() -> list[dict]:
    """Aggregate skill usage over all time (for dead-skill detection)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT skill_name, COUNT(*) AS uses, MAX(used_at) AS last_used "
        "FROM skill_usage GROUP BY skill_name ORDER BY uses DESC"
    ).fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return result
