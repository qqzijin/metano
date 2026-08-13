"""Strategy optimization engine: track actions, compute effectiveness, select optimal strategies.

Uses action_log from evo.db to build an evidence-based strategy layer.
Implements explore-exploit balance: 90% exploit (known-effective rules),
10% explore (try alternative approaches to discover better strategies).
"""
import json
import os
import time
from .evo_models import log_action, get_recent_actions, get_action_stats, get_rules, add_rule, update_rule_effectiveness, get_meta, set_meta, parse_rule_ids
from .evolution import _log
from .llm_call import call_llm
from metano.log import logger
ANTHROPIC_MODEL = os.environ.get('HONCHO_MODEL', 'claude-sonnet-4-6')


def _llm_provider_available() -> bool:
    """Whether a usable LLM provider exists, resolved at call time.

    M6: the old gate read a module-level ANTHROPIC_API_KEY env snapshot that is
    unset under the cron/daemon process, so the LLM strategy-pattern branch was
    dead at runtime. Reflect the live ModelRouter instead.
    """
    try:
        from .model_router import model_router
        p = model_router.get_provider()
        if p and getattr(p, 'api_key', ''):
            return True
    except Exception:
        logger.exception("strategy: provider resolution failed")
    return bool(os.environ.get('ANTHROPIC_API_KEY', ''))

def record_action(session_id: str, action_type: str, action_detail: str, rule_ids: list[str] | None=None) -> int:
    """Record an agent action for strategy tracking.

    Call this BEFORE the action executes. After outcome is known,
    call record_outcome() with the same action_id.
    """
    # F-08: store rule ids as a JSON array so get_recent_actions / parse_rule_ids
    # can round-trip them (legacy comma-strings still parse via parse_rule_ids).
    rule_ids_str = json.dumps(list(rule_ids)) if rule_ids else json.dumps([])
    action_id = log_action(session_id=session_id, action_type=action_type, action_detail=action_detail, rule_ids_applied=rule_ids_str, outcome='pending')
    return action_id

def record_outcome(action_id: int, outcome: str, detail: str='') -> dict:
    """Record the outcome of a previously logged action.

    Updates the action_log entry and adjusts rule effectiveness.
    outcome: "success" | "failure" | "partial"
    """
    from .evo_models import _get_conn
    conn = _get_conn()
    try:
        row = conn.execute('SELECT * FROM action_log WHERE id = ?', (action_id,)).fetchone()
        if not row:
            return {'status': 'not_found'}
        action = dict(row)
        # F-08: parse both JSON-array and legacy comma-separated storage.
        rule_ids = parse_rule_ids(action.get('rule_ids_applied', ''))
        conn.execute('UPDATE action_log SET outcome = ? WHERE id = ?', (outcome, action_id))
        conn.commit()
        for rid in rule_ids:
            _update_rule_effectiveness(rid, outcome)
        return {'status': 'recorded', 'action_id': action_id, 'outcome': outcome, 'rules_updated': len(rule_ids)}
    finally:
        conn.close()

def _update_rule_effectiveness(rule_id: str, outcome: str):
    """Update a rule's effectiveness based on action outcome."""
    from .evo_models import _get_conn
    conn = _get_conn()
    row = conn.execute('SELECT * FROM agent_rules WHERE id = ?', (rule_id,)).fetchone()
    if not row:
        conn.close()
        return
    rule = dict(row)
    applied = rule.get('times_applied', 0) + 1
    succeeded = rule.get('times_succeeded', 0)
    failed = rule.get('times_failed', 0)
    if outcome == 'success':
        succeeded += 1
    elif outcome == 'failure':
        failed += 1
    effectiveness = (succeeded + 1) / (applied + 2)
    update_rule_effectiveness(rule_id, effectiveness=effectiveness, times_applied=applied, times_succeeded=succeeded, times_failed=failed)

def get_effectiveness(rule_id: str) -> dict:
    """Get effectiveness metrics for a specific rule."""
    from .evo_models import _get_conn
    conn = _get_conn()
    try:
        row = conn.execute('SELECT * FROM agent_rules WHERE id = ?', (rule_id,)).fetchone()
        if not row:
            return {'rule_id': rule_id, 'found': False}
        r = dict(row)
        return {'rule_id': r['id'], 'found': True, 'content': r['content'][:80], 'effectiveness': r['effectiveness'], 'times_applied': r['times_applied'], 'times_succeeded': r['times_succeeded'], 'times_failed': r['times_failed'], 'active': bool(r['active'])}
    finally:
        conn.close()

def select_strategy(context: str='') -> list[dict]:
    """Select optimal rules to apply for a given context.

    90% exploit: pick highest-effectiveness rules
    10% explore: try less-tested rules to discover better strategies
    """
    import random
    rules = get_rules(active_only=True)
    if not rules:
        return []
    scored = []
    for r in rules:
        eff = r.get('effectiveness', 0.0)
        applied = r.get('times_applied', 0)
        score = eff
        if context:
            context_lower = context.lower()
            content_lower = r['content'].lower()
            overlap = sum((1 for w in content_lower.split() if w in context_lower))
            score += overlap * 0.05
        if applied < 3:
            score += 0.15
        scored.append({**r, 'strategy_score': score})
    scored.sort(key=lambda x: x['strategy_score'], reverse=True)
    n_exploit = max(1, int(len(scored) * 0.9))
    exploit_pool = scored[:n_exploit]
    explore_pool = scored[n_exploit:]
    selected = exploit_pool[:5]
    if explore_pool and random.random() < 0.1:
        selected.append(random.choice(explore_pool))
    _log('strategy', 'select', {'context': context[:100], 'rules_considered': len(scored), 'rules_selected': len(selected)})
    return selected

def detect_strategy_patterns() -> list[dict]:
    """Analyze action_log to discover new strategy patterns.

    Finds sequences where specific rule combinations lead to
    consistently good or bad outcomes.
    """
    actions = get_recent_actions(limit=200)
    if len(actions) < 10:
        return []
    by_type: dict[str, list[dict]] = {}
    for a in actions:
        atype = a.get('action_type', 'unknown')
        by_type.setdefault(atype, []).append(a)
    patterns = []
    for atype, atype_actions in by_type.items():
        if len(atype_actions) < 3:
            continue
        successes = [a for a in atype_actions if a.get('outcome') == 'success']
        failures = [a for a in atype_actions if a.get('outcome') == 'failure']
        if not successes:
            continue
        success_rules: dict[str, int] = {}
        failure_rules: dict[str, int] = {}
        for a in successes:
            # get_recent_actions already normalized rule_ids_applied to a list.
            for rid in (a.get('rule_ids_applied') or []):
                rid = str(rid).strip()
                if rid:
                    success_rules[rid] = success_rules.get(rid, 0) + 1
        for a in failures:
            for rid in (a.get('rule_ids_applied') or []):
                rid = str(rid).strip()
                if rid:
                    failure_rules[rid] = failure_rules.get(rid, 0) + 1
        for rid, s_count in success_rules.items():
            f_count = failure_rules.get(rid, 0)
            total = s_count + f_count
            if total >= 2 and s_count / total >= 0.7:
                rule = next((r for r in get_rules() if str(r['id']) == rid), None)
                if rule:
                    patterns.append({'type': 'effective_rule', 'action_type': atype, 'rule_id': rid, 'rule_content': rule['content'][:80], 'success_rate': s_count / total, 'sample_size': total})
    if patterns or len(actions) >= 30:
        llm_patterns = _llm_detect_patterns(actions)
        patterns.extend(llm_patterns)
    _log('strategy', 'detect_patterns', {'actions_analyzed': len(actions), 'patterns': len(patterns)})
    return patterns

def _llm_detect_patterns(actions: list[dict]) -> list[dict]:
    """Use LLM to detect subtle strategy patterns from action log."""
    if not _llm_provider_available():
        return []
    system = 'You are a strategy pattern detector. Given an AI agent\'s action log with outcomes,\nidentify patterns where specific approaches or rule combinations consistently lead to success or failure.\n\nReturn a JSON array of patterns:\n[{"pattern": "description", "rule_suggestion": "concrete rule to add/modify", "confidence": 0.7-0.95, "evidence": "what data supports this"}]\n\nFocus on:\n- Rule combinations that work well together\n- Contexts where certain strategies fail\n- Timing or sequencing patterns'
    summary = []
    for a in actions[:50]:
        summary.append({'action_type': a.get('action_type', ''), 'detail': a.get('action_detail', '')[:100], 'rules': a.get('rule_ids_applied', ''), 'outcome': a.get('outcome', '')})
    try:
        response, _ = call_llm(system, json.dumps(summary, ensure_ascii=False), max_tokens=2000, timeout=30, session_id='')
        if '[' in response and ']' in response:
            start = response.index('[')
            end = response.rindex(']') + 1
            return json.loads(response[start:end])
    except Exception:
        logger.exception("strategy: LLM pattern detection failed")
    return []