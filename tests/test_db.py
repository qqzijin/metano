"""M16: key-path coverage for metano/db.py — persistence, retention, error paths.

The autouse ``isolated_env`` fixture redirects ``db.DB_PATH``/``DB_DIR`` to a
throwaway tmp dir, so purge backups and deletion never touch production.
"""

import sqlite3
import time
from unittest.mock import MagicMock

import pytest

from metano import db as metano_db

pytestmark = pytest.mark.usefixtures("isolated_env")


def test_init_db_creates_tables():
    conn = metano_db.get_db()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert {'sessions', 'messages'} <= tables


# ── persist_exchange ───────────────────────────────────────────────────────

def test_persist_exchange_new_session():
    sid, persisted = metano_db.persist_exchange(
        '', 'web:alice', 'web', 'hello', 'hi there', usage={'input_tokens': 10, 'output_tokens': 5}, model='m1')
    assert persisted is True
    assert sid
    conn = metano_db.get_db()
    row = conn.execute('SELECT user_key, message_count, input_tokens, output_tokens FROM sessions WHERE id=?', (sid,)).fetchone()
    messages = conn.execute('SELECT COUNT(*) FROM messages WHERE session_id=?', (sid,)).fetchone()[0]
    conn.close()
    assert row['user_key'] == 'web:alice'
    assert row['message_count'] == 2
    assert row['input_tokens'] == 10
    assert row['output_tokens'] == 5
    assert messages == 2


def test_persist_exchange_continues_existing_session():
    sid, _ = metano_db.persist_exchange('', 'web:bob', 'web', 'first', 'r1')
    sid2, persisted = metano_db.persist_exchange(sid, 'web:bob', 'web', 'second', 'r2')
    assert sid2 == sid
    assert persisted is True
    conn = metano_db.get_db()
    n = conn.execute('SELECT COUNT(*) FROM messages WHERE session_id=?', (sid,)).fetchone()[0]
    conn.close()
    assert n == 4


def test_persist_exchange_locked_returns_false():
    # Error path: a "database is locked" OperationalError must be caught and
    # surface as persisted=False, never an exception.
    mock_conn = MagicMock()
    mock_conn.execute.side_effect = sqlite3.OperationalError('database is locked')
    sid, persisted = metano_db.persist_exchange('', 'web:alice', 'web', 'x', 'y', conn=mock_conn)
    assert persisted is False


def test_persist_exchange_generic_error_returns_false():
    mock_conn = MagicMock()
    mock_conn.execute.side_effect = RuntimeError('boom')
    sid, persisted = metano_db.persist_exchange('', 'web:alice', 'web', 'x', 'y', conn=mock_conn)
    assert persisted is False


# ── live_db_stats ──────────────────────────────────────────────────────────

def test_live_db_stats_shape():
    metano_db.persist_exchange('', 'web:alice', 'web', 'a', 'b')
    stats = metano_db.live_db_stats()
    assert stats['page_count'] > 0
    assert stats['page_size'] > 0
    assert stats['live_size_mb'] >= 0
    assert stats['file_size_mb'] >= 0


# ── purge_old_sessions (retention) ─────────────────────────────────────────

def _seed_session(sid, last_active):
    conn = metano_db.get_db()
    conn.execute(
        'INSERT INTO sessions (id, project, title, model, user_key, started_at, last_active, message_count) '
        'VALUES (?,?,?,?,?,?,?,1)',
        (sid, 'web', sid, 'm1', 'web:alice', last_active, last_active),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'x', ?)",
        (sid, last_active),
    )
    conn.commit()
    conn.close()


def test_purge_dry_run_reports_without_deleting():
    old = time.time() - 100 * 86400
    recent = time.time()
    _seed_session('old1', old)
    _seed_session('recent1', recent)
    result = metano_db.purge_old_sessions(days=30, dry_run=True)
    assert result['status'] == 'dry_run'
    assert result['candidate_sessions'] == 1
    # Nothing deleted, no backup written.
    conn = metano_db.get_db()
    n = conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
    conn.close()
    assert n == 2


def test_purge_deletes_old_keeps_recent_and_backs_up():
    old = time.time() - 100 * 86400
    recent = time.time()
    _seed_session('old1', old)
    _seed_session('old2', old)
    _seed_session('recent1', recent)
    result = metano_db.purge_old_sessions(days=30, dry_run=False, vacuum=False)
    assert result['status'] == 'ok'
    assert result['deleted_sessions'] == 2
    assert 'backup' in result and result['backup']
    conn = metano_db.get_db()
    ids = [r[0] for r in conn.execute('SELECT id FROM sessions').fetchall()]
    conn.close()
    assert ids == ['recent1']
    import os
    assert os.path.exists(result['backup'])


def test_purge_backup_failure_aborts():
    old = time.time() - 100 * 86400
    _seed_session('old1', old)
    orig_backup = metano_db._backup_bridge_db
    metano_db._backup_bridge_db = MagicMock(side_effect=RuntimeError('disk full'))
    try:
        result = metano_db.purge_old_sessions(days=30, dry_run=False)
    finally:
        metano_db._backup_bridge_db = orig_backup  # restore, never delete the function
    assert result['status'] == 'error'
    assert 'backup failed' in result['error']
    # No deletion happened.
    conn = metano_db.get_db()
    n = conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
    conn.close()
    assert n == 1


def test_purge_deletes_lone_old_session():
    # The age purge removes any session past the retention cutoff, even when it
    # is the only one.  (The docstring's "history anchor" guarantee applies only
    # to the size-cap eviction loop, not the age purge.)
    old = time.time() - 100 * 86400
    _seed_session('only-one', old)
    result = metano_db.purge_old_sessions(days=30, dry_run=False, vacuum=False)
    assert result['status'] == 'ok'
    assert result['deleted_sessions'] == 1
    conn = metano_db.get_db()
    ids = [r[0] for r in conn.execute('SELECT id FROM sessions').fetchall()]
    conn.close()
    assert ids == []


# ── redact_sensitive (audit N2: message write path must not store secrets) ──

def test_redact_sensitive_patterns():
    r = metano_db.redact_sensitive
    assert r('app_secret=[REDACTED]') == 'app_secret=[REDACTED]'
    assert r('ANTHROPIC_API_KEY=sk-mWbiLOPVabcdef123456') == 'ANTHROPIC_API_KEY=[REDACTED]'
    assert r('"api_key": "sk-abc123def456"') == '"api_key": "[REDACTED]"'
    assert r("Client.builder().app_secret('3Y3fHqN7cFbwO4cP5dWfGdEeFbAaBbCc')") == \
        "Client.builder().app_secret('[REDACTED]')"
    assert '[REDACTED]' in r('Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature')
    assert '[REDACTED]' in r('eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.xYsV3A9qK0ZPxTk9xL8v')
    # Numeric-only "token: 10" (LLM count) and ordinary prose stay untouched.
    assert r('token: 10') == 'token: 10'
    assert r('the token count is normal prose here') == 'the token count is normal prose here'
    assert r('sk-artist hello world') == 'sk-artist hello world'
    # GitHub personal access tokens (ghp_/gho_/ghu_/ghs_/ghr_), including when
    # a literal "\n" (backslash + n) precedes them in command/response text.
    pat = 'ghp_' + 'A' * 36
    assert r(pat) == '[REDACTED]'
    assert r('key=' + 'gho_' + 'B' * 36) == 'key=[REDACTED]'
    assert r('PLACEHOLDER' + chr(92) + 'n' + 'ghu_' + 'C' * 36) == \
        'PLACEHOLDER' + chr(92) + 'n[REDACTED]'
    assert r(None) is None
    assert r('') == ''


def test_persist_exchange_redacts_secrets():
    sid, persisted = metano_db.persist_exchange(
        '', 'web:alice', 'web',
        'please set app_secret=[REDACTED]',
        'ok ANTHROPIC_API_KEY=sk-mWbiLOPVabcdef123456 now',
        model='m1')
    assert persisted is True
    conn = metano_db.get_db()
    rows = conn.execute(
        'SELECT role, content FROM messages WHERE session_id=? ORDER BY timestamp', (sid,)).fetchall()
    conn.close()
    contents = {r['role']: r['content'] for r in rows}
    assert contents['user'] == 'please set app_secret=[REDACTED]'
    assert contents['assistant'] == 'ok ANTHROPIC_API_KEY=[REDACTED] now'
