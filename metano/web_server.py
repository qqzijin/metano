"""FastAPI web dashboard for metano."""
import json
import sqlite3
import time
import uuid
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, Request, Response, Depends, UploadFile, File
from fastapi.responses import StreamingResponse
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from .auth import authenticate_user, check_login_rate, record_login_attempt, set_auth_cookies, clear_auth_cookies, get_current_user_from_request, try_refresh_from_request, decode_token, change_password, AUTH_WHITELIST, ACCESS_TOKEN_EXPIRE_MINUTES, _audit, require_role
from .db import get_db, init_db, DB_PATH
from .indexer import index_all
from .paths import CRON_DIR, CRON_JOBS_FILE, CONFIG_PATH, EVO_LOG, AUDIT_LOG, EVO_DB_PATH, HONCHO_DB, KB_DB, MEMORY_DB, UPLOADS_DIR
from . import collab as collab
WEB_DIR = Path(__file__).parent.parent / 'web' / 'dist'
SENSITIVE_KEYS = {'api_key', 'bot_token', 'app_secret', 'encryption_key', 'verification_token', 'token', 'secret', 'password', 'ha_token'}
# gateway_config.yaml 中所有 SENSITIVE_KEYS 字段在 GET /api/config 返回时自动脱敏（***）
# 文件受 METANO_HOME 目录文件系统权限保护
app = FastAPI(title='metano')
app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:5173', 'http://localhost:9120'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])

class AuthMiddleware(BaseHTTPMiddleware):

    ALLOWED_ORIGINS = {'http://localhost:5173', 'http://localhost:9120'}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # CSRF protection: for mutating requests, verify Origin/Referer matches allowed origins
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH') and path.startswith('/api/'):
            origin = request.headers.get('origin', '')
            referer = request.headers.get('referer', '')
            source = origin or referer
            if source:
                matched = any(allowed in source for allowed in self.ALLOWED_ORIGINS)
                # Also allow same-host access (Origin matches request host)
                if not matched:
                    host = request.headers.get('host', '')
                    if host and host in source:
                        matched = True
                if not matched:
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
                response.set_cookie('access_token', new_access, max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60, httponly=True, samesite='lax', path='/')
                return response
            return JSONResponse(status_code=401, content={'detail': '未登录'})
        return await call_next(request)
from metano.log import logger
app.add_middleware(AuthMiddleware)

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
    new_access = try_refresh_from_request(request)
    if not new_access:
        raise HTTPException(status_code=401, detail='请重新登录')
    token = request.cookies.get('refresh_token')
    payload = decode_token(token)
    if payload:
        response.set_cookie('access_token', new_access, max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60, httponly=True, samesite='lax', path='/')
        return {'username': payload['sub'], 'role': payload.get('role', 'user')}
    raise HTTPException(status_code=401, detail='请重新登录')

@app.post('/api/auth/logout')
async def auth_logout(response: Response):
    clear_auth_cookies(response)
    return {'status': 'logged_out'}

@app.get('/api/auth/me')
async def auth_me(request: Request):
    user = get_current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail='未登录')
    return user

@app.post('/api/auth/change-password')
async def auth_change_password(request: Request):
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
    return {'status': 'password_changed'}

def _error_response(message: str, status_code: int = 500, extra: dict | None = None) -> JSONResponse:
    """Standard error envelope for API responses."""
    content = {'success': False, 'error': {'message': message}}
    if extra:
        content.update(extra)
    return JSONResponse(content=content, status_code=status_code)


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
    try:
        from .evo_models import get_cron_jobs, init_db as init_evo_db
        init_evo_db()
        return get_cron_jobs()
    except Exception:
        CRON_DIR.mkdir(parents=True, exist_ok=True)
        if CRON_JOBS_FILE.exists():
            data = json.loads(CRON_JOBS_FILE.read_text())
            if isinstance(data, dict):
                data = data.get('jobs', [])
            return [_normalize_cron_job(j, i) for i, j in enumerate(data)]
        return []

def _save_cron_jobs(jobs: list[dict]):
    try:
        from .evo_models import init_db as init_evo_db, _get_conn
        init_evo_db()
        conn = _get_conn()
        conn.execute("DELETE FROM cron_jobs")
        for j in jobs:
            schedule = j.get('schedule', {})
            kind = schedule.get('kind', 'cron') if isinstance(schedule, dict) else 'cron'
            expr = schedule.get('expr', '0 0 * * *') if isinstance(schedule, dict) else str(schedule)
            conn.execute(
                "INSERT INTO cron_jobs (id, name, action, schedule_kind, schedule_expr, "
                "enabled, prompt, last_run_at, next_run_at, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (j.get('id', ''), j.get('name', ''), j.get('action', ''), kind, expr,
                 1 if j.get('enabled', True) else 0, j.get('prompt', ''),
                 j.get('last_run_at'), j.get('next_run_at'), j.get('last_error'))
            )
        conn.commit()
        conn.close()
    except Exception:
        CRON_DIR.mkdir(parents=True, exist_ok=True)
        CRON_JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))

@app.get('/health')
def health_check():
    """Health check: verify all databases and tables are accessible."""
    checks = {}
    db_specs = [
        ('bridge', DB_PATH, ['sessions', 'messages']),
        ('evo', EVO_DB_PATH, ['agent_rules', 'action_log', 'evolution_meta', 'architecture_snapshots']),
        ('honcho', HONCHO_DB, ['users', 'beliefs', 'observations']),
        ('knowledge', KB_DB, ['documents', 'chunks']),
        ('memory', MEMORY_DB, ['memories']),
    ]
    all_ok = True
    for name, path, expected_tables in db_specs:
        if not path.exists():
            checks[name] = {'status': 'missing', 'path': str(path)}
            all_ok = False
            continue
        try:
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            tables = {r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            missing = set(expected_tables) - tables
            conn.close()
            if missing:
                checks[name] = {'status': 'degraded', 'missing_tables': list(missing)}
                all_ok = False
            else:
                checks[name] = {'status': 'ok'}
        except Exception as e:
            checks[name] = {'status': 'error', 'error': str(e)}
            all_ok = False
    return {'status': 'ok' if all_ok else 'degraded', 'databases': checks}

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
def list_sessions(limit: int=20, offset: int=0, search: str=''):
    conn = get_db()
    if search:
        rows = conn.execute('SELECT id, title, model, message_count, tool_call_count, input_tokens, output_tokens, estimated_cost_usd, started_at, last_active FROM sessions WHERE title LIKE ? ORDER BY last_active DESC LIMIT ? OFFSET ?', (f'%{search}%', limit, offset)).fetchall()
    else:
        rows = conn.execute('SELECT id, title, model, message_count, tool_call_count, input_tokens, output_tokens, estimated_cost_usd, started_at, last_active FROM sessions ORDER BY last_active DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
    total = conn.execute('SELECT COUNT(*) as c FROM sessions').fetchone()['c']
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
def search_sessions(q: str=Query(...), limit: int=20, offset: int=0):
    conn = get_db()
    try:
        pattern = f'%{q}%'
        total = conn.execute('SELECT COUNT(*) as c FROM messages WHERE content LIKE ?', (pattern,)).fetchone()['c']
        rows = conn.execute('SELECT m.session_id, m.role, m.content AS raw, m.timestamp, s.title FROM messages m JOIN sessions s ON s.id = m.session_id WHERE m.content LIKE ? ORDER BY m.timestamp DESC LIMIT ? OFFSET ?', (pattern, limit, offset)).fetchall()
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
def global_search(q: str=Query(...), limit: int=20, offset: int=0):
    """Alias for /api/sessions/search."""
    return search_sessions(q=q, limit=limit, offset=offset)

@app.get('/api/sessions/{session_id}')
def get_session(session_id: str):
    conn = get_db()
    row = conn.execute('SELECT id, title, model, message_count, tool_call_count, input_tokens, output_tokens, estimated_cost_usd, started_at, last_active FROM sessions WHERE id = ?', (session_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail='Session not found')
    return dict(row)

@app.get('/api/sessions/{session_id}/messages')
def get_session_messages(session_id: str, limit: int=200, offset: int=0):
    conn = get_db()
    rows = conn.execute('SELECT id, role, content, tool_name, tool_calls, timestamp, input_tokens, output_tokens, duration_ms FROM messages WHERE session_id = ? ORDER BY timestamp ASC LIMIT ? OFFSET ?', (session_id, limit, offset)).fetchall()
    total = conn.execute('SELECT COUNT(*) as c FROM messages WHERE session_id = ?', (session_id,)).fetchone()['c']
    return {'items': [dict(r) for r in rows], 'total': total}

@app.get('/api/analytics/usage')
@app.get('/api/analytics')
def analytics_usage(days: int=30):
    """统计总览。口径分离：「单次对话 token」与「每日总用量」互不混淆。

    - ``daily``：**每日总用量** —— 按消息实际发生日聚合，跨日会话的 in/out token
      按消息时间戳拆到各自发生日；费用按该会话各日 in/out token 占比分摊
      ``estimated_cost_usd``（缓存 token 无消息级明细，按占比近似分摊）。
    - ``total`` / ``by_model`` / ``by_project``：in/out 与 ``daily`` 同口径（消息级，
      只统计窗口内实际发生的请求），保证加总永远一致；缓存 token 与费用没有消息级
      明细，按「last_active 落在窗口内」的会话汇总。
    - ``sessions``：**单次对话** token 排行 —— 每条会话的输入/输出/缓存 token 与费用。
    """
    conn = get_db()
    cutoff = time.time() - days * 86400
    daily = _analytics_daily(conn, cutoff)
    # in/out：消息级（与 daily 同口径，跨窗口长会话只统计窗口内发生的请求）
    msg = conn.execute(
        'SELECT COUNT(DISTINCT session_id) as session_count, COUNT(*) as message_count, '
        'SUM(tool_name IS NOT NULL) as tool_call_count, '
        'COALESCE(SUM(input_tokens),0) as input_tokens, COALESCE(SUM(output_tokens),0) as output_tokens '
        'FROM messages WHERE timestamp >= ?',
        (cutoff,)
    ).fetchone()
    by_model = conn.execute(
        'SELECT COALESCE(s.model, \'<unknown>\') as model, COUNT(DISTINCT m.session_id) as session_count, '
        'COALESCE(SUM(m.input_tokens),0) as input_tokens, COALESCE(SUM(m.output_tokens),0) as output_tokens '
        'FROM messages m LEFT JOIN sessions s ON s.id = m.session_id WHERE m.timestamp >= ? '
        'GROUP BY s.model ORDER BY SUM(m.input_tokens) DESC',
        (cutoff,)
    ).fetchall()
    by_project = conn.execute(
        'SELECT COALESCE(s.project, \'<unknown>\') as project, COUNT(DISTINCT m.session_id) as session_count, '
        'COALESCE(SUM(m.input_tokens),0) as input_tokens, COALESCE(SUM(m.output_tokens),0) as output_tokens '
        'FROM messages m LEFT JOIN sessions s ON s.id = m.session_id WHERE m.timestamp >= ? '
        'GROUP BY s.project ORDER BY SUM(m.input_tokens) DESC',
        (cutoff,)
    ).fetchall()
    # 缓存 token / 费用：会话级（消息表无缓存明细）
    sess = conn.execute(
        'SELECT COALESCE(SUM(cache_read_tokens),0) as cache_read_tokens, '
        'COALESCE(SUM(estimated_cost_usd),0) as estimated_cost_usd '
        'FROM sessions WHERE last_active >= ?',
        (cutoff,)
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
        'FROM sessions WHERE last_active >= ? AND model IS NOT NULL GROUP BY model',
        (cutoff,)
    ).fetchall()
    sess_by_project = conn.execute(
        'SELECT project, COALESCE(SUM(cache_read_tokens),0) as cache_read_tokens, '
        'COALESCE(SUM(estimated_cost_usd),0) as estimated_cost_usd '
        'FROM sessions WHERE last_active >= ? AND project IS NOT NULL GROUP BY project',
        (cutoff,)
    ).fetchall()
    by_model = _merge_sess_agg(by_model, {r['model']: r for r in sess_by_model}, 'model')
    by_project = _merge_sess_agg(by_project, {r['project']: r for r in sess_by_project}, 'project')
    # 单次对话排行（会话级全量，含缓存）
    sessions = conn.execute('SELECT id, title, project, model, message_count, tool_call_count, input_tokens, output_tokens, cache_read_tokens, estimated_cost_usd, started_at, last_active FROM sessions WHERE last_active >= ? ORDER BY (input_tokens + output_tokens + cache_read_tokens) DESC LIMIT 20', (cutoff,)).fetchall()
    return {'period_days': days, 'total': total, 'by_model': [dict(r) for r in by_model], 'by_project': [dict(r) for r in by_project], 'daily': daily, 'sessions': [dict(r) for r in sessions]}


def _analytics_daily(conn, cutoff: float) -> list[dict]:
    """每日总用量（消息级，按实际发生日聚合）。"""
    daily: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT date(m.timestamp, 'unixepoch', 'localtime') as day, "
        "COUNT(DISTINCT m.session_id) as session_count, "
        "COALESCE(SUM(m.input_tokens),0) as input_tokens, "
        "COALESCE(SUM(m.output_tokens),0) as output_tokens "
        "FROM messages m WHERE m.timestamp >= ? GROUP BY day ORDER BY day",
        (cutoff,)
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
        "WHERE m.timestamp >= ? AND COALESCE(s.estimated_cost_usd, 0) > 0 "
        "GROUP BY s.id, day",
        (cutoff,)
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

@app.get('/api/skills/{name}')
async def api_skill_detail(name: str):
    from .skills.loader import SkillLoader
    loader = SkillLoader()
    rec = loader.find_by_name(name)
    if not rec:
        return _error_response('Not found', status_code=404)
    return {'name': rec.name, 'description': rec.description, 'trigger': rec.trigger, 'category': rec.category, 'content': rec.body, 'source': rec.source}

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

@app.get('/api/evolution/suggestions')
async def api_evolution_suggestions():
    try:
        from .adapter import load_suggestions
        return {'items': load_suggestions()}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'items': []})

@app.post('/api/evolution/approve/{suggestion_id}')
async def api_evolution_approve(suggestion_id: str, _admin=Depends(require_role("admin"))):
    try:
        from .adapter import approve_suggestion
        result = approve_suggestion(suggestion_id)
        if result:
            return {'status': 'approved', 'suggestion': result}
        return _error_response('suggestion not found', status_code=404)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/evolution/reject/{suggestion_id}')
async def api_evolution_reject(suggestion_id: str, _admin=Depends(require_role("admin"))):
    try:
        from .adapter import reject_suggestion
        result = reject_suggestion(suggestion_id)
        if result:
            return {'status': 'rejected', 'suggestion': result}
        return _error_response('suggestion not found', status_code=404)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

# ── Proposal CRUD API (replaces old suggestion endpoints) ──

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
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(yaml.dump(merged, allow_unicode=True, default_flow_style=False))
        return {'status': 'saved'}
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
async def api_profile(user_id: str='default'):
    try:
        from .honcho.models import init_honcho_db, get_profile, get_user, create_user
        conn = init_honcho_db()
        if not get_user(conn, user_id):
            create_user(conn, user_id=user_id)
        return get_profile(conn, user_id)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/upload')
async def api_upload(file: UploadFile = File(...), _user=Depends(require_role("user"))):
    """Upload a file for the AI to read in chat. Saved to UPLOADS_DIR."""
    ALLOWED_EXT = {'.txt', '.md', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.csv', '.json', '.py', '.js', '.ts', '.html', '.docx'}
    MAX_SIZE = 20 * 1024 * 1024
    filename = file.filename or 'upload'
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f'不支持的文件类型: {ext or "(无扩展名)"}')
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(status_code=400, detail='文件过大（上限 20MB）')
    dest = UPLOADS_DIR / f'{uuid.uuid4().hex[:8]}{ext}'
    dest.write_bytes(content)
    return {'path': str(dest), 'name': filename, 'size': len(content)}

@app.post('/api/chat')
async def api_chat(body: dict):
    msg = body.get('message', '')
    if not isinstance(msg, str) or not msg.strip():
        raise HTTPException(status_code=400, detail='message 不能为空')
    from .gateway.router import router
    user_id = body.get('user_id', 'web_user')
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
    """Load messages from bridge.db session and inject into router session."""
    try:
        from .db import get_db
        conn = get_db()
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

@app.post('/api/knowledge/semantic-search')
async def api_knowledge_semantic_search(body: dict):
    try:
        from .knowledge import knowledge_semantic_search
        return knowledge_semantic_search(body.get('query', ''), project=body.get('project', ''), limit=body.get('limit', 5))
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/knowledge/explore')
async def api_knowledge_explore(body: dict):
    try:
        from .knowledge_explorer import explore_domain
        return explore_domain(body.get('topic', ''), depth=body.get('depth', 3))
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/knowledge/gaps')
async def api_knowledge_gaps():
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
        return knowledge_ingest(body.get('path', ''), title=body.get('title', ''))
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
    jobs = _load_cron_jobs()
    for j in jobs:
        if j['id'] == job_id:
            j['last_run_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ')
            j['last_error'] = None
    _save_cron_jobs(jobs)
    return {'triggered': job_id}

@app.put('/api/models/{name}/default')
async def api_model_set_default(name: str):
    try:
        from .model_router import model_router
        model_router.set_default(name)
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
async def api_browser_browse(body: dict):
    try:
        from .browser import web_browse
        result = web_browse(body.get('url', ''))
        return result
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/browser/search')
async def api_browser_search(body: dict):
    try:
        from .browser import web_search
        result = web_search(body.get('query', ''))
        return result
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/voice/file')
async def get_voice_file(path: str):
    import os
    from .voice.core import AUDIO_DIR
    voice_dir = os.environ.get('VOICE_OUTPUT_DIR', str(AUDIO_DIR))
    safe_path = os.path.normpath(os.path.join(voice_dir, os.path.basename(path)))
    if not safe_path.startswith(voice_dir) or not os.path.exists(safe_path):
        raise HTTPException(status_code=404, detail='File not found')
    return FileResponse(safe_path, media_type='audio/mpeg', filename=os.path.basename(safe_path))

@app.post('/api/voice/tts')
async def api_voice_tts(body: dict):
    try:
        from .voice import voice_speak
        return voice_speak(body.get('text', ''), voice=body.get('voice', 'zh-CN-YunxiNeural'), rate=body.get('rate', '+0%'))
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/voice/voices')
async def api_voice_voices(language: str=''):
    try:
        from .voice import voice_list_voices
        return voice_list_voices(language)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/home/status')
async def api_home_status():
    try:
        from .home_assistant import home_status_full
        return home_status_full()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'entities': [], 'configured': False})

@app.get('/api/home/config')
async def api_home_config_get():
    try:
        from .home_assistant import ha_get_config
        return ha_get_config()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={})

@app.post('/api/home/config')
async def api_home_config_set(body: dict):
    try:
        from .home_assistant import ha_set_config
        return ha_set_config((body.get('url') or '').strip(), (body.get('token') or '').strip())
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={})

@app.get('/api/home/status/{entity_id}')
async def api_home_entity(entity_id: str):
    try:
        from .home_assistant import get_entity_state
        return get_entity_state(entity_id)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/home/control')
async def api_home_control(body: dict):
    try:
        from .home_assistant import home_control
        return home_control(body.get('entity_id', ''), body.get('service', 'toggle'))
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
        result = collab.execute_task(task_id, timeout=timeout)
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

@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        auth_msg = await ws.receive_text()
        auth_data = json.loads(auth_msg)
        token = auth_data.get('token', '')
        if not token or not decode_token(token):
            await ws.send_json({'error': 'Authentication required'})
            await ws.close(code=4001)
            return
    except Exception:
        await ws.close(code=4001)
        return

    _ws_clients.append(ws)
    try:
        import asyncio
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception()
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)

async def ws_broadcast(data: dict):
    for client in _ws_clients:
        try:
            await client.send_json(data)
        except Exception:
            logger.exception()

@app.post('/api/search/web')
async def api_web_search(body: dict):
    try:
        from .mcp_bridge import tavily_search
        return await tavily_search(body.get('query', ''), limit=body.get('limit', 10))
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
async def api_proxy_providers():
    try:
        from .model_router import ModelRouter
        return {'providers': ModelRouter.free_provider_presets()}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'providers': []})

@app.post('/api/proxy/add')
async def api_proxy_add(body: dict, _admin=Depends(require_role("admin"))):
    try:
        import yaml
        name = body.get('name', '')
        if not name:
            raise HTTPException(status_code=400, detail='name required')
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                existing = yaml.safe_load(f) or {}
        models = existing.get('models', {})
        models[name] = {'base_url': body.get('base_url', ''), 'api_key': body.get('api_key', ''), 'model': body.get('model', ''), 'max_tokens': body.get('max_tokens', 4096), 'supports_vision': body.get('supports_vision', False), 'supports_tools': body.get('supports_tools', True), 'enabled': True}
        price = body.get('price')
        if isinstance(price, dict):
            models[name]['price'] = {k: price[k] for k in ('input', 'output', 'cache_read') if k in price}
        existing['models'] = models
        CONFIG_PATH.write_text(yaml.dump(existing, allow_unicode=True, default_flow_style=False))
        # Refresh the shared module-level ModelRouter singleton so the newly
        # added provider becomes visible to /api/models, chat and evolution
        # immediately (previously a throwaway instance was created and never used).
        from .model_router import model_router
        model_router.refresh()
        return {'status': 'added', 'provider': name}
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
        for field in ('base_url', 'api_key', 'model', 'max_tokens', 'supports_vision', 'supports_tools', 'enabled'):
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
        existing['models'] = models
        CONFIG_PATH.write_text(yaml.dump(existing, allow_unicode=True, default_flow_style=False))
        from .model_router import model_router
        model_router.refresh()
        return {'status': 'updated', 'provider': name}
    except Exception as e:
        logger.exception()
        raise HTTPException(status_code=400, detail=str(e))

@app.get('/api/memory/stats')
async def api_memory_stats():
    try:
        from .memory import get_memory_stats
        return get_memory_stats()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/memory/search')
async def api_memory_search(q: str=Query(...), limit: int=10, tag: str=''):
    try:
        from .memory import search_memories
        return search_memories(q, limit=limit, tag=tag or None)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'results': []})

@app.get('/api/memory/timeline')
async def api_memory_timeline(days: int=7, limit: int=20):
    try:
        from .honcho.models import init_honcho_db, get_user, create_user
        from datetime import datetime, timezone
        import time as _time
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
async def api_memory_detail(category: str='', belief_id: str=''):
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

@app.post('/api/memory/compress')
async def api_memory_compress():
    try:
        from .memory import compress_memories
        return compress_memories()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/memory/export')
async def api_memory_export():
    try:
        from .memory import export_memories
        return export_memories()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/memory/import')
async def api_memory_import(body: dict):
    try:
        from .memory import import_memories
        return import_memories(body, merge=body.get('merge', True))
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/memory/seed')
async def api_memory_seed():
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
async def api_evolution_behaviors():
    """Return agent behavior rules from evo.db and recent correction records."""
    try:
        from .behavior_analyzer import get_behavior_patterns
        return get_behavior_patterns()
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'patterns': [], 'recent_corrections': []})

@app.get('/api/evolution/rules')
async def api_evolution_rules():
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
async def api_evolution_action_log(limit: int=50):
    """Recent action log entries."""
    try:
        from .evo_models import get_recent_actions, init_db
        init_db()
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
    """Approve a behavior_improvement suggestion and write to Claude Code memory."""
    try:
        from .adapter import apply_behavior_improvement
        result = apply_behavior_improvement(suggestion_id)
        if result:
            return result
        return _error_response('suggestion not found or not behavior_improvement type', status_code=404)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/evolution/behavior-reject/{suggestion_id}')
async def api_evolution_behavior_reject(suggestion_id: str, _admin=Depends(require_role("admin"))):
    """Reject a behavior_improvement suggestion."""
    try:
        from .adapter import reject_suggestion
        result = reject_suggestion(suggestion_id)
        if result:
            return {'status': 'rejected', 'suggestion': result}
        return _error_response('suggestion not found', status_code=404)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/browser/screenshot')
async def api_browser_screenshot(body: dict):
    try:
        import base64 as _b64
        import os as _os
        from .browser import web_screenshot
        result = web_screenshot(body.get('url', ''), full_page=body.get('full_page', True))
        if result.get('status') == 'ok' and result.get('path'):
            path = result['path']
            if _os.path.isfile(path):
                try:
                    with open(path, 'rb') as f:
                        result['image'] = 'data:image/png;base64,' + _b64.b64encode(f.read()).decode()
                except Exception:
                    logger.exception('failed to read screenshot file for base64')
        return result
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.get('/api/evolution/strategy')
async def api_evolution_strategy(context: str=''):
    """Select optimal strategy rules for a given context."""
    try:
        from .strategy import select_strategy
        strategies = select_strategy(context=context)
        return {'strategies': strategies}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'strategies': []})

@app.get('/api/evolution/effectiveness/{rule_id}')
async def api_evolution_effectiveness(rule_id: str):
    """Get effectiveness metrics for a specific rule."""
    try:
        from .strategy import get_effectiveness
        return get_effectiveness(rule_id)
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')

@app.post('/api/evolution/strategy-detect')
async def api_evolution_strategy_detect():
    """Detect strategy patterns from action log."""
    try:
        from .strategy import detect_strategy_patterns
        patterns = detect_strategy_patterns()
        return {'patterns': patterns}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error', extra={'patterns': []})

@app.get('/api/evolution/architecture')
async def api_evolution_architecture():
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
async def a2a_agent_card():
    """A2A discovery card at the RFC 8615 well-known path (public, no login)."""
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
        file_path = WEB_DIR / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(WEB_DIR / 'index.html'))