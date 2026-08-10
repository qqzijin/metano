"""FastAPI web dashboard for metano."""
import json
import sqlite3
import time
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, Request, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from .auth import authenticate_user, check_login_rate, record_login_attempt, set_auth_cookies, clear_auth_cookies, get_current_user_from_request, try_refresh_from_request, decode_token, change_password, AUTH_WHITELIST, ACCESS_TOKEN_EXPIRE_MINUTES, _audit, require_role
from .db import get_db, init_db, DB_PATH
from .indexer import index_all
CRON_DIR = Path.home() / '.claude' / 'metano' / 'cron'
CRON_JOBS_FILE = CRON_DIR / 'jobs.json'
WEB_DIR = Path(__file__).parent.parent / 'web' / 'dist'
CONFIG_PATH = Path.home() / '.claude' / 'metano' / 'gateway_config.yaml'
EVO_LOG = Path.home() / '.claude' / 'metano' / 'evolution' / 'evolution_log.jsonl'
AUDIT_LOG = Path.home() / '.claude' / 'metano' / 'security' / 'audit.jsonl'
SENSITIVE_KEYS = {'api_key', 'bot_token', 'app_secret', 'encryption_key', 'verification_token', 'token', 'secret', 'password', 'ha_token'}
# gateway_config.yaml 中所有 SENSITIVE_KEYS 字段在 GET /api/config 返回时自动脱敏（***）
# 文件受 ~/.claude/metano/ 目录文件系统权限保护
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
        ('evo', Path.home() / '.claude' / 'metano' / 'evo.db', ['agent_rules', 'action_log', 'evolution_meta', 'architecture_snapshots']),
        ('honcho', Path.home() / '.claude' / 'metano' / 'honcho_data' / 'honcho.db', ['users', 'beliefs', 'observations']),
        ('knowledge', Path.home() / '.claude' / 'metano' / 'knowledge' / 'knowledge.db', ['documents', 'chunks']),
        ('memory', Path.home() / '.claude' / 'metano' / 'memory.db', ['memories']),
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

@app.get('/api/sessions/search')
def search_sessions(q: str=Query(...), limit: int=20, offset: int=0):
    conn = get_db()
    try:
        total = conn.execute('SELECT COUNT(*) as c FROM messages_fts WHERE messages_fts MATCH ?', (q,)).fetchone()['c']
        rows = conn.execute("SELECT m.session_id, m.role, snippet(messages_fts, -1, '<mark>', '</mark>', '...', 30) as snippet, m.timestamp, s.title FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid JOIN sessions s ON s.id = m.session_id WHERE messages_fts MATCH ? ORDER BY m.timestamp DESC LIMIT ? OFFSET ?", (q, limit, offset)).fetchall()
        if total or rows:
            return {'query': q, 'results': [dict(r) for r in rows], 'total': total}
    except Exception:
        logger.exception()
    rows = conn.execute('SELECT m.session_id, m.role, substr(m.content, 1, 300) as snippet, m.timestamp, s.title FROM messages m JOIN sessions s ON s.id = m.session_id WHERE m.content LIKE ? ORDER BY m.timestamp DESC LIMIT ? OFFSET ?', (f'%{q}%', limit, offset)).fetchall()
    total = conn.execute('SELECT COUNT(*) as c FROM messages m JOIN sessions s ON s.id = m.session_id WHERE m.content LIKE ?', (f'%{q}%',)).fetchone()['c']
    return {'query': q, 'results': [dict(r) for r in rows], 'total': total}

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
    conn = get_db()
    cutoff = time.time() - days * 86400
    total = conn.execute('SELECT COUNT(*) as session_count, SUM(message_count) as message_count, SUM(tool_call_count) as tool_call_count, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, SUM(cache_read_tokens) as cache_read_tokens, SUM(estimated_cost_usd) as estimated_cost_usd FROM sessions WHERE last_active >= ?', (cutoff,)).fetchone()
    by_model = conn.execute('SELECT model, COUNT(*) as session_count, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, SUM(estimated_cost_usd) as estimated_cost_usd FROM sessions WHERE last_active >= ? GROUP BY model', (cutoff,)).fetchall()
    daily = conn.execute("SELECT date(last_active, 'unixepoch') as day, COUNT(*) as session_count, COALESCE(SUM(input_tokens),0) as input_tokens, COALESCE(SUM(output_tokens),0) as output_tokens, COALESCE(SUM(estimated_cost_usd),0) as estimated_cost_usd FROM sessions WHERE last_active >= ? GROUP BY day ORDER BY day", (cutoff,)).fetchall()
    return {'period_days': days, 'total': dict(total) if total else {}, 'by_model': [dict(r) for r in by_model], 'daily': [dict(r) for r in daily]}

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

@app.post('/api/chat')
async def api_chat(body: dict):
    msg = body.get('message', '')
    if not isinstance(msg, str) or not msg.strip():
        raise HTTPException(status_code=400, detail='message 不能为空')
    try:
        from .gateway.router import router
        user_id = body.get('user_id', 'web_user')
        platform = body.get('platform', 'web')
        session_id = body.get('session_id', '')
        context = body.get('context', [])
        if session_id:
            _inject_session_context(router, platform, user_id, session_id)
        elif context and isinstance(context, list):
            router.inject_history(platform, user_id, context)
        response = await router.route_message(platform, user_id, msg)
        session_id = _persist_chat(session_id, user_id, platform, msg, response)
        return {'response': response, 'session_id': session_id}
    except Exception as e:
        logger.exception()
        return _error_response('Internal error')


def _persist_chat(session_id: str, user_id: str, platform: str, msg: str, response: str) -> str:
    """Persist one web chat exchange (user message + assistant reply) into bridge.db.

    Best-effort: any failure is logged and must never break the chat response path.
    When no session_id is given, a new session is created. Returns the session_id
    used (existing or newly created) so the caller can return it to the frontend.
    """
    try:
        import uuid
        from .model_router import model_router
        conn = get_db()
        now = time.time()
        model = None
        try:
            model = model_router.get_provider().model or None
        except Exception:
            logger.exception('model lookup failed')
        if not session_id:
            session_id = uuid.uuid4().hex[:12]
            conn.execute(
                'INSERT INTO sessions (id, project, title, model, started_at, last_active, message_count, tool_call_count, input_tokens, output_tokens, cache_read_tokens, estimated_cost_usd) '
                'VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0)',
                (session_id, 'web', (msg or '')[:30], model, now, now)
            )
        ts = now
        conn.execute(
            'INSERT INTO messages (session_id, role, content, tool_name, tool_calls, timestamp, input_tokens, output_tokens, duration_ms) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (session_id, 'user', msg, None, None, ts, len(msg or '') // 4, 0, None)
        )
        conn.execute(
            'INSERT INTO messages (session_id, role, content, tool_name, tool_calls, timestamp, input_tokens, output_tokens, duration_ms) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (session_id, 'assistant', response, None, None, ts + 0.0001, 0, len(response or '') // 4, None)
        )
        conn.execute(
            'UPDATE sessions SET message_count = message_count + 2, last_active = ?, input_tokens = input_tokens + ?, output_tokens = output_tokens + ?, model = COALESCE(?, model) WHERE id = ?',
            (now, len(msg or '') // 4, len(response or '') // 4, model, session_id)
        )
        conn.commit()
    except Exception:
        logger.exception('chat persistence failed')
    return session_id


def _inject_session_context(router, platform: str, user_id: str, session_id: str):
    """Load messages from bridge.db session and inject into router session."""
    try:
        from .db import get_db
        conn = get_db()
        rows = conn.execute(
            'SELECT role, content FROM messages WHERE session_id = ? ORDER BY timestamp ASC LIMIT 20',
            (session_id,)
        ).fetchall()
        if rows:
            history = [{'role': r[0], 'content': r[1]} for r in rows if r[0] in ('user', 'assistant')]
            router.inject_history(platform, user_id, history)
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

if WEB_DIR.exists():
    app.mount('/assets', StaticFiles(directory=str(WEB_DIR / 'assets')), name='assets')

    @app.get('/{full_path:path}')
    def serve_spa(full_path: str):
        file_path = WEB_DIR / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(WEB_DIR / 'index.html'))