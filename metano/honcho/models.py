"""Honcho data models and SQLite schema for dialectic user modeling."""

import json
import sqlite3
import uuid
import time
from dataclasses import dataclass, field

from ..paths import HONCHO_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT 'user',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS beliefs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    source_observations TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    contradicted INTEGER NOT NULL DEFAULT 0,
    last_reinforced_at REAL NOT NULL DEFAULT 0,
    reinforcement_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    session_id TEXT,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    timestamp REAL NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5
);

CREATE INDEX IF NOT EXISTS idx_beliefs_user ON beliefs(user_id);
CREATE INDEX IF NOT EXISTS idx_observations_user ON observations(user_id);
"""

MIGRATIONS = [
    "ALTER TABLE beliefs ADD COLUMN last_reinforced_at REAL NOT NULL DEFAULT 0",
    "ALTER TABLE beliefs ADD COLUMN reinforcement_count INTEGER NOT NULL DEFAULT 0",
    # A11: observations confidence was computed by the harvester but never stored.
    "ALTER TABLE observations ADD COLUMN confidence REAL NOT NULL DEFAULT 0.5",
]


@dataclass
class Belief:
    id: str = ""
    user_id: str = ""
    category: str = "general"
    content: str = ""
    confidence: float = 0.5
    source_observations: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    contradicted: bool = False


@dataclass
class Observation:
    id: str = ""
    user_id: str = ""
    session_id: str = ""
    content: str = ""
    category: str = "general"
    timestamp: float = 0.0


def _ensure_schema(conn: sqlite3.Connection):
    """Idempotently create the Honcho schema + run migrations.

    Safe to call on every connection open: DDL is CREATE ... IF NOT EXISTS and
    migrations swallow the already-exists OperationalError.
    """
    conn.executescript(SCHEMA)
    for migration in MIGRATIONS:
        try:
            conn.execute(migration)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists


def get_honcho_db() -> sqlite3.Connection:
    HONCHO_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(HONCHO_DB), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    # F-03: guarantee the schema exists on every open so first-run entry points
    # (gateway/router, harvester, adapter, reflector) never hit missing tables.
    _ensure_schema(conn)
    return conn


def init_honcho_db() -> sqlite3.Connection:
    return get_honcho_db()


def user_key_to_honcho_user(user_key: str) -> str:
    """Map a bridge.db ``sessions.user_key`` (e.g. ``telegram:12345``) to a
    stable Honcho user id.

    User keys are built by the gateway as ``<platform>:<user_id>``; some
    platform adapters already prefix the raw id, producing keys like
    ``qq:qq:<id>``. This helper collapses the duplicated platform segment so
    multi-user data is never conflated under a single ``default`` profile.
    """
    if not user_key:
        return 'default'
    key = str(user_key).strip()
    if not key or key == 'default':
        return key or 'default'
    parts = key.split(':')
    if len(parts) < 2:
        return key
    # Collapse duplicate adjacent segments: qq:qq:12345 -> qq:12345.
    cleaned = [parts[0]]
    for p in parts[1:]:
        if p and p != cleaned[-1]:
            cleaned.append(p)
    return ':'.join(cleaned)


def create_user(conn: sqlite3.Connection, name: str = "user", user_id: str = "") -> dict:
    if not user_id:
        user_id = uuid.uuid4().hex[:12]
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO users (id, name, created_at) VALUES (?, ?, ?)",
        (user_id, name, now),
    )
    conn.commit()
    return {"id": user_id, "name": name, "created_at": now}


def get_user(conn: sqlite3.Connection, user_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def add_observation(conn: sqlite3.Connection, user_id: str, content: str,
                    category: str = "general", session_id: str = "", confidence: float = 0.5) -> dict:
    obs_id = uuid.uuid4().hex[:12]
    now = time.time()
    conn.execute(
        "INSERT INTO observations (id, user_id, session_id, content, category, timestamp, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (obs_id, user_id, session_id, content, category, now, confidence),
    )
    conn.commit()
    return {"id": obs_id, "user_id": user_id, "content": content, "category": category, "confidence": confidence}


def get_observations(conn: sqlite3.Connection, user_id: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM observations WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_beliefs(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM beliefs WHERE user_id = ? AND contradicted = 0 ORDER BY confidence DESC",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_belief(conn: sqlite3.Connection, user_id: str, category: str,
               content: str, confidence: float = 0.5, source_observations: list[str] | None = None) -> dict:
    """Insert a belief. Signature is (conn, user_id, category, content, ...).

    Raises ValueError when the arguments are clearly malformed — this guards
    against the classic swapped-args regression where ``content`` was passed as
    ``category`` (see reflector.apply_correction refine branch).
    """
    category = str(category or '').strip()
    content = str(content or '').strip()
    if not category:
        raise ValueError('add_belief: category must be a non-empty string')
    if not content:
        raise ValueError('add_belief: content must be a non-empty string')
    # Category is a short label (longest in use: 'temporal_abstraction', 19 chars).
    # A full sentence here means the caller swapped category/content — the exact
    # regression fixed in reflector.apply_correction (F-11).
    if len(category) > 32:
        raise ValueError(
            f'add_belief: category looks like a content string (len={len(category)}); '
            f'category/content args swapped? {category!r}')
    belief_id = uuid.uuid4().hex[:12]
    now = time.time()
    sources = json_dumps(source_observations or [])
    conn.execute(
        "INSERT INTO beliefs (id, user_id, category, content, confidence, source_observations, created_at, updated_at, last_reinforced_at, reinforcement_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (belief_id, user_id, category, content, confidence, sources, now, now, now, 0),
    )
    conn.commit()
    return {"id": belief_id, "category": category, "content": content, "confidence": confidence}


def update_belief(conn: sqlite3.Connection, belief_id: str, content: str,
                  confidence: float | None = None) -> dict | None:
    now = time.time()
    if confidence is not None:
        conn.execute(
            "UPDATE beliefs SET content = ?, confidence = ?, updated_at = ? WHERE id = ?",
            (content, confidence, now, belief_id),
        )
    else:
        conn.execute(
            "UPDATE beliefs SET content = ?, updated_at = ? WHERE id = ?",
            (content, now, belief_id),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM beliefs WHERE id = ?", (belief_id,)).fetchone()
    return dict(row) if row else None


def contradict_belief(conn: sqlite3.Connection, belief_id: str) -> dict | None:
    now = time.time()
    # A10: contradicting must also reset confidence/reinforcement so a refuted
    # belief can't linger as high-confidence "core" in later retrieval/reflection.
    conn.execute(
        "UPDATE beliefs SET contradicted = 1, confidence = 0.0, reinforcement_count = 0, updated_at = ? WHERE id = ?",
        (now, belief_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM beliefs WHERE id = ?", (belief_id,)).fetchone()
    return dict(row) if row else None


def delete_belief(conn: sqlite3.Connection, belief_id: str) -> bool:
    cursor = conn.execute("DELETE FROM beliefs WHERE id = ?", (belief_id,))
    conn.commit()
    return cursor.rowcount > 0


def get_profile(conn: sqlite3.Connection, user_id: str) -> dict:
    user = get_user(conn, user_id)
    if not user:
        return {}
    beliefs = get_beliefs(conn, user_id)
    observations = get_observations(conn, user_id, limit=20)
    return {
        "user": user,
        "beliefs": beliefs,
        "recent_observations": observations,
        "belief_summary": "\n".join(f"- [{b['category']}] {b['content']} (confidence: {b['confidence']:.0%}, stage: {belief_stage(b)})"
                                    for b in beliefs),
    }


def belief_stage(belief: dict) -> str:
    """Classify belief lifecycle stage."""
    # A10: a refuted belief can never be core/established.
    if belief.get("contradicted"):
        return "contradicted"
    conf = belief.get("confidence", 0.5)
    count = belief.get("reinforcement_count", 0)
    if conf >= 0.8 and count >= 5:
        return "core"
    if conf >= 0.6 and count >= 2:
        return "established"
    return "draft"


def reinforce_belief(conn: sqlite3.Connection, belief_id: str,
                     boost: float = 0.05, max_confidence: float = 0.95) -> dict | None:
    """Increase belief confidence when receiving supporting evidence."""
    now = time.time()
    row = conn.execute("SELECT confidence, reinforcement_count FROM beliefs WHERE id = ?", (belief_id,)).fetchone()
    if not row:
        return None
    new_conf = min(row["confidence"] + boost, max_confidence)
    new_count = row["reinforcement_count"] + 1
    conn.execute(
        "UPDATE beliefs SET confidence = ?, updated_at = ?, last_reinforced_at = ?, reinforcement_count = ? WHERE id = ?",
        (new_conf, now, now, new_count, belief_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM beliefs WHERE id = ?", (belief_id,)).fetchone()
    return dict(row) if row else None


def decay_beliefs(conn: sqlite3.Connection, user_id: str,
                  decay_rate: float = 0.02, min_confidence: float = 0.2) -> list[dict]:
    """Decay confidence of beliefs not recently reinforced."""
    now = time.time()
    beliefs = get_beliefs(conn, user_id)
    decayed = []
    for b in beliefs:
        days_since = (now - b.get("last_reinforced_at", b["created_at"])) / 86400
        if days_since < 7:
            continue
        # Core beliefs decay at half rate
        rate = decay_rate / 2 if belief_stage(b) == "core" else decay_rate
        new_conf = b["confidence"] - rate * days_since
        if new_conf < min_confidence:
            contradict_belief(conn, b["id"])
            decayed.append({**b, "action": "contradicted", "new_confidence": 0})
        elif new_conf < b["confidence"]:
            conn.execute(
                "UPDATE beliefs SET confidence = ?, updated_at = ? WHERE id = ?",
                (new_conf, now, b["id"]),
            )
            decayed.append({**b, "action": "decayed", "new_confidence": round(new_conf, 3)})
    conn.commit()
    return decayed


def unreinstate_belief(conn: sqlite3.Connection, belief_id: str) -> dict | None:
    """Reverse a contradiction, restoring the belief at reduced confidence."""
    now = time.time()
    conn.execute(
        "UPDATE beliefs SET contradicted = 0, confidence = 0.5, updated_at = ? WHERE id = ?",
        (now, belief_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM beliefs WHERE id = ?", (belief_id,)).fetchone()
    return dict(row) if row else None


def get_stale_beliefs(conn: sqlite3.Connection, user_id: str, days: int = 14) -> list[dict]:
    """Get beliefs not reinforced in N days."""
    cutoff = time.time() - days * 86400
    rows = conn.execute(
        "SELECT * FROM beliefs WHERE user_id = ? AND contradicted = 0 AND last_reinforced_at < ? AND last_reinforced_at > 0",
        (user_id, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def forget_beliefs(conn: sqlite3.Connection, user_id: str,
                    min_confidence: float = 0.15, min_reinforcements: int = 0,
                    max_age_days: int = 60) -> list[dict]:
    """Selectively forget beliefs: remove low-confidence, unreinforced, old beliefs.

    Criteria:
    - confidence < min_confidence AND reinforcements <= min_reinforcements
    - OR last_reinforced_at older than max_age_days AND confidence < 0.3
    - Core beliefs are protected from deletion
    """
    now = time.time()
    beliefs = get_beliefs(conn, user_id)
    forgotten = []
    for b in beliefs:
        stage = belief_stage(b)
        # Protect core beliefs
        if stage == "core":
            continue
        days_since = (now - b.get("last_reinforced_at", b["created_at"])) / 86400
        conf = b["confidence"]
        reinforcements = b.get("reinforcement_count", 0)
        should_forget = False
        # Criterion 1: Very low confidence, unreinforced
        if conf < min_confidence and reinforcements <= min_reinforcements:
            should_forget = True
        # Criterion 2: Old and low confidence
        elif days_since > max_age_days and conf < 0.3:
            should_forget = True
        # Criterion 3: Effectiveness-based (if tracked in metadata)
        metadata = json.loads(b.get("metadata", "{}"))
        effectiveness = metadata.get("effectiveness", None)
        if effectiveness is not None and effectiveness < 0.2 and days_since > 14:
            should_forget = True
        if should_forget:
            delete_belief(conn, b["id"])
            forgotten.append({**b, "action": "forgotten", "reason": f"conf={conf:.2f}, days={days_since:.0f}, reinforcements={reinforcements}"})
    return forgotten


def merge_similar_beliefs(conn: sqlite3.Connection, user_id: str,
                          similarity_threshold: float = 0.85) -> list[dict]:
    """Find and merge semantically similar beliefs within the same category."""
    beliefs = get_beliefs(conn, user_id)
    merged = []
    by_category = {}
    for b in beliefs:
        by_category.setdefault(b["category"], []).append(b)
    for cat, cat_beliefs in by_category.items():
        if len(cat_beliefs) < 2:
            continue
        # Simple text similarity via substring overlap
        for i in range(len(cat_beliefs)):
            if not cat_beliefs[i]:
                continue
            for j in range(i + 1, len(cat_beliefs)):
                if not cat_beliefs[j]:
                    continue
                b1, b2 = cat_beliefs[i], cat_beliefs[j]
                sim = _text_similarity(b1["content"], b2["content"])
                if sim >= similarity_threshold:
                    # Keep higher confidence one, merge content
                    keep = b1 if b1["confidence"] >= b2["confidence"] else b2
                    delete_id = b2["id"] if keep["id"] == b1["id"] else b1["id"]
                    delete_belief(conn, delete_id)
                    # Update kept belief
                    new_content = _merge_content(keep["content"], b1["content"] if keep["id"] == b2["id"] else b2["content"])
                    update_belief(conn, keep["id"], new_content, keep["confidence"])
                    merged.append({"kept": keep["id"], "deleted": delete_id, "category": cat, "similarity": sim})
                    cat_beliefs[j] = None
                    break
    return merged


def _text_similarity(a: str, b: str) -> float:
    """Char-bigram Jaccard similarity, robust for Chinese (no spaces).

    Word-overlap fails for Chinese: an entire sentence is one token, so two
    near-identical Chinese beliefs scored 0.0. Char bigrams capture the
    surface overlap that matters for belief dedup/merging in Chinese.
    """
    a = (a or '').lower().replace(' ', '')
    b = (b or '').lower().replace(' ', '')
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    grams_a = {a[i:i + 2] for i in range(len(a) - 1)}
    grams_b = {b[i:i + 2] for i in range(len(b) - 1)}
    if not grams_a or not grams_b:
        # Too short for bigrams — fall back to substring containment.
        return 1.0 if a in b or b in a else 0.0
    inter = grams_a & grams_b
    union = grams_a | grams_b
    return len(inter) / len(union)


def _merge_content(a: str, b: str) -> str:
    """Merge two belief contents, keeping the longer/more specific one."""
    return a if len(a) >= len(b) else b


def archive_old_contradictions(conn: sqlite3.Connection, user_id: str, days: int = 30) -> int:
    """Delete contradicted beliefs older than N days."""
    cutoff = time.time() - days * 86400
    cursor = conn.execute(
        "DELETE FROM beliefs WHERE user_id = ? AND contradicted = 1 AND updated_at < ?",
        (user_id, cutoff),
    )
    conn.commit()
    return cursor.rowcount


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)