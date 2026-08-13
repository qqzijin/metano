"""Shared LLM call utility with cost tracking for the evolution system."""
import json
import os
import time
import urllib.request
from metano.log import logger

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_BASE_URL = os.environ.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com/v1')
ANTHROPIC_MODEL = os.environ.get('HONCHO_MODEL', 'claude-sonnet-4-6')
# Some proxies sit behind Cloudflare bot protection that blocks the default
# Python-urllib User-Agent with HTTP 1010. A browser UA keeps requests through.
BROWSER_USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')


# Phase under which llm_call records its audit-log cost. Kept in one place so
# the cost-circuit breaker (evolution._estimate_daily_cost) can assert this
# phase is actually included in its evo_phases set (F-10 regression check).
LLM_AUDIT_PHASE = 'llm'


def _resolve_provider() -> tuple[str, str, str, str]:
    """Resolve (base_url, api_key, model, protocol) for the evolution LLM channel.

    Prefers the provider configured in gateway_config.yaml (via ModelRouter) so
    the evolution system uses the same model/endpoint as user-facing chat.
    Falls back to environment variables when no provider is configured.
    """
    try:
        from .model_router import model_router
        p = model_router.get_provider()
        if p:
            return (p.base_url or ANTHROPIC_BASE_URL,
                    p.api_key or ANTHROPIC_API_KEY,
                    p.model or ANTHROPIC_MODEL,
                    (getattr(p, 'protocol', None) or 'anthropic').lower())
    except Exception:
        logger.exception("llm_call: provider resolution failed, using env")
    return ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY, ANTHROPIC_MODEL, 'anthropic'

def _estimate_cost(model: str, input_tokens: int, output_tokens: int,
                   cache_read_tokens: int = 0) -> float:
    """Estimate USD cost from token counts.

    The single pricing authority is ``model_router.estimate_cost`` (config →
    builtin → default fallback; placeholder models like ``<synthetic>`` price
    at 0.0, disabled providers price at their configured rates). The old local
    price table was removed (audit P1-5): it had drifted from the router and
    mis-priced placeholder / disabled providers.
    """
    from .model_router import model_router
    return model_router.estimate_cost(model, input_tokens, output_tokens,
                                      cache_read_tokens)


def _record_cost(model: str, protocol: str, input_tokens: int, output_tokens: int,
                 cache_read_tokens: int, cost: float, session_id: str = ''):
    """Write one audit entry for an LLM API call (best-effort).

    M6: includes cache_read_tokens so the engine cost is not systematically
    under-reported (the /v1/messages response carries cache_read_input_tokens).
    M13: a failed audit write is logged, never silently swallowed.
    N16: the audit row carries the caller's ``session_id`` so LLM cost can be
    attributed to a session when one exists ('' when unattributable).
    """
    try:
        from .evo_models import add_audit
        add_audit(LLM_AUDIT_PHASE, 'api_call', json.dumps({
            'model': model,
            'protocol': protocol,
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'cache_read_tokens': cache_read_tokens,
        }, ensure_ascii=False), cost=cost, model=model, session_id=session_id)
    except Exception:
        logger.exception('llm_call: failed to record audit cost entry')


def _call_anthropic(base_url: str, api_key: str, model: str, system_prompt: str,
                    user_prompt: str, max_tokens: int, timeout: int) -> tuple[str, dict]:
    """POST /v1/messages (Anthropic format). Returns (text, usage)."""
    payload = {
        'model': model,
        'max_tokens': max_tokens,
        'system': system_prompt,
        'messages': [{'role': 'user', 'content': user_prompt}],
    }
    headers = {
        'Content-Type': 'application/json',
        'x-api-key': api_key,
        'anthropic-version': '2023-06-01',
        'User-Agent': BROWSER_USER_AGENT,
    }
    # base_url may or may not already end in /v1 (api.anthropic.com/v1 vs a proxy root).
    endpoint = f'{base_url}/messages' if base_url.rstrip('/').endswith('/v1') else f'{base_url}/v1/messages'
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())
    usage = result.get('usage', {})
    content = result.get('content', [])
    # Some providers (e.g. DeepSeek via proxy) emit a leading 'thinking' block —
    # take the first real 'text' block instead.
    text = next((c.get('text') for c in content if c.get('type') == 'text'), None)
    if text is None and content and content[0].get('type') == 'thinking':
        text = content[0].get('thinking', '')
    if text is None:
        text = str(content)
    usage_norm = {
        'input_tokens': usage.get('input_tokens', 0) or 0,
        'output_tokens': usage.get('output_tokens', 0) or 0,
        # Anthropic /v1/messages reports cache reads as a per-request counter.
        'cache_read_tokens': usage.get('cache_read_input_tokens', 0) or 0,
    }
    return text, usage_norm


def _call_openai(base_url: str, api_key: str, model: str, system_prompt: str,
                 user_prompt: str, max_tokens: int, timeout: int) -> tuple[str, dict]:
    """POST /chat/completions (OpenAI-compatible format). Returns (text, usage)."""
    base = (base_url or '').rstrip('/')
    endpoint = base + '/chat/completions'
    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'max_tokens': max_tokens,
    }
    headers = {'Content-Type': 'application/json', 'User-Agent': BROWSER_USER_AGENT}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())
    usage = result.get('usage', {})
    choices = result.get('choices') or []
    if choices:
        msg = choices[0].get('message', {})
        text = (msg.get('content') or '').strip() or '(empty response)'
    else:
        text = str(result)
    usage_norm = {
        'input_tokens': usage.get('prompt_tokens', 0) or 0,
        'output_tokens': usage.get('completion_tokens', 0) or 0,
        # OpenAI-compatible proxies may expose prompt-cache hits under a few
        # names; best-effort so engine cost is not under-reported.
        'cache_read_tokens': (usage.get('prompt_tokens_details', {}) or {}).get('cached_tokens', 0)
                             or usage.get('cache_read_input_tokens', 0) or 0,
    }
    return text, usage_norm


def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 3000,
             timeout: int = 60, session_id: str = '') -> tuple[str, float]:
    """Call the configured LLM and return (response_text, estimated_cost_usd).

    All evolution system LLM calls should go through this function so costs are
    consistently tracked. F-02: the request format/headers follow the provider's
    ``protocol`` — Anthropic ``/v1/messages`` for ``anthropic``, OpenAI
    ``/v1/chat/completions`` for ``openai`` (Ollama/DeepSeek/OpenRouter/…).

    N16: ``session_id`` is written to the ``audit_log`` cost row so LLM spend
    can be attributed to the calling session. Pass '' when the call has no
    session context (the field is still populated on the audit row).
    """
    base_url, api_key, model, protocol = _resolve_provider()
    if not api_key:
        return '[]', 0.0
    # M5: cost circuit breaker pre-check — block every LLM call while the
    # evolution engine is cost-paused/stopped, instead of only the 03:03 daily
    # maintenance pass. Lazy import avoids a circular dependency at module load.
    try:
        from .evolution import _get_circuit_state
        if _get_circuit_state().get('state') in ('paused', 'stopped'):
            return '[]', 0.0
    except Exception:
        pass
    cost = 0.0
    try:
        if protocol == 'openai':
            text, usage = _call_openai(base_url, api_key, model, system_prompt,
                                       user_prompt, max_tokens, timeout)
        else:
            text, usage = _call_anthropic(base_url, api_key, model, system_prompt,
                                          user_prompt, max_tokens, timeout)
        input_tokens = usage.get('input_tokens', 0) or 0
        output_tokens = usage.get('output_tokens', 0) or 0
        cache_read_tokens = usage.get('cache_read_tokens', 0) or 0
        cost = _estimate_cost(model, input_tokens, output_tokens, cache_read_tokens)
        _record_cost(model, protocol, input_tokens, output_tokens, cache_read_tokens, cost, session_id)
        return text, cost
    except Exception:
        logger.exception("llm_call: API request failed")
        return '[]', cost


def get_llm_config() -> dict:
    """Return current LLM configuration."""
    base_url, api_key, model, protocol = _resolve_provider()
    return {
        'model': model,
        'base_url': base_url,
        'protocol': protocol,
        'has_api_key': bool(api_key),
    }
