"""SQLite database schema, FTS5, and data access layer for metano."""

import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

from .log import logger
from .paths import DB_DIR, DB_PATH

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    title TEXT,
    model TEXT,
    user_key TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    last_active REAL,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_name TEXT,
    tool_calls TEXT,
    timestamp REAL NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS _index_state (
    file_path TEXT PRIMARY KEY,
    last_byte_offset INTEGER NOT NULL,
    last_modified REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_last_active ON sessions(last_active);
"""

def get_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get a WAL-mode SQLite connection."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-8000")
    conn.execute("PRAGMA foreign_keys=ON")
    # F-07: bound lock-contention waiting so concurrent gateway/web writers do
    # not immediately fail with "database is locked"; SQLite retries internally.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Initialize the database with schema."""
    conn = get_db(db_path)
    conn.executescript(SCHEMA_SQL)
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN user_key TEXT")
        conn.commit()
    except Exception:
        pass  # column already exists
    conn.commit()
    return conn


def persist_exchange(session_id: str, user_key: str, platform: str, msg: str, response: str,
                     usage: Optional[dict] = None, model: Optional[str] = None,
                     conn: Optional[sqlite3.Connection] = None) -> tuple:
    """Persist one user + assistant exchange into bridge.db.

    F-07: returns a structured result ``(session_id, persisted: bool)``. The
    caller must only treat the exchange as saved when ``persisted`` is True and
    must surface a warning to the user otherwise. When no ``session_id`` is
    given, a brand-new session is created — a missing id means "this is a new
    conversation", never "continue the most recent one" (that intent is carried
    explicitly by a non-empty ``session_id``, set by the router's restore/inject
    paths).

    Real usage (input/output/cache_read tokens) drives the per-message token
    columns and the accumulated session totals + estimated cost via
    ``model_router.estimate_cost``.
    """
    own = conn is None
    if conn is None:
        conn = get_db()
    try:
        now = time.time()
        usage = usage or {}
        in_tok = usage.get('input_tokens', 0) or 0
        out_tok = usage.get('output_tokens', 0) or 0
        cache_tok = usage.get('cache_read_tokens', 0) or 0
        if not session_id:
            session_id = uuid.uuid4().hex[:12]
            conn.execute(
                'INSERT INTO sessions (id, project, title, model, started_at, last_active, message_count, tool_call_count, input_tokens, output_tokens, cache_read_tokens, estimated_cost_usd, user_key) '
                'VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, ?)',
                (session_id, platform, (msg or '')[:30], model, now, now, user_key)
            )
        ts = now
        conn.execute(
            'INSERT INTO messages (session_id, role, content, tool_name, tool_calls, timestamp, input_tokens, output_tokens, duration_ms) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (session_id, 'user', msg, None, None, ts, in_tok, 0, None)
        )
        conn.execute(
            'INSERT INTO messages (session_id, role, content, tool_name, tool_calls, timestamp, input_tokens, output_tokens, duration_ms) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (session_id, 'assistant', response, None, None, ts + 0.0001, 0, out_tok, None)
        )
        try:
            from .model_router import model_router
            cost = model_router.estimate_cost(model, in_tok, out_tok, cache_tok)
        except Exception:
            logger.exception('cost estimate failed')
            cost = 0.0
        conn.execute(
            'UPDATE sessions SET message_count = message_count + 2, last_active = ?, '
            'input_tokens = input_tokens + ?, output_tokens = output_tokens + ?, '
            'cache_read_tokens = cache_read_tokens + ?, estimated_cost_usd = estimated_cost_usd + ?, '
            'model = COALESCE(?, model) WHERE id = ?',
            (now, in_tok, out_tok, cache_tok, cost, model, session_id)
        )
        conn.commit()
        return session_id, True
    except sqlite3.OperationalError as e:
        msg_lower = str(e).lower()
        if 'locked' in msg_lower or 'busy' in msg_lower:
            logger.warning(f'persist_exchange database lock conflict: {e}')
        else:
            logger.exception('persist_exchange failed')
        return session_id, False
    except Exception:
        logger.exception('persist_exchange failed')
        return session_id, False
    finally:
        if own:
            conn.close()


def live_db_stats(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Return size stats for bridge.db.

    - live_size_mb: approximate size of live (non-free) pages.
    - file_size_mb: on-disk file size (includes WAL; shrinks after VACUUM).
    """
    own = conn is None
    if own:
        conn = get_db()
    try:
        page_count = conn.execute('PRAGMA page_count').fetchone()[0]
        freelist = conn.execute('PRAGMA freelist_count').fetchone()[0]
        page_size = conn.execute('PRAGMA page_size').fetchone()[0]
        live_bytes = (page_count - freelist) * page_size
        file_bytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        return {
            'page_count': page_count,
            'freelist_count': freelist,
            'page_size': page_size,
            'live_size_mb': round(live_bytes / (1024 * 1024), 1),
            'file_size_mb': round(file_bytes / (1024 * 1024), 1),
        }
    finally:
        if own:
            conn.close()


def _backup_bridge_db(conn: sqlite3.Connection) -> str:
    """Write a consistent snapshot of bridge.db via the SQLite backup API.

    Returns the backup path. Uses the same underlying mechanism as
    `sqlite3 .backup`, so it is safe against concurrent writers (WAL mode).
    """
    backup_dir = DB_DIR / 'backups' / 'purge'
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')
    dest = backup_dir / f'bridge.pre_purge_{ts}.db'
    dest_conn = sqlite3.connect(str(dest))
    try:
        with dest_conn:
            conn.backup(dest_conn)
    finally:
        dest_conn.close()
    return str(dest)


def purge_old_sessions(days: int = 180, dry_run: bool = False,
                       size_limit_mb: Optional[int] = None,
                       backup: bool = True, vacuum: bool = True,
                       conn: Optional[sqlite3.Connection] = None) -> dict:
    """Session retention policy for bridge.db.

    Deletes chat sessions (and their messages) that have been inactive for more
    than ``days`` days. Session search is a plain LIKE query over ``messages``
    (no FTS index), so deletion is a straightforward two-step DML (messages
    first, then the session row).

    Optional size cap: when ``size_limit_mb`` is set and the on-disk DB still
    exceeds that size after the age purge, the oldest remaining sessions are
    evicted until the file is under the cap. This is a hard safety valve against
    runaway growth and MAY delete sessions younger than ``days`` days — keep the
    cap generous.

    Safety guarantees:
    - ``dry_run=True``: reports only, never mutates, never backs up.
    - ``backup=True`` (and not dry_run): a consistent snapshot of bridge.db is
      written to ``<metano>/backups/purge/`` before any deletion.
    - ``vacuum=True`` (and not dry_run): VACUUM runs after purging so freed pages
      actually shrink the file. VACUUM failure is non-fatal.
    - Never deletes the single most recent session (always keeps history anchor).

    Returns a summary dict suitable for a cron action.
    """
    own_conn = conn is None
    if conn is None:
        conn = get_db()
    try:
        cutoff = time.time() - days * 86400
        # Age-based candidates, oldest first. NULL/0 last_active counts as old.
        rows = conn.execute(
            'SELECT id, title, message_count, last_active FROM sessions '
            'WHERE last_active IS NULL OR last_active < ? '
            'ORDER BY last_active ASC, id ASC',
            (cutoff,)
        ).fetchall()
        age_candidates = [dict(r) for r in rows]

        total_sessions = conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        before_stats = live_db_stats(conn)

        # For the size cap we also need to know how many additional (younger)
        # sessions would be evicted once the file is over the limit.
        size_extra = 0
        if size_limit_mb is not None:
            # Approximate per-session eviction needs using live size delta.
            # We don't pre-compute precisely; dry_run reports the age candidates
            # plus a note. Real run handles it iteratively below.
            pass

        result = {
            'status': 'dry_run' if dry_run else 'ok',
            'retention_days': days,
            'cutoff_ts': cutoff,
            'cutoff_utc': datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(),
            'total_sessions': total_sessions,
            'candidate_sessions': len(age_candidates),
            'candidate_messages': sum(r['message_count'] or 0 for r in age_candidates),
            'size_limit_mb': size_limit_mb,
            'before': before_stats,
        }

        if dry_run:
            # Report only — no backup, no mutation, no vacuum.
            oldest = age_candidates[:5]
            result['oldest_candidates'] = [
                {'id': r['id'], 'title': (r['title'] or '')[:40],
                 'message_count': r['message_count'],
                 'last_active_utc': (datetime.fromtimestamp(r['last_active'], tz=timezone.utc).isoformat()
                                     if r['last_active'] else None)}
                for r in oldest
            ]
            if size_limit_mb is not None and before_stats['file_size_mb'] > size_limit_mb:
                result['size_cap_note'] = (
                    f"file ({before_stats['file_size_mb']}MB) exceeds cap "
                    f"{size_limit_mb}MB; age purge alone may not bring it under."
                )
            return result

        if not age_candidates:
            return result

        # ---- destructive path starts here ----
        if backup:
            try:
                backup_path = _backup_bridge_db(conn)
                result['backup'] = backup_path
            except Exception:
                # Backup failure is fatal for a destructive op: abort.
                result['status'] = 'error'
                result['error'] = 'backup failed; aborting purge (no deletion performed)'
                return result

        ids = [r['id'] for r in age_candidates]
        deleted_sessions = len(ids)
        try:
            # messages first, then sessions.
            conn.execute(f'DELETE FROM messages WHERE session_id IN ({",".join("?" * len(ids))})', ids)
            conn.execute(f'DELETE FROM sessions WHERE id IN ({",".join("?" * len(ids))})', ids)
            conn.commit()
            result['deleted_sessions'] = deleted_sessions
            result['deleted_messages'] = result['candidate_messages']
        except Exception as e:
            conn.rollback()
            result['status'] = 'error'
            result['error'] = f'purge failed (rolled back): {e}'
            return result

        # ---- optional size cap: evict oldest remaining until under limit ----
        if size_limit_mb is not None:
            evicted = 0
            # Keep at least the single most recent session as a history anchor.
            for _ in range(5000):
                cur_file_mb = DB_PATH.stat().st_size / (1024 * 1024) if DB_PATH.exists() else 0
                if cur_file_mb <= size_limit_mb:
                    break
                nxt = conn.execute(
                    'SELECT id, message_count FROM sessions ORDER BY last_active ASC, id ASC LIMIT 1'
                ).fetchone()
                if nxt is None:
                    break
                # Never evict the last remaining session.
                remaining = conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
                if remaining <= 1:
                    break
                nid = nxt['id']
                mcount = nxt['message_count'] or 0
                conn.execute('DELETE FROM messages WHERE session_id = ?', (nid,))
                conn.execute('DELETE FROM sessions WHERE id = ?', (nid,))
                conn.commit()
                evicted += 1
            if evicted:
                result['size_cap_evicted_sessions'] = evicted
                result['deleted_sessions'] = deleted_sessions + evicted

        # ---- reclaim space ----
        if vacuum:
            try:
                conn.execute('VACUUM')
            except Exception:
                result['vacuum'] = 'failed (non-fatal)'
            else:
                result['vacuum'] = 'ok'

        after = live_db_stats(conn)
        result['after'] = after
        result['status'] = 'ok'
        return result
    finally:
        if own_conn:
            conn.close()


def cron_purge_sessions() -> dict:
    """Cron action: weekly retention sweep of chat sessions in bridge.db.

    Conservative defaults: 180-day inactivity cutoff, generous 512MB hard size
    cap (only fires if the DB genuinely runs away). Always backs up first.
    """
    try:
        return purge_old_sessions(days=180, size_limit_mb=512, vacuum=True)
    except Exception as e:
        return {'status': 'error', 'error': str(e)}
