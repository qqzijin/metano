"""Shared LLM call utility with cost tracking for the evolution system."""
import json
import os
import time
import urllib.request
from metano.log import logger

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_BASE_URL = os.environ.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com/v1')
ANTHROPIC_MODEL = os.environ.get('HONCHO_MODEL', 'claude-sonnet-4-6')

# Cost per million tokens (USD) — update as pricing changes
_COST_PER_MILLION = {
    'claude-sonnet-4-6': (3.0, 15.0),    # (input, output)
    'claude-haiku-4-5-20251001': (0.80, 4.0),
    'claude-opus-4-8': (15.0, 75.0),
    'claude-opus-4-7': (15.0, 75.0),
    'claude-opus-4-6': (15.0, 75.0),
}

# Fallback pricing for unknown models
_DEFAULT_COST = (3.0, 15.0)


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost from token counts."""
    input_price, output_price = _COST_PER_MILLION.get(model, _DEFAULT_COST)
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 3000,
             timeout: int = 60) -> tuple[str, float]:
    """Call Claude API and return (response_text, estimated_cost_usd).

    All evolution system LLM calls should go through this function
    so costs are consistently tracked.
    """
    if not ANTHROPIC_API_KEY:
        return '[]', 0.0
    payload = {
        'model': ANTHROPIC_MODEL,
        'max_tokens': max_tokens,
        'system': system_prompt,
        'messages': [{'role': 'user', 'content': user_prompt}],
    }
    headers = {
        'Content-Type': 'application/json',
        'x-api-key': ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
    }
    cost = 0.0
    try:
        req = urllib.request.Request(
            f'{ANTHROPIC_BASE_URL}/messages',
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode())
            content = result.get('content', [])
            usage = result.get('usage', {})
            input_tokens = usage.get('input_tokens', 0)
            output_tokens = usage.get('output_tokens', 0)
            cost = _estimate_cost(ANTHROPIC_MODEL, input_tokens, output_tokens)
            # Record cost in audit log
            try:
                from .evo_models import add_audit
                add_audit('llm', 'api_call', json.dumps({
                    'model': ANTHROPIC_MODEL,
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                }, ensure_ascii=False), cost=cost, model=ANTHROPIC_MODEL)
            except Exception:
                pass
            if content and content[0].get('type') == 'text':
                return content[0]['text'], cost
            return str(content), cost
    except Exception:
        logger.exception("llm_call: API request failed")
        return '[]', cost


def get_llm_config() -> dict:
    """Return current LLM configuration."""
    return {
        'model': ANTHROPIC_MODEL,
        'base_url': ANTHROPIC_BASE_URL,
        'has_api_key': bool(ANTHROPIC_API_KEY),
    }
