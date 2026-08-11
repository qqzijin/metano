"""Routing feedback loop (Plan A): record task trajectories, bandit-optimize
strategy selection, and feed experiences back into prompts.

The gateway logs every routed task with its signature, chosen strategy, and
outcome, then a contextual epsilon-greedy bandit adapts strategy (provider)
selection per task type. Failed tasks produce Reflexion-style lessons that are
stored and injected into future prompts (see :mod:`metano.experience`).

Reused metano infrastructure (no third-party libs):
- ``evo.db`` WAL SQLite (same pattern as ``metano.evo_models``).
- ``model_router.estimate_cost`` for USD cost of each route.
- ``metano.experience`` for lesson storage / retrieval / injection.

Anti-degradation:
- Success is judged independently of the generating model (heuristic on the
  response + pluggable external judge, see :func:`set_judge`).
- Bandit has cold-start forced exploration (< 5 events per task type) and
  epsilon decay with event count, so it never locks onto a bad strategy early.
- Reward penalizes cost and latency: quality - alpha*cost - beta*latency.
- Experiences lose weight on contradicting outcomes and are swept ~every 50
  events (see ``experience.cleanup_experiences``).

Config (read at call time, cached 30s, read-only against gateway_config.yaml):
``METANO_EXPERIENCE_ENABLED`` env var, or ``experience: {enabled: true}`` in
gateway_config.yaml, or :func:`set_enabled` for programmatic control.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import time
from typing import Callable, Optional

from metano.log import logger
from .paths import EVO_DB_PATH, CONFIG_PATH
from . import experience as experience_mod

# Tests override this module attribute to isolate the DB.
DB_PATH = EVO_DB_PATH

# None = auto (env var → gateway_config.yaml → False). True/False = forced.
ENABLED: Optional[bool] = None

# reward = quality - REWARD_ALPHA*cost_usd - REWARD_BETA*latency_s
REWARD_ALPHA = 1.0
REWARD_BETA = 0.01
# eps = max(EPS_MIN, 1 - n/EPS_DECAY); cold start forces exploration below COLD_START_MIN.
EPS_MIN = 0.1
EPS_DECAY = 50.0
COLD_START_MIN = 5

_JUDGE: Optional[Callable[[str], str]] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS route_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_signature TEXT NOT NULL,
    task_type TEXT NOT NULL,
    strategy TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'pending',   -- pending / success / failure / partial
    error_class TEXT DEFAULT '',
    cost REAL DEFAULT 0,
    latency_ms REAL DEFAULT 0,
    tokens TEXT DEFAULT '{}',                  -- JSON {input_tokens, output_tokens, cache_read_tokens}
    reflect TEXT DEFAULT '',
    model TEXT DEFAULT '',
    platform TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_route_events_type ON route_events(task_type);
CREATE INDEX IF NOT EXISTS idx_route_events_sig ON route_events(task_signature);
CREATE INDEX IF NOT EXISTS idx_route_events_outcome ON route_events(outcome);

CREATE TABLE IF NOT EXISTS route_strategy_stats (
    task_type TEXT NOT NULL,
    strategy TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    reward_sum REAL NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    last_selected REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (task_type, strategy)
);
"""

_cfg_cache: Optional[dict] = None
_cfg_ts = 0.0


_init_path = None  # DB path whose schema has already been applied this process


def _get_conn() -> sqlite3.Connection:
    global _init_path
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if _init_path != str(DB_PATH):
        # First use of this DB path: ensure tables exist (idempotent). DDL is
        # serialized by SQLite, so concurrent first-use is safe.
        conn.executescript(_SCHEMA)
        conn.commit()
        _init_path = str(DB_PATH)
    return conn


def init_db() -> sqlite3.Connection:
    """Create route_events + route_strategy_stats tables (idempotent)."""
    conn = _get_conn()
    experience_mod.init_db()
    return conn


# ── configuration ───────────────────────────────────────────────────────────

def set_enabled(value: bool):
    """Force the feedback loop on/off (overrides env/config). None re-enables auto."""
    global ENABLED
    ENABLED = value


def configure(**kwargs) -> None:
    """Tune bandit hyper-parameters (mainly for tests/experimentation)."""
    global REWARD_ALPHA, REWARD_BETA, EPS_MIN, EPS_DECAY, COLD_START_MIN
    for key, value in kwargs.items():
        if key == 'reward_alpha':
            REWARD_ALPHA = value
        elif key == 'reward_beta':
            REWARD_BETA = value
        elif key == 'eps_min':
            EPS_MIN = value
        elif key == 'eps_decay':
            EPS_DECAY = value
        elif key == 'cold_start_min':
            COLD_START_MIN = value
        else:
            raise ValueError(f'unknown route_events config key: {key}')


def _read_cfg() -> dict:
    global _cfg_cache, _cfg_ts
    now = time.time()
    if _cfg_cache is not None and now - _cfg_ts < 30:
        return _cfg_cache
    cfg = {}
    try:
        import yaml
        if CONFIG_PATH.exists():
            data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
            cfg = data.get('experience') or {}
    except Exception:
        logger.exception('route_events: read config failed')
    _cfg_cache, _cfg_ts = cfg, now
    return cfg


def is_enabled() -> bool:
    """Whether the feedback loop is active.

    Resolution order: forced flag → ``METANO_EXPERIENCE_ENABLED`` env var →
    ``experience.enabled`` in gateway_config.yaml → default False (off, so
    existing gateway behaviour is unchanged unless explicitly enabled).
    """
    if ENABLED is not None:
        return ENABLED
    env = os.environ.get('METANO_EXPERIENCE_ENABLED')
    if env is not None:
        return env.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(_read_cfg().get('enabled', False))


# ── task signature + classification ────────────────────────────────────────

_CODE_KW = ('code', 'bug', 'error', 'fix', 'compile', 'python', 'javascript',
            'typescript', 'function', 'class', 'sql', 'exception', 'traceback',
            '写代码', '报错', '修复', '实现', '脚本', '命令行', 'debug')
_RESEARCH_KW = ('research', '搜索', '资料', '研究', '报告', '分析', '调查',
                '调研', '背景调查', 'research')
_QA_KW = ('what is', 'who is', 'how to', 'why', 'what does', '解释', '是什么',
          '为什么', '怎么做', '区别', '对比', '定义', '含义', 'diff')
_CRON_KW = ('cron', '定时', '每天', '每日', '调度', 'automation', '定时任务', '定期')
_CHAT_KW = ('summar', '翻译', 'translate', '总结', '邮件', '写一段', '写一封',
            '回复', '你好', '闲聊', '介绍')


def classify_task_type(text: str) -> str:
    """Classify a user request into a coarse task type by keyword heuristics.

    Returns one of: code / research / qa / cron / chat. Order matters — code and
    research checks run first.
    """
    t = (text or '').strip().lower()
    if not t:
        return 'chat'
    if any(kw in t for kw in _CODE_KW):
        return 'code'
    if any(kw in t for kw in _RESEARCH_KW):
        return 'research'
    if any(kw in t for kw in _QA_KW):
        return 'qa'
    if any(kw in t for kw in _CRON_KW):
        return 'cron'
    return 'chat'


def _normalize(text: str) -> str:
    """Normalize a request: lowercase, strip punctuation, order-independent tokens."""
    s = (text or '').lower()
    s = re.sub(r'[\W_]+', ' ', s, flags=re.UNICODE)
    tokens = [tok for tok in s.split() if tok]
    tokens.sort()
    return ' '.join(tokens)


def make_task_signature(text: str) -> str:
    """Deterministic, order-independent signature: ``<task_type>:<sha256[:16]>``."""
    norm = _normalize(text)
    h = hashlib.sha256(norm.encode('utf-8')).hexdigest()[:16]
    return f'{classify_task_type(text)}:{h}'


# ── reward + bandit ─────────────────────────────────────────────────────────

def compute_reward(quality: float, cost_usd: float = 0.0, latency_s: float = 0.0,
                   alpha: Optional[float] = None, beta: Optional[float] = None) -> float:
    """reward = quality - alpha*cost_usd - beta*latency_s."""
    alpha = REWARD_ALPHA if alpha is None else alpha
    beta = REWARD_BETA if beta is None else beta
    return quality - alpha * (cost_usd or 0.0) - beta * (latency_s or 0.0)


def _quality_for_outcome(outcome: str) -> float:
    return {'success': 1.0, 'partial': 0.5, 'failure': 0.0}.get(outcome, 0.0)


def _strategy_pool() -> list[str]:
    """Candidate strategies = configured model providers (fallback: ['default'])."""
    try:
        from .model_router import model_router
        names = [p['name'] for p in model_router.list_providers()]
        if names:
            return names
    except Exception:
        logger.exception('route_events: strategy pool resolution failed')
    return ['default']


def _best_strategy(task_type: str, pool: list[str]) -> Optional[str]:
    """Highest mean historical reward for the task type, restricted to the pool."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            'SELECT strategy, '
            'AVG(CASE outcome WHEN "success" THEN 1.0 WHEN "partial" THEN 0.5 ELSE 0.0 END '
            '    - ? * cost - ? * latency_ms/1000.0) AS mean_reward, '
            'COUNT(*) AS n '
            'FROM route_events WHERE task_type=? AND outcome != "pending" '
            'GROUP BY strategy',
            (REWARD_ALPHA, REWARD_BETA, task_type),
        ).fetchall()
    finally:
        conn.close()
    best, best_r = None, float('-inf')
    for r in rows:
        if r['strategy'] in pool and r['n'] >= 1 and r['mean_reward'] is not None \
                and r['mean_reward'] > best_r:
            best, best_r = r['strategy'], r['mean_reward']
    return best


def select_strategy(task_type: str = '', force_explore: bool = False) -> str:
    """Contextual epsilon-greedy strategy selection.

    - Cold start (< ``COLD_START_MIN`` events for the task type) forces random
      exploration to build evidence.
    - Otherwise exploit the highest mean-reward strategy with probability
      (1 - eps), explore randomly with probability eps, where eps decays with
      the task type's event count.
    """
    pool = _strategy_pool()
    if not pool:
        return 'default'
    if len(pool) == 1:
        return pool[0]
    conn = _get_conn()
    try:
        n = conn.execute(
            'SELECT COUNT(*) FROM route_events WHERE task_type=? AND outcome != "pending"',
            (task_type,),
        ).fetchone()[0]
    finally:
        conn.close()
    if n < COLD_START_MIN or force_explore:
        return random.choice(pool)
    eps = max(EPS_MIN, 1.0 - n / EPS_DECAY)
    if random.random() < eps:
        return random.choice(pool)
    best = _best_strategy(task_type, pool)
    return best if best else random.choice(pool)


# ── event recording ─────────────────────────────────────────────────────────

def record_event(task_signature: str, task_type: str, strategy: str,
                 outcome: str = 'pending', error_class: str = '', cost: float = 0.0,
                 latency_ms: float = 0.0, usage: Optional[dict] = None,
                 reflect: str = '', model: str = '', platform: str = '',
                 user_id: str = '', session_id: str = '') -> int:
    """Insert one route event. Returns the new event id."""
    conn = _get_conn()
    try:
        cur = conn.execute(
            'INSERT INTO route_events (task_signature, task_type, strategy, outcome, '
            'error_class, cost, latency_ms, tokens, reflect, model, platform, user_id, '
            'session_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (task_signature, task_type, strategy, outcome, error_class, cost, latency_ms,
             json.dumps(usage or {}), reflect, model, platform, user_id, session_id,
             time.time()),
        )
        eid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    maybe_cleanup()
    return eid


def _update_strategy_stats(task_type: str, strategy: str, reward: float, outcome: str):
    conn = _get_conn()
    try:
        now = time.time()
        wins = 1 if outcome == 'success' else 0
        row = conn.execute(
            'SELECT task_type FROM route_strategy_stats WHERE task_type=? AND strategy=?',
            (task_type, strategy),
        ).fetchone()
        if row:
            conn.execute(
                'UPDATE route_strategy_stats SET n=n+1, reward_sum=reward_sum+?, '
                'wins=wins+?, last_selected=? WHERE task_type=? AND strategy=?',
                (reward, wins, now, task_type, strategy),
            )
        else:
            conn.execute(
                'INSERT INTO route_strategy_stats (task_type, strategy, n, reward_sum, wins, last_selected) '
                'VALUES (?,?,1,?,?,?)',
                (task_type, strategy, reward, wins, now),
            )
        conn.commit()
    finally:
        conn.close()


def record_outcome(event_id: int, outcome: str, error_class: str = '', cost: float = 0.0,
                   latency_ms: float = 0.0, usage: Optional[dict] = None,
                   model: str = '', response: str = '') -> dict:
    """Close an event: record outcome, update bandit stats, and on failure
    store a Reflexion lesson (see :mod:`metano.experience`)."""
    if outcome not in ('success', 'failure', 'partial'):
        raise ValueError(f'outcome must be success/failure/partial, got {outcome!r}')
    conn = _get_conn()
    row = conn.execute('SELECT * FROM route_events WHERE id=?', (event_id,)).fetchone()
    if not row:
        conn.close()
        return {'status': 'not_found', 'event_id': event_id}
    ev = dict(row)
    conn.execute(
        'UPDATE route_events SET outcome=?, error_class=?, cost=?, latency_ms=?, '
        'tokens=?, model=? WHERE id=?',
        (outcome, error_class, cost, latency_ms, json.dumps(usage or {}), model, event_id),
    )
    conn.commit()
    quality = _quality_for_outcome(outcome)
    reward = compute_reward(quality, cost, latency_ms / 1000.0)
    _update_strategy_stats(ev['task_type'], ev['strategy'], reward, outcome)
    conn.close()

    try:
        experience_mod.reward_relevant(ev['task_type'], outcome)
        if outcome == 'failure':
            experience_mod.record_reflection(
                task_type=ev['task_type'], task_signature=ev['task_signature'],
                error_class=error_class, response=response, outcome=outcome,
                source_event_id=event_id,
            )
    except Exception:
        logger.exception('route_events: experience update failed')
    return {'status': 'recorded', 'event_id': event_id, 'outcome': outcome, 'reward': reward}


# ── independent outcome judgement ──────────────────────────────────────────

def set_judge(fn: Optional[Callable[[str], str]]):
    """Install an external outcome judge (independent of the generating model).

    ``fn(response_text) -> 'success' | 'failure' | 'partial'``. Examples: a code
    executor that runs the answer, an evaluator, or a user-feedback resolver.
    """
    global _JUDGE
    _JUDGE = fn


def _detect_error_class(response: str) -> str:
    r = (response or '').lower()
    if 'timed out' in r or 'timeout' in r:
        return 'timeout'
    if 'api key' in r or 'auth' in r or 'permission' in r:
        return 'auth'
    if 'rate limit' in r or '429' in r:
        return 'rate_limit'
    if 'traceback' in r or 'exception' in r:
        return 'exception'
    if not r.strip() or 'empty response' in r:
        return 'empty'
    return 'generic'


def _judge_outcome(response: str) -> str:
    """Default independent heuristic: a non-empty, non-error response is success."""
    if _JUDGE is not None:
        try:
            return _JUDGE(response or '')
        except Exception:
            logger.exception('route_events: custom judge failed, using heuristic')
    r = (response or '').strip()
    if not r:
        return 'failure'
    if r.startswith('Error:'):
        return 'failure'
    if r.startswith('Response timed out'):
        return 'failure'
    if r.startswith('⚠️'):
        return 'failure'
    return 'success'


# ── router hooks ───────────────────────────────────────────────────────────

def begin_route(message: str, prompt: str, platform: str = '', user_id: str = '',
                session_id: str = '') -> Optional[dict]:
    """Router pre-call hook: sign the task, pick a strategy, inject experiences.

    Returns a context dict for :func:`end_route`, or None when the loop is
    disabled. The returned ``prompt`` carries the injected experience block.
    """
    if not is_enabled():
        return None
    sig = make_task_signature(message)
    ttype = classify_task_type(message)
    strategy = select_strategy(ttype)
    model = ''
    try:
        from .model_router import model_router
        model = model_router.get_provider(strategy).model or ''
    except Exception:
        logger.exception('route_events: resolve model failed')
    injected, n_exp = experience_mod.inject_experiences(prompt, message, ttype)
    eid = record_event(
        task_signature=sig, task_type=ttype, strategy=strategy, outcome='pending',
        platform=platform, user_id=user_id, session_id=session_id,
    )
    return {
        'event_id': eid,
        'task_signature': sig,
        'task_type': ttype,
        'strategy': strategy,
        'model': model,
        'prompt': injected,
        'injected_experiences': n_exp,
        'start_ts': time.time(),
    }


def end_route(ctx: Optional[dict], response: str, latency_ms: float = 0.0,
              usage: Optional[dict] = None, outcome: Optional[str] = None,
              error_class: Optional[str] = None) -> Optional[dict]:
    """Router post-call hook: judge, record, reward, and (on failure) reflect."""
    if not ctx:
        return None
    usage = usage or {}
    cost = 0.0
    try:
        from .model_router import model_router
        cost = model_router.estimate_cost(
            ctx.get('model', ''),
            usage.get('input_tokens', 0) or 0,
            usage.get('output_tokens', 0) or 0,
            usage.get('cache_read_tokens', 0) or 0,
        )
    except Exception:
        logger.exception('route_events: cost estimate failed')
    if outcome is None:
        outcome = _judge_outcome(response)
    if error_class is None and outcome == 'failure':
        error_class = _detect_error_class(response)
    res = record_outcome(
        event_id=ctx['event_id'], outcome=outcome, error_class=error_class or '',
        cost=cost, latency_ms=latency_ms, usage=usage, model=ctx.get('model', ''),
        response=response,
    )
    return {
        'event_id': ctx['event_id'],
        'outcome': outcome,
        'cost': cost,
        'reward': res.get('reward', 0.0),
    }


# ── maintenance + stats ─────────────────────────────────────────────────────

def maybe_cleanup() -> dict:
    """Trigger an experience sweep roughly every 50 route events."""
    try:
        conn = _get_conn()
        try:
            n = conn.execute('SELECT COUNT(*) FROM route_events').fetchone()[0]
        finally:
            conn.close()
        if n and n % 50 == 0:
            return experience_mod.cleanup_experiences()
    except Exception:
        logger.exception('route_events: cleanup failed')
    return {'status': 'skip'}


def get_route_stats() -> dict:
    """Observability: event counts, bandit table, experience store size."""
    conn = _get_conn()
    try:
        total = conn.execute('SELECT COUNT(*) FROM route_events').fetchone()[0]
        by_outcome = {
            r['outcome'] or 'pending': r['cnt']
            for r in conn.execute('SELECT outcome, COUNT(*) cnt FROM route_events GROUP BY outcome')
        }
        by_type = {
            r['task_type']: r['cnt']
            for r in conn.execute('SELECT task_type, COUNT(*) cnt FROM route_events GROUP BY task_type')
        }
        by_strategy = {
            r['strategy']: r['cnt']
            for r in conn.execute('SELECT strategy, COUNT(*) cnt FROM route_events GROUP BY strategy')
        }
        bandit = [
            dict(r)
            for r in conn.execute(
                'SELECT task_type, strategy, n, reward_sum, wins, '
                'CASE WHEN n > 0 THEN reward_sum / n ELSE 0 END AS mean_reward '
                'FROM route_strategy_stats ORDER BY mean_reward DESC'
            )
        ]
    finally:
        conn.close()
    return {
        'total_events': total,
        'by_outcome': by_outcome,
        'by_type': by_type,
        'by_strategy': by_strategy,
        'bandit': bandit,
        'experiences': experience_mod.get_experience_stats(),
    }
