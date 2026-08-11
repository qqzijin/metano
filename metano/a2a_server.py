"""A2A (Agent-to-Agent) task-delegation server for metano.

A standalone FastAPI/Starlette app exposing a Google A2A v1.0-compatible
surface that is backed by metano's own sub-agent executor
:class:`metano.sub_agent.AgentDelegator` (spawns ``claude -p`` subprocesses
and tracks a pending/running/completed task state machine).  This module is
*independent* — it is intentionally NOT mounted by ``metano.web_server``; the
integrating layer calls :func:`create_a2a_app` and mounts it (typically under
``/a2a``) when ready.

Provided bindings (all mirroring the reference implementation in
``agent-company/a2a-gateway``):

* ``GET  /.well-known/agent-card.json``  — AgentCard discovery (public).
* ``POST /rpc``                          — JSON-RPC 2.0 endpoint for
  ``message/send``, ``tasks/get``, ``tasks/cancel``, ``tasks/list``.
* ``POST /message:send``                 — REST binding for message/send.
* ``GET  /tasks/{id}``                   — REST binding for tasks/get.
* ``POST /tasks/{id}:cancel``            — REST binding for tasks/cancel.
* ``GET  /tasks/{id}:subscribe``         — SSE stream (poll-based MVP) of
  task status/result updates.
* ``POST /message:stream``               — SSE stream for a newly sent task.

Auth: every non-public request must carry a Bearer JWT with
``aud == "metano-a2a"`` (HS256, same secret as the rest of metano — see
:mod:`metano.mcp_gateway` for the identical MCP pattern).  Token issuance is
exposed through :func:`create_a2a_token`.

SECURITY — holding a valid A2A bearer token is equivalent to having local
command execution (RCE) on this host: a token bearer can spawn arbitrary
``claude -p`` sub-agents (i.e. run code) and SIGKILL their process groups via
``tasks/cancel``.  A2A tokens are therefore issued ONLY to admins
(:func:`create_a2a_token`, and the web server's ``POST /api/a2a/token`` which
requires the ``admin`` role) and must never be logged, shared, embedded in
frontend code, or leaked.  Scopes are enforced per method: ``a2a:read`` grants
read-only access (``tasks/get``, ``tasks/list``, SSE subscribe), while
``a2a:task`` grants full task delegation (``message/send``, ``message:stream``,
``tasks/cancel``) in addition to everything ``a2a:read`` allows.  A token whose
scope does not cover a method is rejected with HTTP 403 (REST) or an A2A
``FORBIDDEN`` error (JSON-RPC).

Task-state mapping (AgentDelegator status -> A2A ``TaskState``)::

    pending   -> submitted
    running   -> working
    completed -> completed
    failed    -> failed
    timeout   -> failed
    canceled  -> canceled

MVP note: ``message/send`` always launches the sub-agent in the background and
returns the task immediately in ``working`` state (the client polls
``tasks/get`` or subscribes over SSE).  ``return_immediately`` is accepted but
treated as a hint — this mirrors the "poll tasks/get first" MVP the caller
specified and avoids holding the HTTP connection open for the whole sub-agent
run.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import JWT_ALGORITHM, _audit, get_jwt_secret
from .log import logger
from .sub_agent import ConcurrencyLimitError, delegator

__all__ = ["create_a2a_app", "create_a2a_token", "verify_a2a_token", "A2A_AUDIENCE"]

# ── Identity / config ──────────────────────────────────────────────────────

A2A_AUDIENCE = "metano-a2a"
# Configurable via A2A_TOKEN_TTL env (seconds); same default as MCP tokens.
A2A_TOKEN_TTL_SECONDS = int(os.environ.get("A2A_TOKEN_TTL", "86400"))
# Public base URL advertised in the AgentCard (override at deploy time).
A2A_BASE_URL = os.environ.get("A2A_BASE_URL", "http://localhost:9120/a2a")

# Paths that bypass the Bearer guard.  The discovery card is public by design
# (RFC 8615 well-known + A2A discovery flow); everything else requires a token.
PUBLIC_PATHS = {
    "/.well-known/agent-card.json",
    "/.well-known/agent.json",
    "/health",
}


# ── A2A scope model ─────────────────────────────────────────────────────────
#
# SECURITY: see the module docstring — a valid A2A token is a local-RCE trust
# boundary.  Scopes are enforced at every handler entry; the value recorded in
# request.state.a2a_scope by A2AAuthMiddleware is checked per method below.
#
# Scope tiers (least-privilege upward); a token whose scope list contains a
# tier implicitly holds every lower tier:
_SCOPE_TIERS: dict[str, int] = {
    "a2a:read": 1,   # read-only: tasks/get, tasks/list, SSE subscribe
    "a2a:task": 2,   # full delegation: message/send, message:stream, tasks/cancel + reads
}
# Minimum scope tier required to invoke each method / REST binding.
_METHOD_MIN_SCOPE: dict[str, str] = {
    "message/send":    "a2a:task",
    "message:stream":  "a2a:task",   # REST-only binding (send + subscribe)
    "tasks/cancel":    "a2a:task",
    "tasks/get":       "a2a:read",
    "tasks/list":      "a2a:read",
    "tasks/subscribe": "a2a:read",   # REST-only SSE binding
}


def _scope_tier(scopes: Optional[list[str]]) -> int:
    """Highest scope tier granted by a token's scope list; 0 for none/unknown."""
    tiers = [_SCOPE_TIERS.get(s, 0) for s in (scopes or [])]
    return max(tiers) if tiers else 0


def _scope_allows(scopes: Optional[list[str]], required_scope: str) -> bool:
    """True when ``scopes`` grants at least the tier of ``required_scope``."""
    return _scope_tier(scopes) >= _SCOPE_TIERS.get(required_scope, 1 << 30)


def _scope_denied(scopes: Optional[list[str]], method: str) -> bool:
    """True when a token's scope is insufficient for ``method``.

    Unknown methods are NOT denied here — the dispatcher reports
    ``METHOD_NOT_FOUND`` for those.
    """
    required = _METHOD_MIN_SCOPE.get(method)
    if required is None:
        return False
    return not _scope_allows(scopes, required)


def _scope_response(request: Request, method: str) -> Optional[JSONResponse]:
    """Return a 403 ``JSONResponse`` when the request's A2A token scope forbids
    ``method``; return ``None`` when the call may proceed."""
    scopes = getattr(request.state, "a2a_scope", []) or []
    if not _scope_denied(scopes, method):
        return None
    user = getattr(request.state, "a2a_user", "a2a") or "a2a"
    _audit("a2a_scope_denied", user, {
        "method": method,
        "path": request.url.path,
        "scope": scopes,
    })
    return JSONResponse(status_code=403, content={
        "detail": "Forbidden: A2A token scope does not permit this operation",
        "method": method,
        "required_scope": _METHOD_MIN_SCOPE.get(method),
    })


# ── A2A JSON-RPC error codes (spec-defined) ────────────────────────────────

class A2AErrorCode:
    TASK_NOT_FOUND = -32001
    TASK_NOT_CANCELABLE = -32002
    FORBIDDEN = -32003            # token scope does not permit this method
    UNSUPPORTED_OPERATION = -32004
    CONCURRENCY_LIMIT = -32005    # too many concurrent sub-agents running
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600


A2A_ERRORS: dict[int, str] = {
    A2AErrorCode.TASK_NOT_FOUND: "Task not found",
    A2AErrorCode.TASK_NOT_CANCELABLE: "Task cannot be canceled",
    A2AErrorCode.FORBIDDEN: "Forbidden: insufficient A2A scope",
    A2AErrorCode.UNSUPPORTED_OPERATION: "Unsupported operation",
    A2AErrorCode.CONCURRENCY_LIMIT: "Concurrency limit reached",
    A2AErrorCode.METHOD_NOT_FOUND: "Method not found",
    A2AErrorCode.INVALID_PARAMS: "Invalid params",
    A2AErrorCode.INTERNAL_ERROR: "Internal error",
    A2AErrorCode.PARSE_ERROR: "Parse error",
    A2AErrorCode.INVALID_REQUEST: "Invalid Request",
}

# AgentDelegator status -> A2A TaskState (see module docstring).
_MAP_STATE: dict[str, str] = {
    "pending": "submitted",
    "running": "working",
    "completed": "completed",
    "failed": "failed",
    "timeout": "failed",
    "canceled": "canceled",
}

_TERMINAL_STATES = {"completed", "failed", "canceled"}

# Background asyncio.Task handles (for tasks/cancel) and A2A context ids.
_background_tasks: dict[str, asyncio.Task] = {}
_task_context: dict[str, str] = {}
# Serializes the "launch + discover task id" section of message/send so two
# concurrent sends never mis-attribute a freshly registered delegator task.
_launch_lock = asyncio.Lock()

_id_counter = 0


def _next_id() -> int:
    global _id_counter
    _id_counter += 1
    return _id_counter


# ── Token issuance / verification ──────────────────────────────────────────

def create_a2a_token(
    username: str,
    scope: Optional[list[str]] = None,
    ttl_seconds: int = A2A_TOKEN_TTL_SECONDS,
) -> str:
    """Issue a short-lived Bearer JWT for the A2A surface (``aud=metano-a2a``).

    Reuses metano's HS256 secret so :func:`verify_a2a_token` (and the auth
    middleware) can check it with the same key.  Raises ``RuntimeError`` if the
    JWT secret is not configured.

    ``scope`` defaults to ``["a2a:task"]`` (full task delegation).  Pass
    ``scope=["a2a:read"]`` to mint a read-only token.

    SECURITY: an A2A token is a local-RCE trust boundary (it can spawn and
    SIGKILL ``claude -p`` sub-processes on this host).  Only ever issue it to
    admins, keep it server-side, and never log or share it.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "aud": A2A_AUDIENCE,
        # Default to full delegation ONLY when the caller passes None; an empty
        # list explicitly mints a no-permission token (deny-by-default).
        "scope": scope if scope is not None else ["a2a:task"],
        "type": "a2a",
        "jti": secrets.token_urlsafe(16),
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def verify_a2a_token(token: str) -> Optional[dict]:
    """Validate an A2A Bearer token.  Returns the payload, or ``None``.

    Enforces the HS256 signature, ``aud == "metano-a2a"`` and ``exp`` (PyJWT
    raises for bad signature / wrong-or-missing audience / expired — all are
    ``PyJWTError`` subclasses and collapse to ``None`` -> 401).  A missing or
    unreadable JWT secret also collapses to ``None`` (deny-by-default).
    """
    try:
        secret = get_jwt_secret()
    except Exception:
        return None
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            audience=A2A_AUDIENCE,
        )
    except jwt.PyJWTError:
        return None


# ── Auth middleware ────────────────────────────────────────────────────────

def _extract_bearer(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if auth_header[:7].lower() == "bearer ":
        return auth_header[7:].strip()
    return ""


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class A2AAuthMiddleware(BaseHTTPMiddleware):
    """Guard every non-public A2A request with a Bearer JWT (``aud=metano-a2a``).

    Mirrors ``MCPAuthMiddleware`` in :mod:`metano.mcp_gateway`.  The discovery
    card (``/.well-known/agent-card.json``) and CORS preflight pass through;
    everything else (JSON-RPC, REST bindings, SSE subscribe) must carry a valid
    A2A bearer token.  Authenticated calls are written to the audit log with
    the JSON-RPC method when it can be parsed.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path in PUBLIC_PATHS:
            return await call_next(request)

        ip = _client_ip(request)
        token = _extract_bearer(request)
        if not token:
            _audit("a2a_auth_denied", "unknown",
                   {"path": path, "reason": "missing_token", "ip": ip})
            return JSONResponse(status_code=401,
                                content={"detail": "Missing bearer token"})

        payload = verify_a2a_token(token)
        if not payload:
            _audit("a2a_auth_denied", "unknown",
                   {"path": path, "reason": "invalid_or_expired_token", "ip": ip})
            return JSONResponse(status_code=401,
                                content={"detail": "Invalid or expired token"})

        request.state.a2a_user = payload.get("sub") or "a2a"
        request.state.a2a_scope = payload.get("scope") or []
        _audit("a2a_call", request.state.a2a_user, {
            "path": path,
            "http_method": request.method,
            "ip": ip,
            "jti": payload.get("jti"),
        })
        return await call_next(request)


# ── Helpers ────────────────────────────────────────────────────────────────

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_from_ts(ts: float) -> str:
    if not ts:
        return _iso_now()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_text(parts: list) -> str:
    """Flatten A2A message parts into a single task prompt string."""
    chunks: list[str] = []
    for p in parts:
        if isinstance(p, str):
            chunks.append(p)
        elif isinstance(p, dict):
            ptype = p.get("type")
            if ptype == "text" or "text" in p:
                chunks.append(str(p.get("text", "")))
            elif ptype == "data":
                chunks.append(json.dumps(p.get("data", {}), ensure_ascii=False, default=str))
            elif ptype == "file":
                f = p.get("file") or {}
                if isinstance(f, dict):
                    if f.get("url"):
                        chunks.append(f"file: {f['url']}")
                    elif f.get("bytes"):
                        chunks.append("file (inline bytes)")
    return "\n\n".join(c for c in chunks if c).strip()


def _get_agent_task(task_id: str):
    """Resolve an AgentTask from the delegator (memory, then disk)."""
    t = delegator._tasks.get(task_id)
    if t is None:
        t = delegator._load_task(task_id)
    return t


def _agent_message(task_id: str, text: str) -> dict:
    return {
        "messageId": f"msg-{task_id}",
        "role": "agent",
        "parts": [{"type": "text", "text": text[:2000]}],
    }


def _build_task(t) -> dict:
    """Render a delegator AgentTask as an A2A ``Task`` object (dict)."""
    state = _MAP_STATE.get(t.status, "working")
    status: dict[str, Any] = {
        "state": state,
        "timestamp": _iso_from_ts(t.started_at or time.time()),
    }
    if state == "completed" and t.result:
        status["message"] = _agent_message(t.id, t.result)
    elif state == "failed" and t.error:
        status["message"] = _agent_message(t.id, t.error)

    task: dict[str, Any] = {
        "id": t.id,
        "status": status,
        "metadata": {
            "task": t.task[:500],
            "statusRaw": t.status,
            "model": t.model or "",
            "startedAt": t.started_at or None,
            "completedAt": t.completed_at or None,
        },
    }
    cid = _task_context.get(t.id)
    if cid:
        task["contextId"] = cid
    if state == "completed" and t.result:
        task["artifacts"] = [{
            "artifact_id": f"artifact-{t.id}",
            "name": "result",
            "parts": [{"type": "text", "text": t.result}],
        }]
    return task


# ── JSON-RPC dispatch ──────────────────────────────────────────────────────

class _RPCError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _rpc_error(code: int, rpc_id: Any = None, data: Any = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": A2A_ERRORS.get(code, "Error"), "data": data},
        "id": rpc_id,
    }


# ── RPC handlers ───────────────────────────────────────────────────────────

async def _handle_message_send(params: dict) -> dict:
    msg = params.get("message") or {}
    if not isinstance(msg, dict):
        msg = params
    parts = msg.get("parts") or []
    text = _extract_text(parts)
    if not text:
        raise _RPCError(A2AErrorCode.INVALID_PARAMS,
                        "Message must contain a text part", data={"parts": parts})

    metadata = msg.get("metadata") or {}
    model = str(metadata.get("model") or params.get("model") or "")
    try:
        timeout = int(metadata.get("timeout") or params.get("timeout") or 120)
    except (TypeError, ValueError):
        timeout = 120
    timeout = max(1, min(timeout, 3600))
    context_id = msg.get("contextId") or msg.get("context_id") or ""

    # Concurrency gate: wait up to delegator._acquire_timeout for a free
    # sub-agent slot.  When the limit is reached we reject with a clear
    # "concurrency limit" error instead of forking an unbounded number of
    # claude processes (DoS / fork-bomb).  The slot is released by
    # delegator._spawn_async_impl when the background sub-agent finishes,
    # times out, errors, or is cancelled.
    if not await delegator.acquire_async():
        raise _RPCError(
            A2AErrorCode.CONCURRENCY_LIMIT,
            f"Concurrency limit reached ({delegator.active_count()}/"
            f"{delegator._max_concurrent} sub-agents running); retry later",
            data={"max_concurrent": delegator._max_concurrent,
                  "active": delegator.active_count()},
        )

    # Launch the sub-agent in the background.  The delegator registers the
    # task synchronously at the start of _spawn_async_impl, so we discover its
    # id by diffing delegator._tasks right after yielding to the loop once.
    # The lock serializes this section against concurrent message/send calls.
    async with _launch_lock:
        before = set(delegator._tasks.keys())
        try:
            bg = asyncio.create_task(
                delegator._spawn_async_impl(text, model=model, timeout=timeout))
        except BaseException:
            # create_task failing is essentially impossible outside loop
            # shutdown; release the slot we pre-acquired above.
            delegator.release()
            raise
        task_id: Optional[str] = None
        for _ in range(100):
            await asyncio.sleep(0.01)
            new_ids = [i for i in delegator._tasks.keys() if i not in before]
            if new_ids:
                task_id = new_ids[0]
                break
    if task_id is None:
        bg.cancel()
        try:
            await bg
        except asyncio.CancelledError:
            pass
        # Guarded release: no-op if _spawn_async_impl already released it.
        delegator.release()
        raise _RPCError(A2AErrorCode.INTERNAL_ERROR, "Failed to register delegated task")

    _background_tasks[task_id] = bg
    bg.add_done_callback(lambda _t, _tid=task_id: _background_tasks.pop(_tid, None))
    if context_id:
        _task_context[task_id] = context_id

    return _build_task(delegator._tasks[task_id])


async def _handle_tasks_get(params: dict) -> dict:
    task_id = params.get("id")
    if not task_id:
        raise _RPCError(A2AErrorCode.INVALID_PARAMS, "Missing required param 'id'")
    t = _get_agent_task(task_id)
    if t is None:
        raise KeyError(f"Task '{task_id}' not found")
    return _build_task(t)


async def _handle_tasks_cancel(params: dict) -> dict:
    task_id = params.get("id")
    if not task_id:
        raise _RPCError(A2AErrorCode.INVALID_PARAMS, "Missing required param 'id'")
    t = _get_agent_task(task_id)
    if t is None:
        raise KeyError(f"Task '{task_id}' not found")
    if t.status in ("completed", "failed", "timeout", "canceled"):
        raise _RPCError(A2AErrorCode.TASK_NOT_CANCELABLE,
                        f"Task '{task_id}' is in terminal state '{t.status}'")

    # Cancel = kill the process tree.  First SIGKILL the claude process group
    # (sub-agents are spawned with start_new_session=True, so os.killpg reaches
    # every descendant), then cancel the background asyncio.Task so
    # _spawn_async_impl can't later overwrite the status, then persist.
    t.status = "canceled"
    t.error = "Canceled via A2A tasks/cancel"
    t.completed_at = time.time()
    killed = delegator.cancel_task(task_id)
    bg = _background_tasks.pop(task_id, None)
    if bg is not None:
        bg.cancel()
        try:
            await bg
        except (asyncio.CancelledError, Exception):
            pass
    try:
        delegator._save_task(t)
    except Exception:
        logger.exception("Failed to persist canceled task %s", task_id)
    logger.info("A2A task %s canceled (process group SIGKILLed=%s)", task_id, killed)
    return _build_task(t)


async def _handle_tasks_list(params: dict) -> dict:
    context_id = params.get("contextId")
    status_filter = params.get("status")
    try:
        page_size = int(params.get("pageSize", 50))
    except (TypeError, ValueError):
        page_size = 50

    tasks: list[dict] = []
    for entry in delegator.list_tasks():
        at = _get_agent_task(entry["id"])
        if at is None:
            continue
        if context_id and _task_context.get(at.id) != context_id:
            continue
        state = _MAP_STATE.get(at.status, "working")
        if status_filter and state != status_filter:
            continue
        tasks.append(_build_task(at))
    return {"tasks": tasks[:page_size]}


_METHODS: dict[str, Any] = {
    "message/send": _handle_message_send,
    "tasks/get": _handle_tasks_get,
    "tasks/cancel": _handle_tasks_cancel,
    "tasks/list": _handle_tasks_list,
}


async def _dispatch_one(body: Any, scopes: Optional[list[str]] = None) -> dict:
    if not isinstance(body, dict) or not body.get("method"):
        return _rpc_error(A2AErrorCode.INVALID_REQUEST)
    method = body["method"]
    params = body.get("params") or {}
    rpc_id = body.get("id")

    # Scope enforcement (deny-by-default): a token whose scope does not cover
    # this method gets an A2A FORBIDDEN error.  The REST bindings surface the
    # same denial as HTTP 403 via _scope_response before reaching here.
    if _scope_denied(scopes, method):
        return _rpc_error(A2AErrorCode.FORBIDDEN, rpc_id,
                          data={"method": method,
                                "required_scope": _METHOD_MIN_SCOPE.get(method)})

    handler = _METHODS.get(method)
    if handler is None:
        return _rpc_error(A2AErrorCode.METHOD_NOT_FOUND, rpc_id)
    try:
        result = await handler(params)
        return {"jsonrpc": "2.0", "result": result, "id": rpc_id}
    except _RPCError as e:
        return _rpc_error(e.code, rpc_id, data=e.data)
    except KeyError as e:
        return _rpc_error(A2AErrorCode.TASK_NOT_FOUND, rpc_id, data=str(e))
    except ConcurrencyLimitError as e:
        return _rpc_error(A2AErrorCode.CONCURRENCY_LIMIT, rpc_id, data=str(e))
    except ValueError as e:
        return _rpc_error(A2AErrorCode.INVALID_PARAMS, rpc_id, data=str(e))
    except Exception as e:
        logger.exception("A2A RPC handler error for method=%s", method)
        return _rpc_error(A2AErrorCode.INTERNAL_ERROR, rpc_id, data=str(e))


# ── SSE (poll-based MVP) ───────────────────────────────────────────────────

async def _subscribe_events(task_id: str):
    """Yield TaskStatusUpdateEvent(s), then a final artifact event when done.

    The delegator has no push channel, so the MVP polls its task record every
    ~0.8s and emits one event per state change, terminating on a terminal
    state (with the completed result as an artifact event).
    """
    last_state: Optional[str] = None
    while True:
        t = _get_agent_task(task_id)
        if t is None:
            yield {"data": json.dumps(
                {"type": "error", "message": f"Task '{task_id}' not found"},
                ensure_ascii=False)}
            return
        state = _MAP_STATE.get(t.status, "working")
        event = {
            "type": "status",
            "taskId": task_id,
            "contextId": _task_context.get(task_id),
            "status": {"state": state, "timestamp": _iso_now()},
            "final": state in _TERMINAL_STATES,
        }
        if state != last_state:
            yield {"data": json.dumps(event, ensure_ascii=False, default=str)}
            last_state = state
        if state in _TERMINAL_STATES:
            if state == "completed" and t.result:
                artifact_event = {
                    "type": "artifact",
                    "taskId": task_id,
                    "contextId": _task_context.get(task_id),
                    "artifact": {
                        "artifact_id": f"artifact-{task_id}",
                        "name": "result",
                        "parts": [{"type": "text", "text": t.result}],
                    },
                    "lastChunk": True,
                }
                yield {"data": json.dumps(artifact_event, ensure_ascii=False, default=str)}
            return
        await asyncio.sleep(0.8)


# ── AgentCard ──────────────────────────────────────────────────────────────

def _build_agent_card() -> dict:
    return {
        "name": "metano A2A Gateway",
        "description": (
            "Task-delegation gateway for metano (self-evolving AI gateway). "
            "Accepts tasks via A2A and executes them by spawning Claude "
            "sub-agents (AgentDelegator), returning results and artifacts."
        ),
        "supportedInterfaces": [
            {"url": A2A_BASE_URL, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
            {"url": A2A_BASE_URL, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"},
        ],
        "provider": {"organization": "metano"},
        "version": "1.0.0",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
        },
        "securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        },
        "securityRequirements": [{"bearerAuth": ["a2a:task"]}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "delegate_task",
                "name": "Delegate Task",
                "description": (
                    "Spawn a Claude sub-agent (metano AgentDelegator) to execute "
                    "a task in the background and return its result."
                ),
                "tags": ["delegation", "sub-agent", "claude", "task"],
                "examples": [
                    "Delegate a coding task and report the result",
                    "Run a research / summarization task",
                ],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            },
            {
                "id": "task_status",
                "name": "Task Status & Cancel",
                "description": (
                    "Poll the status of a delegated task (tasks/get), list tasks "
                    "(tasks/list), or cancel a running task (tasks/cancel)."
                ),
                "tags": ["task", "status", "cancel", "lifecycle"],
                "examples": [
                    "Check on a running task",
                    "Cancel a long-running task",
                ],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
        ],
    }


# ── App factory ────────────────────────────────────────────────────────────

def create_a2a_app() -> FastAPI:
    """Build the standalone A2A Starlette/FastAPI app.

    Returns
    -------
    fastapi.FastAPI
        The app to be mounted (typically at ``/a2a``) by the web server.
    """
    app = FastAPI(
        title="metano A2A",
        description="A2A task-delegation gateway backed by AgentDelegator",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(A2AAuthMiddleware)

    @app.get("/.well-known/agent-card.json")
    async def agent_card():
        return JSONResponse(content=_build_agent_card())

    @app.get("/health")
    async def health():
        return JSONResponse(content={"status": "ok", "service": "metano-a2a"})

    # ── JSON-RPC 2.0 endpoint ──────────────────────────────────────────
    @app.post("/rpc")
    async def rpc_endpoint(request: Request):
        scopes = getattr(request.state, "a2a_scope", []) or []
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(content=_rpc_error(A2AErrorCode.PARSE_ERROR))
        if isinstance(body, list):
            return JSONResponse(content=[await _dispatch_one(b, scopes) for b in body])
        return JSONResponse(content=await _dispatch_one(body, scopes))

    # ── REST bindings ──────────────────────────────────────────────────
    @app.post("/message:send")
    async def rest_message_send(request: Request):
        denied = _scope_response(request, "message/send")
        if denied is not None:
            return denied
        scopes = getattr(request.state, "a2a_scope", []) or []
        body = await request.json()
        rpc = {"method": "message/send", "params": body, "id": _next_id()}
        return JSONResponse(content=await _dispatch_one(rpc, scopes))

    @app.post("/message:stream")
    async def rest_message_stream(request: Request):
        denied = _scope_response(request, "message/send")
        if denied is not None:
            return denied
        try:
            body = await request.json()
            send_result = await _handle_message_send(body)
        except _RPCError as e:
            return JSONResponse(content=_rpc_error(e.code, data=e.data))
        except Exception as e:
            logger.exception("A2A message:stream failed")
            return JSONResponse(content=_rpc_error(A2AErrorCode.INTERNAL_ERROR, data=str(e)))
        task_id = send_result["id"]
        return EventSourceResponse(_subscribe_events(task_id))

    @app.get("/tasks")
    async def rest_list_tasks(
        request: Request,
        context_id: str | None = None,
        status: str | None = None,
        page_size: int | None = None,
    ):
        denied = _scope_response(request, "tasks/list")
        if denied is not None:
            return denied
        scopes = getattr(request.state, "a2a_scope", []) or []
        params: dict[str, Any] = {}
        if context_id:
            params["contextId"] = context_id
        if status:
            params["status"] = status
        if page_size:
            params["pageSize"] = page_size
        rpc = {"method": "tasks/list", "params": params, "id": _next_id()}
        return JSONResponse(content=await _dispatch_one(rpc, scopes))

    # NOTE: the `:subscribe` / `:cancel` routes MUST be registered before the
    # bare `/{task_id}` route — Starlette stops at the first full match (path +
    # method), and `/tasks/{task_id}` would otherwise swallow GET
    # `/tasks/{id}:subscribe` (and 405 the POST cancel via the partial-match
    # fallback).  Registering the more specific suffixed routes first wins.
    @app.get("/tasks/{task_id}:subscribe")
    async def rest_subscribe_task(request: Request, task_id: str):
        denied = _scope_response(request, "tasks/subscribe")
        if denied is not None:
            return denied
        return EventSourceResponse(_subscribe_events(task_id))

    @app.post("/tasks/{task_id}:cancel")
    async def rest_cancel_task(request: Request, task_id: str):
        denied = _scope_response(request, "tasks/cancel")
        if denied is not None:
            return denied
        scopes = getattr(request.state, "a2a_scope", []) or []
        rpc = {"method": "tasks/cancel", "params": {"id": task_id}, "id": _next_id()}
        return JSONResponse(content=await _dispatch_one(rpc, scopes))

    @app.get("/tasks/{task_id}")
    async def rest_get_task(
        request: Request,
        task_id: str,
        history_length: int | None = None,
    ):
        denied = _scope_response(request, "tasks/get")
        if denied is not None:
            return denied
        scopes = getattr(request.state, "a2a_scope", []) or []
        params: dict[str, Any] = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        rpc = {"method": "tasks/get", "params": params, "id": _next_id()}
        return JSONResponse(content=await _dispatch_one(rpc, scopes))

    return app
