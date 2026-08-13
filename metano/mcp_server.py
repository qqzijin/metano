"""MCP stdio server exposing session search, analytics, and cron tools."""
import json
import os
import re
import sqlite3
import time
import functools
from pathlib import Path
from typing import Optional
from mcp.server.fastmcp import FastMCP
from .db import get_db, init_db, DB_PATH
from .indexer import index_all
from .curator import run_curator
from .x_search import search_x
from .honcho.models import init_honcho_db, get_honcho_db, create_user, get_user, get_profile, add_observation, get_observations, get_beliefs, delete_belief
from .honcho.dialectic import dialectic_reason, extract_observations, compress_beliefs
from .voice.tts import speak as tts_speak, list_voices
from .evolution import evolution_status as _evolution_status, evolution_run as _evolution_run, evolution_revert as _evolution_revert, evolution_pause as _evolution_pause, evolution_resume as _evolution_resume
from .adapter import load_suggestions, approve_suggestion, reject_suggestion
from .strategy import record_action, record_outcome

def track_action(action_type: str):
    """Decorator to track MCP tool calls for strategy optimization."""

    def decorator(func):

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            action_id = record_action(session_id='', action_type=action_type, action_detail=f"{func.__name__}({', '.join((str(a)[:50] for a in args))})")
            try:
                result = func(*args, **kwargs)
                record_outcome(action_id, outcome='success')
                return result
            except Exception as e:
                logger.exception()
                record_outcome(action_id, outcome='failure', detail=str(e))
                raise
        return wrapper
    return decorator
from .skills.loader import SkillLoader
from .skills.manager import SkillManager
from .skills.bundles import BundleLoader
from .browser import web_browse as _web_browse_sync, web_screenshot as _web_screenshot_sync
from .code_exec import code_run
from .sub_agent import delegator as _agent_delegator
from .image_gen import image_generate as _image_gen_func, image_describe as _image_desc_func
from .model_router import model_router as _model_router
from .knowledge import knowledge_ingest as _kb_ingest, knowledge_search as _kb_search, knowledge_list as _kb_list, knowledge_delete as _kb_delete
from .voice import voice_speak as _voice_speak, voice_list_voices
from .security import security as _security
from .kanban import kanban_create_board, kanban_add_task, kanban_move_task, kanban_list, kanban_delete_task
from .home_assistant import home_control as _ha_control, home_status as _ha_status, home_automate as _ha_automate
from .memory import add_memory, search_memories, get_memory_stats, compress_memories
from .mcp_bridge import tavily_search
from metano.log import logger
from .paths import PERSONALITIES_DIR, EVO_LOG
_skill_loader = SkillLoader()
_skill_manager = SkillManager()
_bundle_loader = BundleLoader()
CLAUDE_MD = Path.home() / 'CLAUDE.md'
mcp = FastMCP('metano')

def _get_conn() -> sqlite3.Connection:
    return get_db()


# ── Authentication / ownership helpers (H-07) ──────────────────────────────
# The remote Streamable HTTP layer (metano.mcp_gateway.MCPAuthMiddleware)
# captures the authenticated JWT subject + scope in a contextvar before the
# FastMCP app runs; these helpers read it so every data tool can confine its
# query to the caller's own rows.  Local stdio calls have no subject → the
# trusted local operator (full access).
from .mcp_gateway import get_mcp_auth, is_admin_read

_SAFE_BASENAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


def _auth_subject() -> Optional[str]:
    """Authenticated subject of the current MCP request, or None for stdio."""
    return get_mcp_auth()[0]


def _auth_admin_read() -> bool:
    """True when the caller may read instance-wide data.

    Local stdio (no subject) and admin-read scoped tokens return True; a
    user-level remote token returns False.
    """
    subject, scopes = get_mcp_auth()
    return subject is None or is_admin_read(scopes)


def _owner_cond(col: str = 'sessions.user_key') -> tuple[str, list]:
    """Build a SQL ``WHERE`` fragment + params confining rows to the subject.

    Local stdio and admin-read callers see everything (``('', [])``).  A
    user-level remote token is restricted to its own ``user_key`` — the raw
    subject or the ``web:<subject>`` form used by web-login sessions.
    """
    subject, scopes = get_mcp_auth()
    if subject is None or is_admin_read(scopes):
        return '', []
    return f"({col} = ? OR {col} = ?)", [subject, f'web:{subject}']


def _instance_data_denied() -> bool:
    """True when the caller must NOT read instance-wide data.

    The knowledge base, evolution log/suggestions, skill bodies, model config
    and the memory/honcho tables have no per-user column, so a user-level remote
    token is refused rather than leaking the whole instance (audit H7).  Local
    stdio (no subject) and admin-read scoped tokens pass.
    """
    subject, scopes = get_mcp_auth()
    return subject is not None and not is_admin_read(scopes)


def _memory_denied() -> bool:
    """True when the caller must NOT see instance-wide memory data.

    Alias of :func:`_instance_data_denied` (memory tables are instance-wide).
    """
    return _instance_data_denied()


def _cron_denied() -> bool:
    """True when the caller must NOT read or manage cron jobs.

    Cron jobs are instance-wide configuration and ``cron_trigger`` spawns a
    ``claude -p`` subprocess (cost + prompt-injection surface), so user-level
    remote tokens are refused — admin scope required (audit F2).
    """
    return _instance_data_denied()


def _clip(value, lo: int, hi: int, default: int) -> int:
    """Coerce ``value`` to an int inside ``[lo, hi]`` (default on garbage)."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


# ── Gateway capability policy (C-02) ──────────────────────────────────────
# Tools that the gateway allowlist (``/grant``, ``--allowedTools``) is permitted
# to pre-authorize for a non-interactive model.  Everything with a write /
# destructive side effect — or an instance-wide read surface — is excluded: a
# stray ``/grant mcp__metano__home_control`` must never reach the CLI's
# ``--allowedTools`` (which would bypass the safe-mode boundary).
GATEWAY_PREAUTHORIZABLE_TOOLS: frozenset[str] = frozenset({
    # subject-scoped session / analytics reads
    'session_search', 'session_list', 'session_get',
    'analytics_summary', 'analytics_daily',
    # read-only status / log / suggestions
    'evolution_status', 'evolution_log', 'evolution_suggestions',
    # skills / models / web search (no local mutation)
    'skills_list', 'skill_view', 'model_list',
    'web_search', 'web_search_tavily', 'x_search',
    # read-only queries (stdio/gateway = local operator)
    'personality_list', 'personality_current',
    'memory_search', 'memory_stats', 'memory_timeline', 'memory_detail',
    'knowledge_search', 'knowledge_list',
    'honcho_profile', 'honcho_beliefs',
    'voice_list',
})


def is_gateway_preauthorizable(tool: str) -> bool:
    """True when a tool may be pre-authorized via the gateway allowlist.

    Accepts the bare tool name or the ``mcp__metano__<name>`` form used in
    gateway CLI tool strings.  The gateway's ``/grant`` handler MUST call this
    and reject any tool that is not preauthorizable — an unknown string or an
    ``mcp__metano__`` write/destructive tool must never reach ``--allowedTools``.
    """
    name = tool or ''
    if name.startswith('mcp__metano__'):
        name = name[len('mcp__metano__'):]
    return name in GATEWAY_PREAUTHORIZABLE_TOOLS

@mcp.tool()
def session_search(query: str, limit: int=10) -> str:
    """Search Claude Code session messages by substring. Supports Chinese and partial words."""
    limit = _clip(limit, 1, 100, 10)
    cond, params = _owner_cond('s.user_key')
    where = 'WHERE m.content LIKE ?'
    if cond:
        where += f' AND {cond}'
    conn = _get_conn()
    pattern = f'%{query}%'
    rows = conn.execute(
        f'SELECT m.session_id, m.role, substr(m.content, 1, 200) as snippet, m.timestamp, s.title '
        f'FROM messages m JOIN sessions s ON s.id = m.session_id {where} '
        f'ORDER BY m.timestamp DESC LIMIT ?',
        (pattern, *params, limit)).fetchall()
    results = [{'session_id': r['session_id'], 'title': r['title'], 'role': r['role'], 'snippet': r['snippet'], 'timestamp': r['timestamp']} for r in rows]
    return json.dumps(results, ensure_ascii=False, indent=2)

@mcp.tool()
def session_list(limit: int=20, offset: int=0) -> str:
    """List recent Claude Code sessions with titles, token counts, and model info."""
    limit = _clip(limit, 1, 100, 20)
    offset = _clip(offset, 0, 10000, 0)
    cond, params = _owner_cond('sessions.user_key')
    where = f'WHERE {cond}' if cond else ''
    conn = _get_conn()
    rows = conn.execute(
        f'SELECT id, title, model, message_count, tool_call_count, input_tokens, output_tokens, estimated_cost_usd, started_at, last_active '
        f'FROM sessions {where} ORDER BY last_active DESC LIMIT ? OFFSET ?',
        (*params, limit, offset)).fetchall()
    results = [dict(r) for r in rows]
    return json.dumps(results, ensure_ascii=False, indent=2)

@mcp.tool()
def session_get(session_id: str, limit: int=100) -> str:
    """Get messages for a specific session by ID (owner-scoped)."""
    limit = _clip(limit, 1, 500, 100)
    cond, params = _owner_cond('s.user_key')
    where = 'WHERE m.session_id = ?'
    if cond:
        where += f' AND {cond}'
    conn = _get_conn()
    rows = conn.execute(
        f'SELECT m.id, m.role, m.content, m.tool_name, m.timestamp, m.input_tokens, m.output_tokens, m.duration_ms '
        f'FROM messages m JOIN sessions s ON s.id = m.session_id {where} ORDER BY m.timestamp ASC LIMIT ?',
        (session_id, *params, limit)).fetchall()
    results = [dict(r) for r in rows]
    return json.dumps(results, ensure_ascii=False, indent=2)

@mcp.tool()
def analytics_summary(days: int=7) -> str:
    """Aggregate token usage and cost estimates over the last N days.

    Separate per-conversation (``sessions``) from daily totals: ``daily`` is
    message-level (by actual message date), ``sessions`` lists each conversation's
    input/output/cache tokens, ``by_project`` splits usage by channel/project.
    """
    days = _clip(days, 1, 365, 7)
    conn = _get_conn()
    cutoff = time.time() - days * 86400
    cond, params = _owner_cond('sessions.user_key')
    where = 'WHERE last_active >= ?'
    if cond:
        where += f' AND {cond}'
    total = conn.execute(f'SELECT COUNT(*) as session_count, SUM(message_count) as message_count, SUM(tool_call_count) as tool_call_count, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, SUM(cache_read_tokens) as cache_read_tokens, SUM(estimated_cost_usd) as estimated_cost_usd FROM sessions {where}', (cutoff, *params)).fetchone()
    by_model = conn.execute(f'SELECT model, COUNT(*) as session_count, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, SUM(cache_read_tokens) as cache_read_tokens, SUM(estimated_cost_usd) as estimated_cost_usd FROM sessions {where} GROUP BY model', (cutoff, *params)).fetchall()
    by_project = conn.execute(f'SELECT project, COUNT(*) as session_count, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, SUM(cache_read_tokens) as cache_read_tokens, SUM(estimated_cost_usd) as estimated_cost_usd FROM sessions {where} GROUP BY project ORDER BY SUM(input_tokens) DESC', (cutoff, *params)).fetchall()
    sessions = conn.execute(f'SELECT id, title, project, model, message_count, input_tokens, output_tokens, cache_read_tokens, estimated_cost_usd, started_at, last_active FROM sessions {where} ORDER BY (input_tokens + output_tokens + cache_read_tokens) DESC LIMIT 20', (cutoff, *params)).fetchall()
    result = {'period_days': days, 'total': dict(total) if total else {}, 'by_model': [dict(r) for r in by_model], 'by_project': [dict(r) for r in by_project], 'sessions': [dict(r) for r in sessions]}
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def analytics_daily(days: int=30) -> str:
    """Daily token/cost time series (message-level, by actual consumption day)."""
    days = _clip(days, 1, 365, 30)
    conn = _get_conn()
    cutoff = time.time() - days * 86400
    cond, params = _owner_cond('s.user_key')
    where = 'WHERE m.timestamp >= ?'
    if cond:
        where += f' AND {cond}'
    rows = conn.execute(
        f"SELECT date(m.timestamp, 'unixepoch', 'localtime') as day, COUNT(DISTINCT m.session_id) as session_count, "
        f"COALESCE(SUM(m.input_tokens),0) as input_tokens, COALESCE(SUM(m.output_tokens),0) as output_tokens "
        f"FROM messages m JOIN sessions s ON s.id = m.session_id {where} GROUP BY day ORDER BY day",
        (cutoff, *params)).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2)

def _load_cron_jobs() -> list[dict]:
    from .cron_daemon import load_jobs
    return load_jobs()

def _save_cron_jobs(jobs: list[dict]):
    from .cron_daemon import save_jobs
    save_jobs(jobs)

@mcp.tool()
def cron_list() -> str:
    """List persistent cron jobs.

    Instance-wide — user-level remote tokens are refused (admin scope required)."""
    if _cron_denied():
        return json.dumps({'error': 'cron management requires admin scope'}, ensure_ascii=False)
    return json.dumps(_load_cron_jobs(), ensure_ascii=False, indent=2)

_JOB_NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')

@mcp.tool()
def cron_add(name: str, prompt: str, schedule_expr: str, schedule_kind: str='cron') -> str:
    """Create a persistent cron job. schedule_kind: 'cron' or 'interval' (minutes).

    Job name must match ``^[A-Za-z0-9_-]{1,64}$`` (M-03 — prevents output-dir
    traversal via the job name).  Only 'claude'-type jobs are created here:
    arbitrary ``type=shell`` jobs are refused.

    Instance-wide — user-level remote tokens are refused (admin scope required).
    """
    if _cron_denied():
        return json.dumps({'error': 'cron management requires admin scope'}, ensure_ascii=False)
    import uuid
    if not name or not _JOB_NAME_RE.match(name):
        return json.dumps({'error': f"Invalid job name {name!r}: must match ^[A-Za-z0-9_-]{{1,64}}$"})
    if schedule_kind not in ('cron', 'interval'):
        return json.dumps({'error': f"Invalid schedule_kind {schedule_kind!r}: must be 'cron' or 'interval'"})
    expr = str(schedule_expr or '').strip()
    if not expr:
        return json.dumps({'error': 'schedule_expr is required'})
    if schedule_kind == 'interval':
        try:
            minutes = int(expr)
        except (TypeError, ValueError):
            return json.dumps({'error': f"Invalid interval expr {expr!r}: must be an integer number of minutes"})
        if not (1 <= minutes <= 10080):
            return json.dumps({'error': 'interval must be between 1 and 10080 minutes'})
    elif len(expr.split()) != 5:
        return json.dumps({'error': f"Invalid cron expr {expr!r}: expected 5 fields (minute hour day month weekday)"})
    jobs = _load_cron_jobs()
    job = {'id': uuid.uuid4().hex[:12], 'name': name, 'prompt': prompt,
           'schedule': {'kind': schedule_kind, 'expr': expr},
           'type': 'claude', 'action': '',
           'enabled': True, 'last_run_at': None, 'next_run_at': None, 'last_error': None}
    jobs.append(job)
    _save_cron_jobs(jobs)
    return json.dumps(job, ensure_ascii=False, indent=2)

@mcp.tool()
def cron_remove(job_id: str) -> str:
    """Delete a cron job by ID.

    Instance-wide — user-level remote tokens are refused (admin scope required)."""
    if _cron_denied():
        return json.dumps({'error': 'cron management requires admin scope'}, ensure_ascii=False)
    jobs = _load_cron_jobs()
    jobs = [j for j in jobs if j.get('id', '') != job_id]
    _save_cron_jobs(jobs)
    return json.dumps({'removed': job_id})

@mcp.tool()
def cron_pause(job_id: str) -> str:
    """Pause a cron job.

    Instance-wide — user-level remote tokens are refused (admin scope required)."""
    if _cron_denied():
        return json.dumps({'error': 'cron management requires admin scope'}, ensure_ascii=False)
    jobs = _load_cron_jobs()
    for j in jobs:
        if j.get('id', '') == job_id:
            j['enabled'] = False
    _save_cron_jobs(jobs)
    return json.dumps({'paused': job_id})

@mcp.tool()
def cron_resume(job_id: str) -> str:
    """Resume a paused cron job.

    Instance-wide — user-level remote tokens are refused (admin scope required)."""
    if _cron_denied():
        return json.dumps({'error': 'cron management requires admin scope'}, ensure_ascii=False)
    jobs = _load_cron_jobs()
    for j in jobs:
        if j.get('id', '') == job_id:
            j['enabled'] = True
    _save_cron_jobs(jobs)
    return json.dumps({'resumed': job_id})

@mcp.tool()
def cron_trigger(job_id: str) -> str:
    """Immediately trigger a cron job.

    Instance-wide — user-level remote tokens are refused (admin scope required)."""
    if _cron_denied():
        return json.dumps({'error': 'cron management requires admin scope'}, ensure_ascii=False)
    jobs = _load_cron_jobs()
    job = next((j for j in jobs if j.get('id', '') == job_id), None)
    if not job:
        return json.dumps({'error': f'Job {job_id} not found'})
    try:
        # N1: run ``claude -p`` through code_exec's restricted runner — own
        # process group (timeout SIGKILLs the whole tree), CLAUDE_BIN honoured,
        # and the environment scrubbed so the job cannot read metano's secrets.
        import shutil
        from .code_exec import run_command_isolated
        claude_bin = os.environ.get('CLAUDE_BIN') or shutil.which('claude') or '/home/dk/local/node/bin/claude'
        r = run_command_isolated([claude_bin, '-p', job['prompt']], timeout=300)
        if r.get('error') or r.get('exit_code', 0) != 0:
            return json.dumps({'job_id': job_id, 'status': 'error',
                               'exit_code': r.get('exit_code'),
                               'error': r.get('error') or (r.get('stderr') or '')[:500],
                               'output': (r.get('stdout') or '')[:500]})
        return json.dumps({'job_id': job_id, 'status': 'completed',
                           'exit_code': 0, 'output': (r.get('stdout') or '')[:2000]})
    except Exception as e:
        logger.exception()
        return json.dumps({'job_id': job_id, 'status': 'error', 'error': str(e)})

@mcp.tool()
def reindex() -> str:
    """Force a full re-index of all Claude Code session files."""
    conn = init_db()
    count = index_all(conn, force=True)
    return json.dumps({'indexed_files': count})

@mcp.tool()
def personality_list() -> str:
    """List all available personalities."""
    PERSONALITIES_DIR.mkdir(parents=True, exist_ok=True)
    personalities = []
    for f in sorted(PERSONALITIES_DIR.glob('*.md')):
        personalities.append({'name': f.stem, 'preview': f.read_text()[:100]})
    return json.dumps(personalities, ensure_ascii=False, indent=2)

_PERSONALITY_NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
PERSONALITY_PENDING = PERSONALITIES_DIR.parent / 'personality_pending.json'


def _atomic_write_text(path: Path, content: str):
    """Atomic write: temp file in the same dir + os.replace (never partial)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(content, encoding='utf-8')
    os.replace(tmp, path)


@mcp.tool()
def personality_set(name: str) -> str:
    """Stage a personality switch for approval (does NOT overwrite ~/CLAUDE.md).

    The candidate is staged to a pending file and applied atomically by
    ``personality_apply`` after explicit admin approval — a model/remote caller
    can never directly overwrite ~/CLAUDE.md. Available: default, kawaii,
    catgirl, pirate, shakespeare, concise, technical, noir, surfer, uwu,
    philosopher, hype.
    """
    if not name or not _PERSONALITY_NAME_RE.match(name):
        return json.dumps({'error': f"Invalid personality name {name!r}: must match ^[A-Za-z0-9_-]{{1,64}}$"})
    PERSONALITIES_DIR.mkdir(parents=True, exist_ok=True)
    base = PERSONALITIES_DIR.resolve()
    src = (base / f'{name}.md').resolve()
    if not src.is_relative_to(base) or not src.is_file():
        available = [f.stem for f in PERSONALITIES_DIR.glob('*.md')]
        return json.dumps({'error': f"Personality '{name}' not found", 'available': available})
    content = src.read_text(encoding='utf-8', errors='replace')
    _atomic_write_text(PERSONALITY_PENDING, json.dumps(
        {'name': name, 'content': content, 'requested_at': time.time()},
        ensure_ascii=False))
    return json.dumps({'personality': name, 'status': 'pending_approval',
                       'note': 'staged for approval; run personality_apply to activate'},
                      ensure_ascii=False, indent=2)


@mcp.tool()
def personality_apply(name: str='') -> str:
    """Apply a previously staged personality to ~/CLAUDE.md (atomic replace).

    This is the approval step for a staged ``personality_set``.  It atomically
    replaces ~/CLAUDE.md via a temp file + ``os.replace`` so a crash never leaves
    a half-written file.  It is excluded from the remote and gateway tool
    whitelists — only callers authorized to modify global config should invoke it.
    """
    if not PERSONALITY_PENDING.exists():
        return json.dumps({'error': 'no pending personality to apply'})
    try:
        data = json.loads(PERSONALITY_PENDING.read_text(encoding='utf-8'))
    except Exception as e:
        return json.dumps({'error': f'failed to read pending personality: {e}'})
    target = data.get('name') or ''
    if name and target != name:
        return json.dumps({'error': f'pending personality is {target!r}, not {name!r}'})
    if not _PERSONALITY_NAME_RE.match(target):
        return json.dumps({'error': 'staged personality name is invalid'})
    try:
        _atomic_write_text(CLAUDE_MD, data.get('content', ''))
    except Exception as e:
        logger.exception()
        return json.dumps({'error': f'failed to apply personality: {e}'})
    try:
        PERSONALITY_PENDING.unlink()
    except OSError:
        pass
    return json.dumps({'personality': target, 'status': 'active'}, ensure_ascii=False, indent=2)

@mcp.tool()
def personality_current() -> str:
    """Show the current personality from ~/CLAUDE.md."""
    if CLAUDE_MD.exists():
        return CLAUDE_MD.read_text()
    return '(no CLAUDE.md found)'

@mcp.tool()
def curator_report(dry_run: bool=True) -> str:
    """Run the memory curator: scan all memory files for issues (duplicates, stale entries, missing from index). dry_run=True only reports, dry_run=False auto-fixes safe issues."""
    result = run_curator(dry_run=dry_run)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def x_search(query: str, limit: int=10) -> str:
    """Simulate X/Twitter search via xAI Grok LLM. NOTE: Results are LLM-generated, NOT from the real Twitter API. They may be fabricated/hallucinated. Treat as fictional suggestions, not real posts."""
    result = search_x(query, limit=limit)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def honcho_observe(content: str, category: str='general', session_id: str='') -> str:
    """Record an observation about the user. Categories: preference, knowledge, habit, goal, personality, general."""
    conn = get_honcho_db()
    try:
        if not get_user(conn, 'default'):
            create_user(conn, user_id='default')
        obs = add_observation(conn, 'default', content, category, session_id)
    finally:
        conn.close()
    result = dialectic_reason('default', content, category)
    return json.dumps({'observation': obs, 'dialectic_result': result}, ensure_ascii=False, indent=2)

@mcp.tool()
def honcho_profile() -> str:
    """Get the full user profile: beliefs, recent observations, and a summary."""
    conn = get_honcho_db()
    try:
        profile = get_profile(conn, 'default')
        if not profile:
            create_user(conn, user_id='default')
            profile = get_profile(conn, 'default')
        return json.dumps(profile, ensure_ascii=False, indent=2)
    finally:
        conn.close()

@mcp.tool()
def honcho_dialectic(content: str, category: str='general') -> str:
    """Trigger dialectic reasoning on an observation. The engine compares against existing beliefs and decides to add, update, or contradict."""
    conn = get_honcho_db()
    try:
        if not get_user(conn, 'default'):
            create_user(conn, user_id='default')
    finally:
        conn.close()
    result = dialectic_reason('default', content, category)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def honcho_beliefs() -> str:
    """List all current beliefs about the user."""
    conn = get_honcho_db()
    try:
        beliefs = get_beliefs(conn, 'default')
        return json.dumps(beliefs, ensure_ascii=False, indent=2)
    finally:
        conn.close()

@mcp.tool()
def honcho_compress() -> str:
    """Compress beliefs: merge similar ones, remove low-confidence contradicted ones."""
    result = compress_beliefs('default')
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def voice_speak(text: str, voice: str='xiaoxiao') -> str:
    """Convert text to speech and play it. Chinese voices: xiaoxiao, yunxi, yunyang, xiaoyi, yunjian. English voices: aria, guy, jenny, roger."""
    try:
        path = tts_speak(text, voice=voice)
        return json.dumps({'status': 'playing', 'voice': voice, 'path': path, 'text_length': len(text)})
    except ImportError:
        return json.dumps({'error': 'edge-tts not installed. Run: pip install edge-tts'})
    except Exception as e:
        logger.exception()
        return json.dumps({'error': str(e)})

@mcp.tool()
def voice_list() -> str:
    """List all available TTS voices."""
    return json.dumps(list_voices(), ensure_ascii=False, indent=2)

@mcp.tool()
def evolution_status() -> str:
    """Show current evolution system status: belief counts by stage, pending suggestions, estimated cost.

    Instance-wide — user-level remote tokens are refused (admin-read scope required)."""
    if _instance_data_denied():
        return json.dumps({'error': 'instance-wide evolution status requires admin-read scope'},
                          ensure_ascii=False)
    result = _evolution_status()
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
@track_action('evolution_run')
def evolution_run(stage: str='all') -> str:
    """Manually trigger evolution cycle. Stages: observe, act, reflect, maintain, all."""
    result = _evolution_run(stage)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def evolution_suggestions() -> str:
    """List all pending evolution suggestions awaiting approval.

    Instance-wide — user-level remote tokens are refused (admin-read scope required)."""
    if _instance_data_denied():
        return json.dumps({'error': 'instance-wide evolution suggestions require admin-read scope'},
                          ensure_ascii=False)
    suggestions = load_suggestions()
    return json.dumps(suggestions, ensure_ascii=False, indent=2)

@mcp.tool()
def evolution_approve(suggestion_id: str) -> str:
    """Approve a pending evolution suggestion."""
    result = approve_suggestion(suggestion_id)
    return json.dumps(result or {'error': f'Suggestion {suggestion_id} not found'}, ensure_ascii=False, indent=2)

@mcp.tool()
def evolution_reject(suggestion_id: str) -> str:
    """Reject a pending evolution suggestion."""
    result = reject_suggestion(suggestion_id)
    return json.dumps(result or {'error': f'Suggestion {suggestion_id} not found'}, ensure_ascii=False, indent=2)

@mcp.tool()
def evolution_log(limit: int=20) -> str:
    """Show recent evolution operations from the audit log.

    Instance-wide — user-level remote tokens are refused (admin-read scope required)."""
    if _instance_data_denied():
        return json.dumps({'error': 'instance-wide evolution log requires admin-read scope'},
                          ensure_ascii=False)
    log_path = EVO_LOG
    if not log_path.exists():
        return json.dumps([])
    entries = []
    with open(log_path) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return json.dumps(entries[-limit:], ensure_ascii=False, indent=2)

@mcp.tool()
def skills_list(category: str='') -> str:
    """List all available skills. Optionally filter by category. Returns name, description, trigger, and category for each skill."""
    skills = _skill_loader.discover_all()
    if category:
        skills = [s for s in skills if s.category == category]
    result = [{'name': s.name, 'description': s.description, 'trigger': s.trigger, 'category': s.category, 'source': s.source} for s in skills]
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def skill_view(name: str, full: bool=False, file_path: str='') -> str:
    """View a skill's details. full=False returns frontmatter only; full=True returns the complete skill content. file_path optionally loads a supporting file (e.g. references/*.md, templates/*.html) from inside the skill's directory; path traversal outside the skill directory is rejected.

    Skill bodies / supporting files are instance data — user-level remote tokens are refused (admin-read scope required)."""
    if _instance_data_denied():
        return json.dumps({'error': 'skill bodies require admin-read scope'}, ensure_ascii=False)
    rec = _skill_loader.find_by_name(name)
    if not rec:
        return json.dumps({'error': f"Skill '{name}' not found"})
    if file_path:
        base = rec.path.parent.resolve()
        target = (base / file_path).resolve()
        if not target.is_relative_to(base):
            return json.dumps({'error': f'file_path must stay inside the skill directory: {file_path}'})
        if not target.is_file():
            return json.dumps({'error': f'Supporting file not found: {file_path}'})
        try:
            content = target.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            return json.dumps({'error': f'Failed to read {file_path}: {e}'})
        return json.dumps({'name': rec.name, 'file_path': file_path, 'content': content}, ensure_ascii=False, indent=2)
    result = {'name': rec.name, 'description': rec.description, 'version': rec.version, 'author': rec.author, 'trigger': rec.trigger, 'category': rec.category, 'source': rec.source}
    if full:
        result['content'] = rec.body
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def skill_manage(action: str, name: str, category: str='', description: str='', content: str='', old_string: str='', new_string: str='', version: str='1.0.0', author: str='') -> str:
    """Manage skills: create, edit, patch, delete, or get info. Actions: create (new skill), edit (replace body), patch (find/replace in body), delete (remove), info (show path/source)."""
    # M-04: strict basename validation — name/category are joined onto the skills
    # directory, so a '/' or '..' would escape it (path traversal / arbitrary write).
    if not name or not _SAFE_BASENAME_RE.match(name):
        return json.dumps({'error': f'Invalid skill name {name!r}: must match ^[A-Za-z0-9_-]{{1,64}}$'})
    if action == 'create':
        if not category or not _SAFE_BASENAME_RE.match(category):
            return json.dumps({'error': f'Invalid skill category {category!r}: must match ^[A-Za-z0-9_-]{{1,64}}$'})
        if not description or (not content):
            return json.dumps({'error': 'create requires: name, category, description, content'})
        # Containment belt-and-suspenders: the resolved target must stay inside
        # SKILLS_DIR even if the manager gains a new way to combine paths.
        try:
            from .paths import SKILLS_DIR
            base = SKILLS_DIR.resolve()
            target = (base / category / name).resolve()
            if not target.is_relative_to(base):
                return json.dumps({'error': f'Skill path escapes skills directory: {category}/{name}'})
        except Exception as e:
            return json.dumps({'error': f'skill path validation failed: {e}'})
        result = _skill_manager.create(name, category, description, content, version, author)
    elif action == 'edit':
        if not content:
            return json.dumps({'error': 'edit requires: name, content'})
        result = _skill_manager.edit(name, content)
    elif action == 'patch':
        if not old_string or not new_string:
            return json.dumps({'error': 'patch requires: name, old_string, new_string'})
        result = _skill_manager.patch(name, old_string, new_string)
    elif action == 'delete':
        result = _skill_manager.delete(name)
    elif action == 'info':
        result = _skill_manager.info(name)
    else:
        result = {'error': f"Unknown action '{action}'. Use: create, edit, patch, delete, info"}
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def skill_bundle(name: str='', action: str='resolve') -> str:
    """Resolve a skill bundle (load all skills in the bundle) or list available bundles. action: 'resolve' (default) or 'list'."""
    if action == 'list':
        bundles = _bundle_loader.list_bundles()
        return json.dumps(bundles, ensure_ascii=False, indent=2)
    elif action == 'resolve':
        if not name:
            return json.dumps({'error': 'resolve requires a bundle name'})
        content = _bundle_loader.resolve_bundle(name)
        if not content:
            return json.dumps({'error': f"Bundle '{name}' not found or empty"})
        return content
    else:
        return json.dumps({'error': f"Unknown action '{action}'. Use: resolve, list"})

@mcp.tool()
def browser_navigate(url: str, wait_for: str='load') -> str:
    """Navigate browser to a URL. Returns page title and URL."""
    from .browser import web_browse as _wb
    result = _wb(url, wait_for=wait_for)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def browser_screenshot(url: str='', full_page: bool=True, selector: str='') -> str:
    """Take a screenshot. If url provided, navigate first. Optional selector for element screenshot."""
    from .browser import web_screenshot as _ws
    result = _ws(url, full_page=full_page)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def browser_click(selector: str) -> str:
    """Click an element on the current page using CSS selector."""
    import asyncio
    import concurrent.futures
    from .browser import pw_click
    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(asyncio.run, pw_click(selector)).result()
    except RuntimeError:
        result = asyncio.run(pw_click(selector))
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def browser_fill(selector: str, value: str) -> str:
    """Fill a form field on the current page using CSS selector."""
    import asyncio
    import concurrent.futures
    from .browser import pw_fill
    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(asyncio.run, pw_fill(selector, value)).result()
    except RuntimeError:
        result = asyncio.run(pw_fill(selector, value))
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def browser_evaluate(expression: str) -> str:
    """Execute JavaScript in the browser and return the result."""
    import asyncio
    import concurrent.futures
    from .browser import pw_evaluate
    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(asyncio.run, pw_evaluate(expression)).result()
    except RuntimeError:
        result = asyncio.run(pw_evaluate(expression))
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def browser_get_content(url: str='') -> str:
    """Get page text content. If url provided, navigate first."""
    from .browser import web_content
    result = web_content(url)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def web_search(query: str, engine: str='duckduckgo', limit: int=10) -> str:
    """Search the web. engine: duckduckgo (default) or tavily."""
    if engine == 'tavily':
        import asyncio
        import concurrent.futures
        from .mcp_bridge import tavily_search
        try:
            asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(asyncio.run, tavily_search(query, limit=limit)).result()
        except RuntimeError:
            result = asyncio.run(tavily_search(query, limit=limit))
        return json.dumps(result, ensure_ascii=False, indent=2)
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=limit))
        return json.dumps({'query': query, 'engine': 'duckduckgo', 'results': results}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception()
        return json.dumps({'error': str(e)}, ensure_ascii=False)

@mcp.tool()
def code_run(code: str, language: str='python', timeout: int=60) -> str:
    """Execute code in a sandboxed environment. language: python, javascript, shell. timeout: max seconds (1-300)."""
    from .code_exec import code_run as _real_code_run
    result = _real_code_run(code, language=language, timeout=timeout)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def agent_spawn(task: str, model: str='', timeout: int=120) -> str:
    """Spawn a sub-agent (parallel Claude Code instance) to handle a task. Returns task ID and result."""
    result = _agent_delegator.spawn(task, model=model, timeout=timeout)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def agent_status(task_id: str) -> str:
    """Check the status of a spawned sub-agent task."""
    result = _agent_delegator.status(task_id)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def agent_result(task_id: str) -> str:
    """Get the full result of a completed sub-agent task."""
    result = _agent_delegator.result(task_id)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def image_generate(prompt: str, size: str='1024x1024', style: str='vivid') -> str:
    """Generate an image from a text prompt using DALL-E compatible API. size: 256x256, 512x512, 1024x1024. style: vivid, natural."""
    result = _image_gen_func(prompt, size=size, style=style)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def image_describe(image_path: str, prompt: str='Describe this image in detail.') -> str:
    """Describe an image using vision API."""
    result = _image_desc_func(image_path, prompt=prompt)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def model_list() -> str:
    """List all configured model providers.

    Instance config — user-level remote tokens are refused (admin-read scope required)."""
    if _instance_data_denied():
        return json.dumps({'error': 'model configuration requires admin-read scope'},
                          ensure_ascii=False)
    result = _model_router.list_providers()
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def knowledge_ingest(path: str, title: str='', doc_type: str='auto') -> str:
    """Ingest a document into the knowledge base. Supports text, markdown, code, JSON files. doc_type: text, markdown, code, json, auto."""
    result = _kb_ingest(path, title=title, doc_type=doc_type)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
@track_action('knowledge_search')
def knowledge_search(query: str, limit: int=5) -> str:
    """Search the knowledge base for relevant content.

    The knowledge base is instance-wide — user-level remote tokens are refused
    (admin-read scope required)."""
    limit = _clip(limit, 1, 20, 5)
    if _instance_data_denied():
        return json.dumps({'query': query, 'results': [], 'count': 0,
                           'reason': 'instance-wide knowledge base requires admin-read scope'},
                          ensure_ascii=False, indent=2)
    result = _kb_search(query, limit=limit)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def knowledge_list(doc_type: str='') -> str:
    """List all documents in the knowledge base.

    Instance-wide — user-level remote tokens are refused (admin-read scope required)."""
    if _instance_data_denied():
        return json.dumps({'documents': [], 'count': 0,
                           'reason': 'instance-wide knowledge base requires admin-read scope'},
                          ensure_ascii=False, indent=2)
    result = _kb_list(doc_type=doc_type)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def security_check(user_id: str, message: str, capability: str='chat') -> str:
    """Check if a message is allowed (permission + rate limit + content filter).

    A user-level remote token may only check its own subject.
    """
    subject, scopes = get_mcp_auth()
    if subject is not None and not is_admin_read(scopes):
        user_id = subject
    result = _security.check_message(user_id, message, capability)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def security_status(user_id: str) -> str:
    """Get security status for a user (tier, rate limits, blocked count).

    A user-level token may only inspect its own subject; admin-read (or local
    stdio) callers may inspect any user.  Unknown user ids are reported without
    creating an in-memory UserSecurity record (H-07 — an attacker must not be
    able to grow the memory map with arbitrary ids).
    """
    subject, scopes = get_mcp_auth()
    if subject is not None and not is_admin_read(scopes):
        user_id = subject
    known = getattr(_security, '_users', {})
    if user_id not in known:
        return json.dumps({
            'user_id': user_id,
            'tier': 'guest',
            'capabilities': [],
            'rate_limit': '0/20 per 60s',
            'blocked_count': 0,
            'note': 'unknown user (no record created)',
        }, ensure_ascii=False, indent=2)
    result = _security.get_user_status(user_id)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def kanban_board(action: str, name: str='', board_id: str='', description: str='') -> str:
    """Manage kanban boards. action: create (requires name), list."""
    if action == 'create':
        result = kanban_create_board(name, description)
    elif action == 'list':
        result = kanban_list()
    else:
        result = {'error': f'Unknown action: {action}. Use: create, list'}
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def kanban_task(action: str, board_id: str='', task_id: str='', title: str='', description: str='', column: str='todo', priority: str='medium') -> str:
    """Manage kanban tasks. action: add (requires board_id, title), move (requires task_id, column), list (requires board_id), delete (requires task_id)."""
    if action == 'add':
        result = kanban_add_task(board_id, title, description, column, priority=priority)
    elif action == 'move':
        result = kanban_move_task(task_id, column)
    elif action == 'list':
        result = kanban_list(board_id, column if column else '')
    elif action == 'delete':
        result = kanban_delete_task(task_id)
    else:
        result = {'error': f'Unknown action: {action}. Use: add, move, list, delete'}
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def home_control(entity_id: str, action: str, value: str='') -> str:
    """Control a Home Assistant entity. action: turn_on, turn_off, toggle, set_value. Requires HA_URL and HA_TOKEN configured."""
    result = _ha_control(entity_id, action, value)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def home_status(entity_id: str='') -> str:
    """Get status of Home Assistant entities. Leave entity_id empty to list all domains."""
    result = _ha_status(entity_id)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
@track_action('memory_add')
def memory_add(content: str, category: str='general', importance: float=0.5, tags: str='') -> str:
    """Add a memory observation. Categories: preference, knowledge, habit, goal, general. Importance: 0.0-1.0.
    tags: optional scenario keywords (comma or space separated, e.g. 'backend,frontend,sync') that gate when this memory is relevant.
    The memory store is instance-wide; user-level remote tokens are refused here (admin-read required)."""
    if _memory_denied():
        return json.dumps({'status': 'denied',
                           'reason': 'instance-wide memory requires admin-read scope'},
                          ensure_ascii=False, indent=2)
    result = add_memory(content, category=category, importance=importance, tags=tags or None)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def memory_search(query: str='', limit: int=10, tag: str='') -> str:
    """Search accumulated memories for relevant information.
    query: content keywords (empty string returns nothing unless tag is given).
    tag: optional scenario keyword to filter by (e.g. 'backend', 'test,verify'). Omit for full-text search.
    The memory store is instance-wide; user-level remote tokens are refused here (admin-read required)."""
    limit = _clip(limit, 1, 50, 10)
    if _memory_denied():
        return json.dumps({'query': query, 'tag': tag, 'results': [], 'count': 0,
                           'method': 'denied',
                           'reason': 'instance-wide memory requires admin-read scope'},
                          ensure_ascii=False, indent=2)
    result = search_memories(query, limit=limit, tag=tag or None)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def memory_stats() -> str:
    """Get memory system statistics. Instance-wide — user-level remote tokens are refused."""
    if _memory_denied():
        return json.dumps({'error': 'instance-wide memory stats require admin-read scope'},
                          ensure_ascii=False, indent=2)
    result = get_memory_stats()
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def memory_compress() -> str:
    """Compress old low-importance memories by merging similar ones."""
    result = compress_memories()
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def web_search_tavily(query: str, limit: int=10) -> str:
    """Search the web using Tavily API. Returns answer summary and ranked results."""
    import asyncio
    import concurrent.futures
    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(asyncio.run, tavily_search(query, limit=limit)).result()
    except RuntimeError:
        result = asyncio.run(tavily_search(query, limit=limit))
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def memory_timeline(days: int=7, limit: int=20) -> str:
    """加载近期观察的时间线上下文（Layer 2）。需要了解近期用户活动时使用。days: 回看天数(1-90)，limit: 最多返回条数(1-50)。
    Honcho observations are instance-wide ('default' user) — user-level remote tokens are refused."""
    days = _clip(days, 1, 90, 7)
    limit = _clip(limit, 1, 50, 20)
    if _memory_denied():
        return json.dumps({'observations': 0, 'formatted': '',
                           'reason': 'instance-wide timeline requires admin-read scope'},
                          ensure_ascii=False, indent=2)
    from .honcho.models import init_honcho_db, get_honcho_db, get_user, create_user, get_observations
    from datetime import datetime, timezone
    conn = init_honcho_db()
    if not get_user(conn, 'default'):
        create_user(conn, user_id='default')
    cutoff = time.time() - days * 86400
    rows = conn.execute(
        "SELECT id, category, content, timestamp FROM observations "
        "WHERE user_id = 'default' AND timestamp >= ? "
        "ORDER BY timestamp DESC LIMIT ?",
        (cutoff, limit)
    ).fetchall()
    results = []
    for r in rows:
        ts = datetime.fromtimestamp(r['timestamp'], tz=timezone.utc).astimezone().strftime('%m-%d %H:%M')
        results.append(f"[{ts}] [{r['category']}] {r['content'][:100]}")
    return json.dumps({'observations': len(results), 'formatted': '\n'.join(results)}, ensure_ascii=False, indent=2)

@mcp.tool()
def memory_detail(category: str='', belief_id: str='') -> str:
    """加载完整信念/观察详情（Layer 3）。需要特定信念的完整内容时使用。category: 按类别过滤(如habit/preference/knowledge)，belief_id: 按ID查看单条。
    Honcho beliefs are instance-wide ('default' user) — user-level remote tokens are refused."""
    if _memory_denied():
        return json.dumps({'beliefs': [],
                           'reason': 'instance-wide memory detail requires admin-read scope'},
                          ensure_ascii=False, indent=2)
    from .honcho.models import init_honcho_db, get_honcho_db, get_user, create_user, get_beliefs
    conn = init_honcho_db()
    if not get_user(conn, 'default'):
        create_user(conn, user_id='default')
    if belief_id:
        row = conn.execute("SELECT * FROM beliefs WHERE id = ? AND contradicted = 0", (belief_id,)).fetchone()
        return json.dumps(dict(row) if row else {'error': 'not found'}, ensure_ascii=False, indent=2)
    beliefs = get_beliefs(conn, 'default')
    if category:
        beliefs = [b for b in beliefs if b['category'] == category]
    return json.dumps({'beliefs': beliefs}, ensure_ascii=False, indent=2)

def main():
    init_db()
    init_honcho_db()
    mcp.run()
if __name__ == '__main__':
    main()