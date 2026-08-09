"""Knowledge exploration engine: semantic search, active exploration, gap detection.

Integrates CocoIndex for semantic code search and Tavily for web-based
knowledge discovery. Detects knowledge gaps from action_log failure patterns
and proactively explores to fill them.
"""
import json
import os
import subprocess
import time
import urllib.request
from .evo_models import get_recent_actions, get_meta, set_meta
from .evolution import _log
from .llm_call import call_llm
from metano.log import logger
TAVILY_API_KEY = os.environ.get('TAVILY_API_KEY', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_BASE_URL = os.environ.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com/v1')
ANTHROPIC_MODEL = os.environ.get('HONCHO_MODEL', 'claude-sonnet-4-6')

def semantic_search(query: str, project: str='') -> dict:
    """Search indexed codebases using CocoIndex semantic search.

    Falls back to keyword search if ccc is unavailable.
    """
    try:
        # ccc searches the project indexed in a directory; it has no --project
        # flag, so run in the target project dir when one is given.
        cmd = ['ccc', 'search', query]
        run_cwd = project if project and os.path.isdir(project) else None
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, cwd=run_cwd)
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
    if not TAVILY_API_KEY:
        return []
    payload = {'api_key': TAVILY_API_KEY, 'query': query, 'max_results': max_results, 'search_depth': 'advanced', 'include_raw_content': False}
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
        from pathlib import Path
        import tempfile
        content = f'# Knowledge Exploration: {topic}\n\n'
        for f in findings:
            content += f"## {f.get('title', 'Untitled')}\n"
            content += f"{f.get('summary', '')}\n"
            content += f"Source: {f.get('source_url', 'N/A')}\n\n"
        tmp = Path(tempfile.gettempdir()) / f'evo_explore_{int(time.time())}.md'
        tmp.write_text(content)
        ingest_result = knowledge_ingest(str(tmp), title=f'Exploration: {topic}')
        tmp.unlink(missing_ok=True)
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