"""Knowledge exploration engine: semantic search, active exploration, gap detection.

Integrates CocoIndex for semantic code search and Tavily for web-based
knowledge discovery. Detects knowledge gaps from action_log failure patterns
and proactively explores to fill them.
"""
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from .evo_models import get_recent_actions, get_meta, set_meta
from .evolution import _log
from .llm_call import call_llm
from metano.log import logger
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_BASE_URL = os.environ.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com/v1')
ANTHROPIC_MODEL = os.environ.get('HONCHO_MODEL', 'claude-sonnet-4-6')

# Exploration markdown is written under the project root so knowledge_ingest's
# path validator (which only allows PROJECT_ROOT + a few sibling dirs) accepts
# it — /tmp would be silently rejected.
from .paths import EXPLORATION_DIR

# Fallback topics explored when action_log shows no failures. Keeps the KB
# actively accumulating exploration docs. Each topic costs 1 web search + 1 LLM
# synthesis call, so the scheduler only picks 1-2 per run and dedups for 14 days.
DEFAULT_EXPLORE_TOPICS = [
    'Claude Code agent hooks and automation best practices',
    'Web scraping and anti-bot detection bypass techniques 2026',
    'Building persistent memory systems for AI agents',
    'LLM cost optimization and circuit breaker patterns',
    'Self-improving autonomous agent architecture patterns',
]
# evo_meta key holding {topic: ISO-timestamp} of recently explored topics.
EXPLORED_META_KEY = 'explored_knowledge_topics'


def _resolve_tavily_key() -> str:
    """Find the Tavily API key: env first, then ~/.mcp.json (where the MCP
    server config stores it). The cron daemon has no TAVILY_API_KEY in env, so
    the ~/.mcp.json fallback is what makes web search work under cron."""
    if os.environ.get('TAVILY_API_KEY'):
        return os.environ['TAVILY_API_KEY']
    mcp_path = Path.home() / '.mcp.json'
    if mcp_path.exists():
        try:
            cfg = json.loads(mcp_path.read_text())
            return cfg.get('mcpServers', {}).get('tavily', {}).get('env', {}).get('TAVILY_API_KEY', '')
        except (json.JSONDecodeError, OSError):
            pass
    return ''

def semantic_search(query: str, project: str='') -> dict:
    """Search indexed codebases using CocoIndex semantic search.

    Falls back to keyword search if ccc is unavailable.
    """
    try:
        # ccc searches the project indexed in a directory; it has no --project
        # flag, so run in the target project dir when one is given.
        cmd = ['ccc', 'search', query]
        run_cwd = project if project and os.path.isdir(project) else None
        # Cold start (first search after daemon restart) loads the embedding
        # model and can take ~70s; warm searches are <1s. Generous timeout.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=run_cwd)
        if result.returncode != 0:
            return {'results': [], 'source': 'ccc_error', 'error': result.stderr[:200]}
        results = []
        current = {}
        for line in result.stdout.split('\n'):
            if line.startswith('--- Result'):
                if current.get('file'):
                    results.append(current)
                current = {}
            elif line.startswith('File:'):
                current['file'] = line.split(':', 1)[1].strip()
            elif line.startswith('[') and ']' in line and current.get('file'):
                current['content'] = current.get('content', '') + line + '\n'
            elif current.get('file') and line.strip():
                current['content'] = current.get('content', '') + line + '\n'
        if current.get('file'):
            results.append(current)
        _log('knowledge', 'semantic_search', {'query': query, 'results': len(results)})
        return {'results': results[:10], 'source': 'cocoindex'}
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {'results': [], 'source': 'ccc_unavailable', 'error': str(e)}

def _tavily_search(query: str, max_results: int=5) -> list[dict]:
    """Search the web using Tavily API."""
    api_key = _resolve_tavily_key()
    if not api_key:
        _log('knowledge', 'tavily_no_key', {'error': 'TAVILY_API_KEY not configured'})
        return []
    payload = {'api_key': api_key, 'query': query, 'max_results': max_results, 'search_depth': 'advanced', 'include_raw_content': False}
    try:
        req = urllib.request.Request('https://api.tavily.com/search', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')
        if proxy:
            import urllib.request as ur
            handler = ur.ProxyHandler({'https': proxy, 'http': proxy})
            opener = ur.build_opener(handler)
            with opener.open(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        else:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        results = []
        for r in data.get('results', []):
            results.append({'title': r.get('title', ''), 'url': r.get('url', ''), 'content': r.get('content', '')[:500], 'score': r.get('score', 0)})
        return results
    except Exception as e:
        _log('knowledge', 'tavily_error', {'error': str(e)})
        return []

def _call_llm(system_prompt: str, user_prompt: str) -> str:
    text, _ = call_llm(system_prompt, user_prompt, max_tokens=2000, timeout=30)
    return text

def explore_domain(topic: str, depth: int=3) -> dict:
    """Actively explore a knowledge domain using web search.

    Searches for the topic, synthesizes findings into structured knowledge.
    """
    search_results = _tavily_search(topic, max_results=5)
    if not search_results:
        return {'status': 'no_results', 'topic': topic, 'findings': []}
    system = 'You are a knowledge synthesis engine. Given web search results about a topic,\nextract the most useful and actionable information. Return a JSON array of findings:\n[{"title": "...", "summary": "concise summary", "source_url": "...", "relevance": "high/medium/low"}]\n\nFocus on practical, actionable knowledge rather than abstract concepts.'
    search_text = json.dumps(search_results, ensure_ascii=False)
    try:
        response = _call_llm(system, f'Topic: {topic}\n\nSearch results:\n{search_text}')
        if '[' in response and ']' in response:
            start = response.index('[')
            end = response.rindex(']') + 1
            findings = json.loads(response[start:end])
        else:
            findings = []
    except (json.JSONDecodeError, ValueError):
        findings = []
    if findings:
        from .knowledge import knowledge_ingest
        slug = re.sub(r'[^a-zA-Z0-9一-鿿]+', '_', topic)[:40].strip('_') or 'topic'
        out_dir = EXPLORATION_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        content = f'# Knowledge Exploration: {topic}\n\n'
        content += f'> Generated {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}\n\n'
        for f in findings:
            content += f"## {f.get('title', 'Untitled')}\n"
            content += f"{f.get('summary', '')}\n"
            content += f"Source: {f.get('source_url', 'N/A')}\n\n"
        out_path = out_dir / f'{slug}_{int(time.time())}.md'
        out_path.write_text(content)
        # Ingest from a project-root path so knowledge_ingest's validator
        # accepts it. The markdown artifact is kept for provenance/traceability.
        ingest_result = knowledge_ingest(str(out_path), title=f'Exploration: {topic}')
    else:
        ingest_result = None
    result = {'status': 'completed', 'topic': topic, 'sources_found': len(search_results), 'findings': findings, 'ingested': bool(ingest_result and ingest_result.get('status') == 'ingested')}
    _log('knowledge', 'explore_domain', {'topic': topic, 'findings': len(findings)})
    return result

def discover_knowledge_gaps() -> list[dict]:
    """Analyze action_log failures to identify knowledge gaps.

    Compares failure patterns against current knowledge base coverage
    and returns topics that need exploration.
    """
    actions = get_recent_actions(limit=50)
    failures = [a for a in actions if a.get('outcome') == 'failure']
    if not failures:
        return []
    failure_types: dict[str, list[dict]] = {}
    for a in failures:
        action_type = a.get('action_type', 'unknown')
        failure_types.setdefault(action_type, []).append(a)
    system = 'You are a knowledge gap analyzer. Given failure patterns from an AI agent\'s action log,\nidentify knowledge gaps that, if filled, would prevent these failures.\n\nReturn a JSON array of gaps:\n[{"topic": "...", "description": "what knowledge is missing", "failure_count": N, "priority": "high/medium/low"}]\n\nFocus on gaps that are actionable — specific topics that can be researched and learned.'
    failure_summary = []
    for ftype, items in failure_types.items():
        failure_summary.append({'action_type': ftype, 'count': len(items), 'examples': [i.get('action_detail', '')[:100] for i in items[:3]]})
    try:
        response = _call_llm(system, json.dumps(failure_summary, ensure_ascii=False))
        if '[' in response and ']' in response:
            start = response.index('[')
            end = response.rindex(']') + 1
            gaps = json.loads(response[start:end])
        else:
            gaps = []
    except (json.JSONDecodeError, ValueError):
        gaps = []
    _log('knowledge', 'discover_gaps', {'failures': len(failures), 'gaps': len(gaps)})
    return gaps

def synthesize_from_experience() -> list[dict]:
    """Extract reusable patterns from successful action sequences.

    Finds consecutive successful actions and asks LLM to identify
    repeatable patterns that can become agent rules.
    """
    actions = get_recent_actions(limit=100)
    successes = [a for a in actions if a.get('outcome') == 'success']
    if len(successes) < 3:
        return []
    system = 'You are an experience synthesis engine. Given a sequence of successful actions by an AI agent,\nidentify reusable patterns that could become rules or heuristics.\n\nReturn a JSON array of patterns:\n[{"pattern": "description of the pattern", "rule": "concrete rule to follow", "confidence": 0.7-0.95, "evidence": "what success sequences support this"}]\n\nRules should be:\n- Specific and actionable (not vague)\n- Based on repeated success, not single incidents\n- Written as positive rules ("do X") or negative rules ("avoid Y")'
    success_summary = []
    for a in successes[:20]:
        success_summary.append({'action_type': a.get('action_type', ''), 'detail': a.get('action_detail', '')[:150], 'rules_applied': a.get('rule_ids_applied', '')})
    try:
        response = _call_llm(system, json.dumps(success_summary, ensure_ascii=False))
        if '[' in response and ']' in response:
            start = response.index('[')
            end = response.rindex(']') + 1
            patterns = json.loads(response[start:end])
        else:
            patterns = []
    except (json.JSONDecodeError, ValueError):
        patterns = []
    from .evo_models import add_rule, get_rules
    existing = {r['content'] for r in get_rules(kind='knowledge_pattern')}
    stored = []
    for p in patterns:
        content = p.get('rule', '')
        confidence = p.get('confidence', 0.7)
        if content and content not in existing and (confidence >= 0.75):
            rule_id = add_rule(kind='knowledge_pattern', content=content, confidence=confidence, source='experience_synthesis')
            stored.append({**p, 'rule_id': rule_id, 'action': 'added'})
            existing.add(content)
    _log('knowledge', 'synthesize_experience', {'successes': len(successes), 'patterns': len(stored)})
    return stored


# ── Scheduled knowledge exploration ──

def _load_explored_topics() -> dict:
    """Load {topic: ISO-timestamp} of recently explored topics from evo meta."""
    raw = get_meta(EXPLORED_META_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _save_explored_topics(explored: dict):
    set_meta(EXPLORED_META_KEY, explored)


def _topic_recently_explored(explored: dict, topic: str, dedup_days: int) -> bool:
    """True if `topic` was explored within `dedup_days` (cost gate)."""
    ts = explored.get(topic)
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
        return age_days < dedup_days
    except (ValueError, TypeError):
        return False


def _dedup_pick(pool: list[str], explored: dict, max_topics: int,
                dedup_days: int, already: list[str]) -> tuple[list[str], int]:
    """Pick topics from pool, skipping recently-explored ones.

    Returns (new_picked, skipped_count). `already` holds topics already picked
    this run so the cap is enforced across pools.
    """
    picked: list[str] = []
    skipped = 0
    for t in pool:
        if len(already) + len(picked) >= max_topics:
            break
        if not t or t in already:
            continue
        if _topic_recently_explored(explored, t, dedup_days):
            skipped += 1
            continue
        picked.append(t)
    return picked, skipped


def run_knowledge_exploration(max_topics: int = 2, dedup_days: int = 14,
                              fallback_topics: list[str] | None = None) -> dict:
    """Scheduled knowledge exploration: gaps → topics → explore → ingest.

    Orchestration wrapper for the cron daemon (evolution.cron_explore →
    evolution.explore job). Pipeline:
      1. discover_knowledge_gaps() — topics derived from recent action_log
         failures (costs an LLM call only when failures exist).
      2. If failures yield no topics, fall back to a curated evergreen topic
         list so the KB keeps accumulating exploration docs.
      3. Cap to max_topics (default 2), skipping topics explored within
         dedup_days — each topic is 1 web search + 1 LLM synthesis call, so
         this is the primary cost gate.
      4. explore_domain() per topic writes a markdown doc and ingests it into
         the knowledge base. Each topic is failure-isolated so one bad explore
         can't abort the whole pass.

    Returns a summary dict suitable for the cron daemon's output file.
    """
    from .evolution import _is_paused
    if _is_paused():
        _log('knowledge', 'explore_paused_skip', {})
        return {'status': 'paused', 'gaps_found': 0, 'topics_picked': 0,
                'skipped_recent': 0, 'results': [], 'source': 'paused'}

    # 1. Gap discovery (no LLM cost when action_log has no failures).
    try:
        gaps = discover_knowledge_gaps() or []
    except Exception as e:
        _log('knowledge', 'explore_gaps_failed', {'error': str(e)})
        gaps = []
    gap_topics = [(g.get('topic') or '').strip() for g in gaps if g.get('topic')]
    curated = fallback_topics if fallback_topics is not None else list(DEFAULT_EXPLORE_TOPICS)

    # 2. Dedup + cap across pools (gaps first, then curated top-up).
    explored = _load_explored_topics()
    picked: list[str] = []
    skipped = 0
    new_p, sk = _dedup_pick(gap_topics, explored, max_topics, dedup_days, picked)
    picked.extend(new_p)
    skipped += sk
    from_gaps = bool(new_p)
    if len(picked) < max_topics:
        new_p, sk = _dedup_pick(curated, explored, max_topics, dedup_days, picked)
        picked.extend(new_p)
        skipped += sk
    source = 'failure_gaps' if from_gaps else 'fallback_topics'

    # 3. Explore each picked topic (failure-isolated; ingest happens inside
    #    explore_domain). Markdown artifact is kept in knowledge/explorations/.
    results = []
    for topic in picked:
        try:
            r = explore_domain(topic, depth=2)
            results.append({
                'topic': topic,
                'status': r.get('status'),
                'findings': len(r.get('findings', [])),
                'ingested': r.get('ingested', False),
            })
            if r.get('status') == 'completed':
                explored[topic] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            logger.exception("run_knowledge_exploration: topic %r failed", topic)
            results.append({'topic': topic, 'status': 'error', 'error': str(e)})

    _save_explored_topics(explored)
    _log('knowledge', 'run_exploration', {
        'gaps_found': len(gaps), 'source': source,
        'topics_picked': len(picked), 'skipped_recent': skipped,
        'explored': len(results),
    })
    return {
        'status': 'completed',
        'gaps_found': len(gaps),
        'source': source,
        'topics_picked': len(picked),
        'skipped_recent': skipped,
        'results': results,
    }

def sink_evolution_knowledge(user_id: str = 'default') -> dict:
    """Sink the evolution system's learned knowledge into the knowledge base.

    Root-cause gap: the knowledge base held only external explorations and
    skills — the actual wisdom the system learned (user-model observations,
    behavior rules, memories) never entered it. This closes that loop by
    writing a "learned knowledge" document built from:
      - honcho observations (what we've observed about the user / world)
      - agent_rules (behavior rules distilled from corrections)
      - memories (cross-session memory entries)

    Idempotent: rewrites a stable titled doc each run (knowledge_ingest upserts
    by title).
    """
    try:
        import sqlite3
        from .honcho.models import get_honcho_db
        from .paths import EVO_DB_PATH, MEMORY_DB
        sections = []

        # 1. Behavior rules (evo.db agent_rules)
        try:
            conn = sqlite3.connect(str(EVO_DB_PATH))
            conn.row_factory = sqlite3.Row
            rules = conn.execute(
                "SELECT content, effectiveness, source FROM agent_rules WHERE active=1 ORDER BY effectiveness DESC LIMIT 50"
            ).fetchall()
            if rules:
                s = ['## 行为规则（从纠正中学到）', '']
                for r in rules:
                    s.append(f"- {r['content']} (eff={r['effectiveness']:.2f})")
                sections.append('\n'.join(s))
            conn.close()
        except Exception:
            logger.exception('sink: rules')

        # 2. Observations (honcho)
        try:
            honcho = get_honcho_db()
            obs = honcho.execute(
                "SELECT category, content FROM observations WHERE user_id=? ORDER BY timestamp DESC LIMIT 40",
                (user_id,)
            ).fetchall()
            if obs:
                s = ['## 观察（收割的原始信号）', '']
                for o in obs:
                    s.append(f"- [{o['category']}] {o['content']}")
                sections.append('\n'.join(s))
            honcho.close()
        except Exception:
            logger.exception('sink: observations')

        # 3. Memories
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            conn.row_factory = sqlite3.Row
            mem = conn.execute(
                "SELECT category, content FROM memories ORDER BY id DESC LIMIT 30"
            ).fetchall()
            if mem:
                s = ['## 跨会话记忆', '']
                for m in mem:
                    s.append(f"- [{m['category']}] {m['content']}")
                sections.append('\n'.join(s))
            conn.close()
        except Exception:
            logger.exception('sink: memories')

        if not sections:
            return {'status': 'no_data'}

        from .knowledge import knowledge_ingest
        content = '# 进化系统学到的知识\n\n' + '\n\n'.join(sections) + '\n'
        # knowledge_ingest takes a file path (validated against allowed dirs);
        # write a temp artifact under EXPLORATION_DIR then ingest it.
        import time as _t
        out = EXPLORATION_DIR / f'evolution_sink_{int(_t.time())}.md'
        out.write_text(content, encoding='utf-8')
        result = knowledge_ingest(str(out), title='进化系统学习沉淀', doc_type='markdown')
        return {'status': 'ingested', 'sections': len(sections), 'result': result}
    except Exception:
        logger.exception('sink_evolution_knowledge failed')
        return {'status': 'error'}
