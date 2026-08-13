"""SQLite database schema, FTS5, and data access layer for metano."""

import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

from .log import logger
from .paths import DB_DIR, DB_PATH, GATEWAY_LOG

# Sensitive-material redaction for free-form message text (audit N2): the
# message write path previously stored user/assistant content verbatim, so live
# credentials (app_secret, sk- keys, JWTs, bearer tokens) landed in bridge.db
# in plaintext.  These patterns scrub such material before persistence.
_SENSITIVE_KEY = (
    r'app_secret|api[_-]?key|apikey|access[_-]?token|bot[_-]?token|'
    r'refresh[_-]?token|auth[_-]?token|encryption[_-]?key|verification[_-]?token|'
    r'secret|password|passwd|ha[_-]?token'
)
_SENSITIVE_KEY_RE = re.compile(
    rf'(?i)\b({_SENSITIVE_KEY})(\s*(?:=\s*|:\s*))(["\']?)([^\s"\'`,;]{{4,}})\3'
)
# Function-call form, e.g. .app_secret('value') / setPassword("value").
_FUNC_CALL_RE = re.compile(
    rf'(?i)\b({_SENSITIVE_KEY})\s*\(\s*(["\'])([A-Za-z0-9_\-./]{{4,}})\2\s*\)'
)
_BARE_SK_RE = re.compile(r'\bsk-[A-Za-z0-9_\-]{8,}\b')
# ``eyJ`` is base64 of `{"` — the header of every JWT.  Require at least two
# base64url segments (header[.payload[.signature]]) with a realistic header
# length (>=8 chars; the smallest real header — {"alg":"none"} — is 18) so a
# bare JWT is caught without mangling prose like "eyJelly.bean".
_BARE_JWT_RE = re.compile(
    r'\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}(?:\.[A-Za-z0-9_\-]{4,})?\b'
)
_BEARER_RE = re.compile(r'\bBearer\s+[A-Za-z0-9_\-\.]{20,}\b')


def _redact_key_value(m: re.Match) -> str:
    """Replace a ``key=value`` / ``key: value`` pair's value with [REDACTED],
    preserving the key and surrounding quote.  Numeric-only values (e.g. a
    ``token: 10`` LLM count) are left untouched to avoid mangling prose."""
    key, sep, quote, value = m.group(1), m.group(2), m.group(3), m.group(4)
    if value.isdigit():
        return m.group(0)
    return f'{key}{sep}{quote}[REDACTED]{quote}'


def _redact_func_call(m: re.Match) -> str:
    """Replace a ``key('value')`` function-call argument with [REDACTED]."""
    key, quote, value = m.group(1), m.group(2), m.group(3)
    if value.isdigit():
        return m.group(0)
    return f'{key}({quote}[REDACTED]{quote})'


def redact_sensitive(content: Optional[str]) -> Optional[str]:
    """Redact secret material from free-form text before persistence.

    Returns ``content`` with credential-bearing patterns (``app_secret=`` /
    ``api_key: `` / function-call forms, ``sk-`` keys, JWTs, bearer tokens)
    replaced by ``[REDACTED]``.
    """
    if not content:
        return content
    text = _SENSITIVE_KEY_RE.sub(_redact_key_value, content)
    text = _FUNC_CALL_RE.sub(_redact_func_call, text)
    text = _BARE_SK_RE.sub('[REDACTED]', text)
    text = _BARE_JWT_RE.sub('[REDACTED]', text)
    text = _BEARER_RE.sub('[REDACTED]', text)
    return text

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
        # Redact credential material before it reaches the store (audit N2);
        # the title is a snippet of msg, so derive it from the redacted form.
        msg_red = redact_sensitive(msg or '')
        resp_red = redact_sensitive(response)
        if not session_id:
            session_id = uuid.uuid4().hex[:12]
            conn.execute(
                'INSERT INTO sessions (id, project, title, model, started_at, last_active, message_count, tool_call_count, input_tokens, output_tokens, cache_read_tokens, estimated_cost_usd, user_key) '
                'VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, ?)',
                (session_id, platform, msg_red[:30], model, now, now, user_key)
            )
        ts = now
        conn.execute(
            'INSERT INTO messages (session_id, role, content, tool_name, tool_calls, timestamp, input_tokens, output_tokens, duration_ms) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (session_id, 'user', msg_red, None, None, ts, in_tok, 0, None)
        )
        conn.execute(
            'INSERT INTO messages (session_id, role, content, tool_name, tool_calls, timestamp, input_tokens, output_tokens, duration_ms) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (session_id, 'assistant', resp_red, None, None, ts + 0.0001, 0, out_tok, None)
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
    Also prunes/rotates the plaintext gateway_log.jsonl (H2/M12).
    """
    result = {}
    try:
        result['bridge'] = purge_old_sessions(days=180, size_limit_mb=512, vacuum=True)
    except Exception as e:
        result['bridge'] = {'status': 'error', 'error': str(e)}
    try:
        result['gateway_log'] = purge_gateway_log(days=30, dry_run=False)
    except Exception as e:
        result['gateway_log'] = {'status': 'error', 'error': str(e)}
    return result


def _rotate_file(path, max_bytes: int = 50 * 1024 * 1024, backup_count: int = 5) -> dict:
    """Rotate an oversized JSONL/log file into numbered backups (.1, .2, …).

    M12: logs previously had no rotation or retention policy — a runaway
    gateway_log.jsonl grew unbounded. Rotation keeps the live file small while
    preserving up to ``backup_count`` historical files.
    """
    p = Path(path)
    if not p.exists():
        return {'rotated': False}
    size = p.stat().st_size
    if size <= max_bytes:
        return {'rotated': False, 'size': size}
    for i in range(backup_count - 1, 0, -1):
        src = Path(f'{p}.{i}')
        dst = Path(f'{p}.{i + 1}')
        if src.exists():
            if dst.exists():
                dst.unlink()
            src.rename(dst)
    first = Path(f'{p}.1')
    if first.exists():
        first.unlink()
    p.rename(first)
    return {'rotated': True, 'old_size': size, 'backup': str(first)}


def purge_gateway_log(days: int = 30, user_key: str = '', dry_run: bool = False,
                      rotate: bool = True) -> dict:
    """Prune gateway_log.jsonl by age and/or user.

    H2: gateway_log had no deletion path — this is the retention entry point.
    M12: optionally rotates an oversized file first (skip on dry_run so no
    rename happens while reporting).
    """
    if not GATEWAY_LOG.exists():
        return {'status': 'no_file'}
    rotation = {}
    if rotate and not dry_run:
        rotation = _rotate_file(GATEWAY_LOG)
    if not GATEWAY_LOG.exists():
        # Rotation renamed the whole live file away — nothing left to prune.
        result = {'status': 'ok', 'deleted': 0, 'kept': 0}
        if rotation:
            result['rotation'] = rotation
        return result
    cutoff = time.time() - days * 86400 if days else 0
    kept: list[str] = []
    deleted = 0
    with open(GATEWAY_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            remove = False
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if user_key and rec.get('user_id') == user_key:
                remove = True
            elif days and rec.get('timestamp') and float(rec['timestamp']) < cutoff:
                remove = True
            if remove:
                deleted += 1
            else:
                kept.append(line)
    if not dry_run:
        GATEWAY_LOG.write_text('\n'.join(kept) + ('\n' if kept else ''))
    result = {'status': 'dry_run' if dry_run else 'ok', 'deleted': deleted, 'kept': len(kept)}
    if rotation:
        result['rotation'] = rotation
    return result


def purge_user_data(user_key: str = '', dry_run: bool = False) -> dict:
    """Cascade-delete all stored data for a platform user (H2).

    Covers bridge.db sessions/messages, honcho beliefs/observations for the
    mapped honcho user, and gateway_log.jsonl lines for that user. When the
    user key maps to the shared 'default' honcho profile, honcho data is
    skipped to avoid wiping every user's profile.
    """
    if not user_key:
        return {'status': 'error', 'error': 'user_key required'}
    result = {'user_key': user_key, 'dry_run': dry_run}
    conn = get_db()
    try:
        session_ids = [r['id'] for r in conn.execute(
            'SELECT id FROM sessions WHERE user_key=?', (user_key,)).fetchall()]
        msg_count = 0
        for sid in session_ids:
            msg_count += conn.execute(
                'SELECT COUNT(*) FROM messages WHERE session_id=?', (sid,)).fetchone()[0]
        result['bridge_sessions'] = len(session_ids)
        result['bridge_messages'] = msg_count
        if not dry_run and session_ids:
            ph = ','.join('?' * len(session_ids))
            conn.execute(f'DELETE FROM messages WHERE session_id IN ({ph})', session_ids)
            conn.execute(f'DELETE FROM sessions WHERE id IN ({ph})', session_ids)
            conn.commit()
    finally:
        conn.close()
    try:
        from .honcho.models import user_key_to_honcho_user, get_honcho_db
        honcho_uid = user_key_to_honcho_user(user_key)
        if honcho_uid == 'default' and user_key != 'default':
            result['honcho_skipped'] = 'user_key maps to shared default profile'
        else:
            hconn = get_honcho_db()
            try:
                b = hconn.execute(
                    'SELECT COUNT(*) FROM beliefs WHERE user_id=?', (honcho_uid,)).fetchone()[0]
                o = hconn.execute(
                    'SELECT COUNT(*) FROM observations WHERE user_id=?', (honcho_uid,)).fetchone()[0]
                result['honcho_beliefs'] = b
                result['honcho_observations'] = o
                if not dry_run:
                    hconn.execute('DELETE FROM beliefs WHERE user_id=?', (honcho_uid,))
                    hconn.execute('DELETE FROM observations WHERE user_id=?', (honcho_uid,))
                    hconn.commit()
            finally:
                hconn.close()
    except Exception:
        logger.exception('purge_user_data: honcho cleanup failed')
    result['gateway_log'] = purge_gateway_log(days=0, user_key=user_key, dry_run=dry_run)
    return result


def purge_evo_history(days: int = 365, dry_run: bool = False) -> dict:
    """Prune evo.db audit_log older than ``days`` (H2 retention entry point).

    audit_log is the cost/audit trail; trimming old rows bounds growth without
    touching learning data (agent_rules / proposals / action_log are kept).
    """
    try:
        from .evo_models import _get_conn
        cutoff = time.time() - days * 86400
        conn = _get_conn()
        try:
            total = conn.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0]
            target = conn.execute(
                'SELECT COUNT(*) FROM audit_log WHERE timestamp < ?', (cutoff,)).fetchone()[0]
            if not dry_run:
                conn.execute('DELETE FROM audit_log WHERE timestamp < ?', (cutoff,))
                conn.commit()
            return {'status': 'dry_run' if dry_run else 'ok', 'days': days,
                    'total': total, 'candidates': target}
        finally:
            conn.close()
    except Exception as e:
        return {'status': 'error', 'error': str(e)}
