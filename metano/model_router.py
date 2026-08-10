"""Multi-model provider support: route requests to different LLM backends."""
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional
from metano.log import logger
from metano.paths import CONFIG_PATH

# Built-in pricing fallback (USD per million tokens): (input, output, cache_read)
BUILTIN_PRICES = {
    'claude-sonnet-4-6': (3.0, 15.0, 0.3),
    'claude-haiku-4-5-20251001': (0.80, 4.0, 0.08),
    'claude-opus-4-8': (15.0, 75.0, 1.5),
    'claude-opus-4-7': (15.0, 75.0, 1.5),
    'claude-opus-4-6': (15.0, 75.0, 1.5),
    'deepseek-v4-flash': (0.14, 0.28, 0.028),
}
DEFAULT_PRICE = (3.0, 15.0, 0.3)  # input, output, cache_read


@dataclass
class ModelProvider:
    name: str
    base_url: str
    api_key: str
    model: str
    max_tokens: int = 4096
    supports_vision: bool = False
    supports_tools: bool = True
    price_input: float = 3.0
    price_output: float = 15.0
    price_cache_read: float = 0.3

class ModelRouter:

    def __init__(self):
        self._providers: dict[str, ModelProvider] = {}
        self._default = 'default'
        self._load_config()

    def _load_config(self):
        """Load model providers from gateway_config.yaml."""
        try:
            import yaml
            config_path = CONFIG_PATH
            if config_path.exists():
                with open(config_path) as f:
                    config = yaml.safe_load(f) or {}
                models_config = config.get('models', {})
                for name, cfg in models_config.items():
                    if cfg.get('enabled', True):
                        # Effective price: explicit config price → builtin table → default
                        # Missing individual fields are filled from builtin/default.
                        price = cfg.get('price') or {}
                        in_p = price.get('input')
                        out_p = price.get('output')
                        cache_p = price.get('cache_read')
                        if in_p is None or out_p is None or cache_p is None:
                            builtin = BUILTIN_PRICES.get(cfg.get('model', ''))
                            if builtin is None:
                                builtin = DEFAULT_PRICE
                            if in_p is None:
                                in_p = builtin[0]
                            if out_p is None:
                                out_p = builtin[1]
                            if cache_p is None:
                                cache_p = builtin[2]
                        self._providers[name] = ModelProvider(name=name, base_url=cfg.get('base_url', ''), api_key=cfg.get('api_key', ''), model=cfg.get('model', ''), max_tokens=cfg.get('max_tokens', 4096), supports_vision=cfg.get('supports_vision', False), supports_tools=cfg.get('supports_tools', True), price_input=in_p, price_output=out_p, price_cache_read=cache_p)
                        if cfg.get('default', False):
                            self._default = name
        except Exception:
            logger.exception()
        if 'default' not in self._providers:
            model_name = os.environ.get('ANTHROPIC_MODEL', '')
            builtin = BUILTIN_PRICES.get(model_name)
            if builtin:
                in_p, out_p, cache_p = builtin
            else:
                in_p, out_p, cache_p = DEFAULT_PRICE
            self._providers['default'] = ModelProvider(name='default', base_url=os.environ.get('ANTHROPIC_BASE_URL', ''), api_key=os.environ.get('ANTHROPIC_API_KEY', ''), model=model_name, price_input=in_p, price_output=out_p, price_cache_read=cache_p)

    def get_provider(self, name: str='') -> ModelProvider:
        """Get a model provider by name."""
        if name and name in self._providers:
            return self._providers[name]
        return self._providers.get(self._default, self._providers['default'])

    def set_default(self, name: str):
        """Set a provider as the default (persisted to config, best-effort)."""
        if name in self._providers:
            self._default = name
            self._persist_default(name)
        else:
            raise ValueError(f'Provider not found: {name}')

    def refresh(self):
        """Reload providers from gateway_config.yaml, preserving the current default if it still exists."""
        prev_default = self._default
        self._providers = {}
        self._default = 'default'
        self._load_config()
        if prev_default in self._providers:
            self._default = prev_default
        return self

    def _persist_default(self, name: str):
        """Best-effort: write the default flag into gateway_config.yaml so it survives restart."""
        try:
            import yaml
            config_path = CONFIG_PATH
            if not config_path.exists():
                return
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            models = config.get('models', {})
            changed = False
            for mname, mcfg in models.items():
                is_def = (mname == name)
                if mcfg.get('default', False) != is_def:
                    mcfg['default'] = is_def
                    changed = True
            if changed:
                with open(config_path, 'w') as f:
                    yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        except Exception:
            logger.exception('failed to persist default model')

    def list_providers(self) -> list[dict]:
        """List all configured model providers."""
        return [{'name': p.name, 'model': p.model, 'base_url': p.base_url[:30] + '...' if p.base_url else '', 'max_tokens': p.max_tokens, 'supports_vision': p.supports_vision, 'is_default': p.name == self._default, 'price': {'input': p.price_input, 'output': p.price_output, 'cache_read': p.price_cache_read}} for p in self._providers.values()]

    @staticmethod
    def estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0) -> float:
        """Estimate USD cost for a model + token counts.

        Resolution order: configured provider whose ``model`` matches →
        builtin pricing table → default price.
        """
        input_price = output_price = cache_price = None
        try:
            for p in model_router._providers.values():
                if p.model and p.model == model:
                    input_price, output_price, cache_price = p.price_input, p.price_output, p.price_cache_read
                    break
        except Exception:
            pass
        if input_price is None:
            builtin = BUILTIN_PRICES.get(model)
            if builtin:
                input_price, output_price, cache_price = builtin
            else:
                input_price, output_price, cache_price = DEFAULT_PRICE
        return (input_tokens * input_price + output_tokens * output_price + cache_read_tokens * (cache_price or 0)) / 1_000_000

    @staticmethod
    def free_provider_presets() -> list[dict]:
        """Return preset configurations for free/low-cost LLM backends."""
        return [{'name': 'ollama-local', 'base_url': 'http://localhost:11434/v1', 'model': 'llama3', 'max_tokens': 4096, 'supports_vision': False, 'note': 'Ollama local, free'}, {'name': 'nvidia-nim', 'base_url': 'https://integrate.api.nvidia.com/v1', 'model': 'meta/llama3-70b-instruct', 'max_tokens': 4096, 'supports_vision': False, 'note': 'NVIDIA NIM free tier'}, {'name': 'deepseek', 'base_url': 'https://api.deepseek.com/v1', 'model': 'deepseek-chat', 'max_tokens': 4096, 'supports_vision': False, 'note': 'DeepSeek V3, low cost'}, {'name': 'kimi', 'base_url': 'https://api.moonshot.cn/v1', 'model': 'moonshot-v1-8k', 'max_tokens': 4096, 'supports_vision': False, 'note': 'Moonshot Kimi'}, {'name': 'openrouter', 'base_url': 'https://openrouter.ai/api/v1', 'model': 'meta-llama/llama-3-8b-instruct:free', 'max_tokens': 4096, 'supports_vision': False, 'note': 'OpenRouter free models'}, {'name': 'siliconflow', 'base_url': 'https://api.siliconflow.cn/v1', 'model': 'Qwen/Qwen2.5-7B-Instruct', 'max_tokens': 4096, 'supports_vision': False, 'note': 'SiliconFlow free tier'}]

    def call_claude(self, prompt: str, provider_name: str='', session_id: str='', timeout: int=120) -> str:
        """Call Claude Code CLI with a specific model provider."""
        claude_bin = shutil.which('claude') or '/home/dk/local/node/bin/claude'
        provider = self.get_provider(provider_name)
        cmd = [claude_bin, '-p', prompt]
        if session_id:
            cmd = [claude_bin, '--resume', session_id, '-p', prompt]
        env = os.environ.copy()
        if provider.base_url:
            env['ANTHROPIC_BASE_URL'] = provider.base_url
        if provider.api_key:
            env['ANTHROPIC_API_KEY'] = provider.api_key
        if provider.model:
            env['ANTHROPIC_MODEL'] = provider.model
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
            response = result.stdout.strip()
            if not response and result.stderr:
                response = f'Error: {result.stderr[:200]}'
            return response or '(no response)'
        except subprocess.TimeoutExpired:
            return 'Response timed out.'
        except Exception as e:
            logger.exception()
            return f'Error: {str(e)}'
model_router = ModelRouter()