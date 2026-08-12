"""Collaboration control plane: cross-device tasks, permissions, audit.

Unified management of cross-device collaboration tasks (which device executes
what, with what prompt, at what cost), a simple role-based allow policy, and an
audit trail. Task rows live in the same SQLite ``bridge.db`` used by
``metano.db`` (``collab_tasks`` table); audit entries reuse ``metano.auth._audit``
which appends JSONL to ``AUDIT_LOG``.

Execution itself (``execute_task``) is intentionally thin: local targets call
the existing Claude Code CLI, remote targets (e.g. ``remote-<host>``) are a
placeholder that will be wired to MCP/A2A in a later iteration.
"""

import os
import time
import uuid
from typing import Optional

from .db import get_db
from .paths import AUDIT_LOG

# ── Constants / policy ──────────────────────────────────────────────────────

# Lifecycle: pending -> running -> completed / failed
TASK_STATUSES = {"pending", "running", "completed", "failed"}

# Task types the control plane understands. Anything else is treated as
# 'general'.
TASK_TYPES = {
    "general",
    "search",
    "web",
    "memory",
    "knowledge",
    "code",
    "file",
    "skill",
}

# Simple allow policy: minimum role needed to create a task of each type.
# 'admin' may create anything; lower roles are limited to read-only types.
TASK_TYPE_MIN_ROLE = {
    "general": "user",
    "search": "user",
    "web": "user",
    "memory": "user",
    "knowledge": "user",
    "code": "admin",
    "file": "admin",
    "skill": "admin",
}

_ROLE_LEVELS = {"admin": 3, "user": 2, "guest": 1}

COLLAB_SCHEMA = """
CREATE TABLE IF NOT EXISTS collab_tasks (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL DEFAULT 'general',
    target TEXT NOT NULL DEFAULT 'local',
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    assigned_to TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    result TEXT DEFAULT '',
    cost REAL DEFAULT 0,
    error TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_collab_tasks_status ON collab_tasks(status);
CREATE INDEX IF NOT EXISTS idx_collab_tasks_created_at ON collab_tasks(created_at);
"""


# ── DB access ───────────────────────────────────────────────────────────────

def init_db(db_path=None):
    """Ensure the collab_tasks table exists in bridge.db (idempotent)."""
    conn = get_db(db_path)
    conn.executescript(COLLAB_SCHEMA)
    conn.commit()
    conn.close()


def _conn(db_path=None):
    """Return a connection with the collab schema guaranteed present."""
    conn = get_db(db_path)
    conn.executescript(COLLAB_SCHEMA)
    return conn


# ── Policy ──────────────────────────────────────────────────────────────────



def task_type_allowed(task_type: str, role: str) -> bool:
    """Simple allow policy: map a task type to the minimum role required."""
    min_role = TASK_TYPE_MIN_ROLE.get(task_type or "general", "admin")
    return _ROLE_LEVELS.get(role or "guest", 0) >= _ROLE_LEVELS.get(min_role, 0)


# ── Audit (reuses metano.auth._audit -> AUDIT_LOG JSONL) ────────────────────



def list_audit(limit: int = 100, action_prefix: str = "collab_") -> list[dict]:
    """Read collaboration audit entries from AUDIT_LOG, newest first.

    Returns a list of parsed JSONL entries whose ``action`` starts with
    ``action_prefix``. Best-effort: unparseable lines are skipped.
    """
    import json
    if not AUDIT_LOG.exists():
        return []
    raw = AUDIT_LOG.read_text(encoding="utf-8").strip().split("\n")
    entries = []
    for line in raw[-500:]:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if action_prefix and not (entry.get("action") or "").startswith(action_prefix):
            continue
        entries.append(entry)
    entries.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
    return entries[: max(1, int(limit))]


# ── Task CRUD ───────────────────────────────────────────────────────────────

def create_task(task_type: str = "general", target: str = "local",
                prompt: str = "", assigned_to: str = "", created_by: str = "",
                conn=None) -> dict:
    """Create a new collaboration task. Returns the created task dict."""
    own = conn is None
    if conn is None:
        conn = _conn()
    try:
        task_type = (task_type or "general").strip() or "general"
        if task_type not in TASK_TYPES:
            task_type = "general"
        now = time.time()
        task_id = uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO collab_tasks (id, task_type, target, prompt, status, "
            "assigned_to, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (task_id, task_type, (target or "local").strip() or "local",
             prompt or "", assigned_to or "", created_by or "", now, now)
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM collab_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else {"id": task_id}
    finally:
        if own:
            conn.close()


def get_task(task_id: str, conn=None) -> Optional[dict]:
    """Return a task dict, or None if not found."""
    own = conn is None
    if conn is None:
        conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM collab_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def list_tasks(status: Optional[str] = None, limit: int = 100,
               conn=None) -> list[dict]:
    """List tasks, newest first. Optional ``status`` filter (exact match)."""
    own = conn is None
    if conn is None:
        conn = _conn()
    try:
        sql = "SELECT * FROM collab_tasks"
        conds, params = [], []
        if status:
            conds.append("status = ?")
            params.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        if own:
            conn.close()


def update_status(task_id: str, status: str, conn=None) -> Optional[dict]:
    """Transition a task to a new status. Returns updated task or None."""
    if status not in TASK_STATUSES:
        raise ValueError(f"invalid status: {status}")
    own = conn is None
    if conn is None:
        conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE collab_tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), task_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM collab_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def record_result(task_id: str, result: str = "", cost: float = 0.0,
                  error: str = "", status: Optional[str] = None,
                  conn=None) -> Optional[dict]:
    """Record execution result / cost / error, optionally with a final status.

    Returns the updated task, or None if the task does not exist.
    """
    own = conn is None
    if conn is None:
        conn = _conn()
    try:
        updates = ["result = ?", "cost = ?", "error = ?", "updated_at = ?"]
        params = [result or "", cost or 0.0, error or "", time.time()]
        if status is not None:
            if status not in TASK_STATUSES:
                raise ValueError(f"invalid status: {status}")
            updates.append("status = ?")
            params.append(status)
        params.append(task_id)
        cur = conn.execute(
            f"UPDATE collab_tasks SET {', '.join(updates)} WHERE id = ?", params
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM collab_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


# ── Execution ───────────────────────────────────────────────────────────────

def is_local_target(target: str) -> bool:
    """True if the target means "this device" (empty / local / localhost)."""
    t = (target or "local").strip().lower()
    return t in ("", "local", "localhost", "127.0.0.1") or t.startswith("local")


def _dispatch_remote_task(task: dict, timeout: int = 120) -> dict:
    """Dispatch a task to a remote metano via A2A (message/send + tasks/get).

    ``task['target']`` is ``remote-<host>`` or ``remote-<host>:<port>``. The
    A2A Bearer token is read from ``METANO_A2A_TOKEN`` (issue one on the REMOTE
    via its ``POST /api/a2a/token`` with scope ``a2a:task``), so cross-device
    auth does not require the two instances to share a JWT secret.
    """
    import json
    import urllib.request
    import urllib.error
    host = (task.get("target") or "remote-localhost").strip()
    if host.startswith("remote-"):
        host = host[len("remote-"):]
    if ":" not in host:
        host = f"{host}:9120"
    base = f"http://{host}/a2a"
    token = os.environ.get("METANO_A2A_TOKEN", "")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    prompt = task.get("prompt") or ""
    rpc_id = f'collab-{task.get("id")}'

    def _rpc(method: str, params: dict) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}).encode()
        req = urllib.request.Request(f"{base}/rpc", data=body, headers=headers, method="POST")
        # Disable the system proxy (HTTP_PROXY) explicitly: urllib does NOT honor
        # NO_PROXY (curl does), and A2A cross-device traffic is typically LAN
        # direct. Otherwise an env proxy routes LAN traffic through the WAN proxy
        # and times out.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    try:
        data = _rpc("message/send", {"message": {"parts": [{"text": prompt}], "metadata": {"timeout": timeout}}})
    except urllib.error.HTTPError as e:
        return {"status": "failed", "mode": "remote", "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"status": "failed", "mode": "remote", "error": f"connection: {e}"}

    result = data.get("result") or {}
    remote_id = result.get("id")
    if not remote_id:
        return {"status": "failed", "mode": "remote", "error": f"remote did not return task id: {data.get('error') or result}"}

    # Poll tasks/get until terminal or deadline.
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            g = _rpc("tasks/get", {"id": remote_id})
        except Exception as e:
            return {"status": "failed", "mode": "remote", "error": f"poll: {e}"}
        t = g.get("result") or {}
        state = (t.get("status") or {}).get("state", "")
        if state in ("completed", "failed", "canceled"):
            text = ""
            for art in t.get("artifacts") or []:
                for part in art.get("parts") or []:
                    text += part.get("text", "")
            return {
                "status": "completed" if state == "completed" else "failed",
                "mode": "remote", "remote_id": remote_id,
                "state": state,
                "result": text.strip(),
            }
        time.sleep(2)
    return {"status": "timeout", "mode": "remote", "remote_id": remote_id,
            "error": "poll deadline exceeded"}


def execute_task(task_id: str, timeout: int = 120) -> dict:
    """Execute a task.

    Local targets run the prompt through the existing Claude Code CLI
    (``model_router.call_claude``) and persist result/status. Remote targets
    (e.g. ``remote-<host>``) are not yet wired — the task is left untouched and
    a placeholder ``execution`` block is returned; the eventual implementation
    will dispatch over MCP/A2A.

    Returns ``{"task": ..., "execution": ...}``.
    """
    task = get_task(task_id)
    if not task:
        return {"task": None, "execution": {"error": "task not found"}}

    if not is_local_target(task["target"]):
        # Remote dispatch via A2A (message/send + tasks/get poll). The remote
        # Bearer token comes from METANO_A2A_TOKEN (issued on the remote).
        started = time.time()
        try:
            update_status(task_id, "running")
            exec_result = _dispatch_remote_task(task, timeout)
            if exec_result.get("status") == "completed":
                record_result(task_id, result=exec_result.get("result", ""), cost=0.0, status="completed")
            else:
                record_result(task_id, result="", error=exec_result.get("error", exec_result.get("status", "")), status="failed")
            exec_result["duration_seconds"] = round(time.time() - started, 3)
            return {"task": get_task(task_id), "execution": exec_result}
        except Exception as e:
            record_result(task_id, result="", error=f"Remote dispatch error: {e}", status="failed")
            return {"task": get_task(task_id), "execution": {"mode": "remote", "status": "failed", "error": str(e)}}

    # Local execution via existing claude CLI call logic.
    started = time.time()
    try:
        update_status(task_id, "running")
        from .model_router import model_router
        from .log import logger
        response = model_router.call_claude(
            task["prompt"], provider_name="", session_id="", timeout=timeout
        )
    except Exception as e:
        from .log import logger
        logger.exception("collab execute failed")
        record_result(task_id, result="", error=f"Execution error: {e}",
                      status="failed")
        return {
            "task": get_task(task_id),
            "execution": {
                "mode": "local",
                "status": "failed",
                "duration_seconds": round(time.time() - started, 3),
                "error": str(e),
            },
        }

    duration = round(time.time() - started, 3)
    failed = (not response or response.startswith("Error:")
              or response.startswith("Response timed out."))
    if failed:
        task = record_result(task_id, result="", error=response[:2000],
                             status="failed")
        execution_status = "failed"
    else:
        task = record_result(task_id, result=response[:200000], cost=0.0,
                             status="completed")
        execution_status = "completed"
    return {
        "task": task,
        "execution": {
            "mode": "local",
            "status": execution_status,
            "duration_seconds": duration,
            "timeout": timeout,
        },
    }
