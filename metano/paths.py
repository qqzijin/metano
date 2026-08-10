"""Centralized path resolution for metano.

Every module resolves its data / cache / log locations through this module
instead of hardcoding ``~/.claude/metano``. The install/data root is
controlled by the ``METANO_HOME`` environment variable; when unset it defaults
to ``~/.claude/metano`` for backward compatibility, so existing deployments
keep working without any changes.
"""
import os
from pathlib import Path


def home_dir() -> Path:
    """Return the metano data root (``METANO_HOME`` or the legacy default)."""
    return Path(os.environ.get('METANO_HOME', str(Path.home() / '.claude' / 'metano')))


# Base root
HOME = home_dir()

# --- Databases ---------------------------------------------------------------
DB_DIR = HOME
DB_PATH = DB_DIR / 'bridge.db'
EVO_DB_PATH = DB_DIR / 'evo.db'
MEMORY_DB = DB_DIR / 'memory.db'
HONCHO_DB = DB_DIR / 'honcho_data' / 'honcho.db'
KANBAN_DB = DB_DIR / 'kanban' / 'kanban.db'

# --- Directories -------------------------------------------------------------
CRON_DIR = HOME / 'cron'
EVOLUTION_DIR = HOME / 'evolution'
KNOWLEDGE_DIR = HOME / 'knowledge'
SECURITY_DIR = HOME / 'security'
GATEWAY_DIR = HOME / 'gateway'
GATEWAY_SESSIONS_DIR = HOME / 'gateway_sessions'
PERSONALITIES_DIR = HOME / 'personalities'
SKILLS_DIR = HOME / 'skills'
AUDIO_DIR = HOME / 'audio'
VOICE_CACHE_DIR = HOME / 'voice_cache'
IMAGE_DIR = HOME / 'images'
AGENT_DIR = HOME / 'agents'
KANBAN_DIR = HOME / 'kanban'
KB_DIR = KNOWLEDGE_DIR
KB_DB = KB_DIR / 'knowledge.db'
EXPLORATION_DIR = KNOWLEDGE_DIR / 'explorations'
ARCH_SNAP_DIR = HOME / 'architecture_snapshots'

# --- Files -------------------------------------------------------------------
CONFIG_PATH = HOME / 'gateway_config.yaml'
AUDIT_LOG = SECURITY_DIR / 'audit.jsonl'
EVO_LOG = EVOLUTION_DIR / 'evolution_log.jsonl'
LOG_FILE = EVO_LOG
GATEWAY_LOG = GATEWAY_DIR / 'gateway_log.jsonl'
CRON_JOBS_FILE = CRON_DIR / 'jobs.json'
CURATOR_STATE = HOME / 'curator_state.json'
BUNDLES_FILE = HOME / 'bundles.yaml'
PAUSE_FLAG = EVOLUTION_DIR / 'paused'
COST_CONFIG_FLAG = EVOLUTION_DIR / 'cost_circuit_config.json'
SUGGESTIONS_FILE = EVOLUTION_DIR / 'pending_suggestions.json'
HOOK_STATE_FILE = EVOLUTION_DIR / 'hook_state.json'
