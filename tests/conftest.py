"""pytest configuration: mandatory METANO_HOME isolation + per-test DB redirection.

Security contract (audit cross-conclusion #1):
  The metano test suite must NEVER touch the production / runtime data
  directories.  The whole suite refuses to run unless ``METANO_HOME`` is set to
  an isolated directory (a throwaway path, never ``~/.claude/metano``).  On top
  of that guard, the autouse ``isolated_env`` fixture redirects every DB /
  config / log path used by the suite onto a fresh ``tmp_path`` per test, so a
  test can never write production data even when ``METANO_HOME`` is mis-set.

``metano.paths`` computes its constants at import time, so monkeypatching
``METANO_HOME`` later and reloading modules does NOT re-resolve paths.  That is
why this conftest patches each module's own resolved attribute
(``metano.evo_models.EVO_DB_PATH``, ``metano.db.DB_PATH``, ...) instead of the
``paths`` module — the attribute each module actually consults at call time.
"""

import logging
import os

import pytest

_METANO_HOME = os.environ.get("METANO_HOME", "").strip()


def _refuse(reason: str):
    # Hard-exit so the refusal propagates a non-zero status even from the
    # conftest-import phase (where pytest would otherwise swallow pytest.exit
    # and report a generic "no tests" / collection error).
    import sys

    sys.stderr.write(reason)
    sys.stderr.flush()
    os._exit(2)


if not _METANO_HOME:
    _refuse(
        "ERROR: tests require METANO_HOME isolation — set METANO_HOME to a "
        "temp dir, e.g.  METANO_HOME=/tmp/metano-test-home python3 -m pytest -q\n"
    )

# The guard must do more than check "non-empty": pointing METANO_HOME at the
# production runtime dir would run the suite against live data.  Refuse the
# default production path explicitly (audit N5 — guard previously only checked
# non-emptiness).
if os.path.abspath(os.path.expanduser(_METANO_HOME)) == os.path.abspath(
    os.path.expanduser("~/.claude/metano")
):
    _refuse(
        "ERROR: METANO_HOME points at the production runtime dir "
        f"({_METANO_HOME}) — refusing to run tests against live data\n"
    )


@pytest.fixture(autouse=True)
def _metano_logger_propagate():
    """caplog compatibility (audit R4-4): metano.log.get_logger sets
    propagate=False so records never reach the root logger where pytest's
    caplog listens.  Re-enable propagation for the duration of each test so
    caplog-based assertions work; the project handler still writes stderr."""
    import metano.log as mlog
    prev = mlog.logger.propagate
    mlog.logger.propagate = True
    try:
        yield
    finally:
        mlog.logger.propagate = prev


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Redirect every DB / config / log path consulted by the suite to tmp_path.

    Patching the module-level attribute (rather than ``metano.paths``) is what
    makes the redirect actually take effect: each module holds a frozen copy of
    the constant it imported at load time.
    """
    evo_db = tmp_path / "evo.db"
    bridge_db = tmp_path / "bridge.db"

    # ---- evo.db consumers (evolution data: proposals, rules, audit, ...) ----
    monkeypatch.setattr("metano.evo_models.EVO_DB_PATH", evo_db)
    monkeypatch.setattr("metano.route_events.EVO_DB_PATH", evo_db)
    monkeypatch.setattr("metano.experience.EVO_DB_PATH", evo_db)
    # Frozen-alias / lazy-import paths (audit N5): experience and route_events
    # copy ``DB_PATH = EVO_DB_PATH`` at import time, and knowledge_explorer
    # imports from ``metano.paths`` lazily at call time — patching only
    # ``EVO_DB_PATH`` leaves those aliases pointing at the original METANO_HOME
    # value, so a test could write live data.  Patch the aliases and the
    # canonical ``paths`` constants too.
    monkeypatch.setattr("metano.experience.DB_PATH", evo_db)
    monkeypatch.setattr("metano.route_events.DB_PATH", evo_db)
    monkeypatch.setattr("metano.paths.DB_PATH", bridge_db)
    monkeypatch.setattr("metano.paths.EVO_DB_PATH", evo_db)
    monkeypatch.setattr("metano.paths.MEMORY_DB", tmp_path / "memory.db")

    # ---- bridge.db consumers (chat sessions + messages) ----
    monkeypatch.setattr("metano.db.DB_PATH", bridge_db)
    monkeypatch.setattr("metano.db.DB_DIR", tmp_path)
    monkeypatch.setattr("metano.web_server.DB_PATH", bridge_db)

    # ---- memory / kanban / knowledge (self-contained per-test DBs) ----
    monkeypatch.setattr("metano.memory.DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr("metano.kanban.KANBAN_DIR", tmp_path / "kanban")
    monkeypatch.setattr("metano.kanban.KANBAN_DB", tmp_path / "kanban" / "kanban.db")
    monkeypatch.setattr("metano.knowledge.KB_DIR", tmp_path / "knowledge")
    monkeypatch.setattr("metano.knowledge.KB_DB", tmp_path / "knowledge" / "knowledge.db")
    monkeypatch.setattr("metano.knowledge.ALLOWED_INGEST_PREFIXES", [tmp_path])

    # ---- security audit trail ----
    monkeypatch.setattr("metano.security.AUDIT_LOG", tmp_path / "security" / "audit.jsonl")
    monkeypatch.setattr("metano.auth.AUDIT_LOG", tmp_path / "security" / "audit.jsonl")
    monkeypatch.setattr("metano.collab.AUDIT_LOG", tmp_path / "security" / "audit.jsonl")
    monkeypatch.setattr("metano.web_server.AUDIT_LOG", tmp_path / "security" / "audit.jsonl")

    # ---- gateway_config.yaml consumers ----
    cfg = tmp_path / "gateway_config.yaml"
    monkeypatch.setattr("metano.auth.CONFIG_PATH", cfg)
    monkeypatch.setattr("metano.web_server.CONFIG_PATH", cfg)
    monkeypatch.setattr("metano.home_assistant.CONFIG_PATH", cfg)
    monkeypatch.setattr("metano.model_router.CONFIG_PATH", cfg)
    monkeypatch.setattr("metano.config_watcher.CONFIG_PATH", cfg)

    # ---- cron store ----
    monkeypatch.setattr("metano.cron_daemon.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("metano.cron_daemon.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("metano.architect.CRON_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("metano.architect.ARCH_SNAP_DIR", tmp_path / "architecture_snapshots")
    monkeypatch.setattr("metano.architect.PROJECT_DIR", tmp_path)

    # ---- evolution / honcho side-effect paths (adapter._log, honcho db) ----
    monkeypatch.setattr("metano.honcho.models.HONCHO_DB", tmp_path / "honcho_data" / "honcho.db")
    monkeypatch.setattr("metano.evolution.EVOLUTION_DIR", tmp_path / "evolution")
    monkeypatch.setattr("metano.evolution.LOG_FILE", tmp_path / "evolution" / "evolution_log.jsonl")
    monkeypatch.setattr("metano.evolution.PAUSE_FLAG", tmp_path / "evolution" / "paused")
    monkeypatch.setattr("metano.adapter.EVOLUTION_DIR", tmp_path / "evolution")
    monkeypatch.setattr("metano.adapter.LOG_FILE", tmp_path / "evolution" / "evolution_log.jsonl")
    monkeypatch.setattr("metano.adapter.SUGGESTIONS_FILE", tmp_path / "evolution" / "pending_suggestions.json")

    # Initialise the two core databases so tests that read tables work against
    # an empty throwaway DB instead of assuming a pre-existing production DB.
    from metano import db as metano_db
    from metano import evo_models

    evo_models.init_db()
    metano_db.init_db()
    return tmp_path


@pytest.fixture()
def auth_config(tmp_path):
    """Write a minimal gateway_config.yaml with admin + user to CONFIG_PATH.

    Returns a dict with ``username`` / ``password`` / ``role`` for each user so
    auth / web-server tests can log in deterministically.
    """
    import bcrypt
    import yaml

    from metano import auth as auth_mod

    admin_pw = "admin-secret-123"
    user_pw = "user-secret-456"
    hashed = lambda pw: bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    config = {
        "auth": {
            "jwt_secret": "t" * 48,
            "users": [
                {"username": "admin", "password": hashed(admin_pw), "role": "admin"},
                {"username": "alice", "password": hashed(user_pw), "role": "user"},
            ],
        }
    }
    auth_mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    auth_mod.CONFIG_PATH.write_text(yaml.dump(config, allow_unicode=True))
    return {
        "admin": {"username": "admin", "password": admin_pw, "role": "admin"},
        "user": {"username": "alice", "password": user_pw, "role": "user"},
    }
