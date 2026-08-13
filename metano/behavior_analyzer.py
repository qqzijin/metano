"""Behavior pattern analyzer: detect recurring mistakes and generate improvement rules.

Reads correction/tool_error observations, clusters them by similarity,
and generates agent rules stored in evo.db (separate from user modeling).
"""
import json
import os
import time
from .honcho.models import get_honcho_db, get_observations, get_beliefs
from .evo_models import add_rule, get_rules, init_db as init_evo_db
from .evolution import _log
from .llm_call import call_llm
from metano.log import logger
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_MODEL = os.environ.get('HONCHO_MODEL', 'claude-sonnet-4-6')

def _call_llm(system_prompt: str, user_prompt: str, session_id: str = '') -> str:
    text, _ = call_llm(system_prompt, user_prompt, max_tokens=2000, timeout=30, session_id=session_id)
    return text

def _cluster_corrections(corrections: list[dict]) -> dict[str, list[dict]]:
    """Group correction observations by similarity of keywords."""
    clusters: dict[str, list[dict]] = {}
    for c in corrections:
        content = c.get('content', '').lower()
        assigned = False
        # NOTE: bare 'id' was removed from field_mismatch — it matches any
        # string containing "id" (kid/did/modified-id) and misclassified
        # unrelated corrections. Only explicit field terms remain.
        keywords_map = {'field_mismatch': ['字段', 'field', '不匹配', '不显示', '显示不出来', 'doc_id', 'chunk'], 'no_verification': ['验证', 'verify', 'curl', '真的做过验证', '没有验证', '没验证'], 'repeat_mistake': ['重复', '又来', '又重复', 'again', 'repeat', '为什么又', '为什么总是'], 'reinvent_wheel': ['造轮子', '重新写', '重写', '已有', '复用', '已有的组件'], 'ui_quality': ['简陋', '太简单', '不够完善', '不完善', '功能缺失']}
        for cluster_name, keywords in keywords_map.items():
            if any((kw in content for kw in keywords)):
                clusters.setdefault(cluster_name, []).append(c)
                assigned = True
                break
        if not assigned:
            clusters.setdefault('other', []).append(c)
    return clusters

def analyze_behavior_patterns(user_id: str='default', days: int=7, session_id: str='') -> dict:
    """Analyze correction and error observations to find recurring patterns.

    Returns a dict with:
    - patterns: list of detected behavior_pattern beliefs
    - corrections_analyzed: count of correction observations processed
    - tool_errors_analyzed: count of tool_error observations processed

    N16: ``session_id`` (when known, e.g. from immediate_learn triggered by a
    session_end hook) is threaded to the LLM call so its audit cost row is
    attributed to the originating session.
    """
    conn = get_honcho_db()
    try:
        cutoff = time.time() - days * 86400
        rows = conn.execute("SELECT * FROM observations WHERE user_id = ? AND timestamp >= ? AND (category = 'correction' OR category = 'tool_error') ORDER BY timestamp DESC LIMIT 50", (user_id, cutoff)).fetchall()
        observations = [dict(r) for r in rows]
        corrections = [o for o in observations if o['category'] == 'correction']
        tool_errors = [o for o in observations if o['category'] == 'tool_error']
        if not corrections and (not tool_errors):
            return {'status': 'no_data', 'corrections_analyzed': 0, 'tool_errors_analyzed': 0, 'patterns': []}
        clusters = _cluster_corrections(corrections)
        cluster_summary = []
        for name, items in clusters.items():
            cluster_summary.append({'pattern_type': name, 'count': len(items), 'examples': [i['content'][:150] for i in items[:3]], 'strengths': [i.get('content', '') for i in items if 'strong' in i.get('content', '').lower() or '又' in i.get('content', '') or '为什么' in i.get('content', '')]})
        tool_error_summary = []
        for e in tool_errors[:5]:
            tool_error_summary.append({'content': e['content'][:150]})
        system = 'You are a behavior pattern analyzer for an AI coding assistant. Given clusters of user corrections and tool errors, identify recurring behavioral patterns and generate concrete improvement rules.\n\nReturn a JSON array of behavior patterns, each with:\n- "content": a concise, actionable rule in Chinese (e.g., "修改后端API后必须同步修改前端TS类型和hooks")\n- "category": "behavior_pattern"\n- "confidence": 0.7-0.95 (higher for more frequent/stronger corrections)\n- "reasoning": brief explanation of why this pattern was detected\n\nRules for good patterns:\n- Must be specific and actionable, not vague ("必须curl验证" not "要小心")\n- Must address the ROOT CAUSE, not the symptom\n- Must be written as a POSITIVE RULE ("must do X") or NEGATIVE RULE ("must NOT do Y")\n- If a pattern has 3+ corrections with strong language, confidence should be >= 0.85\n- If a pattern has only 1-2 corrections, confidence should be 0.7-0.75'
        user_prompt = f'Correction clusters:\n{json.dumps(cluster_summary, ensure_ascii=False)}\n\nTool errors:\n{json.dumps(tool_error_summary, ensure_ascii=False)}'
        existing_behavior = get_rules(kind='behavior')
        if existing_behavior:
            user_prompt += f"\n\nExisting behavior rules (avoid duplicates):\n{json.dumps([{'content': b['content']} for b in existing_behavior], ensure_ascii=False)}"
        try:
            response = _call_llm(system, user_prompt, session_id=session_id)
            if '[' in response and ']' in response:
                start = response.index('[')
                end = response.rindex(']') + 1
                patterns = json.loads(response[start:end])
            else:
                patterns = []
        except (json.JSONDecodeError, ValueError):
            patterns = []
        stored = []
        existing_contents = {b['content'] for b in existing_behavior}
        for p in patterns:
            content = p.get('content', '')
            confidence = p.get('confidence', 0.7)
            if not content or content in existing_contents:
                continue
            rule_id = add_rule(kind='behavior', content=content, confidence=confidence, source='correction_cluster')
            stored.append({**p, 'action': 'added', 'rule_id': rule_id})
        result = {'status': 'completed', 'corrections_analyzed': len(corrections), 'tool_errors_analyzed': len(tool_errors), 'clusters_found': {k: len(v) for k, v in clusters.items()}, 'patterns': stored}
        _log('behavior_analyze', 'analyze', result)
        return result
    finally:
        conn.close()

def get_behavior_patterns(user_id: str='default') -> dict:
    """Return agent rules from evo.db and recent corrections from honcho."""
    agent_rules = get_rules(kind='behavior')
    conn = get_honcho_db()
    cutoff = time.time() - 30 * 86400
    recent_corrections = conn.execute("SELECT content, timestamp FROM observations WHERE user_id = ? AND category = 'correction' AND timestamp >= ? ORDER BY timestamp DESC LIMIT 20", (user_id, cutoff)).fetchall()
    conn.close()
    return {'patterns': agent_rules, 'recent_corrections': [dict(r) for r in recent_corrections]}