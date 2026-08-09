"""Multi-model provider support: route requests to different LLM backends."""
import json
import os
import shutil
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
from metano.log import logger

@dataclass
class ModelProvider:
    name: str
    base_url: str
    api_key: str
    model: str
    max_tokens: int = 4096
    supports_vision: bool = False
    supports_tools: bool = True

class ModelRouter:

    def __init__(self):
        self._providers: dict[str, ModelProvider] = {}
        self._default = 'default'
        self._load_config()

    def _load_config(self):
        """Load model providers from gateway_config.yaml."""
        try:
            import yaml
            config_path = Path.home() / '.claude' / 'metano' / 'gateway_config.yaml'
            if config_path.exists():
                with open(config_path) as f:
                    config = yaml.safe_load(f) or {}
                models_config = config.get('models', {})
                for name, cfg in models_config.items():
                    if cfg.get('enabled', True):
                        self._providers[name] = ModelProvider(name=name, base_url=cfg.get('base_url', ''), api_key=cfg.get('api_key', ''), model=cfg.get('model', ''), max_tokens=cfg.get('max_tokens', 4096), supports_vision=cfg.get('supports_vision', False), supports_tools=cfg.get('supports_tools', True))
                        if cfg.get('default', False):
                            self._default = name
        except Exception:
            logger.exception()
        if 'default' not in self._providers:
            self._providers['default'] = ModelProvider(name='default', base_url=os.environ.get('ANTHROPIC_BASE_URL', ''), api_key=os.environ.get('ANTHROPIC_API_KEY', ''), model=os.environ.get('ANTHROPIC_MODEL', ''))

    def get_provider(self, name: str='') -> ModelProvider:
        """Get a model provider by name."""
        if name and name in self._providers:
            return self._providers[name]
        return self._providers.get(self._default, self._providers['default'])

    def set_default(self, name: str):
        """Set a provider as the default."""
        if name in self._providers:
            self._default = name
        else:
            raise ValueError(f'Provider not found: {name}')

    def list_providers(self) -> list[dict]:
        """List all configured model providers."""
        return [{'name': p.name, 'model': p.model, 'base_url': p.base_url[:30] + '...' if p.base_url else '', 'max_tokens': p.max_tokens, 'supports_vision': p.supports_vision, 'is_default': p.name == self._default} for p in self._providers.values()]

    @staticmethod
    def free_provider_presets() -> list[dict]:
        """Return preset configurations for free/low-cost LLM backends."""
        return [{'name': 'ollama-local', 'base_url': 'http://localhost:11434/v1', 'model': 'llama3', 'max_tokens': 4096, 'supports_vision': False, 'note': 'Ollama local, free'}, {'name': 'nvidia-nim', 'base_url': 'https://integrate.api.nvidia.com/v1', 'model': 'meta/llama3-70b-instruct', 'max_tokens': 4096, 'supports_vision': False, 'note': 'NVIDIA NIM free tier'}, {'name': 'deepseek', 'base_url': 'https://api.deepseek.com/v1', 'model': 'deepseek-chat', 'max_tokens': 4096, 'supports_vision': False, 'note': 'DeepSeek V3, low cost'}, {'name': 'kimi', 'base_url': 'https://api.moonshot.cn/v1', 'model': 'moonshot-v1-8k', 'max_tokens': 4096, 'supports_vision': False, 'note': 'Moonshot Kimi'}, {'name': 'openrouter', 'base_url': 'https://openrouter.ai/api/v1', 'model': 'meta-llama/llama-3-8b-instruct:free', 'max_tokens': 4096, 'supports_vision': False, 'note': 'OpenRouter free models'}, {'name': 'siliconflow', 'base_url': 'https://api.siliconflow.cn/v1', 'model': 'Qwen/Qwen2.5-7B-Instruct', 'max_tokens': 4096, 'supports_vision': False, 'note': 'SiliconFlow free tier'}]

    def call_claude(self, prompt: str, provider_name: str='', session_id: str='', timeout: int=120) -> str:
        """Call Claude Code CLI with a specific model provider."""
        claude_bin = shutil.which('claude') or '/usr/local/bin/claude'
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