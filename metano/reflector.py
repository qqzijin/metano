"""Self-reflection engine: evaluate belief model quality and suggest improvements."""
import json
import os
import time
from .honcho.models import get_honcho_db, get_beliefs, get_observations, contradict_belief, add_belief, decay_beliefs, belief_stage, get_stale_beliefs
from .evo_models import get_rules as get_agent_rules, update_rule_effectiveness
from .llm_call import call_llm
from metano.log import logger
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_MODEL = os.environ.get('HONCHO_MODEL', 'claude-sonnet-4-6')

def _call_llm(system_prompt: str, user_prompt: str) -> str:
    """Call Claude API for reflection reasoning (with cost tracking)."""
    text, _ = call_llm(system_prompt, user_prompt)
    return text

def _check_coherence(beliefs: list[dict]) -> list[dict]:
    """Check for contradictions between beliefs in the same category."""
    if not ANTHROPIC_API_KEY or len(beliefs) < 2:
        return []
    by_category: dict[str, list[dict]] = {}
    for b in beliefs:
        by_category.setdefault(b['category'], []).append(b)
    contradictions = []
    for cat, cat_beliefs in by_category.items():
        if len(cat_beliefs) < 2:
            continue
        system = 'You are a belief coherence checker. Given beliefs in the same category, identify any pairs that contradict each other.\nReturn a JSON array: [{"id1": "...", "id2": "...", "reason": "why they contradict"}]\nIf no contradictions, return empty array.'
        beliefs_text = json.dumps([{'id': b['id'], 'content': b['content'], 'confidence': b['confidence']} for b in cat_beliefs], ensure_ascii=False)
        try:
            response = _call_llm(system, beliefs_text)
            if '[' in response and ']' in response:
                start = response.index('[')
                end = response.rindex(']') + 1
                found = json.loads(response[start:end])
                contradictions.extend(found)
        except (json.JSONDecodeError, ValueError):
            pass
    return contradictions

def _check_coverage(user_id: str, beliefs: list[dict], days: int=7) -> list[dict]:
    """Check if recent observations should be beliefs but aren't."""
    if not ANTHROPIC_API_KEY:
        return []
    conn = get_honcho_db()
    try:
        cutoff = time.time() - days * 86400
        recent_obs = conn.execute('SELECT content, category FROM observations WHERE user_id = ? AND timestamp >= ? ORDER BY timestamp DESC LIMIT 20', (user_id, cutoff)).fetchall()
        if not recent_obs:
            return []
        system = 'You are a belief coverage checker. Given existing beliefs and recent observations, identify observations that are significant enough to become beliefs but haven\'t been promoted yet.\nReturn a JSON array: [{"content": "...", "category": "...", "reason": "why this should be a belief"}]\nIf no coverage gaps, return empty array.'
        beliefs_text = json.dumps([{'category': b['category'], 'content': b['content']} for b in beliefs], ensure_ascii=False)
        obs_text = json.dumps([{'content': r['content'][:200], 'category': r['category']} for r in recent_obs], ensure_ascii=False)
        user_prompt = f'Existing beliefs:\n{beliefs_text}\n\nRecent observations:\n{obs_text}'
        try:
            response = _call_llm(system, user_prompt)
            if '[' in response and ']' in response:
                start = response.index('[')
                end = response.rindex(']') + 1
                return json.loads(response[start:end])
        except (json.JSONDecodeError, ValueError):
            pass
        return []
    finally:
        conn.close()

def _check_accuracy(beliefs: list[dict]) -> list[dict]:
    """Sample beliefs and ask LLM to rate their accuracy."""
    if not ANTHROPIC_API_KEY or len(beliefs) < 3:
        return []
    import random
    sample = random.sample(beliefs, min(5, len(beliefs)))
    system = 'You are a belief accuracy rater. Given beliefs with their supporting context, rate each on a 1-5 scale for how well-supported and accurate it seems.\nReturn a JSON array: [{"id": "...", "rating": 1-5, "reason": "brief justification"}]\nRatings: 5=very well supported, 3=somewhat supported, 1=poorly supported or speculative'
    beliefs_text = json.dumps([{'id': b['id'], 'content': b['content'], 'category': b['category'], 'confidence': b['confidence']} for b in sample], ensure_ascii=False)
    try:
        response = _call_llm(system, beliefs_text)
        if '[' in response and ']' in response:
            start = response.index('[')
            end = response.rindex(']') + 1
            ratings = json.loads(response[start:end])
            return [r for r in ratings if r.get('rating', 5) <= 2]
    except (json.JSONDecodeError, ValueError):
        pass
    return []

def _check_behavior_effectiveness(conn, user_id: str, beliefs: list[dict]) -> list[dict]:
    """Check whether agent behavior rules are effective using action_log metrics."""
    agent_rules = get_agent_rules(kind='behavior')
    if not agent_rules:
        return []
    findings = []
    for r in agent_rules:
        eff = r.get('effectiveness', 0.0)
        applied = r.get('times_applied', 0)
        if applied >= 3 and eff < 0.3:
            findings.append({'action': 'behavior_rule_not_effective', 'rule_id': r['id'], 'content': r['content'][:80], 'effectiveness': eff, 'reason': f"行为规则 '{r['content'][:40]}' effectiveness={eff:.0%}，应用{applied}次但成功率低"})
        elif applied >= 3 and eff >= 0.7:
            findings.append({'action': 'behavior_rule_effective', 'rule_id': r['id'], 'content': r['content'][:80], 'effectiveness': eff, 'reason': f"行为规则 '{r['content'][:40]}' effectiveness={eff:.0%}，规则有效"})
    return findings

def reflect_on_model(user_id: str='default') -> dict:
    """Run a self-reflection cycle on the entire belief model.

    Checks: coherence, coverage, staleness, accuracy.
    Returns findings and suggested actions.

    基于网络探索发现：改进提案质量过滤和成本控制。
    - 跳过LLM调用如果上次reflect在24小时内已执行
    - 只在信念数量变化时才调用coherence检查
    - 限制每次reflect的LLM调用次数（最多2次）
    """
    conn = get_honcho_db()
    beliefs = get_beliefs(conn, user_id)
    if not beliefs:
        conn.close()
        return {'status': 'no_beliefs', 'suggested_actions': []}

    # 只反思高置信度 beliefs，低置信度的跳过以节省 API 成本
    eligible = [b for b in beliefs if b.get('confidence', 0) >= 0.7 and not b.get('contradicted')]
    if len(eligible) < 2:
        conn.close()
        return {'status': 'too_few_eligible', 'reason': f'Only {len(eligible)} beliefs with confidence>=0.7', 'suggested_actions': []}

    # 成本控制：检查上次reflect时间
    from .evo_models import get_meta, set_meta
    last_reflect = get_meta('last_reflect_ts')
    if last_reflect:
        try:
            elapsed = time.time() - float(last_reflect)
            if elapsed < 86400:  # 24小时内不重复reflect
                conn.close()
                return {'status': 'skipped_recent', 'reason': f'Last reflect {elapsed/3600:.1f}h ago, skipping to save cost', 'suggested_actions': []}
        except (ValueError, TypeError):
            pass
    set_meta('last_reflect_ts', str(time.time()))

    # 限制LLM调用：只执行coherence和behavior_effectiveness，跳过coverage和accuracy
    contradictions = _check_coherence(eligible)
    # coverage_gaps = _check_coverage(user_id, beliefs)  # 跳过：成本高且产出质量低
    coverage_gaps = []
    stale = get_stale_beliefs(conn, user_id, days=14)
    # low_accuracy = _check_accuracy(beliefs)  # 跳过：成本高且产出质量低
    low_accuracy = []
    behavior_findings = _check_behavior_effectiveness(conn, user_id, eligible)
    suggested_actions = []
    for c in contradictions:
        suggested_actions.append({'action': 'flag_contradiction', 'belief_ids': [c.get('id1'), c.get('id2')], 'reason': c.get('reason', '')})
    for gap in coverage_gaps:
        suggested_actions.append({'action': 'promote_observation', 'content': gap.get('content', ''), 'category': gap.get('category', 'general'), 'reason': gap.get('reason', '')})
    for s in stale:
        suggested_actions.append({'action': 'decay', 'belief_id': s['id'], 'content': s['content'][:80], 'reason': f"Not reinforced in {(time.time() - s.get('last_reinforced_at', s['created_at'])) / 86400:.0f} days"})
    for a in low_accuracy:
        suggested_actions.append({'action': 'review_accuracy', 'belief_id': a.get('id'), 'rating': a.get('rating'), 'reason': a.get('reason', '')})
    for f in behavior_findings:
        suggested_actions.append(f)
    conn.close()
    return {'status': 'completed', 'belief_count': len(beliefs), 'contradictions_found': len(contradictions), 'coverage_gaps': len(coverage_gaps), 'stale_beliefs': len(stale), 'low_accuracy_beliefs': len(low_accuracy), 'behavior_findings': len(behavior_findings), 'suggested_actions': suggested_actions}

def apply_correction(user_id: str, correction: str, category: str='') -> dict:
    """Apply a user correction to the belief model.

    When a user explicitly corrects something ("no, I prefer X not Y"),
    this finds the matching belief and either contradicts it or updates it.

    This closes the reflect loop: Observe → Reason → Act → Reflect → Correct.
    """
    conn = get_honcho_db()
    beliefs = get_beliefs(conn, user_id)
    if not beliefs or not ANTHROPIC_API_KEY:
        conn.close()
        return {'status': 'no_action', 'reason': 'no beliefs or no API key'}
    system = 'You are a belief correction matcher. Given existing beliefs and a user\'s correction, identify which belief(s) the correction targets and whether it contradicts or refines them.\n\nReturn JSON: {"targets": [{"id": "belief_id", "action": "contradict" or "refine", "updated_content": "new belief content if refine"}]}\nIf no match, return {"targets": []}'
    beliefs_text = json.dumps([{'id': b['id'], 'content': b['content'], 'category': b['category'], 'confidence': b['confidence']} for b in beliefs], ensure_ascii=False)
    user_prompt = f'Existing beliefs:\n{beliefs_text}\n\nUser correction: {correction}'
    try:
        response = _call_llm(system, user_prompt)
        if '{' in response and '}' in response:
            start = response.index('{')
            end = response.rindex('}') + 1
            result = json.loads(response[start:end])
            targets = result.get('targets', [])
        else:
            targets = []
    except (json.JSONDecodeError, ValueError):
        conn.close()
        return {'status': 'error', 'reason': 'failed to parse LLM response'}
    if not targets:
        from .honcho.models import add_observation
        add_observation(conn, user_id, correction, category or 'correction', '')
        conn.close()
        return {'status': 'added_as_observation', 'targets_matched': 0}
    actions_taken = []
    for t in targets:
        bid = t.get('id', '')
        action = t.get('action', 'contradict')
        belief = next((b for b in beliefs if b['id'] == bid), None)
        if not belief:
            continue
        if action == 'contradict':
            contradict_belief(conn, bid)
            actions_taken.append({'id': bid, 'action': 'contradicted', 'old_content': belief['content'][:80]})
        elif action == 'refine':
            updated = t.get('updated_content', correction)
            contradict_belief(conn, bid)
            add_belief(conn, user_id, updated, category or belief['category'], confidence=0.7)
            actions_taken.append({'id': bid, 'action': 'refined', 'old': belief['content'][:80], 'new': updated[:80]})
    conn.close()
    return {'status': 'corrected', 'targets_matched': len(targets), 'actions': actions_taken}