"""M16: key-path coverage for metano/web_server.py.

Covers the audit's high-value control paths without needing a live deployment:
CSRF Origin exact-match (M-02), IDOR session scoping (H-01), role gate,
login/rate-limit flow, and the one-time WebSocket ticket (F-12).  Uses
FastAPI TestClient against the real ``web_server.app`` with an isolated config.
"""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from metano import web_server
from metano.auth import (
    AUTH_WHITELIST,
    consume_ws_ticket,
    create_ws_ticket,
    require_role,
)
from metano.auth import authenticate_user

pytestmark = pytest.mark.usefixtures("isolated_env")


# ── CSRF Origin exact-match (M-02) ─────────────────────────────────────────

def test_origin_allowed_known_origins():
    assert web_server._origin_allowed('http://localhost:9120', 'localhost:9120') is True
    assert web_server._origin_allowed('http://localhost:5173', 'localhost:9120') is True


def test_origin_allowed_same_host():
    # The request's own Host header is always accepted (same-site).
    assert web_server._origin_allowed('http://localhost:9120', 'localhost:9120') is True


def test_origin_allowed_rejects_subdomain_suffix():
    # M-02: a suffix trick like localhost:9120.evil.com must NOT bypass.
    assert web_server._origin_allowed('http://localhost:9120.evil.com', 'localhost:9120') is False


def test_origin_allowed_rejects_unknown():
    assert web_server._origin_allowed('http://evil.com', 'localhost:9120') is False
    assert web_server._origin_allowed('', 'localhost:9120') is False
    assert web_server._origin_allowed('http://localhost:9999', 'localhost:9120') is False


# ── IDOR session scoping (H-01) ────────────────────────────────────────────

def _request_with_user(user):
    req = Request({'type': 'http', 'method': 'GET', 'path': '/api/sessions'})
    req.state.user = user
    return req


def test_session_scope_admin_sees_all():
    where, params = web_server._session_scope_sql(_request_with_user({'username': 'root', 'role': 'admin'}))
    assert where == '1=1'
    assert params == []


def test_session_scope_regular_user_scoped_to_self():
    where, params = web_server._session_scope_sql(_request_with_user({'username': 'alice', 'role': 'user'}))
    assert where == 'sessions.user_key = ?'
    assert params == ['web:alice']


def test_session_scope_no_user_raises_401():
    req = Request({'type': 'http', 'method': 'GET', 'path': '/api/sessions'})
    with pytest.raises(HTTPException) as exc:
        web_server._session_scope_sql(req)
    assert exc.value.status_code == 401


def test_session_scope_alias_qualifies_column():
    where, params = web_server._session_scope_sql_alias(
        _request_with_user({'username': 'bob', 'role': 'user'}), alias='s')
    assert where == 's.user_key = ?'
    assert params == ['web:bob']


def test_user_scope_prefix():
    assert web_server._user_scope(_request_with_user({'username': 'alice', 'role': 'user'})) == 'web:alice'


def test_user_scope_unauthenticated():
    req = Request({'type': 'http', 'method': 'GET', 'path': '/api/sessions'})
    with pytest.raises(HTTPException) as exc:
        web_server._user_scope(req)
    assert exc.value.status_code == 401


# ── role gate ──────────────────────────────────────────────────────────────

def test_require_role_guest_rejected():
    checker = require_role('admin')
    req = Request({'type': 'http', 'method': 'GET', 'path': '/api/cron/jobs'})
    req.state.user = {'username': 'guest', 'role': 'guest'}
    with pytest.raises(HTTPException) as exc:
        checker(req)
    assert exc.value.status_code == 403


def test_require_role_unauthenticated():
    checker = require_role('admin')
    req = Request({'type': 'http', 'method': 'GET', 'path': '/api/cron/jobs'})
    with pytest.raises(HTTPException) as exc:
        checker(req)
    assert exc.value.status_code == 401


def test_require_role_admin_allowed():
    checker = require_role('admin')
    req = Request({'type': 'http', 'method': 'GET', 'path': '/api/cron/jobs'})
    req.state.user = {'username': 'root', 'role': 'admin'}
    assert checker(req)['role'] == 'admin'


def test_auth_whitelist_covers_health_and_login():
    assert '/health' in AUTH_WHITELIST
    assert '/api/auth/login' in AUTH_WHITELIST


# ── cron job normalization helper ──────────────────────────────────────────

def test_normalize_cron_job_adds_defaults():
    job = {'name': 'harvest', 'schedule': {'kind': 'interval', 'expr': '30'}, 'enabled': True}
    out = web_server._normalize_cron_job(job, 0)
    assert out['id']  # stable id added
    assert out['prompt'] == ''
    assert out['last_run_at'] is None


def test_normalize_cron_job_string_schedule():
    job = {'name': 'x', 'schedule': '0 * * * *', 'enabled': True}
    out = web_server._normalize_cron_job(job, 1)
    assert out['schedule'] == {'kind': 'cron', 'expr': '0 * * * *'}


# ── WebSocket ticket (F-12): issue + single-use + replay ───────────────────

def test_ws_ticket_consume_and_replay(auth_config):
    ticket = create_ws_ticket('alice', 'user')
    user = consume_ws_ticket(ticket)
    assert user['username'] == 'alice'
    # Replay of the same ticket is refused (one-time).
    assert consume_ws_ticket(ticket) is None


def test_ws_ticket_invalid(auth_config):
    assert consume_ws_ticket('not-a-token') is None
    assert consume_ws_ticket('') is None


# ── End-to-end login / CSRF / auth-gated endpoints (TestClient) ────────────

@pytest.fixture()
def client(auth_config):
    from fastapi.testclient import TestClient
    # auth_config writes the isolated gateway_config.yaml (admin/alice users)
    # to auth.CONFIG_PATH / web_server.CONFIG_PATH (same tmp path via autouse).
    # https base_url: auth sets the cookies with the Secure flag (H-06), so the
    # test client must speak https to send them back on follow-up requests.
    return TestClient(web_server.app, base_url='https://localhost:9120')


def test_health_public(client):
    r = client.get('/health')
    assert r.status_code == 200
    assert r.json()['status'] == 'ok'


def test_api_requires_auth(client):
    assert client.get('/api/status').status_code == 401


def test_auth_me_requires_auth(client):
    assert client.get('/api/auth/me').status_code == 401


def test_login_success_sets_cookies(client, auth_config):
    r = client.post('/api/auth/login', json={'username': 'admin', 'password': auth_config['admin']['password']})
    assert r.status_code == 200
    assert r.json()['role'] == 'admin'
    assert 'access_token' in r.cookies


def test_login_wrong_password_401(client):
    r = client.post('/api/auth/login', json={'username': 'admin', 'password': 'nope'})
    assert r.status_code == 401


def test_csrf_blocks_spoofed_origin(client):
    # A mutating /api request with an Origin that only suffix-matches must be 403.
    r = client.post(
        '/api/auth/login',
        json={'username': 'admin', 'password': 'x'},
        headers={'Origin': 'http://localhost:9120.evil.com'},
    )
    assert r.status_code == 403
    assert 'CSRF' in r.json()['detail']


def test_csrf_allows_known_origin(client, auth_config):
    r = client.post(
        '/api/auth/login',
        json={'username': 'admin', 'password': auth_config['admin']['password']},
        headers={'Origin': 'http://localhost:9120'},
    )
    assert r.status_code == 200


def test_ws_ticket_endpoint_requires_auth(client):
    assert client.post('/api/auth/ws-ticket').status_code == 401


def test_ws_ticket_endpoint_issues_ticket(client, auth_config):
    login = client.post('/api/auth/login', json={'username': 'alice', 'password': auth_config['user']['password']})
    assert login.status_code == 200
    r = client.post('/api/auth/ws-ticket')
    assert r.status_code == 200
    assert 'ticket' in r.json()
    assert r.json()['expires_in'] == 30
    # Ticket is consumable and single-use.
    assert consume_ws_ticket(r.json()['ticket'])['username'] == 'alice'
    assert consume_ws_ticket(r.json()['ticket']) is None


def test_authenticate_user_isolated(auth_config):
    """Login resolution reads the isolated config, not production."""
    assert authenticate_user('admin', auth_config['admin']['password'])
    assert authenticate_user('admin', 'wrong') is None
    assert authenticate_user('ghost', 'x') is None
