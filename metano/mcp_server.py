"""MCP stdio server exposing session search, analytics, and cron tools."""
import json
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
from .voice.stt import listen as stt_listen, transcribe
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
from .knowledge import knowledge_ingest, knowledge_search, knowledge_list, knowledge_delete
from .voice import voice_speak as _voice_speak, voice_transcribe as _voice_transcribe, voice_list_voices
from .security import security as _security
from .kanban import kanban_create_board, kanban_add_task, kanban_move_task, kanban_list, kanban_delete_task
from .home_assistant import home_control, home_status, home_automate
from .memory import add_memory, search_memories, get_memory_stats, compress_memories
from .mcp_bridge import tavily_search
from metano.log import logger
_skill_loader = SkillLoader()
_skill_manager = SkillManager()
_bundle_loader = BundleLoader()
CRON_DIR = Path.home() / '.claude' / 'metano' / 'cron'
CRON_JOBS_FILE = CRON_DIR / 'jobs.json'
PERSONALITIES_DIR = Path.home() / '.claude' / 'metano' / 'personalities'
CLAUDE_MD = Path.home() / 'CLAUDE.md'
mcp = FastMCP('metano')

def _get_conn() -> sqlite3.Connection:
    return get_db()

@mcp.tool()
def session_search(query: str, limit: int=10) -> str:
    """Full-text search across all Claude Code session messages. Supports Chinese (3+ chars for best results)."""
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT m.session_id, m.role, snippet(messages_fts, -1, '⟨', '⟩', '...', 20) as snippet, m.timestamp, s.title FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid JOIN sessions s ON s.id = m.session_id WHERE messages_fts MATCH ? ORDER BY m.timestamp DESC LIMIT ?", (query, limit)).fetchall()
        if rows:
            results = []
            for r in rows:
                results.append({'session_id': r['session_id'], 'title': r['title'], 'role': r['role'], 'snippet': r['snippet'], 'timestamp': r['timestamp']})
            return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception()
    rows = conn.execute('SELECT m.session_id, m.role, substr(m.content, 1, 200) as snippet, m.timestamp, s.title FROM messages m JOIN sessions s ON s.id = m.session_id WHERE m.content LIKE ? ORDER BY m.timestamp DESC LIMIT ?', (f'%{query}%', limit)).fetchall()
    results = [{'session_id': r['session_id'], 'title': r['title'], 'role': r['role'], 'snippet': r['snippet'], 'timestamp': r['timestamp']} for r in rows]
    return json.dumps(results, ensure_ascii=False, indent=2)

@mcp.tool()
def session_list(limit: int=20, offset: int=0) -> str:
    """List recent Claude Code sessions with titles, token counts, and model info."""
    conn = _get_conn()
    rows = conn.execute('SELECT id, title, model, message_count, tool_call_count, input_tokens, output_tokens, estimated_cost_usd, started_at, last_active FROM sessions ORDER BY last_active DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
    results = [dict(r) for r in rows]
    return json.dumps(results, ensure_ascii=False, indent=2)

@mcp.tool()
def session_get(session_id: str, limit: int=100) -> str:
    """Get messages for a specific session by ID."""
    conn = _get_conn()
    rows = conn.execute('SELECT id, role, content, tool_name, timestamp, input_tokens, output_tokens, duration_ms FROM messages WHERE session_id = ? ORDER BY timestamp ASC LIMIT ?', (session_id, limit)).fetchall()
    results = [dict(r) for r in rows]
    return json.dumps(results, ensure_ascii=False, indent=2)

@mcp.tool()
def analytics_summary(days: int=7) -> str:
    """Aggregate token usage and cost estimates over the last N days."""
    conn = _get_conn()
    cutoff = time.time() - days * 86400
    total = conn.execute('SELECT COUNT(*) as session_count, SUM(message_count) as message_count, SUM(tool_call_count) as tool_call_count, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, SUM(cache_read_tokens) as cache_read_tokens, SUM(estimated_cost_usd) as estimated_cost_usd FROM sessions WHERE last_active >= ?', (cutoff,)).fetchone()
    by_model = conn.execute('SELECT model, COUNT(*) as session_count, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, SUM(estimated_cost_usd) as estimated_cost_usd FROM sessions WHERE last_active >= ? GROUP BY model', (cutoff,)).fetchall()
    result = {'period_days': days, 'total': dict(total) if total else {}, 'by_model': [dict(r) for r in by_model]}
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def analytics_daily(days: int=30) -> str:
    """Daily token/cost time series for the last N days."""
    conn = _get_conn()
    cutoff = time.time() - days * 86400
    rows = conn.execute("SELECT date(last_active, 'unixepoch') as day, COUNT(*) as session_count, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, SUM(estimated_cost_usd) as estimated_cost_usd FROM sessions WHERE last_active >= ? GROUP BY day ORDER BY day", (cutoff,)).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2)

def _load_cron_jobs() -> list[dict]:
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    if CRON_JOBS_FILE.exists():
        return json.loads(CRON_JOBS_FILE.read_text())
    return []

def _save_cron_jobs(jobs: list[dict]):
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    CRON_JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))

@mcp.tool()
def cron_list() -> str:
    """List persistent cron jobs."""
    return json.dumps(_load_cron_jobs(), ensure_ascii=False, indent=2)

@mcp.tool()
def cron_add(name: str, prompt: str, schedule_expr: str, schedule_kind: str='cron') -> str:
    """Create a persistent cron job. schedule_kind: 'cron' (cron expression) or 'interval' (minutes). schedule_expr: cron expression like '0 9 * * 1-5' or interval in minutes like '30'."""
    import uuid
    jobs = _load_cron_jobs()
    job = {'id': uuid.uuid4().hex[:12], 'name': name, 'prompt': prompt, 'schedule': {'kind': schedule_kind, 'expr': schedule_expr}, 'enabled': True, 'last_run_at': None, 'next_run_at': None, 'last_error': None}
    jobs.append(job)
    _save_cron_jobs(jobs)
    return json.dumps(job, ensure_ascii=False, indent=2)

@mcp.tool()
def cron_remove(job_id: str) -> str:
    """Delete a cron job by ID."""
    jobs = _load_cron_jobs()
    jobs = [j for j in jobs if j['id'] != job_id]
    _save_cron_jobs(jobs)
    return json.dumps({'removed': job_id})

@mcp.tool()
def cron_pause(job_id: str) -> str:
    """Pause a cron job."""
    jobs = _load_cron_jobs()
    for j in jobs:
        if j['id'] == job_id:
            j['enabled'] = False
    _save_cron_jobs(jobs)
    return json.dumps({'paused': job_id})

@mcp.tool()
def cron_resume(job_id: str) -> str:
    """Resume a paused cron job."""
    jobs = _load_cron_jobs()
    for j in jobs:
        if j['id'] == job_id:
            j['enabled'] = True
    _save_cron_jobs(jobs)
    return json.dumps({'resumed': job_id})

@mcp.tool()
def cron_trigger(job_id: str) -> str:
    """Immediately trigger a cron job."""
    jobs = _load_cron_jobs()
    job = next((j for j in jobs if j['id'] == job_id), None)
    if not job:
        return json.dumps({'error': f'Job {job_id} not found'})
    import subprocess
    try:
        result = subprocess.run(['claude', '-p', job['prompt']], capture_output=True, text=True, timeout=300)
        return json.dumps({'job_id': job_id, 'status': 'completed', 'output': result.stdout[:2000]})
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

@mcp.tool()
def personality_set(name: str) -> str:
    """Switch Claude Code's personality. This updates ~/CLAUDE.md with the chosen personality template. Available: default, kawaii, catgirl, pirate, shakespeare, concise, technical, noir, surfer, uwu, philosopher, hype."""
    PERSONALITIES_DIR.mkdir(parents=True, exist_ok=True)
    src = PERSONALITIES_DIR / f'{name}.md'
    if not src.exists():
        available = [f.stem for f in PERSONALITIES_DIR.glob('*.md')]
        return json.dumps({'error': f"Personality '{name}' not found", 'available': available})
    content = src.read_text()
    CLAUDE_MD.write_text(content)
    return json.dumps({'personality': name, 'status': 'active'}, ensure_ascii=False, indent=2)

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
def voice_listen(duration: int=5, language: str='zh') -> str:
    """Record audio from microphone and transcribe to text. Uses faster-whisper locally. Duration in seconds (1-30)."""
    try:
        result = stt_listen(duration=duration, language=language)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except ImportError:
        return json.dumps({'error': 'faster-whisper not installed. Run: pip install faster-whisper'})
    except Exception as e:
        logger.exception()
        return json.dumps({'error': str(e)})

@mcp.tool()
def voice_transcribe(audio_path: str, language: str='zh') -> str:
    """Transcribe an audio file to text. Supports WAV, MP3, etc."""
    try:
        result = transcribe(audio_path, language=language)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception()
        return json.dumps({'error': str(e)})

@mcp.tool()
def voice_list() -> str:
    """List all available TTS voices."""
    return json.dumps(list_voices(), ensure_ascii=False, indent=2)

@mcp.tool()
def evolution_status() -> str:
    """Show current evolution system status: belief counts by stage, pending suggestions, estimated cost."""
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
    """List all pending evolution suggestions awaiting approval."""
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
    """Show recent evolution operations from the audit log."""
    log_path = Path.home() / '.claude' / 'metano' / 'evolution' / 'evolution_log.jsonl'
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
def skill_view(name: str, full: bool=False) -> str:
    """View a skill's details. full=False returns frontmatter only; full=True returns the complete skill content."""
    rec = _skill_loader.find_by_name(name)
    if not rec:
        return json.dumps({'error': f"Skill '{name}' not found"})
    result = {'name': rec.name, 'description': rec.description, 'version': rec.version, 'author': rec.author, 'trigger': rec.trigger, 'category': rec.category, 'source': rec.source}
    if full:
        result['content'] = rec.body
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def skill_manage(action: str, name: str, category: str='', description: str='', content: str='', old_string: str='', new_string: str='', version: str='1.0.0', author: str='') -> str:
    """Manage skills: create, edit, patch, delete, or get info. Actions: create (new skill), edit (replace body), patch (find/replace in body), delete (remove), info (show path/source)."""
    if action == 'create':
        if not category or not description or (not content):
            return json.dumps({'error': 'create requires: name, category, description, content'})
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
    """List all configured model providers."""
    result = _model_router.list_providers()
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def knowledge_ingest(path: str, title: str='', doc_type: str='auto') -> str:
    """Ingest a document into the knowledge base. Supports text, markdown, code, JSON files. doc_type: text, markdown, code, json, auto."""
    result = knowledge_ingest(path, title=title, doc_type=doc_type)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
@track_action('knowledge_search')
def knowledge_search(query: str, limit: int=5) -> str:
    """Search the knowledge base for relevant content."""
    result = knowledge_search(query, limit=limit)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def knowledge_list(doc_type: str='') -> str:
    """List all documents in the knowledge base."""
    result = knowledge_list(doc_type=doc_type)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def security_check(user_id: str, message: str, capability: str='chat') -> str:
    """Check if a message is allowed (permission + rate limit + content filter)."""
    result = _security.check_message(user_id, message, capability)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def security_status(user_id: str) -> str:
    """Get security status for a user (tier, rate limits, blocked count)."""
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
    result = home_control(entity_id, action, value)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def home_status(entity_id: str='') -> str:
    """Get status of Home Assistant entities. Leave entity_id empty to list all domains."""
    result = home_status(entity_id)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
@track_action('memory_add')
def memory_add(content: str, category: str='general', importance: float=0.5) -> str:
    """Add a memory observation. Categories: preference, knowledge, habit, goal, general. Importance: 0.0-1.0."""
    result = add_memory(content, category=category, importance=importance)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def memory_search(query: str, limit: int=10) -> str:
    """Search accumulated memories for relevant information."""
    result = search_memories(query, limit=limit)
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def memory_stats() -> str:
    """Get memory system statistics."""
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
def hot_sources() -> str:
    """List all available trending/hot list sources from DailyHotApi (56 sources: zhihu, weibo, bilibili, github, etc.)."""
    from .dailyhot import list_sources
    try:
        sources = list_sources()
        return json.dumps(sources, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({'error': str(e), 'hint': 'Is DailyHotApi running on port 6688?'})

@mcp.tool()
def hot_list(source: str, limit: int=10) -> str:
    """Fetch a trending/hot list. source: zhihu, weibo, bilibili, github, douyin, toutiao, etc. limit: max items (1-50)."""
    from .dailyhot import format_hot
    try:
        return format_hot(source, limit=limit)
    except Exception as e:
        return json.dumps({'error': str(e), 'hint': f"Check if '{source}' is a valid source. Use hot_sources to list all."})

@mcp.tool()
def memory_timeline(days: int=7, limit: int=20) -> str:
    """加载近期观察的时间线上下文（Layer 2）。需要了解近期用户活动时使用。days: 回看天数(1-90)，limit: 最多返回条数(1-50)。"""
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
        ts = datetime.fromtimestamp(r['timestamp'], tz=timezone.utc).strftime('%m-%d %H:%M')
        results.append(f"[{ts}] [{r['category']}] {r['content'][:100]}")
    return json.dumps({'observations': len(results), 'formatted': '\n'.join(results)}, ensure_ascii=False, indent=2)

@mcp.tool()
def memory_detail(category: str='', belief_id: str='') -> str:
    """加载完整信念/观察详情（Layer 3）。需要特定信念的完整内容时使用。category: 按类别过滤(如habit/preference/knowledge)，belief_id: 按ID查看单条。"""
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