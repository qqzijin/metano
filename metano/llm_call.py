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


def _resolve_provider() -> tuple[str, str, str]:
    """Resolve (base_url, api_key, model) for the evolution LLM channel.

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
                    p.model or ANTHROPIC_MODEL)
    except Exception:
        logger.exception("llm_call: provider resolution failed, using env")
    return ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY, ANTHROPIC_MODEL

# Cost per million tokens (USD) — update as pricing changes
_COST_PER_MILLION = {
    'claude-sonnet-4-6': (3.0, 15.0),    # (input, output)
    'claude-haiku-4-5-20251001': (0.80, 4.0),
    'claude-opus-4-8': (15.0, 75.0),
    'claude-opus-4-7': (15.0, 75.0),
    'claude-opus-4-6': (15.0, 75.0),
    # DeepSeek V4 Flash off-peak rates (peak weekday hours are ~2x; cache-hit
    # input is cheaper). Announced a significant price hike effective soon —
    # revisit these when the new rates land.
    'deepseek-v4-flash': (0.14, 0.28),
}

# Fallback pricing for unknown models
_DEFAULT_COST = (3.0, 15.0)


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost from token counts (config price table preferred)."""
    try:
        from .model_router import model_router
        return model_router.estimate_cost(model, input_tokens, output_tokens)
    except Exception:
        pass
    input_price, output_price = _COST_PER_MILLION.get(model, _DEFAULT_COST)
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 3000,
             timeout: int = 60) -> tuple[str, float]:
    """Call Claude API and return (response_text, estimated_cost_usd).

    All evolution system LLM calls should go through this function
    so costs are consistently tracked.
    """
    base_url, api_key, model = _resolve_provider()
    if not api_key:
        return '[]', 0.0
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
    cost = 0.0
    try:
        # Anthropic SDK always posts to /v1/messages. base_url may or may not
        # already end in /v1 (e.g. api.anthropic.com/v1 vs a custom proxy root).
        endpoint = f'{base_url}/messages' if base_url.rstrip('/').endswith('/v1') else f'{base_url}/v1/messages'
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode())
            content = result.get('content', [])
            usage = result.get('usage', {})
            input_tokens = usage.get('input_tokens', 0)
            output_tokens = usage.get('output_tokens', 0)
            cost = _estimate_cost(model, input_tokens, output_tokens)
            # Record cost in audit log
            try:
                from .evo_models import add_audit
                add_audit('llm', 'api_call', json.dumps({
                    'model': model,
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                }, ensure_ascii=False), cost=cost, model=model)
            except Exception:
                pass
            # Some providers (e.g. DeepSeek via proxy) emit a leading
            # 'thinking' block — take the first real 'text' block instead.
            text = next((c.get('text') for c in content if c.get('type') == 'text'), None)
            if text is not None:
                return text, cost
            if content and content[0].get('type') == 'thinking':
                return content[0].get('thinking', ''), cost
            return str(content), cost
    except Exception:
        logger.exception("llm_call: API request failed")
        return '[]', cost


def get_llm_config() -> dict:
    """Return current LLM configuration."""
    base_url, api_key, model = _resolve_provider()
    return {
        'model': model,
        'base_url': base_url,
        'has_api_key': bool(api_key),
    }
