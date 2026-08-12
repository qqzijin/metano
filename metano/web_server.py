"""FastAPI web dashboard for metano."""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, Request, Response, Depends, UploadFile, File
from fastapi.responses import StreamingResponse
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from .auth import authenticate_user, check_login_rate, record_login_attempt, set_auth_cookies, clear_auth_cookies, get_current_user_from_request, try_refresh_from_request, decode_token, change_password, AUTH_WHITELIST, ACCESS_TOKEN_EXPIRE_MINUTES, _audit, require_role, bump_token_version, create_ws_ticket, consume_ws_ticket, get_user_by_username, get_token_version, validate_access_token
from .db import get_db, init_db, DB_PATH
from .indexer import index_all
from .paths import CONFIG_PATH, AUDIT_LOG, UPLOADS_DIR, home_dir
from . import collab as collab
from . import cron_daemon
WEB_DIR = Path(__file__).parent.parent / 'web' / 'dist'
SENSITIVE_KEYS = {'api_key', 'bot_token', 'app_secret', 'encryption_key', 'verification_token', 'token', 'secret', 'password', 'ha_token'}
# gateway_config.yaml 中所有 SENSITIVE_KEYS 字段在 GET /api/config 返回时自动脱敏（***）
# 文件受 METANO_HOME 目录文件系统权限保护
app = FastAPI(title='metano')
# SECURITY (M-11): drop the dev-port (localhost:5173) from CORS — it is a
# development origin that must not be able to make credentialed cross-origin
# requests against a live control plane.
app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:9120'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])

class AuthMiddleware(BaseHTTPMiddleware):

    ALLOWED_ORIGINS = {'http://localhost:5173', 'http://localhost:9120'}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # CSRF protection: for mutating requests, verify Origin/Referer matches an
        # allowed origin by EXACT host (M-02). A subdomain-suffix like
        # ``http://localhost:9120.evil.com`` no longer bypasses: the parsed netloc
        # must equal an allow-listed origin or the request's own Host header.
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH') and path.startswith('/api/'):
            origin = request.headers.get('origin', '')
            referer = request.headers.get('referer', '')
            source = origin or referer
            if source:
                host = request.headers.get('host', '')
                if not _origin_allowed(source, host):
                    return JSONResponse(status_code=403, content={'detail': 'CSRF: Origin not allowed'})
            # If no origin/referer, rely on SameSite=Lax cookie + HttpOnly (browser sends on same-site)
        if path in AUTH_WHITELIST or path.startswith('/assets') or path == '/favicon.ico':
            return await call_next(request)
        if path.startswith('/api/'):
            user = get_current_user_from_request(request)
            if user:
                request.state.user = user
                return await call_next(request)
            new_access = try_refresh_from_request(request)
            if new_access:
                response = await call_next(request)
                # SECURITY (H-06): Secure flag — see auth.set_auth_cookies.
                response.set_cookie('access_token', new_access, max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60, httponly=True, samesite='lax', path='/', secure=True)
                return response
            return JSONResponse(status_code=401, content={'detail': '未登录'})
        return await call_next(request)
from metano.log import logger
app.add_middleware(AuthMiddleware)


def _origin_allowed(source: str, host: str) -> bool:
    """Exact-match CSRF Origin/Referer check (M-02).

    Returns True only when ``source``'s parsed netloc exactly equals one of the
    allow-listed origins (``AuthMiddleware.ALLOWED_ORIGINS``) or the request's
    own Host header (same-host access). Substring / suffix tricks such as
    ``http://localhost:9120.evil.com`` are rejected because the comparison is on
    the full netloc, never a substring. The same check backs the WebSocket
    handshake, keeping HTTP and WS Origin enforcement consistent.
    """
    if not source:
        return False
    try:
        netloc = urlparse(source).netloc
    except ValueError:
        return False
    if not netloc:
        return False
    allowed = {urlparse(o).netloc for o in AuthMiddleware.ALLOWED_ORIGINS}
    return netloc in allowed or netloc == host

@app.post('/api/auth/login')
async def auth_login(request: Request, response: Response):
    body = await request.json()
    username = body.get('username', '')
    password = body.get('password', '')
    ip = request.client.host if request.client else 'unknown'
    if not check_login_rate(ip):
        _audit('login_rate_limited', username, {'ip': ip})
        raise HTTPException(status_code=429, detail='登录尝试过多，请5分钟后再试')
    user = authenticate_user(username, password)
    if not user:
        record_login_attempt(ip)
        _audit('login_failed', username, {'ip': ip})
        raise HTTPException(status_code=401, detail='用户名或密码错误')
    _audit('login_success', username, {'ip': ip})
    return set_auth_cookies(response, user['username'], user['role'])

@app.post('/api/auth/refresh')
async def auth_refresh(request: Request, response: Response):
    # SECURITY (H-06): validate the refresh token against the user's current
    # token_version, re-query the user's live role, and rotate BOTH tokens so a
    # stolen refresh token can't be replayed after use.
    token = request.cookies.get('refresh_token')
    payload = decode_token(token) if token else None
    if not payload or payload.get('type') != 'refresh':
        raise HTTPException(status_code=401, detail='请重新登录')
    user = get_user_by_username(payload['sub'])
    if not user:
        raise HTTPException(status_code=401, detail='请重新登录')
    if payload.get('tv', 0) != get_token_version(payload['sub']):
        raise HTTPException(status_code=401, detail='登录已失效，请重新登录')
    # SECURITY (M-01): bump token_version so the just-used refresh token is
    # revoked immediately (anti-replay). set_auth_cookies reads the fresh
    # version, so the newly issued pair stays valid under the new tv.
    bump_token_version(user['username'])
    return set_auth_cookies(response, user['username'], user['role'])

@app.post('/api/auth/logout')
async def auth_logout(request: Request, response: Response):
    user = get_current_user_from_request(request)
    if user:
        # SECURITY (H-06): bump token_version so all outstanding access/refresh
        # tokens for this user are revoked (not just the browser cookie).
        bump_token_version(user['username'])
    clear_auth_cookies(response)
    return {'status': 'logged_out'}

@app.get('/api/auth/me')
async def auth_me(request: Request):
    user = get_current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail='未登录')
    return user

@app.post('/api/auth/ws-ticket')
async def auth_ws_ticket(request: Request):
    """Issue a short-lived (30s) one-time ticket for opening the /ws socket.

    The access token is HttpOnly so the front-end cannot read it to send as the
    WS first message; it fetches this ticket with its normal cookie auth and
    sends it as the first WS message instead (F-12).
    """
    user = get_current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail='未登录')
    ticket = create_ws_ticket(user['username'], user['role'])
    return {'ticket': ticket, 'expires_in': 30}

@app.post('/api/auth/change-password')
async def auth_change_password(request: Request, response: Response):
    user = get_current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail='未登录')
    body = await request.json()
    old_password = body.get('old_password', '')
    new_password = body.get('new_password', '')
    if not new_password or len(new_password) < 6:
        raise HTTPException(status_code=400, detail='新密码长度至少6位')
    if not change_password(user['username'], old_password, new_password):
        raise HTTPException(status_code=400, detail='原密码不正确')
    # change_password bumped token_version (revoking old tokens). Re-issue the
    # cookies so this session stays authenticated under the new version.
    return set_auth_cookies(response, user['username'], user['role'])

def _error_response(message: str, status_code: int = 500, extra: dict | None = None) -> JSONResponse:
    """Standard error envelope for API responses.

    The canonical schema is ``{success: false, error: {message: ...}}`` with a
    correct HTTP status code. ``detail`` is included for backward compatibility
    with the front-end's fetchAPI client (M-01 func).
    """
    content = {'success': False, 'error': {'message': message}, 'detail': message}
    if extra:
        content.update(extra)
    return JSONResponse(content=content, status_code=status_code)


def _result_or_error(result: dict, status_code: int = 400, error_status: int | None = None):
    """Convert a ``{'error': ...}`` business failure into a proper HTTP error.

    Several handlers (home control, browser, tavily, ingest) previously returned
    ``{'error': ...}`` with HTTP 200, which the front-end rendered as success.
    """
    if isinstance(result, dict) and result.get('error'):
        return _error_response(str(result['error']), status_code=error_status or status_code)
    return result


def _user_scope(request: Request) -> str:
    """Return the ``sessions.user_key`` scope for the authenticated web user.

    Web sessions are persisted with ``platform='web'`` so the key is
    ``web:<username>`` (mirrors the sessions.user_key = f'{platform}:{user_id}'
    convention used by _inject_session_context and db.persist_exchange).
    """
    user = getattr(request.state, 'user', None)
    if not user:
        raise HTTPException(status_code=401, detail='Not authenticated')
    return f'web:{user["username"]}'


def _session_scope_sql(request: Request) -> tuple[str, list]:
    """Return (where_fragment, params) scoping a query to the caller's own data.

    The fragment is safe to embed as ``WHERE {frag} AND <rest>``. Admins see
    everything (including legacy ``user_key IS NULL`` rows); regular users only
    see their own web sessions (H-01 IDOR fix).
    """
    user = getattr(request.state, 'user', None)
    if not user:
        raise HTTPException(status_code=401, detail='Not authenticated')
    if user.get('role') == 'admin':
        return '1=1', []
    return 'sessions.user_key = ?', [f'web:{user["username"]}']


def _session_scope_sql_alias(request: Request, alias: str = 's') -> tuple[str, list]:
    """Like _session_scope_sql but qualified with a JOIN alias (e.g. ``s``)."""
    where, params = _session_scope_sql(request)
    return where.replace('sessions.', f'{alias}.', 1), params


def _write_config_safe(config: dict):
    """Write gateway_config.yaml with owner-only permissions (M-08).

    Mirrors auth._save_config's chmod(0600) so config secrets (API keys, token,
    password hashes) are never world-readable regardless of the process umask.
    """
    import yaml
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.dump(config, allow_unicode=True, default_flow_style=False))
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def _normalize_cron_job(j: dict, idx: int) -> dict:
    import uuid
    if 'id' not in j:
        j['id'] = uuid.uuid4().hex[:12]
    if 'schedule' in j and isinstance(j['schedule'], str):
        j['schedule'] = {'kind': 'cron', 'expr': j['schedule']}
    if 'prompt' not in j:
        j['prompt'] = j.get('action', '')
    j.setdefault('last_run_at', None)
    j.setdefault('next_run_at', None)
    j.setdefault('last_error', None)
    return j

def _load_cron_jobs() -> list[dict]:
    """Load jobs from the canonical store (``cron/jobs.json``).

    F-01: jobs.json is the single source of truth. The web panel reads through
    ``cron_daemon.load_jobs()`` — the same loader the daemon tick uses — instead
    of the evo.db ``cron_jobs`` table, so web CRUD affects the jobs the daemon
    actually executes. A light normalization adds display defaults and stable
    ids for jobs that lack them.
    """
    try:
        jobs = cron_daemon.load_jobs()
    except Exception:
        logger.exception('cron: failed to load jobs.json')
        return []
    return [_normalize_cron_job(dict(j), i) for i, j in enumerate(jobs)]


def _save_cron_jobs(jobs: list[dict]):
    """Persist jobs to the canonical store (``cron/jobs.json``) via cron_daemon."""
    cron_daemon.save_jobs(jobs)

@app.get('/health')
def health_check():
    """Minimal public health endpoint (L-02).

    Only returns a boolean status + service name. Absolute paths, database
    names, table lists and raw exception text are logged, never exposed to
    unauthenticated callers.
    """
    ok = True
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute('SELECT 1')
        conn.close()
    except Exception:
        logger.exception('health check failed')
        ok = False
    return {'status': 'ok' if ok else 'degraded', 'service': 'metano'}

@app.get('/api/status')
def status():
    conn = get_db()
    sessions = conn.execute('SELECT COUNT(*) as c FROM sessions').fetchone()['c']
    messages = conn.execute('SELECT COUNT(*) as c FROM messages').fetchone()['c']
    from . import __version__
    result = {'status': 'ok', 'version': __version__, 'sessions': sessions, 'messages': messages}
    try:
        from .skills.loader import SkillLoader
        result['skills_count'] = len(SkillLoader().discover_all())
    except Exception:
        logger.exception()
        result['skills_count'] = 0
    try:
        from .evolution import evolution_status
        result['evolution'] = evolution_status()
    except Exception:
        logger.exception()
        result['evolution'] = {'paused': True}
    try:
        import subprocess
        services = {}
        try:
            r = subprocess.run(['pgrep', '-f', 'metano.gateway.launcher'], capture_output=True, text=True, timeout=3)
            services['gateway'] = 'active' if r.stdout.strip() else 'inactive'
        except Exception:
            logger.exception()
            services['gateway'] = 'inactive'
        for svc, mod in [('evolution', 'metano.evolution'), ('rag', 'metano.knowledge'), ('tts', 'metano.voice.tts'), ('browser', 'metano.browser'), ('home', 'metano.home_assistant')]:
            try:
                __import__(mod)
                services[svc] = 'active'
            except Exception:
                logger.exception()
                services[svc] = 'inactive'
        result['services'] = services
        result['active_services'] = sum((1 for v in services.values() if v == 'active'))
    except Exception:
        logger.exception()
        result['services'] = {}
        result['active_services'] = 0
    return result

@app.get('/api/sessions')
def list_sessions(request: Request, limit: int=20, offset: int=0, search: str=''):
    # SECURITY (H-01): scope to the caller's own web sessions; admins see all.
    scope, params = _session_scope_sql(request)
    limit = min(max(int(limit), 1), 200)
    offset = max(int(offset), 0)
    conn = get_db()
    where = f'WHERE {scope} AND '
    if search:
        rows = conn.execute(f'SELECT id, title, model, message_count, tool_call_count, input_tokens, output_tokens, estimated_cost_usd, started_at, last_active FROM sessions {where}title LIKE ? ORDER BY last_active DESC LIMIT ? OFFSET ?', params + [f'%{search}%', limit, offset]).fetchall()
        total = conn.execute(f'SELECT COUNT(*) as c FROM sessions {where}title LIKE ?', params + [f'%{search}%']).fetchone()['c']
    else:
        rows = conn.execute(f'SELECT id, title, model, message_count, tool_call_count, input_tokens, output_tokens, estimated_cost_usd, started_at, last_active FROM sessions {where}1=1 ORDER BY last_active DESC LIMIT ? OFFSET ?', params + [limit, offset]).fetchall()
        total = conn.execute(f'SELECT COUNT(*) as c FROM sessions {where}1=1', params).fetchone()['c']
    return {'items': [dict(r) for r in rows], 'total': total}

def _search_snippet(content: str, q: str, radius: int = 60) -> str:
    """Context window around the first match of ``q`` in ``content``, with ``<mark>`` highlight."""
    if not content:
        return ''
    idx = content.lower().find(q.lower())
    if idx < 0:
        return content[:300]
    start = max(0, idx - radius)
    end = min(len(content), idx + len(q) + radius)
    before = '...' if start > 0 else ''
    after = '...' if end < len(content) else ''
    match = content[idx:idx + len(q)]
    return f"{before}{content[start:idx]}<mark>{match}</mark>{content[idx + len(q):end]}{after}"


@app.get('/api/sessions/search')
def search_sessions(request: Request, q: str=Query(...), limit: int=20, offset: int=0):
    # SECURITY (H-01): only search the caller's own sessions' messages.
    scope, params = _session_scope_sql_alias(request, 's')
    limit = min(max(int(limit), 1), 100)
    offset = max(int(offset), 0)
    conn = get_db()
    try:
        pattern = f'%{q}%'
        total = conn.execute(f'SELECT COUNT(*) as c FROM messages m JOIN sessions s ON s.id = m.session_id WHERE {scope} AND m.content LIKE ?', params + [pattern]).fetchone()['c']
        rows = conn.execute(f'SELECT m.session_id, m.role, m.content AS raw, m.timestamp, s.title FROM messages m JOIN sessions s ON s.id = m.session_id WHERE {scope} AND m.content LIKE ? ORDER BY m.timestamp DESC LIMIT ? OFFSET ?', params + [pattern, limit, offset]).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d['snippet'] = _search_snippet(d.pop('raw'), q)
            results.append(d)
        return {'query': q, 'results': results, 'total': total}
    except Exception:
        logger.exception()
        return {'query': q, 'results': [], 'total': 0}

@app.get('/api/search')
def global_search(request: Request, q: str=Query(...), limit: int=20, offset: int=0):
    """Alias for /api/sessions/search."""
    return search_sessions(request, q=q, limit=limit, offset=offset)

@app.get('/api/sessions/{session_id}')
def get_session(request: Request, session_id: str):
    conn = get_db()
    scope, params = _session_scope_sql(request)
    row = conn.execute(f'SELECT id, title, model, message_count, tool_call_count, input_tokens, output_tokens, estimated_cost_usd, started_at, last_active FROM sessions WHERE {scope} AND id = ?', params + [session_id]).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail='Session not found')
    return dict(row)

@app.get('/api/sessions/{session_id}/messages')
def get_session_messages(request: Request, session_id: str, limit: int=200, offset: int=0):
    conn = get_db()
    scope, params = _session_scope_sql(request)
    limit = min(max(int(limit), 1), 500)
    offset = max(int(offset), 0)
    rows = conn.execute(f'SELECT m.id, m.role, m.content, m.tool_name, m.tool_calls, m.timestamp, m.input_tokens, m.output_tokens, m.duration_ms FROM messages m JOIN sessions s ON s.id = m.session_id WHERE {scope} AND m.session_id = ? ORDER BY m.timestamp ASC LIMIT ? OFFSET ?', params + [session_id, limit, offset]).fetchall()
    total = conn.execute(f'SELECT COUNT(*) as c FROM messages m JOIN sessions s ON s.id = m.session_id WHERE {scope} AND m.session_id = ?', params + [session_id]).fetchone()['c']
    return {'items': [dict(r) for r in rows], 'total': total}

@app.get('/api/analytics/usage')
@app.get('/api/analytics')
def analytics_usage(request: Request, days: int=30):
    """统计总览。口径分离：「单次对话 token」与「每日总用量」互不混淆。

    - ``daily``：**每日总用量** —— 按消息实际发生日聚合，跨日会话的 in/out token
      按消息时间戳拆到各自发生日；费用按该会话各日 in/out token 占比分摊
      ``estimated_cost_usd``（缓存 token 无消息级明细，按占比近似分摊）。
    - ``total`` / ``by_model`` / ``by_project``：in/out 与 ``daily`` 同口径（消息级，
      只统计窗口内实际发生的请求），保证加总永远一致；缓存 token 与费用没有消息级
      明细，按「last_active 落在窗口内」的会话汇总。
    - ``sessions``：**单次对话** token 排行 —— 每条会话的输入/输出/缓存 token 与费用。

    SECURITY (H-01): every query is scoped to the caller's own web sessions
    (admins see everything).
    """
    conn = get_db()
    days = min(max(int(days), 1), 365)
    cutoff = time.time() - days * 86400
    scope_s, params_s = _session_scope_sql_alias(request, 's')      # messages JOIN sessions
    scope_sess, params_sess = _session_scope_sql(request)            # sessions alone
    daily = _analytics_daily(conn, cutoff, scope_s, params_s)
    # in/out：消息级（与 daily 同口径，跨窗口长会话只统计窗口内发生的请求）
    msg = conn.execute(
        'SELECT COUNT(DISTINCT m.session_id) as session_count, COUNT(*) as message_count, '
        'SUM(m.tool_name IS NOT NULL) as tool_call_count, '
        'COALESCE(SUM(m.input_tokens),0) as input_tokens, COALESCE(SUM(m.output_tokens),0) as output_tokens '
        'FROM messages m LEFT JOIN sessions s ON s.id = m.session_id '
        f'WHERE {scope_s} AND m.timestamp >= ?',
        params_s + [cutoff]
    ).fetchone()
    by_model = conn.execute(
        'SELECT COALESCE(s.model, \'<unknown>\') as model, COUNT(DISTINCT m.session_id) as session_count, '
        'COALESCE(SUM(m.input_tokens),0) as input_tokens, COALESCE(SUM(m.output_tokens),0) as output_tokens '
        'FROM messages m LEFT JOIN sessions s ON s.id = m.session_id '
        f'WHERE {scope_s} AND m.timestamp >= ? '
        'GROUP BY s.model ORDER BY SUM(m.input_tokens) DESC',
        params_s + [cutoff]
    ).fetchall()
    by_project = conn.execute(
        'SELECT COALESCE(s.project, \'<unknown>\') as project, COUNT(DISTINCT m.session_id) as session_count, '
        'COALESCE(SUM(m.input_tokens),0) as input_tokens, COALESCE(SUM(m.output_tokens),0) as output_tokens '
        'FROM messages m LEFT JOIN sessions s ON s.id = m.session_id '
        f'WHERE {scope_s} AND m.timestamp >= ? '
        'GROUP BY s.project ORDER BY SUM(m.input_tokens) DESC',
        params_s + [cutoff]
    ).fetchall()
    # 缓存 token / 费用：会话级（消息表无缓存明细）
    sess = conn.execute(
        'SELECT COALESCE(SUM(cache_read_tokens),0) as cache_read_tokens, '
        'COALESCE(SUM(estimated_cost_usd),0) as estimated_cost_usd '
        f'FROM sessions WHERE {scope_sess} AND last_active >= ?',
        params_sess + [cutoff]
    ).fetchone()
    total = dict(msg)
    total['cache_read_tokens'] = sess['cache_read_tokens']
    total['estimated_cost_usd'] = sess['estimated_cost_usd']
    # 把会话级缓存/费用合并进 by_model / by_project（与 total 同一会话集合）
    def _merge_sess_agg(rows, sess_by_key, key):
        out = []
        for r in rows:
            row = dict(r)
            s = sess_by_key.get(row.get(key))
            row['cache_read_tokens'] = s['cache_read_tokens'] if s else 0
            row['estimated_cost_usd'] = s['estimated_cost_usd'] if s else 0.0
            out.append(row)
        return out
    sess_by_model = conn.execute(
        'SELECT model, COALESCE(SUM(cache_read_tokens),0) as cache_read_tokens, '
        'COALESCE(SUM(estimated_cost_usd),0) as estimated_cost_usd '
        f'FROM sessions WHERE {scope_sess} AND last_active >= ? AND model IS NOT NULL GROUP BY model',
        params_sess + [cutoff]
    ).fetchall()
    sess_by_project = conn.execute(
        'SELECT project, COALESCE(SUM(cache_read_tokens),0) as cache_read_tokens, '
        'COALESCE(SUM(estimated_cost_usd),0) as estimated_cost_usd '
        f'FROM sessions WHERE {scope_sess} AND last_active >= ? AND project IS NOT NULL GROUP BY project',
        params_sess + [cutoff]
    ).fetchall()
    by_model = _merge_sess_agg(by_model, {r['model']: r for r in sess_by_model}, 'model')
    by_project = _merge_sess_agg(by_project, {r['project']: r for r in sess_by_project}, 'project')
    # 单次对话排行（会话级全量，含缓存）
    sessions = conn.execute(
        'SELECT id, title, project, model, message_count, tool_call_count, input_tokens, output_tokens, cache_read_tokens, estimated_cost_usd, started_at, last_active '
        f'FROM sessions WHERE {scope_sess} AND last_active >= ? ORDER BY (input_tokens + output_tokens + cache_read_tokens) DESC LIMIT 20',
        params_sess + [cutoff]
    ).fetchall()
    return {'period_days': days, 'total': total, 'by_model': [dict(r) for r in by_model], 'by_project': [dict(r) for r in by_project], 'daily': daily, 'sessions': [dict(r) for r in sessions]}


def _analytics_daily(conn, cutoff: float, scope_s: str = '1=1', params_s: list | None = None) -> list[dict]:
    """每日总用量（消息级，按实际发生日聚合）。

    ``scope_s``/``params_s`` scope the messages to the caller's own sessions
    (H-01 IDOR fix).
    """
    params_s = params_s or []
    daily: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT date(m.timestamp, 'unixepoch', 'localtime') as day, "
        "COUNT(DISTINCT m.session_id) as session_count, "
        "COALESCE(SUM(m.input_tokens),0) as input_tokens, "
        "COALESCE(SUM(m.output_tokens),0) as output_tokens "
        "FROM messages m LEFT JOIN sessions s ON s.id = m.session_id "
        f"WHERE {scope_s} AND m.timestamp >= ? GROUP BY day ORDER BY day",
        params_s + [cutoff]
    ).fetchall():
        daily[r['day']] = {'day': r['day'], 'session_count': r['session_count'],
                           'input_tokens': r['input_tokens'], 'output_tokens': r['output_tokens'],
                           'estimated_cost_usd': 0.0}
    # 会话费用按日分摊：一次查询拿到每个会话在每天产生的 in/out token 量
    rows = conn.execute(
        "SELECT s.id as sid, COALESCE(s.estimated_cost_usd, 0) as cost, "
        "date(m.timestamp, 'unixepoch', 'localtime') as day, "
        "COALESCE(SUM(m.input_tokens),0) + COALESCE(SUM(m.output_tokens),0) as day_tokens "
        "FROM sessions s JOIN messages m ON m.session_id = s.id "
        f"WHERE {scope_s} AND m.timestamp >= ? AND COALESCE(s.estimated_cost_usd, 0) > 0 "
        "GROUP BY s.id, day",
        params_s + [cutoff]
    ).fetchall()
    sess_total: dict[str, int] = {}
    sess_cost: dict[str, float] = {}
    for r in rows:
        sess_total[r['sid']] = sess_total.get(r['sid'], 0) + r['day_tokens']
        sess_cost[r['sid']] = r['cost']
    for r in rows:
        total_tokens = sess_total.get(r['sid']) or 0
        if not total_tokens or not r['day_tokens']:
            continue
        day_entry = daily.get(r['day'])
        if day_entry is None:
            continue
        day_entry['estimated_cost_usd'] += (sess_cost.get(r['sid']) or 0) * r['day_tokens'] / total_tokens
    return list(daily.values())

@app.get('/api/cron/jobs')
def list_cron_jobs():
    return _load_cron_jobs()

@app.post('/api/cron/jobs')
def create_cron_job(body: dict, _admin=Depends(require_role("admin"))):
    import uuid
    jobs = _load_cron_jobs()
    job = {'id': uuid.uuid4().hex[:12], 'name': body.get('name', 'Untitled'), 'prompt': body.get('prompt', ''), 'schedule': body.get('schedule', {'kind': 'cron', 'expr': '0 9 * * *'}), 'enabled': True, 'last_run_at': None, 'next_run_at': None, 'last_error': None}
    jobs.append(job)
    _save_cron_jobs(jobs)
    return job

@app.post('/api/cron/jobs/{job_id}/pause')
def pause_cron_job(job_id: str, _admin=Depends(require_role("admin"))):
    jobs = _load_cron_jobs()
    for j in jobs:
        if j['id'] == job_id:
            j['enabled'] = False
    _save_cron_jobs(jobs)
    return {'paused': job_id}

@app.post('/api/cron/jobs/{job_id}/resume')
def resume_cron_job(job_id: str, _admin=Depends(require_role("admin"))):
    jobs = _load_cron_jobs()
    for j in jobs:
        if j['id'] == job_id:
            j['enabled'] = True
    _save_cron_jobs(jobs)
    return {'resumed': job_id}

@app.delete('/api/cron/jobs/{job_id}')
def delete_cron_job(job_id: str, _admin=Depends(require_role("admin"))):
    jobs = _load_cron_jobs()
    jobs = [j for j in jobs if j['id'] != job_id]
    _save_cron_jobs(jobs)
    return {'deleted': job_id}

@app.get('/api/skills')
async def api_skills(category: str=''):
    from .skills.loader import SkillLoader
    loader = SkillLoader()
    skills = loader.discover_all()
    if category:
        skills = [s for s in skills if s.category == category]
    return {'items': [{'name': s.name, 'description': s.description, 'trigger': s.trigger, 'category': s.category, 'source': s.source} for s in skills]}

@app.get('/api/skills/usage')
async def api_skill_usage(days: int = 30):
    """Skill usage frequency (how often each skill was activated) — lets the
    operator see which skills are hot and which are dead."""
    try:
        from .evo_models import get_skill_usage, get_skill_usage_all_time
        recent = get_skill_usage(days=days)
        all_time = get_skill_usage_all_time()
        return {'days': days, 'recent': recent, 'all_time': all_time}
    except Exception:
        logger.exception()
        return _error_response('Internal error', extra={'recent': [], 'all_time': []})

@app.get('/api/skills/{name}')
async def api_skill_detail(name: str):
    from .skills.loader import SkillLoader
    loader = SkillLoader()
    rec = loader.find_by_name(name)
    if not rec:
        return _error_response('Not found', status_code=404)
    return {'name': rec.name, 'description': rec.description, 'trigger': rec.trigger, 'category': rec.category, 'content': rec.body, 'source': rec.source}

@app.put('/api/skills/{name}')
async def api_skill_update(name: str, body: dict, _admin=Depends(require_role("admin"))):
    """Edit a skill's content (SkillManager.edit, which protects pinned/bundled)."""
    try:
        from .skills.manager import SkillManager
        result = SkillManager().edit(name, body.get('content', ''))
        if 'error' in result:
            return _error_response(result['error'], status_code=400)
        return result
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.delete('/api/skills/{name}')
async def api_skill_delete(name: str, _admin=Depends(require_role("admin"))):
    """Delete a skill (protected skills are refused by the manager)."""
    try:
        from .skills.manager import SkillManager
        result = SkillManager().delete(name)
        if 'error' in result:
            return _error_response(result['error'], status_code=400)
        return result
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/knowledge')
async def api_knowledge():
    try:
        from .knowledge import knowledge_list
        docs = knowledge_list()
        doc_list = docs.get('documents', []) if isinstance(docs, dict) else docs if isinstance(docs, list) else []
        return {'items': doc_list}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'items': []})

@app.get('/api/evolution')
async def api_evolution():
    try:
        from .evolution import evolution_status
        return evolution_status()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'paused': True})

# ── Proposal CRUD API (suggestions old endpoints removed — same table) ──

@app.get('/api/evolution/proposals')
async def api_proposals(status: str = None, proposal_type: str = None):
    try:
        from .evo_models import get_proposals
        return {'items': get_proposals(status=status, proposal_type=proposal_type)}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'items': []})

@app.post('/api/evolution/proposals/{proposal_id}/approve')
async def api_proposal_approve(proposal_id: int, _admin=Depends(require_role("admin"))):
    try:
        from .evo_models import update_proposal_status
        update_proposal_status(proposal_id, 'approved')
        return {'status': 'approved', 'proposal_id': proposal_id}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/evolution/proposals/{proposal_id}/reject')
async def api_proposal_reject(proposal_id: int, _admin=Depends(require_role("admin"))):
    try:
        from .evo_models import update_proposal_status
        update_proposal_status(proposal_id, 'rejected')
        return {'status': 'rejected', 'proposal_id': proposal_id}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/evolution/proposals/{proposal_id}/apply')
async def api_proposal_apply(proposal_id: int, _admin=Depends(require_role("admin"))):
    try:
        from .adapter import apply_proposal
        result = apply_proposal(proposal_id)
        return result
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/evolution/proposals/apply-approved')
async def api_proposals_apply_approved(_admin=Depends(require_role("admin"))):
    try:
        from .adapter import apply_approved_proposals
        return apply_approved_proposals()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

# ── Self-modification (self-bootstrap) ──────────────────────────────────

@app.get('/api/self-modify/events')
async def api_self_modify_events(status: str = None, limit: int = 50, _admin=Depends(require_role("admin"))):
    """List recorded self-modification mutations (the mutation log).

    ``status`` optionally filters to a single state (e.g. ``pending_approval``
    powers the approval page). Pass ``status=pending_approval`` to list only
    mutations awaiting human review.
    """
    try:
        from .evo_models import get_self_modify_events
        return {'items': get_self_modify_events(limit=limit, status=status)}
    except Exception:
        logger.exception()
        return _error_response('Internal error')


@app.post('/api/self-modify/approve/{event_id}')
async def api_self_modify_approve(event_id: int, _admin=Depends(require_role("admin"))):
    """Approve a pending self-modification candidate (applies it).

    C2: the approval gate previously had no HTTP entry point — this exposes
    ``self_modify.approve_mutation(event_id, approved=True)`` to the admin UI.
    """
    try:
        from .self_modify import approve_mutation
        return approve_mutation(event_id, approved=True)
    except Exception:
        logger.exception()
        return _error_response('Internal error')


@app.post('/api/self-modify/reject/{event_id}')
async def api_self_modify_reject(event_id: int, _admin=Depends(require_role("admin"))):
    """Reject a pending self-modification candidate (marks it rejected)."""
    try:
        from .self_modify import approve_mutation
        return approve_mutation(event_id, approved=False)
    except Exception:
        logger.exception()
        return _error_response('Internal error')


@app.post('/api/self-modify/run')
async def api_self_modify_run(dry_run: bool = False, _admin=Depends(require_role("admin"))):
    """Manually trigger a self-modification pass (dry_run=scan+generate only)."""
    try:
        from .self_modify import self_modify_daily
        return self_modify_daily(dry_run=dry_run, max_mutations=3)
    except Exception:
        logger.exception()
        return _error_response('Internal error')


@app.post('/api/self-modify/revert/{event_id}')
async def api_self_modify_revert(event_id: int, _admin=Depends(require_role("admin"))):
    """Revert an applied mutation via git revert of its commit hash."""
    try:
        from .self_modify import revert_mutation
        return revert_mutation(event_id)
    except Exception:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/models')
async def api_models():
    try:
        from .model_router import model_router
        return {'items': model_router.list_providers()}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'items': []})

@app.get('/api/services')
async def api_services():
    import subprocess
    services = {}
    try:
        r = subprocess.run(['pgrep', '-f', 'metano.gateway.launcher'], capture_output=True, text=True, timeout=3)
        services['gateway'] = 'active' if r.stdout.strip() else 'inactive'
    except Exception:
        logger.exception()
        services['gateway'] = 'inactive'
    import importlib
    for svc, mod in [('evolution', 'metano.evolution'), ('rag', 'metano.knowledge'), ('tts', 'metano.voice.tts'), ('browser', 'metano.browser'), ('home', 'metano.home_assistant')]:
        try:
            importlib.import_module(mod)
            services[svc] = 'active'
        except Exception:
            services[svc] = 'inactive'
    return services

def _redact(obj):
    if isinstance(obj, dict):
        return {k: '***' if k in SENSITIVE_KEYS else _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(item) for item in obj]
    return obj

def _deep_merge(base, override):
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        elif v == '***' and k in result:
            result[k] = result[k]
        else:
            result[k] = v
    return result

@app.get('/api/config')
async def api_get_config(_admin=Depends(require_role("admin"))):
    try:
        import yaml
        if not CONFIG_PATH.exists():
            return {'config': {}}
        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f) or {}
        return {'config': _redact(config)}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'config': {}})

@app.put('/api/config')
async def api_update_config(body: dict, _admin=Depends(require_role("admin"))):
    try:
        import yaml
        existing = {}
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                existing = yaml.safe_load(f) or {}
        config = body.get('config', body)
        merged = _deep_merge(existing, config)
        # SECURITY (M-08): config holds secrets (JWT secret, password hashes, API
        # keys) — force owner-only perms, never rely on the process umask.
        _write_config_safe(merged)
        return {'status': 'saved'}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception()
        raise HTTPException(status_code=400, detail=str(e))

@app.get('/api/logs')
async def api_logs(source: str='all', lines: int=100, _admin=Depends(require_role("admin"))):
    result = {}
    if source in ('all', 'evolution'):
        try:
            from .evo_models import get_audit, init_db as init_evo_db
            init_evo_db()
            entries = get_audit(limit=lines)
            result['evolution'] = entries
        except Exception:
            logger.exception()
    if source in ('all', 'audit') and AUDIT_LOG.exists():
        raw = AUDIT_LOG.read_text().strip().split('\n')
        entries = []
        for line in raw[-lines:]:
            try:
                entries.append(json.loads(line))
            except Exception:
                logger.exception()
        result['audit'] = entries
    if source in ('all', 'gateway'):
        try:
            from .gateway.router import GATEWAY_LOG
            if GATEWAY_LOG.exists():
                raw = GATEWAY_LOG.read_text().strip().split('\n')
                entries = []
                for line in raw[-lines:]:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        logger.exception()
                result['gateway'] = entries
        except Exception:
            logger.exception()
    return result

@app.get('/api/profile')
@app.get('/api/profiles/{user_id}')
async def api_profile(user_id: str='', _user=Depends(require_role("user"))):
    try:
        # SECURITY (S5): a normal user may only read their own profile. The
        # requested user_id must match the authenticated JWT identity unless the
        # caller is an admin (who may inspect any profile).
        if not user_id:
            user_id = _user['username']
        elif _user.get('role') != 'admin' and user_id != _user['username']:
            raise HTTPException(status_code=403, detail='无权查看其他用户画像')
        from .honcho.models import init_honcho_db, get_profile, get_user, create_user
        conn = init_honcho_db()
        if not get_user(conn, user_id):
            create_user(conn, user_id=user_id)
        return get_profile(conn, user_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/upload')
async def api_upload(file: UploadFile = File(...), _user=Depends(require_role("user"))):
    """Upload a file for the AI to read in chat. Saved to UPLOADS_DIR."""
    ALLOWED_EXT = {'.txt', '.md', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.csv', '.json', '.py', '.js', '.ts', '.html', '.docx'}
    MAX_SIZE = 20 * 1024 * 1024
    USER_QUOTA_BYTES = 100 * 1024 * 1024
    USER_QUOTA_FILES = 50
    TOTAL_CAP_BYTES = 512 * 1024 * 1024
    filename = file.filename or 'upload'
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f'不支持的文件类型: {ext or "(无扩展名)"}')
    username = _user['username']
    # Pre-check Content-Length when the client sends it (M-06).
    if file.size is not None and file.size > MAX_SIZE:
        raise HTTPException(status_code=400, detail='文件过大（上限 20MB）')
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    # Per-user quota + total capacity (M-06).
    try:
        upload_entries = list(UPLOADS_DIR.iterdir())
    except OSError:
        upload_entries = []
    user_files = [p for p in upload_entries if p.is_file() and p.name.startswith(f'{username}_')]
    if len(user_files) >= USER_QUOTA_FILES:
        raise HTTPException(status_code=429, detail='上传数量已达上限')
    user_bytes = sum(p.stat().st_size for p in user_files)
    total_bytes = sum(p.stat().st_size for p in upload_entries if p.is_file())
    if user_bytes >= USER_QUOTA_BYTES or total_bytes >= TOTAL_CAP_BYTES:
        raise HTTPException(status_code=429, detail='存储配额已满')
    # Chunked read up to MAX_SIZE+1 — never buffer an unbounded body in memory.
    content = bytearray()
    while True:
        chunk = await file.read(256 * 1024)
        if not chunk:
            break
        content += chunk
        if len(content) > MAX_SIZE:
            raise HTTPException(status_code=400, detail='文件过大（上限 20MB）')
    dest = UPLOADS_DIR / f'{username}_{uuid.uuid4().hex[:8]}{ext}'
    dest.write_bytes(bytes(content))
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return {'path': str(dest), 'name': filename, 'size': len(content)}

@app.post('/api/chat')
async def api_chat(body: dict, _user=Depends(require_role("user"))):
    msg = body.get('message', '')
    if not isinstance(msg, str) or not msg.strip():
        raise HTTPException(status_code=400, detail='message 不能为空')
    from .gateway.router import router
    # SECURITY: user_id must come from the authenticated JWT identity, never
    # from the client body — otherwise a logged-in user could impersonate
    # another user (admin/default), bypass rate limits by rotating user_id, and
    # escalate to user-tier tools from a guest role.
    user_id = _user['username']
    platform = body.get('platform', 'web')
    session_id = body.get('session_id', '')
    context = body.get('context', [])
    reset = bool(body.get('reset', False))
    if reset:
        # Explicit "新对话": drop the in-memory session AND its bridge.db id so
        # this exchange opens a brand-new session row instead of appending to
        # the previous conversation.
        router.reset_session(platform, user_id)
    elif session_id:
        _inject_session_context(router, platform, user_id, session_id)
    elif context and isinstance(context, list):
        router.inject_history(platform, user_id, context)

    async def event_stream():
        q: asyncio.Queue = asyncio.Queue()

        def on_event(ev: dict):
            q.put_nowait(ev)

        async def run():
            try:
                response = await router.route_message(platform, user_id, msg, on_event=on_event)
                try:
                    sess = router.get_or_create_session(platform, user_id)
                    sid = sess.db_session_id or session_id
                except Exception:
                    sid = session_id
                await q.put({'type': 'done', 'response': response, 'session_id': sid})
            except Exception as e:
                logger.exception()
                await q.put({'type': 'error', 'message': str(e)})

        task = asyncio.create_task(run())
        try:
            while True:
                ev = await q.get()
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev['type'] in ('done', 'error'):
                    break
        finally:
            await task

    return StreamingResponse(event_stream(), media_type='text/event-stream')


def _inject_session_context(router, platform: str, user_id: str, session_id: str):
    """Load messages from bridge.db session and inject into router session.

    SECURITY: only allow resuming a session that belongs to this user
    (sessions.user_key == f'{platform}:{user_id}'). Without the ownership check
    any logged-in user could read another user's conversation by guessing or
    leaking a session_id (IDOR).
    """
    try:
        from .db import get_db
        conn = get_db()
        expected_key = f'{platform}:{user_id}'
        owner = conn.execute(
            'SELECT 1 FROM sessions WHERE id = ? AND user_key = ?',
            (session_id, expected_key)
        ).fetchone()
        if not owner:
            # Not this user's session — do not load or pin it.
            return
        rows = conn.execute(
            'SELECT role, content FROM messages WHERE session_id = ? ORDER BY timestamp ASC LIMIT 20',
            (session_id,)
        ).fetchall()
        sess = router.get_or_create_session(platform, user_id)
        if rows:
            history = [{'role': r[0], 'content': r[1]} for r in rows if r[0] in ('user', 'assistant')]
            sess.history = history[-router.max_history * 2:]
            # Pin the resumed session so this exchange appends to IT (not to
            # whatever db_session_id the in-memory session was holding).
            sess.db_session_id = session_id
    except Exception:
        logger.exception()

@app.post('/api/knowledge/search')
async def api_knowledge_search(body: dict):
    try:
        from .knowledge import knowledge_search
        return knowledge_search(body.get('query', ''), limit=body.get('limit', 5))
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

_SEMANTIC_PROJECTS = {
    'metano': None,  # default → metano home dir
    'scrapling': Path.home() / 'scrapling-project',
    'dailyhot': Path.home() / 'DailyHotApi',
}

def _resolve_semantic_project(project: str) -> str | None:
    """Map a server-registered project ID to its absolute path, or None.

    SECURITY (M-01 sec): the request body's ``project`` is never treated as an
    arbitrary filesystem path to run ``ccc search`` inside — only registered IDs
    are accepted, and the caller's path is ignored.
    """
    project = (project or '').strip()
    if not project:
        return str(home_dir().resolve())
    if project not in _SEMANTIC_PROJECTS:
        return None
    root = _SEMANTIC_PROJECTS[project]
    if root is None:
        return str(home_dir().resolve())
    try:
        return str(root.resolve())
    except OSError:
        return None


@app.post('/api/knowledge/semantic-search')
async def api_knowledge_semantic_search(body: dict, _admin=Depends(require_role("admin"))):
    try:
        from .knowledge import knowledge_semantic_search
        query = body.get('query', '')
        if not isinstance(query, str) or not query.strip():
            return _error_response('query is required', status_code=400)
        if len(query) > 200:
            return _error_response('query too long (max 200 chars)', status_code=400)
        project = body.get('project', '')
        proj_path = _resolve_semantic_project(project)
        if proj_path is None:
            return _error_response(f'Unknown project: {project}', status_code=400)
        try:
            limit = max(1, min(int(body.get('limit', 5) or 5), 20))
        except (TypeError, ValueError):
            limit = 5
        return knowledge_semantic_search(query, project=proj_path, limit=limit)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/knowledge/explore')
async def api_knowledge_explore(body: dict, _admin=Depends(require_role("admin"))):
    try:
        from .knowledge_explorer import explore_domain
        return explore_domain(body.get('topic', ''), depth=body.get('depth', 3))
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/knowledge/gaps')
async def api_knowledge_gaps(_admin=Depends(require_role("admin"))):
    """Gap discovery triggers an LLM analysis call — admin-only (H-02)."""
    try:
        from .knowledge_explorer import discover_knowledge_gaps
        return {'gaps': discover_knowledge_gaps()}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/knowledge/ingest')
async def api_knowledge_ingest(body: dict, _admin=Depends(require_role("admin"))):
    try:
        from .knowledge import knowledge_ingest
        result = knowledge_ingest(body.get('path', ''), title=body.get('title', ''))
        if isinstance(result, dict) and result.get('error'):
            return _error_response(str(result['error']), status_code=400)
        return result
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

# ── Knowledge Graph ──

@app.post('/api/knowledge/graph/extract')
async def api_knowledge_graph_extract(body: dict = {}, _admin=Depends(require_role("admin"))):
    """Manually (re)build the knowledge graph. replace defaults to True for a clean rebuild."""
    try:
        from .knowledge import knowledge_extract_graph, knowledge_graph_stats
        result = knowledge_extract_graph(
            doc_id=body.get('doc_id', ''),
            limit=body.get('limit', 0),
            replace=body.get('replace', True),
        )
        return {'extract': result, 'stats': knowledge_graph_stats()}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'extract': {'status': 'error'}})

@app.get('/api/knowledge/graph')
async def api_knowledge_graph(entity: str = '', entity_type: str = '', limit: int = 50):
    """Query knowledge graph entities and their relationships."""
    try:
        from .knowledge import knowledge_graph_query
        return knowledge_graph_query(entity_name=entity, entity_type=entity_type, limit=limit)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'entities': [], 'relationships': []})

@app.get('/api/knowledge/graph/stats')
async def api_knowledge_graph_stats():
    """Knowledge graph statistics."""
    try:
        from .knowledge import knowledge_graph_stats
        return knowledge_graph_stats()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'entities': 0, 'relationships': 0})

# NOTE: /api/knowledge/{doc_id} MUST be declared AFTER all fixed
# /api/knowledge/... routes (graph, graph/stats, explore, gaps, ingest).
# FastAPI matches in declaration order, so a `{doc_id}` before them would
# capture "graph" / "graph/stats" and 404 them.
@app.get('/api/knowledge/{doc_id}')
async def api_knowledge_get(doc_id: str, _admin=Depends(require_role("admin"))):
    """Return a single knowledge document with its full content (Web viewer)."""
    try:
        from .knowledge import knowledge_get_document
        doc = knowledge_get_document(doc_id)
        if not doc:
            return _error_response('Not found', status_code=404)
        return doc
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.delete('/api/knowledge/{doc_id}')
async def api_knowledge_delete(doc_id: str, _admin=Depends(require_role("admin"))):
    try:
        from .knowledge import knowledge_delete
        return knowledge_delete(doc_id)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/evolution/pause')
async def api_evolution_pause(_admin=Depends(require_role("admin"))):
    try:
        from .evolution import evolution_pause
        return evolution_pause()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/evolution/resume')
async def api_evolution_resume(_admin=Depends(require_role("admin"))):
    try:
        from .evolution import evolution_resume
        return evolution_resume()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/evolution/run')
async def api_evolution_run(body: dict, _admin=Depends(require_role("admin"))):
    try:
        from .evolution import evolution_run
        return evolution_run(body.get('stage', 'all'))
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/cron/jobs/{job_id}/trigger')
async def api_cron_trigger(job_id: str, _admin=Depends(require_role("admin"))):
    """Manually run a cron job now, returning its real execution result.

    F-01/C4: the old handler only touched ``last_run_at`` without executing.
    ``cron_daemon.run_job`` is the single execution path shared with the daemon
    tick, so a triggered job behaves exactly like a scheduled one (output file,
    process-group timeout, concurrency cap). Registered action jobs are enabled
    for the web process, which never runs the daemon tick.
    """
    jobs = _load_cron_jobs()
    target = next((j for j in jobs if j['id'] == job_id), None)
    if not target:
        return _error_response('Job not found', status_code=404)
    try:
        cron_daemon._register_default_actions()
    except Exception:
        logger.exception('cron: failed to register default actions')
    result = await asyncio.to_thread(cron_daemon.run_job, target)
    _save_cron_jobs(jobs)  # run_job updated last_run_at/last_error in place
    return {'triggered': job_id, 'result': result}

@app.put('/api/models/{name}/default')
async def api_model_set_default(name: str, _admin=Depends(require_role("admin"))):
    try:
        from .model_router import model_router
        model_router.set_default(name)
        # model_router._persist_default writes the config without chmod; enforce
        # owner-only perms here (M-08).
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
        return {'status': 'default_set', 'provider': name}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/security/users')
async def api_security_users(_admin=Depends(require_role("admin"))):
    try:
        from .security import security
        users = security.list_users()
        return {'users': users}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'users': []})

@app.put('/api/security/{user_id}/tier')
async def api_security_set_tier(user_id: str, body: dict, _admin=Depends(require_role("admin"))):
    try:
        from .security import security
        security.set_tier(user_id, body.get('tier', 'user'))
        return {'status': 'updated', 'user_id': user_id, 'tier': body.get('tier')}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/browser/browse')
async def api_browser_browse(body: dict, _admin=Depends(require_role("admin"))):
    try:
        from .browser import web_browse
        result = web_browse(body.get('url', ''), mode=body.get('mode', 'dynamic'))
        if isinstance(result, dict) and result.get('status') == 'error':
            return _error_response(str(result.get('error', 'browse failed')), status_code=502)
        return result
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/browser/search')
async def api_browser_search(body: dict, _admin=Depends(require_role("admin"))):
    try:
        from .browser import web_search
        result = web_search(body.get('query', ''))
        if isinstance(result, dict) and result.get('error'):
            return _error_response(str(result['error']), status_code=502)
        return result
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/voice/file')
async def get_voice_file(path: str, _user=Depends(require_role("user"))):
    import os
    from .voice.core import AUDIO_DIR
    voice_dir = os.environ.get('VOICE_OUTPUT_DIR', str(AUDIO_DIR))
    safe_path = os.path.normpath(os.path.join(voice_dir, os.path.basename(path)))
    if not safe_path.startswith(voice_dir) or not os.path.exists(safe_path):
        raise HTTPException(status_code=404, detail='File not found')
    return FileResponse(safe_path, media_type='audio/mpeg', filename=os.path.basename(safe_path))

@app.post('/api/voice/tts')
async def api_voice_tts(body: dict, _admin=Depends(require_role("admin"))):
    try:
        from .voice import voice_speak
        result = voice_speak(body.get('text', ''), voice=body.get('voice', 'zh-CN-YunxiNeural'), rate=body.get('rate', '+0%'))
        if isinstance(result, dict) and result.get('error'):
            return _error_response(str(result['error']), status_code=400)
        return result
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/voice/voices')
async def api_voice_voices(language: str='', _user=Depends(require_role("user"))):
    try:
        from .voice import voice_list_voices
        return voice_list_voices(language)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/home/status')
async def api_home_status(_admin=Depends(require_role("admin"))):
    """Full HA state is sensitive device data — admin-only (H-09)."""
    try:
        from .home_assistant import home_status_full
        return home_status_full()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'entities': [], 'configured': False})

@app.get('/api/home/config')
async def api_home_config_get(_admin=Depends(require_role("admin"))):
    """Leaks the HA base URL — admin-only (H-09)."""
    try:
        from .home_assistant import ha_get_config
        return ha_get_config()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={})

@app.post('/api/home/config')
async def api_home_config_set(body: dict, _admin=Depends(require_role("admin"))):
    try:
        from .home_assistant import ha_set_config
        return ha_set_config((body.get('url') or '').strip(), (body.get('token') or '').strip())
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={})

@app.get('/api/home/status/{entity_id}')
async def api_home_entity(entity_id: str, _admin=Depends(require_role("admin"))):
    try:
        from .home_assistant import get_entity_state
        return _result_or_error(get_entity_state(entity_id), status_code=400)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/home/control')
async def api_home_control(body: dict, _admin=Depends(require_role("admin"))):
    try:
        from .home_assistant import home_control
        return _result_or_error(home_control(body.get('entity_id', ''), body.get('service', 'toggle')), status_code=400)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

# ── Collaboration control plane (collab) ────────────────────────────────────
# All collab routes are admin-only; task rows live in bridge.db (collab_tasks).

@app.get('/api/collab/tasks')
async def api_collab_tasks(status: str='', limit: int=100, _admin=Depends(require_role("admin"))):
    try:
        if status and status not in collab.TASK_STATUSES:
            return _error_response(f'Invalid status: {status}', status_code=400)
        items = collab.list_tasks(status=status or None, limit=limit)
        return {'items': items, 'total': len(items)}
    except Exception:
        logger.exception()
        return _error_response('Internal error', extra={'items': []})

@app.post('/api/collab/tasks')
async def api_collab_create_task(body: dict, _admin=Depends(require_role("admin"))):
    task_type = body.get('task_type', 'general')
    prompt = (body.get('prompt') or '').strip()
    if not prompt:
        return _error_response('prompt is required', status_code=400)
    if not collab.task_type_allowed(task_type, _admin.get('role', 'guest')):
        return _error_response(f'task_type "{task_type}" not allowed for your role', status_code=403)
    try:
        task = collab.create_task(
            task_type=task_type,
            target=body.get('target', 'local'),
            prompt=prompt,
            assigned_to=body.get('assigned_to', ''),
            created_by=_admin.get('username', ''),
        )
    except Exception:
        logger.exception()
        return _error_response('Internal error')
    _audit('collab_task_created', _admin.get('username', ''),
           {'task_id': task['id'], 'task_type': task['task_type'],
            'target': task['target'], 'prompt': prompt[:100]})
    return task

@app.get('/api/collab/tasks/{task_id}')
async def api_collab_get_task(task_id: str, _admin=Depends(require_role("admin"))):
    task = collab.get_task(task_id)
    if not task:
        return _error_response('Task not found', status_code=404)
    return task

@app.post('/api/collab/tasks/{task_id}/status')
async def api_collab_update_status(task_id: str, body: dict, _admin=Depends(require_role("admin"))):
    status = body.get('status', '')
    if status not in collab.TASK_STATUSES:
        return _error_response(f'Invalid status: {status}', status_code=400)
    try:
        task = collab.update_status(task_id, status)
    except ValueError as e:
        return _error_response(str(e), status_code=400)
    except Exception:
        logger.exception()
        return _error_response('Internal error')
    if not task:
        return _error_response('Task not found', status_code=404)
    _audit('collab_task_status', _admin.get('username', ''),
           {'task_id': task_id, 'status': status})
    return task

@app.post('/api/collab/tasks/{task_id}/execute')
async def api_collab_execute_task(task_id: str, body: dict={}, _admin=Depends(require_role("admin"))):
    try:
        timeout = int(body.get('timeout', 120) or 120)
    except (TypeError, ValueError):
        timeout = 120
    try:
        # F-18/M-06: the sync implementation blocks on a subprocess/HTTP call;
        # running it via execute_task_async (asyncio.to_thread) keeps the Web
        # event loop responsive for other page requests.
        result = await collab.execute_task_async(task_id, timeout=timeout)
    except Exception:
        logger.exception()
        return _error_response('Internal error')
    if not result.get('task'):
        return _error_response('Task not found', status_code=404)
    _audit('collab_task_executed', _admin.get('username', ''),
           {'task_id': task_id, 'execution': result.get('execution', {})})
    return result

@app.get('/api/collab/audit')
async def api_collab_audit(limit: int=100, _admin=Depends(require_role("admin"))):
    try:
        return {'items': collab.list_audit(limit=limit)}
    except Exception:
        logger.exception()
        return _error_response('Internal error', extra={'items': []})

_ws_clients: list[WebSocket] = []
_ws_conns_by_ip: dict[str, int] = {}
MAX_WS_PER_IP = 5
MAX_WS_TOTAL = 50
MAX_WS_FIRST_MSG_WAIT = 10  # seconds (M-06: handshake timeout)


@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket):
    # M-06: Origin check — reject cross-site WebSocket hijacking. Uses the same
    # exact-netloc check as the HTTP CSRF middleware (M-02) so a subdomain
    # suffix like ``localhost:9120.evil.com`` can't pass either path.
    origin = ws.headers.get('origin', '')
    host = ws.headers.get('host', '')
    if origin:
        if not _origin_allowed(origin, host):
            try:
                await ws.close(code=4403)
            except Exception:
                pass
            return
    # M-06: per-IP + total connection caps.
    ip = ws.client.host if ws.client else 'unknown'
    if _ws_conns_by_ip.get(ip, 0) >= MAX_WS_PER_IP or len(_ws_clients) >= MAX_WS_TOTAL:
        try:
            await ws.close(code=4403)
        except Exception:
            pass
        return
    await ws.accept()

    # F-12: authenticate from the HttpOnly access_token cookie first.
    user = get_current_user_from_request(ws)
    if not user:
        # Then try a one-time short-lived WS ticket (or legacy token) as the
        # first message, with a hard timeout so a silent socket can't hang.
        try:
            auth_msg = await asyncio.wait_for(ws.receive_text(), timeout=MAX_WS_FIRST_MSG_WAIT)
            auth_data = json.loads(auth_msg)
        except Exception:
            auth_data = {}
        ticket = auth_data.get('ticket', '') if isinstance(auth_data, dict) else ''
        token = auth_data.get('token', '') if isinstance(auth_data, dict) else ''
        if ticket:
            tuser = consume_ws_ticket(ticket)
            if tuser:
                user = tuser
        elif token:
            user = validate_access_token(token)
    if not user:
        try:
            await ws.send_json({'error': 'Authentication required'})
            await ws.close(code=4001)
        except Exception:
            # Client may have already disconnected — a second close here
            # triggers the ASGI 'Unexpected websocket.close' race.
            pass
        return

    _ws_clients.append(ws)
    _ws_conns_by_ip[ip] = _ws_conns_by_ip.get(ip, 0) + 1
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception()
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
        _ws_conns_by_ip[ip] = max(0, _ws_conns_by_ip.get(ip, 0) - 1)

async def ws_broadcast(data: dict):
    for client in _ws_clients:
        try:
            await client.send_json(data)
        except Exception:
            logger.exception()

@app.post('/api/search/web')
async def api_web_search(body: dict, _admin=Depends(require_role("admin"))):
    try:
        from .mcp_bridge import tavily_search
        try:
            limit = max(1, min(int(body.get('limit', 10) or 10), 20))
        except (TypeError, ValueError):
            limit = 10
        result = await tavily_search(body.get('query', ''), limit=limit)
        if isinstance(result, dict) and result.get('error'):
            return _error_response(str(result['error']), status_code=502)
        return result
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/mcp/tools')
async def api_mcp_tools(_admin=Depends(require_role("admin"))):
    try:
        from .mcp_bridge import list_available_tools
        return {'tools': await list_available_tools()}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'tools': []})

@app.post('/api/mcp/call')
async def api_mcp_call(body: dict, _admin=Depends(require_role("admin"))):
    try:
        from .mcp_bridge import call_tool
        result = await call_tool(body.get('tool', ''), body.get('args', {}))
        return {'result': result}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/proxy/providers')
async def api_proxy_providers(_admin=Depends(require_role("admin"))):
    try:
        from .model_router import ModelRouter
        return {'providers': ModelRouter.free_provider_presets()}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'providers': []})


def _validate_protocol_url(base_url: str, protocol: str) -> str | None:
    """Return an error string if base_url's path and protocol are inconsistent.

    F-14: an OpenAI-compatible endpoint is ``/v1/chat/completions``; an Anthropic
    endpoint is ``/v1/messages``. Silently storing a mismatched pair makes the
    provider unusable (or mis-formats requests), so reject/warn loudly.
    """
    protocol = (protocol or 'anthropic').lower()
    if not base_url:
        return None
    try:
        path = urlparse(base_url).path.lower()
    except ValueError:
        return f'Invalid base_url: {base_url}'
    has_openai = '/v1/chat/completions' in path or path.endswith('/chat/completions')
    has_anthropic = '/v1/messages' in path or path.endswith('/messages')
    if protocol == 'openai' and has_anthropic and not has_openai:
        return f'base_url 使用 Anthropic /v1/messages 路径，但 protocol=openai，两者不匹配'
    if protocol == 'anthropic' and has_openai and not has_anthropic:
        return f'base_url 使用 OpenAI /v1/chat/completions 路径，但 protocol=anthropic，两者不匹配'
    return None


@app.post('/api/proxy/add')
async def api_proxy_add(body: dict, _admin=Depends(require_role("admin"))):
    try:
        import yaml
        name = body.get('name', '')
        if not name:
            raise HTTPException(status_code=400, detail='name required')
        base_url = body.get('base_url', '')
        protocol = body.get('protocol', 'anthropic')
        proto_err = _validate_protocol_url(base_url, protocol)
        if proto_err:
            raise HTTPException(status_code=400, detail=proto_err)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                existing = yaml.safe_load(f) or {}
        models = existing.get('models', {})
        models[name] = {'base_url': base_url, 'api_key': body.get('api_key', ''), 'model': body.get('model', ''), 'max_tokens': body.get('max_tokens', 4096), 'supports_vision': body.get('supports_vision', False), 'supports_tools': body.get('supports_tools', True), 'protocol': protocol, 'enabled': True}
        price = body.get('price')
        if isinstance(price, dict):
            models[name]['price'] = {k: price[k] for k in ('input', 'output', 'cache_read') if k in price}
        existing['models'] = models
        _write_config_safe(existing)
        # Refresh the shared module-level ModelRouter singleton so the newly
        # added provider becomes visible to /api/models, chat and evolution
        # immediately (previously a throwaway instance was created and never used).
        from .model_router import model_router
        model_router.refresh()
        return {'status': 'added', 'provider': name, 'protocol': protocol}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception()
        raise HTTPException(status_code=400, detail=str(e))

@app.put('/api/proxy/{name}')
async def api_proxy_update(name: str, body: dict, _admin=Depends(require_role("admin"))):
    """Update an existing model provider (incl. price)."""
    try:
        import yaml
        existing = {}
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                existing = yaml.safe_load(f) or {}
        models = existing.get('models', {})
        if name not in models:
            # Not in config (e.g. env-fallback 'default'): materialize from the live provider
            from .model_router import model_router
            live = model_router.get_provider(name)
            if not live:
                raise HTTPException(status_code=404, detail=f'provider not found: {name}')
            m = {'base_url': live.base_url, 'api_key': live.api_key, 'model': live.model,
                 'max_tokens': live.max_tokens, 'supports_vision': live.supports_vision,
                 'supports_tools': live.supports_tools, 'enabled': True,
                 'price': {'input': live.price_input, 'output': live.price_output, 'cache_read': live.price_cache_read}}
            models[name] = m
        else:
            m = models[name]
        for field in ('base_url', 'api_key', 'model', 'max_tokens', 'supports_vision', 'supports_tools', 'enabled', 'protocol'):
            if field in body:
                m[field] = body[field]
        if 'default' in body:
            m['default'] = bool(body['default'])
        price = body.get('price')
        if isinstance(price, dict):
            p = m.setdefault('price', {})
            for k in ('input', 'output', 'cache_read'):
                if k in price:
                    p[k] = price[k]
        # F-14: validate the final protocol/base_url pair after the merge.
        proto_err = _validate_protocol_url(m.get('base_url', ''), m.get('protocol', 'anthropic'))
        if proto_err:
            raise HTTPException(status_code=400, detail=proto_err)
        existing['models'] = models
        _write_config_safe(existing)
        from .model_router import model_router
        model_router.refresh()
        return {'status': 'updated', 'provider': name, 'protocol': m.get('protocol', 'anthropic')}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception()
        raise HTTPException(status_code=400, detail=str(e))

@app.get('/api/memory/stats')
async def api_memory_stats(_admin=Depends(require_role("admin"))):
    try:
        from .memory import get_memory_stats
        return get_memory_stats()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/memory/search')
async def api_memory_search(q: str=Query(...), limit: int=10, tag: str='', _admin=Depends(require_role("admin"))):
    try:
        from .memory import search_memories
        limit = min(max(int(limit), 1), 100)
        return search_memories(q, limit=limit, tag=tag or None)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'results': []})

@app.get('/api/memory/timeline')
async def api_memory_timeline(days: int=7, limit: int=20, _admin=Depends(require_role("admin"))):
    try:
        from .honcho.models import init_honcho_db, get_user, create_user
        from datetime import datetime, timezone
        import time as _time
        days = min(max(int(days), 1), 365)
        limit = min(max(int(limit), 1), 200)
        conn = init_honcho_db()
        if not get_user(conn, 'default'):
            create_user(conn, user_id='default')
        cutoff = _time.time() - days * 86400
        rows = conn.execute(
            "SELECT id, category, content, timestamp FROM observations "
            "WHERE user_id = 'default' AND timestamp >= ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (cutoff, limit)
        ).fetchall()
        results = []
        for r in rows:
            ts = datetime.fromtimestamp(r['timestamp'], tz=timezone.utc).strftime('%m-%d %H:%M')
            results.append(f"[{ts}] [{r['category']}] {r['content'][:100]}")
        return {'observations': len(results), 'formatted': '\n'.join(results)}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/memory/detail')
async def api_memory_detail(category: str='', belief_id: str='', _admin=Depends(require_role("admin"))):
    try:
        from .honcho.models import init_honcho_db, get_user, create_user, get_beliefs
        conn = init_honcho_db()
        if not get_user(conn, 'default'):
            create_user(conn, user_id='default')
        if belief_id:
            row = conn.execute("SELECT * FROM beliefs WHERE id = ? AND contradicted = 0", (belief_id,)).fetchone()
            return dict(row) if row else {'error': 'not found'}
        beliefs = get_beliefs(conn, 'default')
        if category:
            beliefs = [b for b in beliefs if b['category'] == category]
        return {'beliefs': beliefs}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

_memory_ops_times: dict[str, list[float]] = {}

def _memory_op_rate_limit(op: str, max_calls: int = 5, window: int = 300) -> bool:
    """Return True if the admin op is within its rate limit (H-02)."""
    now = time.time()
    times = _memory_ops_times.setdefault(op, [])
    times[:] = [t for t in times if now - t < window]
    if len(times) >= max_calls:
        return False
    times.append(now)
    return True


@app.post('/api/memory/compress')
async def api_memory_compress(_admin=Depends(require_role("admin"))):
    if not _memory_op_rate_limit('compress'):
        return _error_response('compress rate limit (max 5/5min)', status_code=429)
    try:
        from .memory import compress_memories
        return compress_memories()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/memory/export')
async def api_memory_export(_admin=Depends(require_role("admin"))):
    try:
        from .memory import export_memories
        return export_memories()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/memory/import')
async def api_memory_import(body: dict, _admin=Depends(require_role("admin"))):
    # SECURITY (H-02): cap the number of memories per import request.
    try:
        memories = body.get('memories', [])
        if not isinstance(memories, list):
            return _error_response('memories must be a list', status_code=400)
        if len(memories) > 500:
            return _error_response('import too large (max 500 memories)', status_code=400)
        from .memory import import_memories
        return import_memories({'memories': memories}, merge=body.get('merge', True))
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/memory/seed')
async def api_memory_seed(_admin=Depends(require_role("admin"))):
    if not _memory_op_rate_limit('seed'):
        return _error_response('seed rate limit (max 5/5min)', status_code=429)
    try:
        from .memory import seed_from_claude_memory
        return seed_from_claude_memory()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/evolution/correct')
async def api_evolution_correct(body: dict, _admin=Depends(require_role("admin"))):
    try:
        from .reflector import apply_correction
        return apply_correction(body.get('user_id', 'default'), body.get('correction', ''), category=body.get('category', ''))
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/evolution/behaviors')
async def api_evolution_behaviors(_admin=Depends(require_role("admin"))):
    """Return agent behavior rules from evo.db and recent correction records."""
    try:
        from .behavior_analyzer import get_behavior_patterns
        return get_behavior_patterns()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'patterns': [], 'recent_corrections': []})

@app.get('/api/evolution/rules')
async def api_evolution_rules(_admin=Depends(require_role("admin"))):
    """List all agent rules from evo.db."""
    try:
        from .evo_models import get_rules, init_db
        init_db()
        rules = get_rules(active_only=False)
        return {'rules': rules}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'rules': []})

@app.post('/api/evolution/rules/{rule_id}/toggle')
async def api_evolution_rule_toggle(rule_id: int, body: dict={}, _admin=Depends(require_role("admin"))):
    """Enable or disable an agent rule."""
    try:
        from .evo_models import toggle_rule
        toggle_rule(rule_id, body.get('active', True))
        return {'status': 'toggled', 'rule_id': rule_id}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/evolution/action-log')
async def api_evolution_action_log(limit: int=50, _admin=Depends(require_role("admin"))):
    """Recent action log entries."""
    try:
        from .evo_models import get_recent_actions, init_db
        init_db()
        limit = min(max(int(limit), 1), 500)
        return {'actions': get_recent_actions(limit=limit)}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'actions': []})

@app.post('/api/evolution/analyze')
async def api_evolution_analyze(body: dict={}, _admin=Depends(require_role("admin"))):
    """Manually trigger behavior pattern analysis."""
    try:
        from .behavior_analyzer import analyze_behavior_patterns
        return analyze_behavior_patterns(days=body.get('days', 7))
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'status': 'error'})

@app.post('/api/evolution/behavior-approve/{suggestion_id}')
async def api_evolution_behavior_approve(suggestion_id: str, _admin=Depends(require_role("admin"))):
    """Approve a behavior_improvement suggestion and write to Claude Code memory.

    Accepts either the legacy SUGGESTIONS_FILE id (``evo-<rule_id>``) or a
    numeric id from the proposals table (which is what the frontend passes).
    """
    try:
        from .adapter import apply_behavior_improvement, _apply_behavior_improvement
        from .evo_models import get_proposals, update_proposal_status
        # Legacy path: SUGGESTIONS_FILE suggestion id (evo-<rule_id>).
        result = apply_behavior_improvement(suggestion_id)
        if result:
            return result
        # Proposals-table path: numeric proposal id.
        if suggestion_id.isdigit():
            pid = int(suggestion_id)
            prop = next((p for p in get_proposals() if p['id'] == pid), None)
            if prop and prop.get('proposal_type') == 'behavior_improvement':
                update_proposal_status(pid, 'approved')
                applied = _apply_behavior_improvement(prop.get('content', ''), prop.get('detail', ''))
                if applied:
                    update_proposal_status(pid, 'applied', result=applied.get('status', ''))
                    return {**applied, 'proposal_id': pid}
        return _error_response('suggestion not found or not behavior_improvement type', status_code=404)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/evolution/behavior-reject/{suggestion_id}')
async def api_evolution_behavior_reject(suggestion_id: str, _admin=Depends(require_role("admin"))):
    """Reject a behavior_improvement suggestion (legacy id or numeric proposal id)."""
    try:
        from .adapter import reject_suggestion
        from .evo_models import get_proposals, update_proposal_status
        result = reject_suggestion(suggestion_id)
        if result:
            return {'status': 'rejected', 'suggestion': result}
        if suggestion_id.isdigit():
            pid = int(suggestion_id)
            prop = next((p for p in get_proposals() if p['id'] == pid), None)
            if prop and prop.get('proposal_type') == 'behavior_improvement':
                update_proposal_status(pid, 'rejected')
                return {'status': 'rejected', 'proposal_id': pid}
        return _error_response('suggestion not found', status_code=404)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/browser/screenshot')
async def api_browser_screenshot(body: dict, _admin=Depends(require_role("admin"))):
    """Take a browser screenshot. Admin-only and SSRF-hardened (H-03).

    URL validation (http/https + public-IP-only) happens in browser.pw_screenshot;
    we re-validate here as defence-in-depth and cap the inline image size.
    """
    try:
        import base64 as _b64
        from .browser import web_screenshot, _validate_http_url
        url = body.get('url', '')
        err = _validate_http_url(url)
        if err:
            return _error_response(err, status_code=400)
        result = web_screenshot(url, full_page=bool(body.get('full_page', True)))
        if result.get('status') == 'ok' and result.get('path'):
            path = result['path']
            if os.path.isfile(path):
                try:
                    with open(path, 'rb') as f:
                        raw = f.read()
                    if len(raw) > 5 * 1024 * 1024:
                        return _error_response('screenshot too large to return inline', status_code=413)
                    result['image'] = 'data:image/png;base64,' + _b64.b64encode(raw).decode()
                except Exception:
                    logger.exception('failed to read screenshot file for base64')
        return result
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/evolution/strategy')
async def api_evolution_strategy(context: str='', _admin=Depends(require_role("admin"))):
    """Select optimal strategy rules for a given context."""
    try:
        from .strategy import select_strategy
        strategies = select_strategy(context=context)
        return {'strategies': strategies}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'strategies': []})

@app.get('/api/evolution/effectiveness/{rule_id}')
async def api_evolution_effectiveness(rule_id: str, _admin=Depends(require_role("admin"))):
    """Get effectiveness metrics for a specific rule."""
    try:
        from .strategy import get_effectiveness
        return get_effectiveness(rule_id)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

_strategy_detect_times: list[float] = []

@app.post('/api/evolution/strategy-detect')
async def api_evolution_strategy_detect(_admin=Depends(require_role("admin"))):
    """Detect strategy patterns from action log."""
    # SECURITY (H-02): strategy detection triggers LLM analysis — rate-limit it.
    now = time.time()
    _strategy_detect_times[:] = [t for t in _strategy_detect_times if now - t < 60]
    if len(_strategy_detect_times) >= 5:
        return _error_response('strategy-detect rate limit (max 5/min)', status_code=429)
    _strategy_detect_times.append(now)
    try:
        from .strategy import detect_strategy_patterns
        patterns = detect_strategy_patterns()
        return {'patterns': patterns}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'patterns': []})

@app.get('/api/evolution/architecture')
async def api_evolution_architecture(_admin=Depends(require_role("admin"))):
    """Get latest architecture snapshot and model."""
    try:
        from .evo_models import get_latest_architecture_snapshot, init_db
        init_db()
        snap = get_latest_architecture_snapshot()
        if snap:
            return snap
        from .architect import build_architecture_model
        model = build_architecture_model()
        return {'model': model, 'created_at': time.time()}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/evolution/restructure-apply/{proposal_id}')
async def api_evolution_restructure_apply(proposal_id: int, _admin=Depends(require_role("admin"))):
    """Apply an approved architecture restructure proposal."""
    try:
        from .architect import apply_restructure
        result = apply_restructure(proposal_id)
        if result:
            return result
        return {'error': 'proposal not found'}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/evolution/cost-circuit')
async def api_evolution_cost_circuit():
    """Get cost circuit breaker status."""
    try:
        from .evolution import _get_circuit_state
        return _get_circuit_state()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/evolution/cost-circuit/config')
async def api_evolution_cost_circuit_config(body: dict, _admin=Depends(require_role("admin"))):
    """Update cost circuit breaker thresholds."""
    try:
        from .evolution import evolution_update_cost_config
        return evolution_update_cost_config(
            warn=body.get('warn'),
            pause=body.get('pause'),
            stop=body.get('stop'),
            auto_resume_hours=body.get('auto_resume_hours')
        )
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

# ── MCP remote read-only endpoint (D-plan) ──────────────────────────────
# The read-only MCP surface (metano/mcp_http.py) is served at /mcp and guarded
# by MCPAuthMiddleware at the FastAPI layer (FastMCP in mcp==1.27.1 has no
# first-class Streamable HTTP auth middleware). POST /api/mcp/token issues 1h
# read-only bearer tokens for that endpoint. See metano/mcp_gateway.py.
from .mcp_gateway import MCPAuthMiddleware, create_mcp_token, FastMCPMount

try:
    from .mcp_http import create_http_app as _create_mcp_http_app
    _mcp_http_app = _create_mcp_http_app()
    _mcp_http_ready = True
except Exception:  # pragma: no cover - depends on the parallel agent finishing mcp_http.py
    # TODO: remove this fallback once metano/mcp_http.py is stable; the guard
    # middleware and /api/mcp/token still work, only the /mcp endpoint is skipped.
    logger.exception('mcp_http.create_http_app() failed; /mcp endpoint deferred')
    _mcp_http_app = None
    _mcp_http_ready = False

app.add_middleware(MCPAuthMiddleware)

if _mcp_http_ready:
    # FastMCP serves its endpoint at the full "/mcp" path and needs its
    # session-manager lifespan run, so we use FastMCPMount (Route registration,
    # not a literal Mount — see mcp_gateway.FastMCPMount for why). Routes are
    # inserted ahead of the SPA catch-all so GET /mcp (SSE) is not swallowed.
    FastMCPMount(_mcp_http_app).install(app)


@app.post('/api/mcp/token')
async def api_mcp_token(request: Request, _admin=Depends(require_role("admin"))):
    """Issue a short-lived (1h) read-only MCP bearer token for /mcp.

    The returned token is used as ``Authorization: Bearer <token>`` against the
    mounted MCP endpoint (``aud=metano-mcp``, ``scope=[mcp:read]``).
    """
    username = _admin['username']
    ip = request.client.host if request.client else 'unknown'
    ttl = 3600
    token = create_mcp_token(username, scope=['mcp:read'], ttl_seconds=ttl)
    _audit('mcp_token_issued', username, {'ip': ip, 'scope': ['mcp:read'], 'expires_in': ttl})
    return {'token': token, 'expires_in': ttl}


# ── A2A task-delegation endpoint (Google A2A) ──────────────────────────────
# The standalone A2A app (metano/a2a_server.py) is mounted at /a2a and guards
# itself with A2AAuthMiddleware (aud=metano-a2a, same HS256 secret as the rest
# of metano).  web_server adds the RFC 8615 discovery card at the root
# well-known path and the POST /api/a2a/token issuance endpoint.
#
# Why not a plain app.mount("/a2a", ...)?  Starlette's Mount keeps the full
# "/a2a" prefix in scope["path"] and sets root_path, so the sub-app's
# middleware sees request.url.path == "/a2a/health" — A2AAuthMiddleware's
# PUBLIC_PATHS check compares against bare "/health" / "/.well-known/..." and
# would 401 every public route.  A2AMount strips the prefix and clears
# root_path before delegating (same pattern as FastMCPMount for /mcp).
from contextlib import asynccontextmanager
from starlette.routing import Route
from .a2a_server import create_a2a_app as _create_a2a_app
from .a2a_server import create_a2a_token as _create_a2a_token
from .a2a_server import _build_agent_card as _a2a_build_agent_card


class A2AMount:
    """Mount the standalone A2A app (relative-path routes) at ``/a2a``.

    Routes are inserted ahead of the SPA catch-all so ``/a2a/*`` is not
    swallowed by the ``/{full_path:path}`` fallback.  The A2A app's lifespan
    (currently a no-op) is merged into the host's so it runs if one is added.
    """

    def __init__(self, sub_app, prefix: str = '/a2a'):
        self.sub_app = sub_app
        self.prefix = prefix
        self._lifespan = sub_app.router.lifespan_context(sub_app)

    @asynccontextmanager
    async def lifespan(self):
        async with self._lifespan:
            yield

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if path == self.prefix:
            stripped = '/'
        elif path.startswith(self.prefix + '/'):
            stripped = path[len(self.prefix):]
        else:
            stripped = path
        child_scope = dict(scope)
        child_scope['path'] = stripped
        child_scope['root_path'] = ''
        await self.sub_app(child_scope, receive, send)

    def install(self, host_app, path: str = '/a2a'):
        host_app.router.routes.insert(
            0, Route(path, endpoint=self, methods=['GET', 'POST', 'OPTIONS'])
        )
        host_app.router.routes.insert(
            0, Route(f'{path}/{{rest:path}}', endpoint=self,
                     methods=['GET', 'POST', 'OPTIONS'])
        )

        previous_lifespan = host_app.router.lifespan_context

        @asynccontextmanager
        async def merged_lifespan(app):
            async with self.lifespan():
                async with previous_lifespan(app):
                    yield

        host_app.router.lifespan_context = merged_lifespan


try:
    _a2a_app = _create_a2a_app()
    _a2a_ready = True
except Exception:  # pragma: no cover - depends on a2a_server.py being importable
    logger.exception('a2a_server.create_a2a_app() failed; /a2a endpoint deferred')
    _a2a_app = None
    _a2a_ready = False

if _a2a_ready:
    A2AMount(_a2a_app).install(app)


@app.get('/.well-known/agent-card.json')
async def a2a_agent_card(request: Request):
    """A2A discovery card at the RFC 8615 well-known path.

    SECURITY (C10): this web-root copy is admin-only — it exposes the A2A
    security scheme (bearer JWT) and the capability surface, so it is no longer
    served to unauthenticated callers. Real A2A clients that need public
    discovery use the mounted A2A app's own ``/a2a/.well-known/agent-card.json``
    (which stays public for the protocol); this duplicate is fetched on demand
    by an authenticated operator.
    """
    user = get_current_user_from_request(request)
    if not user:
        return JSONResponse(status_code=401, content={'detail': '未登录'})
    if user.get('role') != 'admin':
        return JSONResponse(status_code=403, content={'detail': 'Forbidden'})
    return JSONResponse(content=_a2a_build_agent_card())


@app.post('/api/a2a/token')
async def api_a2a_token(request: Request, _admin=Depends(require_role("admin"))):
    """Issue a short-lived (1h) A2A bearer token for the mounted /a2a endpoint.

    The returned token is used as ``Authorization: Bearer <token>`` against
    ``/a2a`` (``aud=metano-a2a``, ``scope=[a2a:task]``).
    """
    username = _admin['username']
    ip = request.client.host if request.client else 'unknown'
    ttl = 3600
    token = _create_a2a_token(username, scope=['a2a:task'], ttl_seconds=ttl)
    _audit('a2a_token_issued', username, {'ip': ip, 'scope': ['a2a:task'], 'expires_in': ttl})
    return {'token': token, 'expires_in': ttl}


if WEB_DIR.exists():
    app.mount('/assets', StaticFiles(directory=str(WEB_DIR / 'assets')), name='assets')

    @app.get('/{full_path:path}')
    def serve_spa(full_path: str):
        # SECURITY: never serve files outside WEB_DIR. URL path segments can
        # contain ".." (e.g. /../../etc/hostname) and would let an unauthenticated
        # caller read arbitrary files (bridge.db, gateway_config.yaml, …).
        # Resolve and verify containment before returning any file.
        try:
            web_root = WEB_DIR.resolve()
            candidate = (web_root / full_path).resolve()
            if candidate.is_relative_to(web_root) and candidate.is_file():
                return FileResponse(str(candidate))
        except (ValueError, OSError):
            pass
        return FileResponse(str(web_root / 'index.html'))