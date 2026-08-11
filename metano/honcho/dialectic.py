"""Dialectic reasoning engine for Honcho user modeling.

Uses LLM to:
1. Extract observations from conversation context
2. Detect contradictions between new observations and existing beliefs
3. Create, update, or contradict beliefs accordingly
"""
import json
import os
from .models import get_honcho_db, get_beliefs, add_belief, update_belief, contradict_belief, add_observation, get_observations, reinforce_belief
from ..llm_call import call_llm
from metano.log import logger
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

def _call_llm(system_prompt: str, user_prompt: str) -> str:
    """Call Claude API for dialectic reasoning (with cost tracking)."""
    if not ANTHROPIC_API_KEY:
        return _rule_based_reasoning(system_prompt, user_prompt)
    text, _ = call_llm(system_prompt, user_prompt, max_tokens=2000, timeout=30)
    if not text or text == '[]':
        return _rule_based_reasoning(system_prompt, user_prompt)
    return text

def _rule_based_reasoning(system_prompt: str, user_prompt: str) -> str:
    """Rule-based fallback when no API key is available (or LLM returned empty).

    A missing/no-op LLM response means "nothing meaningful to extract" — the
    correct action is IGNORE, never a fabricated belief. Previously this
    returned {'action':'add', 'content':'User observation recorded'} which
    planted garbage beliefs into the store on every empty LLM reply.
    """
    return json.dumps({'action': 'ignore', 'reasoning': 'No LLM signal; nothing meaningful to store'})

def extract_observations(user_id: str, conversation_text: str) -> list[dict]:
    """Extract user observations from a conversation snippet."""
    conn = get_honcho_db()
    try:
        beliefs = get_beliefs(conn, user_id)
        system = 'You are a user modeling assistant. Analyze the conversation and extract NEW, high-value observations about the user.\nReturn a JSON array of observations, each with:\n- "content": CONCISE observation (one key point, generalizable)\n- "category": one of "preference", "knowledge", "habit", "goal", "personality", "general"\n- "confidence": 0.0-1.0\n\nHard rules:\n- Extract ONLY observations that add NEW information NOT already in the current beliefs (the beliefs are provided below)\n- SKIP anything already covered by an existing belief\n- SKIP trivial one-off details, technical implementation specifics, transient facts\n- Merge similar observations into one concise statement\n- Return [] if nothing new and meaningful'
        user = f"Current beliefs about user:\n{json.dumps([{'category': b['category'], 'content': b['content']} for b in beliefs], ensure_ascii=False)}\n\nConversation:\n{conversation_text[:3000]}"
        try:
            response = _call_llm(system, user)
            if '[' in response and ']' in response:
                start = response.index('[')
                end = response.rindex(']') + 1
                return json.loads(response[start:end])
        except (json.JSONDecodeError, ValueError):
            pass
        return []
    finally:
        conn.close()

def dialectic_reason(user_id: str, observation_content: str, observation_category: str='general') -> dict:
    """Run dialectic reasoning: compare observation against existing beliefs.

    Returns an action dict:
    - action: "add" (new belief), "update" (modify existing), "contradict" (invalidate), "ignore" (not meaningful)
    """
    conn = get_honcho_db()
    try:
        beliefs = get_beliefs(conn, user_id)
        if not beliefs:
            belief = add_belief(conn, user_id, observation_category, observation_content, confidence=0.6)
            return {'action': 'add', 'belief': belief, 'reasoning': 'First belief in this category'}
        system = 'You are a dialectic reasoning engine for user modeling. Given a new observation and existing beliefs, decide what to do.\n\nReturn a JSON object with:\n- "action": "add" (new belief needed), "update" (modify existing belief id), "contradict" (invalidate existing belief id), "ignore" (not meaningful enough)\n- "belief_id": id of affected belief (for update/contradict)\n- "content": new or updated belief content\n- "category": belief category\n- "confidence": 0.0-1.0\n- "reasoning": why you chose this action\n\nDialectic principles (STRICT, to avoid belief bloat):\n- If observation CONFIRMS / is substantially covered by an existing belief → "update" (raise confidence) — DO NOT add a duplicate\n- If observation is trivial, one-off, or adds nothing new → "ignore"\n- Only "add" for genuinely NEW and meaningful beliefs\n- Beliefs must stay CONCISE and generalizable (one key point)'
        beliefs_text = json.dumps([{'id': b['id'], 'category': b['category'], 'content': b['content'], 'confidence': b['confidence']} for b in beliefs], ensure_ascii=False)
        user = f'Existing beliefs:\n{beliefs_text}\n\nNew observation: [{observation_category}] {observation_content}'
        try:
            response = _call_llm(system, user)
            if '{' in response and '}' in response:
                start = response.index('{')
                end = response.rindex('}') + 1
                result = json.loads(response[start:end])
            else:
                result = {'action': 'add', 'content': observation_content, 'category': observation_category, 'confidence': 0.5, 'reasoning': 'Parse fallback'}
        except (json.JSONDecodeError, ValueError):
            result = {'action': 'add', 'content': observation_content, 'category': observation_category, 'confidence': 0.5, 'reasoning': 'Parse error fallback'}
        action = result.get('action', 'add')
        if action == 'add':
            belief = add_belief(conn, user_id, result.get('category', observation_category), result.get('content', observation_content), result.get('confidence', 0.5))
            result['belief'] = belief
        elif action == 'update' and result.get('belief_id'):
            new_content = result.get('content')
            if new_content and new_content != observation_content:
                update_belief(conn, result['belief_id'], new_content, result.get('confidence'))
            reinforce_belief(conn, result['belief_id'])
            row = conn.execute('SELECT * FROM beliefs WHERE id = ?', (result['belief_id'],)).fetchone()
            result['belief'] = dict(row) if row else None
        elif action == 'contradict' and result.get('belief_id'):
            contradict_belief(conn, result['belief_id'])
            belief = add_belief(conn, user_id, result.get('category', observation_category), result.get('content', observation_content), result.get('confidence', 0.5))
            result['new_belief'] = belief
        return result
    finally:
        conn.close()

def compress_beliefs(user_id: str) -> dict:
    """Compress beliefs: merge similar ones, remove low-confidence contradicted ones."""
    conn = get_honcho_db()
    try:
        beliefs = get_beliefs(conn, user_id)
        if len(beliefs) < 5:
            return {'action': 'skip', 'reason': 'Too few beliefs to compress'}
        by_category = {}
        for b in beliefs:
            by_category.setdefault(b['category'], []).append(b)
        merged = 0
        removed = 0
        for cat, cat_beliefs in by_category.items():
            if len(cat_beliefs) < 2:
                continue
            system = 'You are a belief compression engine. Given multiple beliefs in the same category, identify which can be merged.\nReturn a JSON array of merge operations:\n- {"action": "merge", "source_ids": ["id1", "id2"], "merged_content": "combined statement", "confidence": 0.0-1.0}\n- {"action": "remove", "id": "id_to_remove", "reason": "why it\'s redundant"}\nIf no merges needed, return empty array.'
            beliefs_text = json.dumps([{'id': b['id'], 'content': b['content'], 'confidence': b['confidence']} for b in cat_beliefs], ensure_ascii=False)
            try:
                response = _call_llm(system, beliefs_text)
                if '[' in response and ']' in response:
                    start = response.index('[')
                    end = response.rindex(']') + 1
                    ops = json.loads(response[start:end])
                else:
                    ops = []
            except (json.JSONDecodeError, ValueError):
                ops = []
            for op in ops:
                if op.get('action') == 'merge' and len(op.get('source_ids', [])) >= 2:
                    for sid in op['source_ids']:
                        contradict_belief(conn, sid)
                    add_belief(conn, user_id, cat, op['merged_content'], op.get('confidence', 0.6))
                    merged += 1
                elif op.get('action') == 'remove' and op.get('id'):
                    contradict_belief(conn, op['id'])
                    removed += 1
        return {'merged': merged, 'removed': removed, 'total_beliefs_before': len(beliefs)}
    finally:
        conn.close()