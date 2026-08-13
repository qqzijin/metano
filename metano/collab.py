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

import http.client
import os
import ssl
import time
import uuid
from typing import Optional

from .db import get_db
from .log import logger
from .paths import AUDIT_LOG, CONFIG_PATH

# ── Remote-dispatch SSRF policy (M3) ───────────────────────────────────────
# Cross-device A2A dispatch resolves the target host and refuses loopback,
# link-local (incl. the 169.254.169.254 cloud-metadata address), multicast,
# unspecified and reserved addresses.  Private RFC1918 networks are refused by
# default too — LAN collaboration must be explicitly enabled with
# METANO_COLLAB_ALLOW_PRIVATE=1.
_COLLAB_ALLOW_PRIVATE = os.environ.get(
    'METANO_COLLAB_ALLOW_PRIVATE', '').strip().lower() in ('1', 'true', 'yes')
_COLLAB_DEFAULT_PORT = 9120
_COLLAB_MAX_TIMEOUT = 3600

# ── Collab transport config (P1-8: pin + encrypt remote A2A) ───────────────
# Scheme precedence: env METANO_COLLAB_SCHEME > collab.scheme in
# gateway_config.yaml > https (default).  TLS verification: collab.verify_ssl
# (default true).  Per-host tokens: METANO_A2A_TOKEN_<HOST> > collab.tokens[host]
# > METANO_A2A_TOKEN.


def _collab_section() -> Optional[dict]:
    """Return the ``collab`` section of gateway_config.yaml (None if absent)."""
    try:
        import yaml
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            section = data.get("collab")
            return section if isinstance(section, dict) else None
    except Exception:
        return None
    return None


def _collab_scheme() -> str:
    """A2A dispatch scheme: env METANO_COLLAB_SCHEME > collab.scheme > https.

    Defaults to **https** — the audit (P1-8) flagged the shipped plaintext
    ``http://`` default.  Existing http-only remotes opt back in with
    ``METANO_COLLAB_SCHEME=http`` or ``collab.scheme: http``.
    """
    env = os.environ.get("METANO_COLLAB_SCHEME", "").strip().lower()
    if env in ("http", "https"):
        return env
    section = _collab_section()
    if section:
        scheme = str(section.get("scheme") or "").strip().lower()
        if scheme in ("http", "https"):
            return scheme
    return "https"


def _collab_verify_ssl() -> bool:
    """TLS certificate verification for https dispatch (default true).

    Env ``METANO_COLLAB_VERIFY_SSL`` (1/true/yes/on vs 0/false/no/off) overrides
    ``collab.verify_ssl`` in gateway_config.yaml.
    """
    env = os.environ.get("METANO_COLLAB_VERIFY_SSL", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    section = _collab_section()
    if section is not None and isinstance(section.get("verify_ssl"), bool):
        return section["verify_ssl"]
    return True


def _env_token_key(host: str) -> str:
    """Env-var key for a per-host A2A token: host uppercased, '.' -> '_'."""
    return "METANO_A2A_TOKEN_" + host.upper().replace(".", "_")


def _collab_token(host: str) -> str:
    """Per-host A2A Bearer token for ``host``.

    Precedence: env ``METANO_A2A_TOKEN_<HOST>`` (host uppercased, '.' -> '_')
    > ``collab.tokens[host]`` in gateway_config.yaml
    > legacy ``METANO_A2A_TOKEN``.
    """
    env_val = os.environ.get(_env_token_key(host), "").strip()
    if env_val:
        return env_val
    section = _collab_section()
    if section:
        tokens = section.get("tokens")
        if isinstance(tokens, dict):
            val = tokens.get(host)
            if isinstance(val, str) and val:
                return val
    return os.environ.get("METANO_A2A_TOKEN", "")


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


def _clamp_timeout(timeout) -> int:
    """Clamp a remote-dispatch timeout into ``[1, COLLAB_MAX_TIMEOUT]`` seconds."""
    try:
        t = int(timeout)
    except (TypeError, ValueError):
        return 120
    return max(1, min(t, _COLLAB_MAX_TIMEOUT))


def _validate_remote_host(hostport: str) -> tuple[str, int, str]:
    """Validate + normalize a remote A2A target ``host[:port]``.

    SSRF guard (M3): strips any scheme/path, then resolves the host and rejects
    loopback, link-local (incl. the 169.254.169.254 cloud-metadata address),
    multicast, unspecified and reserved addresses.  Private RFC1918 networks are
    refused unless ``METANO_COLLAB_ALLOW_PRIVATE=1``.

    Returns ``(host, port, ip)`` — ``host``/``port`` for routing (Host header,
    per-host token lookup) and ``ip`` = the validated IP the caller MUST use for
    the connection.  Pinning the connection to the validated IP closes the
    DNS-rebinding TOCTOU window between this check and the connect (P1-8).

    Raises ``ValueError`` on forbidden / invalid targets.
    """
    import ipaddress
    import socket
    hostport = (hostport or '').strip()
    if not hostport:
        raise ValueError('empty remote target')
    # Split host:port (handle a bracketed IPv6 literal).
    host, port_s = hostport, ''
    if hostport.startswith('['):
        end = hostport.find(']')
        if end == -1:
            raise ValueError(f'invalid remote target: {hostport!r}')
        host = hostport[1:end]
        port_s = hostport[end + 1:].lstrip(':')
    elif hostport.count(':') == 1:
        host, port_s = hostport.split(':', 1)
    # Strip any URL scheme / path that a caller may have pasted.
    host = host.split('//')[-1].split('/')[0].strip()
    if not host:
        raise ValueError('empty remote target host')
    if host.lower() in ('localhost', 'localhost.localdomain'):
        raise ValueError('refusing loopback remote target: localhost')
    try:
        port = int(port_s) if port_s else _COLLAB_DEFAULT_PORT
    except (TypeError, ValueError):
        raise ValueError(f'invalid remote port: {port_s!r}')
    if not (1 <= port <= 65535):
        raise ValueError(f'invalid remote port: {port}')
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError(f'cannot resolve remote target host: {host}')
    validated = []
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_unspecified or ip.is_reserved):
            raise ValueError(
                f'refusing SSRF target {host} ({addr}): forbidden address class')
        if not _COLLAB_ALLOW_PRIVATE and ip.is_private:
            raise ValueError(
                f'refusing SSRF target {host} ({addr}): private network — set '
                'METANO_COLLAB_ALLOW_PRIVATE=1 to allow LAN collaboration')
        validated.append(addr)
    if not validated:
        raise ValueError(f'cannot resolve remote target host: {host}')
    return host, port, validated[0]


def _estimate_call_cost(prompt: str, response: str) -> float:
    """Best-effort cost estimate from prompt/response text.

    ``model_router.call_claude`` returns plain text (no usage object), so tokens
    are approximated as ``chars / 4`` and priced with the default provider's
    rates.  A structured-usage contract (needs a ``model_router`` change) would
    give an exact figure; this at least keeps collab costs non-zero and
    proportional to the actual work performed (F-18).
    """
    try:
        from .model_router import model_router
        provider = model_router.get_provider("")
        model_name = (provider.model if provider else "") or "claude"
        in_tokens = max(1, len(prompt or "") // 4)
        out_tokens = max(1, len(response or "") // 4)
        return round(model_router.estimate_cost(model_name, in_tokens, out_tokens), 6)
    except Exception:
        return 0.0


class _RPCError(Exception):
    """Raised when the remote A2A endpoint answers with an HTTP error status."""

    def __init__(self, code: int, body: str):
        self.code = code
        self.body = body
        super().__init__(f"HTTP {code}: {body[:200]}")


def _host_header(host: str, port: int, scheme: str) -> str:
    """Build the ``Host`` header for a pinned-IP connection.

    Uses the original hostname (not the validated IP) so virtual-host routing on
    the remote keeps working; brackets IPv6 literals; omits the port when it is
    the scheme's default.
    """
    h = f"[{host}]" if (":" in host and not host.startswith("[")) else host
    default_port = 443 if scheme == "https" else 80
    return h if port == default_port else f"{h}:{port}"


def _open_conn(scheme: str, ip: str, port: int, hostname: str,
               verify_ssl: bool, timeout: int):
    """Open a blocking connection pinned to the validated IP.

    https uses ``hostname`` for SNI + certificate verification (certificates are
    issued to the hostname, not the IP), so IP pinning does not break TLS.
    ``verify_ssl=False`` disables certificate verification (check_hostname is
    cleared first, as ssl forbids CERT_NONE with check_hostname True) while
    still encrypting the channel.
    """
    if scheme == "https":
        ctx = ssl.create_default_context()
        if not verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(ip, port, timeout=timeout, context=ctx)
        # The socket connects to the validated IP but the TLS handshake must
        # present / verify the *hostname* certificate (SNI + check_hostname).
        conn._server_hostname = hostname
        return conn
    return http.client.HTTPConnection(ip, port, timeout=timeout)


def _dispatch_remote_task(task: dict, timeout: int = 120) -> dict:
    """Dispatch a task to a remote metano via A2A (message/send + tasks/get).

    ``task['target']`` is ``remote-<host>`` or ``remote-<host>:<port>``. The
    A2A Bearer token is read per-host: ``METANO_A2A_TOKEN_<HOST>`` (host
    uppercased, '.' -> '_'), else ``collab.tokens[host]`` in gateway_config.yaml,
    else the legacy ``METANO_A2A_TOKEN`` (issue one on the REMOTE via its
    ``POST /api/a2a/token`` with scope ``a2a:task``), so cross-device auth does
    not require the two instances to share a JWT secret.

    Security (M3 + P1-8):
      * the target host is validated against the SSRF policy
        (``_validate_remote_host`` — no loopback / link-local / cloud-metadata /
        private-by-default) and the returned **validated IP** is pinned for the
        connection, with the original hostname carried in the ``Host`` header
        (closes the DNS-rebinding TOCTOU window between check and connect);
      * the scheme comes from ``METANO_COLLAB_SCHEME`` / ``collab.scheme``
        (default **https**); ``collab.verify_ssl`` (default true) controls
        certificate verification;
      * the ``timeout`` is clamped to ``[1, 3600]`` seconds, and the A2A token
        is only attached to an address that passed that check.
    """
    import json
    host = (task.get("target") or "remote-localhost").strip()
    if host.startswith("remote-"):
        host = host[len("remote-"):]
    timeout = _clamp_timeout(timeout)
    try:
        host, port, ip = _validate_remote_host(host)
    except ValueError as e:
        return {"status": "failed", "mode": "remote", "error": str(e)}
    scheme = _collab_scheme()
    verify_ssl = _collab_verify_ssl()
    token = _collab_token(host)
    if scheme != "https":
        logger.warning(
            "[collab] remote A2A dispatch to %s uses plaintext %s:// — set "
            "METANO_COLLAB_SCHEME=https (or collab.scheme: https) to encrypt "
            "the channel", host, scheme)
    host_header = _host_header(host, port, scheme)
    prompt = task.get("prompt") or ""
    rpc_id = f'collab-{task.get("id")}'

    def _rpc(method: str, params: dict) -> dict:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        ).encode()
        conn = _open_conn(scheme, ip, port, host, verify_ssl, timeout)
        try:
            # POST to the pinned IP; the Host header carries the original
            # hostname so virtual-host remotes route correctly (P1-8). The
            # connection is direct (no proxy) like the previous urllib opener.
            conn.putrequest("POST", "/a2a/rpc", skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host_header)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body)))
            if token:
                conn.putheader("Authorization", f"Bearer {token}")
            conn.endheaders(body)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status >= 400:
                raise _RPCError(resp.status, data.decode(errors="replace"))
            return json.loads(data.decode(errors="replace"))
        finally:
            conn.close()

    try:
        data = _rpc("message/send", {"message": {"parts": [{"text": prompt}], "metadata": {"timeout": timeout}}})
    except _RPCError as e:
        return {"status": "failed", "mode": "remote", "error": f"HTTP {e.code}: {e.body[:200]}"}
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
    timeout = _clamp_timeout(timeout)
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
                cost = _estimate_call_cost(task.get("prompt", ""), exec_result.get("result", ""))
                record_result(task_id, result=exec_result.get("result", ""), cost=cost, status="completed")
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
        cost = _estimate_call_cost(task["prompt"], response)
        task = record_result(task_id, result=response[:200000], cost=cost,
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


async def execute_task_async(task_id: str, timeout: int = 120) -> dict:
    """Run :func:`execute_task` without blocking the event loop.

    F-18: the sync implementation makes a blocking subprocess / HTTP call that
    would freeze the Web event loop for the whole execution.  Running it in an
    executor keeps other page requests responsive.  For a fire-and-forget flow
    the async endpoint should return the task id immediately and let the caller
    poll ``get_task`` — switch the Web endpoint to this function via
    ``await asyncio.to_thread`` semantics.
    """
    import asyncio
    return await asyncio.to_thread(execute_task, task_id, timeout)
