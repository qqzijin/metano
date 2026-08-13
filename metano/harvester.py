"""Observation harvester: automatically extract observations from session messages.

Reads ALL roles (user + assistant + system) to detect:
- User preferences and habits (from user messages)
- User corrections and complaints (keyword detection)
- Tool call failures (from assistant messages)
"""

import json
import sqlite3
import time
import re

from .db import get_db, init_db
from .honcho.models import get_honcho_db, init_honcho_db, get_user, create_user, add_observation, user_key_to_honcho_user
from .honcho.dialectic import extract_observations, dialectic_reason

HARVEST_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS _harvest_state (
    session_id TEXT PRIMARY KEY,
    harvested_at REAL NOT NULL,
    observations_extracted INTEGER DEFAULT 0
);
"""

# Strong correction signals only. Weak/noisy patterns (e.g. bare 不要/别/不行)
# match ordinary negation in almost any sentence and flood the correction
# store with false positives → every skill gets a bogus "补充经验" proposal.
CORRECTION_PATTERNS = [
    r"不对", r"错了", r"不是这样", r"不是那样", r"还是不对", r"还是不行",
    r"这不行", r"那样不行", r"这样不行[！!。]?", r"做不行", r"怎么不行", r"就是不行",
    r"为啥.{0,6}不行", r"为什么.{0,6}不行",
    r"又重复", r"又来了", r"你又(来|错|说错|弄错|搞错)", r"为什么总是", r"为什么又",
    r"没有验证", r"没验证", r"真的做过验证",
    r"wrong again", r"not right", r"incorrect", r"didn't verify",
    r"again\?", r"repeat",
    r"不要(再|这样|那样|给我|这么|一直|告诉|总是|只)", r"别再", r"不要再",
    r"简陋", r"太简单", r"不够完善", r"不完善",
    r"又显示不出来", r"又不显示", r"数据显示不出来",
]

CORRECTION_RE = re.compile("|".join(CORRECTION_PATTERNS), re.IGNORECASE)

TOOL_CALL_PATTERN = re.compile(r"\[tool:(\w+)\]", re.IGNORECASE)
TOOL_ERROR_INDICATORS = ["error", "failed", "exception", "traceback", "errno", "non-zero"]


def _ensure_harvest_state(conn: sqlite3.Connection):
    """Create _harvest_state table if not exists."""
    conn.execute(HARVEST_STATE_SCHEMA)
    conn.commit()


def get_unharvested_sessions(conn: sqlite3.Connection, limit: int = 10) -> list[str]:
    """Return session IDs with messages that haven't been harvested yet."""
    _ensure_harvest_state(conn)
    rows = conn.execute(
        "SELECT DISTINCT m.session_id FROM messages m "
        "LEFT JOIN _harvest_state h ON m.session_id = h.session_id "
        "WHERE h.session_id IS NULL "
        "ORDER BY m.timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["session_id"] for r in rows]


SYSTEM_TEXT_PREFIXES = [
    "你是", "你的角色是", "你是一个", "你的功能是", "你有以下",
    "你具备", "你可以", "你能够", "请记住",
    "系统设定", "系统提示", "以下是你",
    "profile", "你这个agent", "你的配置",
    "# 系统", "# 设定", "# 配置",
    "## user profile", "## 用户画像", "[memory]", "[evolution]",
    "=== 画像类", "=== approved", "=== 用户", "===",

]

def _is_system_generated(content: str) -> bool:
    """Check if a message looks system-generated rather than user-typed."""
    if len(content) > 800:
        return True
    first_line = content.strip().split('\n')[0].strip()
    if any(first_line.startswith(p) for p in SYSTEM_TEXT_PREFIXES):
        return True
    return False


def _detect_corrections(user_msgs: list[dict], assistant_msgs: list[dict]) -> list[dict]:
    """Scan user messages for correction keywords and pair with preceding assistant message."""
    corrections = []
    # Build a timeline by timestamp for easy "previous message" lookup
    all_msgs = sorted(
        [{"role": ".role", "content": r["content"], "timestamp": r["timestamp"]} for r in user_msgs]
        + [{"role": "assistant", "content": r["content"], "timestamp": r["timestamp"]} for r in assistant_msgs],
        key=lambda m: m["timestamp"],
    )

    for msg in user_msgs:
        content = msg["content"]
        if _is_system_generated(content):
            continue
        if not CORRECTION_RE.search(content):
            continue
        # Correction must be a short, conversational user turn — not a pasted
        # tool output, search result, diff, or code block (these contain 不要/别
        # incidentally and were flooding the correction store).
        stripped = content.strip()
        if len(stripped) > 200:
            continue
        if "```" in stripped or "http://" in stripped or "https://" in stripped:
            continue
        # A question (ends with 吗/？/?) is asking, not correcting.
        if re.search(r"[吗？?]$", stripped):
            continue
        # ABAB rhetorical questions (对不对/是不是/行不行/好不好/要不要)
        # contain 不对/不行 but are asking, not correcting.
        if re.search(r"[对是不好不好要]不(对|是|好|行|要)", stripped):
            continue
        # A real correction responds to something the assistant just said.
        prev_assistant = None
        for m in all_msgs:
            if m["role"] == "assistant" and m["timestamp"] < msg["timestamp"]:
                prev_assistant = m
        if prev_assistant is None or not prev_assistant["content"].strip():
            continue

        correction_entry = {
            "type": "correction",
            "user_content": stripped[:200],
            "prev_assistant_content": (prev_assistant["content"][:200] if prev_assistant else ""),
            "timestamp": msg["timestamp"],
            "strength": "strong" if any(kw in stripped for kw in ["又", "重复", "为什么总是", "真的做过"]) else "moderate",
        }
        corrections.append(correction_entry)

    return corrections


def _detect_tool_errors(assistant_msgs: list[dict]) -> list[dict]:
    """Scan assistant messages for failed tool calls."""
    errors = []
    for msg in assistant_msgs:
        content = msg["content"]
        if TOOL_CALL_PATTERN.search(content):
            # Check if the content or nearby messages indicate failure
            lower = content.lower()
            matched_indicators = [i for i in TOOL_ERROR_INDICATORS if i in lower]
            if matched_indicators:
                tool_name = TOOL_CALL_PATTERN.search(content).group(1)
                errors.append({
                    "type": "tool_error",
                    "tool": tool_name,
                    "error_type": matched_indicators[0],
                    "content": content[:200],
                    "timestamp": msg["timestamp"],
                })
    return errors


def harvest_session(conn: sqlite3.Connection, session_id: str, user_id: str | None = None,
                     max_seconds: float = 120) -> dict:
    """Extract observations from a single session's messages.

    Reads ALL roles (user + assistant + system) to detect:
    1. User preferences (via LLM observation extraction from user messages)
    2. User corrections (keyword detection)
    3. Tool call failures (from assistant messages)

    max_seconds: time budget — skips LLM-dependent steps if exceeded.

    C5: unless a user is explicitly supplied, harvest into the session owner's
    honcho profile (mapped from the session's ``user_key``) instead of a shared
    ``default`` — otherwise one user's messages poison everyone's profile.
    """
    _ensure_harvest_state(conn)

    # C5: derive the owner from the session's user_key when not explicitly given.
    if not user_id:
        row = conn.execute("SELECT user_key FROM sessions WHERE id = ?", (session_id,)).fetchone()
        user_id = user_key_to_honcho_user(row["user_key"]) if row and row["user_key"] else "default"

    # Get ALL messages for this session
    rows = conn.execute(
        "SELECT role, content, timestamp FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
        (session_id,),
    ).fetchall()

    if not rows:
        _mark_harvested(conn, session_id, 0)
        return {"session_id": session_id, "observations": 0, "results": []}

    user_msgs = [r for r in rows if r["role"] == "user"]
    assistant_msgs = [r for r in rows if r["role"] == "assistant"]

    # Ensure user exists in honcho
    honcho_conn = get_honcho_db()
    if not get_user(honcho_conn, user_id):
        create_user(honcho_conn, user_id=user_id)

    total_obs = 0
    results = []
    _start = time.time()

    # A. Extract preferences from user messages (existing logic)
    if user_msgs and (time.time() - _start) < max_seconds:
        conversation_text = "\n---\n".join(r["content"] for r in user_msgs)[:4000]
        observations = extract_observations(user_id, conversation_text)
        for obs in observations:
            result = dialectic_reason(user_id, obs.get("content", ""), obs.get("category", "general"))
            results.append({
                "observation": obs.get("content", "")[:100],
                "action": result.get("action"),
                "confidence": result.get("confidence"),
            })
        total_obs += len(observations)

    # B. Detect user corrections → store concise summary only
    corrections = _detect_corrections(user_msgs, assistant_msgs) if (time.time() - _start) < max_seconds else []
    for c in corrections:
        summary = c['user_content'][:80]
        obs_content = f"[用户纠正] {summary}"
        # A11: persist the confidence the harvester computed — corrections are
        # direct user signals, so they carry high confidence.
        add_observation(honcho_conn, user_id, obs_content, "correction", session_id,
                        confidence=c.get("strength", "moderate") in ("high", "strong") and 0.95 or 0.8)
        total_obs += 1
        results.append({
            "observation": obs_content[:100],
            "action": "correction_detected",
            "strength": c.get("strength", "moderate"),
        })

    # C. Detect tool call failures → store a compact tool_error observation so
    # behavior_analyzer can learn from recurring tool failures. The message
    # content is truncated; only tool name, error type, and a summary are kept.
    tool_errors = _detect_tool_errors(assistant_msgs) if (time.time() - _start) < max_seconds else []
    for e in tool_errors:
        obs_content = f"[tool_error] tool={e['tool']} type={e.get('error_type', 'error')} {e['content'][:100]}"
        # A tool error is an objective signal (error/failed/exception markers
        # next to a tool call), so it carries high confidence.
        add_observation(honcho_conn, user_id, obs_content, "tool_error", session_id,
                        confidence=1.0)
        total_obs += 1
        results.append({
            "observation": f"tool_error:{e['tool']}",
            "action": "tool_error_detected",
            "tool": e["tool"],
        })

    _mark_harvested(conn, session_id, total_obs)
    return {
        "session_id": session_id,
        "observations": total_obs,
        "corrections": len(corrections),
        "tool_errors": len(tool_errors),
        "results": results,
    }


def harvest_recent_sessions(max_sessions: int = 3) -> dict:
    """Harvest the N most recent unharvested sessions."""
    conn = init_db()
    session_ids = get_unharvested_sessions(conn, limit=max_sessions)

    if not session_ids:
        return {"sessions_processed": 0, "total_observations": 0, "details": []}

    details = []
    total_obs = 0
    for sid in session_ids:
        result = harvest_session(conn, sid)
        details.append(result)
        total_obs += result["observations"]

    return {"sessions_processed": len(session_ids), "total_observations": total_obs, "details": details}


def _mark_harvested(conn: sqlite3.Connection, session_id: str, obs_count: int):
    """Mark a session as harvested."""
    conn.execute(
        "INSERT OR REPLACE INTO _harvest_state (session_id, harvested_at, observations_extracted) VALUES (?, ?, ?)",
        (session_id, time.time(), obs_count),
    )
    conn.commit()
