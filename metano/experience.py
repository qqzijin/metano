"""Experience memory for the routing feedback loop (Plan A).

Stores Reflexion-style lessons extracted from failed tasks as ``DO:`` /
``AVOID:`` experiences, retrieves the most relevant ones for a new request,
and injects them into the prompt so the gateway gets smarter over time.

The store lives in the same WAL SQLite database as the evolution/strategy data
(``evo.db``, see :mod:`metano.route_events`). Retrieval is a lightweight
keyword-overlap scorer today (semantic/vector retrieval is a future step; the
scoring function is the single plug point to swap in embeddings).

Anti-degradation measures:
- Experiences carry an ``effectiveness`` score. Successes decay AVOID lessons
  (they warned about a failure mode that did not recur) and reinforce DO
  lessons; failures reinforce all lessons of that task type.
- Weak old lessons are deactivated automatically and purged by
  :func:`cleanup_experiences`, which the event recorder triggers roughly every
  50 route events.
- Reflections are deduplicated by (task_type, direction, summary) so repeated
  failures of the same kind reinforce one lesson instead of flooding the store.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Optional

from metano.log import logger
from .paths import EVO_DB_PATH

# Tests override this module attribute to isolate the DB.
DB_PATH = EVO_DB_PATH

# 'heuristic' = template reflections (fast, deterministic, no LLM call).
# 'llm'       = Reflexion via the LLM channel (opt-in; synchronous, can block).
REFLECTION_MODE = 'heuristic'

_SCHEMA = """
CREATE TABLE IF NOT EXISTS route_experiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    task_signature TEXT DEFAULT '',
    direction TEXT NOT NULL,                    -- 'do' / 'avoid'
    summary TEXT NOT NULL,
    detail TEXT DEFAULT '',
    outcome TEXT DEFAULT '',
    source_event_id INTEGER,
    effectiveness REAL NOT NULL DEFAULT 0.5,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_route_exp_type ON route_experiences(task_type);
CREATE INDEX IF NOT EXISTS idx_route_exp_active ON route_experiences(active);
"""

# Default heuristic reflections per error class / task type.
_AVOID_MIN_EFFECTIVENESS = 0.15
_AVOID_AGE_DAYS = 7


_init_path = None  # DB path whose schema has already been applied this process


def _get_conn() -> sqlite3.Connection:
    global _init_path
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if _init_path != str(DB_PATH):
        conn.executescript(_SCHEMA)
        conn.commit()
        _init_path = str(DB_PATH)
    return conn


def init_db() -> sqlite3.Connection:
    """Create the route_experiences table (idempotent)."""
    return _get_conn()




# ── reflection generation ───────────────────────────────────────────────────

def _heuristic_reflection(task_type: str, error_class: str, response: str) -> tuple[str, str, str]:
    """Template-based Reflexion: return (do, avoid, detail).

    Deterministic and fast — the default for the hot path. Good enough to make
    the loop run end-to-end without spending tokens on every failure.
    """
    ec = error_class or 'generic'
    task_desc = task_type or 'the task'
    r = (response or '').strip()
    detail = r[:160] if r else ''
    if ec == 'timeout':
        do = f"对于{task_desc}任务，设置更短超时或拆分为更小步骤，避免长时间等待。"
        avoid = f"不要在{task_desc}任务中长时间等待单一阻塞调用；优先重试或并行。"
    elif ec == 'auth':
        do = f"处理{task_desc}任务前先检查凭证/权限是否就绪。"
        avoid = f"不要在{task_desc}任务中忽略鉴权失败直接继续。"
    elif ec == 'rate_limit':
        do = f"{task_desc}任务遇到限流时退避重试或更换通道。"
        avoid = f"不要在{task_desc}任务中连续密集请求同一通道。"
    elif ec == 'exception':
        do = f"{task_desc}任务出错时读取异常信息并分步排查。"
        avoid = f"不要在{task_desc}任务中忽略 traceback / 错误详情。"
    elif ec == 'empty':
        do = f"{task_desc}任务无输出时补充上下文或换一种表达重试。"
        avoid = f"不要在{task_desc}任务中直接返回空结果。"
    else:
        do = f"处理{task_desc}任务时先确认需求并给出明确可执行的步骤。"
        avoid = f"不要在{task_desc}任务中重复上次失败的做法（错误类型：{ec}）。"
    return do, avoid, detail


def _llm_reflection(task_type: str, error_class: str, response: str) -> tuple[str, str, str]:
    """Reflexion via the LLM channel (opt-in). Falls back to heuristic on any failure."""
    try:
        from .llm_call import call_llm
        system = (
            "You are a Reflexion engine. Given a failed task, extract one concise, "
            "reusable lesson. Return ONLY JSON: "
            '{"do": "what to do next time", "avoid": "what to avoid", "detail": "root cause"}'
        )
        user = f"task_type={task_type}\nerror_class={error_class}\nresponse={str(response)[:1000]}"
        text, _ = call_llm(system, user, max_tokens=300, timeout=15)
        start, end = text.find('{'), text.rfind('}')
        if start != -1 and end != -1 and end > start:
            data = json.loads(text[start:end + 1])
            return (
                str(data.get('do', '')).strip(),
                str(data.get('avoid', '')).strip(),
                str(data.get('detail', '')).strip()[:200],
            )
    except Exception:
        logger.exception('experience: LLM reflection failed, using heuristic')
    return _heuristic_reflection(task_type, error_class, response)


def _generate_reflection(task_type: str, error_class: str, response: str) -> tuple[str, str, str]:
    if REFLECTION_MODE == 'llm':
        return _llm_reflection(task_type, error_class, response)
    return _heuristic_reflection(task_type, error_class, response)


# ── experience CRUD ─────────────────────────────────────────────────────────

def record_reflection(task_type: str, task_signature: str = '', error_class: str = '',
                      response: str = '', outcome: str = 'failure',
                      source_event_id: int = 0) -> dict:
    """Extract a DO/AVOID lesson from a failure and store it as an experience.

    Deduplicates by (task_type, direction, summary): a repeated failure of the
    same kind reinforces the existing lesson instead of adding a near-duplicate.
    """
    if not task_type:
        return {'status': 'skip', 'reason': 'no task_type'}
    do, avoid, detail = _generate_reflection(task_type, error_class, response)
    conn = _get_conn()
    try:
        created = 0
        now = time.time()
        for direction, summary in (('do', do), ('avoid', avoid)):
            if not summary or not summary.strip():
                continue
            row = conn.execute(
                'SELECT id, effectiveness, task_signature FROM route_experiences '
                'WHERE task_type=? AND direction=? AND summary=? AND active=1',
                (task_type, direction, summary),
            ).fetchone()
            if row:
                # M7: a repeated failure of the SAME source (same task_signature)
                # means the deterministic template lesson failed to prevent this
                # exact failure — reinforcing it would push it to 1.0 forever
                # (inject → fail → reinforce). Weaken it slightly instead so it
                # can eventually deactivate; lessons from a DIFFERENT source that
                # happen to share the summary still get reinforced.
                if row['task_signature'] and row['task_signature'] == task_signature:
                    eff = max(0.0, (row['effectiveness'] or 0.5) - 0.05)
                else:
                    eff = min(1.0, (row['effectiveness'] or 0.5) + 0.1)
                conn.execute(
                    'UPDATE route_experiences SET effectiveness=?, created_at=?, outcome=?, source_event_id=? WHERE id=?',
                    (eff, now, outcome, source_event_id, row['id']),
                )
            else:
                conn.execute(
                    'INSERT INTO route_experiences '
                    '(task_type, task_signature, direction, summary, detail, outcome, source_event_id, effectiveness, active, created_at) '
                    'VALUES (?,?,?,?,?,?,?,?,1,?)',
                    (task_type, task_signature, direction, summary, detail, outcome,
                     source_event_id, 0.5, now),
                )
                created += 1
        conn.commit()
        return {'status': 'ok', 'created': created}
    finally:
        conn.close()


def reward_relevant(task_type: str, outcome: str, task_signature: str = '') -> dict:
    """Adjust experience effectiveness after an outcome for a task type.

    - success: AVOID lessons lose a little weight (the warned failure mode did
      not recur), DO lessons gain weight (they worked).
    - failure: lessons from a DIFFERENT failure source gain weight (they were
      relevant); lessons whose signature matches the current failure (same
      source) are weakened slightly instead of reinforced.

    M7: previously EVERY failure reinforced ALL lessons of the task type. The
    heuristic templates are deterministic per (task_type, error_class), so the
    same template was injected on every repeat and reinforced to 1.0 forever —
    an infinite "fail → inject same template → reinforce" loop. Signature-aware
    reinforcement breaks that loop while still rewarding genuinely informative
    cross-source lessons.

    Weak, old lessons are deactivated so they stop being retrieved.
    """
    conn = _get_conn()
    try:
        if outcome == 'success':
            conn.execute(
                'UPDATE route_experiences SET effectiveness = MAX(0.0, effectiveness - 0.05) '
                'WHERE task_type=? AND direction="avoid" AND active=1',
                (task_type,),
            )
            conn.execute(
                'UPDATE route_experiences SET effectiveness = MIN(1.0, effectiveness + 0.05) '
                'WHERE task_type=? AND direction="do" AND active=1',
                (task_type,),
            )
        elif outcome == 'failure':
            conn.execute(
                'UPDATE route_experiences SET effectiveness = '
                'CASE WHEN task_signature = ? THEN MAX(0.0, effectiveness - 0.05) '
                '     ELSE MIN(1.0, effectiveness + 0.1) END '
                'WHERE task_type=? AND active=1',
                (task_signature, task_type),
            )
        cutoff = time.time() - _AVOID_AGE_DAYS * 86400
        conn.execute(
            'UPDATE route_experiences SET active=0 '
            'WHERE effectiveness < ? AND created_at < ?',
            (_AVOID_MIN_EFFECTIVENESS, cutoff),
        )
        conn.commit()
        return {'status': 'ok'}
    finally:
        conn.close()


# ── retrieval + injection ───────────────────────────────────────────────────

_STOPWORDS = {
    'the', 'a', 'an', 'of', 'to', 'in', 'for', 'on', 'and', 'or', 'is', 'are',
    'be', 'it', 'i', 'you', 'me', 'my', 'this', 'that', 'with', 'please', '帮我',
    '一下', '一个', '什么', '怎么', '如何', '能', '可以', '请', '给', '写',
}


def _keywords(text: str) -> set[str]:
    """Extract low-noise keywords from a query (works for CJK too)."""
    s = (text or '').lower()
    s = re.sub(r'[\W_]+', ' ', s, flags=re.UNICODE)
    return {t for t in s.split() if t and t not in _STOPWORDS}


def retrieve_experiences(query: str, task_type: str = '', limit: int = 3,
                         conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Return the top-k most relevant active experiences for a query.

    Scoring: task-type match (strongest) + keyword overlap on summary/detail +
    signature overlap + effectiveness weight + mild recency bonus. Deterministic
    and dependency-free; swap for vector similarity later.
    """
    own = conn is None
    if conn is None:
        conn = _get_conn()
    try:
        qk = _keywords(query)
        rows = conn.execute(
            'SELECT * FROM route_experiences WHERE active=1'
        ).fetchall()
        scored: list[tuple[float, dict]] = []
        now = time.time()
        for r in rows:
            d = dict(r)
            score = 0.0
            if d['task_type'] and d['task_type'] == task_type:
                score += 3.0
            elif d['task_type'] and task_type and d['task_type'] != task_type:
                score -= 1.0
            text = f"{d['summary']} {d['detail']}".lower()
            score += sum(1.0 for kw in qk if kw in text) * 1.5
            sig_kws = set((d['task_signature'] or '').split())
            score += len(qk & sig_kws) * 1.0
            eff = d.get('effectiveness', 0.5) or 0.5
            score *= 0.5 + eff
            age_days = max(0.0, (now - (d.get('created_at') or now)) / 86400)
            score += max(0.0, 1.0 - age_days / 30.0) * 0.2
            if score > 0.0:  # skip clearly irrelevant lessons (mismatched type, no keyword overlap)
                scored.append((score, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored[:limit]]
    finally:
        if own:
            conn.close()


def inject_experiences(prompt: str, query: str, task_type: str = '',
                       limit: int = 3) -> tuple[str, int]:
    """Retrieve relevant experiences and append them to the prompt as DO:/AVOID:.

    Returns (new_prompt, injected_count). The injected block is clearly framed
    as reference material the model may ignore if not applicable, and all text
    comes from our own store (no user-controlled injection surface beyond the
    stored lesson itself).
    """
    items = retrieve_experiences(query, task_type, limit=limit)
    if not items:
        return prompt, 0
    lines = ['[经验参考 · 来自相似任务的历史经验]']
    for it in items:
        summary = str(it.get('summary', '')).strip()
        if not summary:
            continue
        if it.get('direction') == 'do':
            lines.append(f"- DO: {summary}")
        else:
            lines.append(f"- AVOID: {summary}")
        detail = str(it.get('detail', '')).strip()
        if detail:
            lines.append(f"  (原因: {detail[:120]})")
    lines.append('（以上经验仅供参考，如不适用请忽略。）')
    block = '\n'.join(lines)
    return f"{prompt}\n\n{block}", len(items)


# ── maintenance / cleanup ───────────────────────────────────────────────────

def cleanup_experiences(keep_per_type: int = 10) -> dict:
    """Anti-degradation sweep: drop inactive and weak lessons, cap per task type."""
    conn = _get_conn()
    try:
        cur = conn.execute('DELETE FROM route_experiences WHERE active=0')
        deleted_inactive = cur.rowcount
        cutoff = time.time() - 30 * 86400
        cur = conn.execute(
            'DELETE FROM route_experiences WHERE effectiveness < ? AND created_at < ?',
            (_AVOID_MIN_EFFECTIVENESS, cutoff),
        )
        deleted_low = cur.rowcount
        deleted_overflow = 0
        for trow in conn.execute('SELECT DISTINCT task_type FROM route_experiences'):
            tt = trow[0]
            rows = conn.execute(
                'SELECT id FROM route_experiences WHERE task_type=? '
                'ORDER BY effectiveness DESC, created_at DESC',
                (tt,),
            ).fetchall()
            for extra in rows[keep_per_type:]:
                conn.execute('DELETE FROM route_experiences WHERE id=?', (extra['id'],))
                deleted_overflow += 1
        conn.commit()
        return {
            'status': 'ok',
            'deleted_inactive': deleted_inactive,
            'deleted_low': deleted_low,
            'deleted_overflow': deleted_overflow,
        }
    finally:
        conn.close()


def get_experience_stats() -> dict:
    conn = _get_conn()
    try:
        total = conn.execute('SELECT COUNT(*) FROM route_experiences').fetchone()[0]
        active = conn.execute('SELECT COUNT(*) FROM route_experiences WHERE active=1').fetchone()[0]
        by_type = {
            r['task_type']: r['cnt']
            for r in conn.execute('SELECT task_type, COUNT(*) cnt FROM route_experiences GROUP BY task_type')
        }
        return {'total': total, 'active': active, 'by_type': by_type}
    finally:
        conn.close()
