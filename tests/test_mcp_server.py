"""M16: key-path coverage for metano/mcp_server.py — MCP subject filtering.

The remote Streamable HTTP layer records the authenticated JWT subject + scopes
in a contextvar (``metano.mcp_gateway._mcp_auth_ctx``); every data tool reads
it through ``_owner_cond`` / ``_memory_denied`` so a user-level token is
confined to its own rows (H-07).  These tests drive that contextvar directly
against the real helpers, plus a live DB check that ``session_list`` actually
scopes rows.
"""

import json

import pytest

from metano import db as metano_db
from metano import mcp_server
from metano.mcp_gateway import _mcp_auth_ctx

pytestmark = pytest.mark.usefixtures("isolated_env")


def _set_auth(subject, scopes):
    token = _mcp_auth_ctx.set((subject, scopes))
    try:
        return token
    except Exception:
        _mcp_auth_ctx.reset(token)
        raise


# ── gateway preauthorizable allowlist (C-02) ───────────────────────────────

def test_gateway_preauthorizable_read_tools():
    assert mcp_server.is_gateway_preauthorizable('session_list') is True
    assert mcp_server.is_gateway_preauthorizable('session_search') is True
    assert mcp_server.is_gateway_preauthorizable('analytics_summary') is True
    assert mcp_server.is_gateway_preauthorizable('skills_list') is True
    assert mcp_server.is_gateway_preauthorizable('mcp__metano__session_list') is True


def test_gateway_preauthorizable_rejects_writes_and_unknown():
    assert mcp_server.is_gateway_preauthorizable('home_control') is False
    assert mcp_server.is_gateway_preauthorizable('skill_manage') is False
    assert mcp_server.is_gateway_preauthorizable('skill_edit') is False
    assert mcp_server.is_gateway_preauthorizable('cron_add') is False
    assert mcp_server.is_gateway_preauthorizable('mcp__metano__skill_manage') is False
    assert mcp_server.is_gateway_preauthorizable('') is False
    assert mcp_server.is_gateway_preauthorizable('nonsense') is False


# ── owner condition (H-07 subject confinement) ─────────────────────────────

def test_owner_cond_local_operator_unrestricted():
    tok = _mcp_auth_ctx.set((None, []))
    try:
        cond, params = mcp_server._owner_cond()
    finally:
        _mcp_auth_ctx.reset(tok)
    assert cond == ''
    assert params == []


def test_owner_cond_admin_read_unrestricted():
    tok = _mcp_auth_ctx.set(('alice', ['mcp:admin:read']))
    try:
        cond, params = mcp_server._owner_cond()
    finally:
        _mcp_auth_ctx.reset(tok)
    assert cond == ''
    assert params == []


def test_owner_cond_user_level_scoped():
    tok = _mcp_auth_ctx.set(('alice', []))
    try:
        cond, params = mcp_server._owner_cond()
    finally:
        _mcp_auth_ctx.reset(tok)
    assert cond == '(sessions.user_key = ? OR sessions.user_key = ?)'
    assert params == ['alice', 'web:alice']


def test_memory_denied_for_user_level_token():
    tok = _mcp_auth_ctx.set(('alice', []))
    try:
        assert mcp_server._memory_denied() is True
    finally:
        _mcp_auth_ctx.reset(tok)


def test_memory_allowed_for_admin_read():
    tok = _mcp_auth_ctx.set(('alice', ['mcp:admin:read']))
    try:
        assert mcp_server._memory_denied() is False
    finally:
        _mcp_auth_ctx.reset(tok)


def test_memory_allowed_for_local_operator():
    tok = _mcp_auth_ctx.set((None, []))
    try:
        assert mcp_server._memory_denied() is False
    finally:
        _mcp_auth_ctx.reset(tok)


# ── _clip bounds helper ────────────────────────────────────────────────────

def test_clip_bounds():
    assert mcp_server._clip('10', 1, 100, 20) == 10
    assert mcp_server._clip('0', 1, 100, 20) == 1
    assert mcp_server._clip('1000', 1, 100, 20) == 100
    assert mcp_server._clip('abc', 1, 100, 20) == 20
    assert mcp_server._clip(None, 1, 100, 20) == 20


# ── live DB: session_list scopes rows to the subject ───────────────────────

def _seed_sessions():
    conn = metano_db.get_db()
    now = 1700000000.0
    conn.execute(
        'INSERT INTO sessions (id, project, title, model, user_key, started_at, last_active) '
        'VALUES (?,?,?,?,?,?,?)',
        ('sa', 'web', 'alice session', 'm1', 'web:alice', now, now),
    )
    conn.execute(
        'INSERT INTO sessions (id, project, title, model, user_key, started_at, last_active) '
        'VALUES (?,?,?,?,?,?,?)',
        ('sb', 'web', 'bob session', 'm1', 'web:bob', now, now),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('sa', 'user', 'alice hello world', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('sb', 'user', 'bob hello world', ?)",
        (now,),
    )
    conn.commit()
    conn.close()


def _session_ids_from_json(text):
    return [row['id'] for row in json.loads(text)]


def test_session_list_user_scoped():
    _seed_sessions()
    tok = _mcp_auth_ctx.set(('alice', []))
    try:
        out = mcp_server.session_list(limit=10)
    finally:
        _mcp_auth_ctx.reset(tok)
    assert _session_ids_from_json(out) == ['sa']


def test_session_list_admin_read_sees_all():
    _seed_sessions()
    tok = _mcp_auth_ctx.set(('alice', ['mcp:admin:read']))
    try:
        out = mcp_server.session_list(limit=10)
    finally:
        _mcp_auth_ctx.reset(tok)
    assert set(_session_ids_from_json(out)) == {'sa', 'sb'}


def test_session_list_local_operator_sees_all():
    _seed_sessions()
    tok = _mcp_auth_ctx.set((None, []))
    try:
        out = mcp_server.session_list(limit=10)
    finally:
        _mcp_auth_ctx.reset(tok)
    assert set(_session_ids_from_json(out)) == {'sa', 'sb'}


def test_session_search_scoped():
    _seed_sessions()
    tok = _mcp_auth_ctx.set(('bob', []))
    try:
        out = mcp_server.session_search('hello', limit=10)
    finally:
        _mcp_auth_ctx.reset(tok)
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]['session_id'] == 'sb'


# ── cron tools auth (F2: cron_* previously had no auth gate) ──────────────

def test_cron_tools_refuse_user_token():
    """User-level remote tokens are refused by every cron_* tool (403-analog),
    since cron jobs are instance-wide and cron_trigger spawns claude -p."""
    mcp_server._save_cron_jobs([{'id': 'j1', 'name': 'test', 'enabled': True}])

    tok = _mcp_auth_ctx.set(('alice', []))  # user-level token
    try:
        assert 'error' in json.loads(mcp_server.cron_list())
        assert 'error' in json.loads(mcp_server.cron_add('n1', 'p', '0 0 * * *'))
        assert 'error' in json.loads(mcp_server.cron_remove('j1'))
        assert 'error' in json.loads(mcp_server.cron_pause('j1'))
        assert 'error' in json.loads(mcp_server.cron_resume('j1'))
        assert 'error' in json.loads(mcp_server.cron_trigger('j1'))
    finally:
        _mcp_auth_ctx.reset(tok)


def test_cron_tools_admin_allowed():
    """Local stdio (no subject) and admin-read scoped tokens pass the gate."""
    for subject, scopes in ((None, []), ('admin', ['mcp:admin:read'])):
        tok = _mcp_auth_ctx.set((subject, scopes))
        try:
            assert 'error' not in json.loads(mcp_server.cron_list())
        finally:
            _mcp_auth_ctx.reset(tok)
