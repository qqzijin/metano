"""FastAPI-layer guard + token issuance for the remote MCP endpoint (D-plan).

metano's read-only MCP surface (:mod:`metano.mcp_http`) is mounted at ``/mcp``
on the FastAPI app in :mod:`metano.web_server`. FastMCP in ``mcp==1.27.1`` has
no first-class Streamable HTTP auth middleware, so the gate lives here, in the
FastAPI layer:

- ``MCPAuthMiddleware`` — intercepts every ``/mcp`` request and requires a
  Bearer JWT (``aud == "metano-mcp"``; HS256 signature and ``exp`` verified
  against the same secret the rest of metano uses).  Writes one audit line per
  call (path, HTTP method, client IP, JSON-RPC method/tool when parseable).
- ``create_mcp_token`` — issues a short-lived (1h), read-only scoped JWT for
  ``POST /api/mcp/token``.
- ``verify_mcp_token`` — decodes + audience/expiry-validates an MCP token.

Replay protection note: tokens carry a ``jti`` for audit correlation / future
revocation, but it is *not* single-use enforced here.  MCP clients legitimately
reuse one bearer token across many JSON-RPC requests (tools/list, tools/call,
notifications, ping…), so per-request jti uniqueness would break the protocol.
MVP protection = signature + ``aud`` + ``exp``.
"""
from __future__ import annotations

import contextvars
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route

from .auth import JWT_ALGORITHM, _audit, get_jwt_secret

MCP_AUDIENCE = "metano-mcp"
# Configurable via MCP_TOKEN_TTL env (seconds); default 24h for cross-device
# collaboration (a short 1h token forces frequent re-issuing on the remote side).
MCP_TOKEN_TTL_SECONDS = int(os.environ.get("MCP_TOKEN_TTL", "86400"))
# User-level read scope (subject is confined to its own data).  This is what
# ``POST /api/mcp/token`` issues by default.
MCP_READ_SCOPE = ["mcp:read"]
# Instance-level read scope: a token carrying this scope (or any scope list that
# contains ``mcp:admin:read`` / ``admin``) may read the whole instance.  It is
# NOT issued by default — mint it explicitly for a trusted remote host.
MCP_ADMIN_READ_SCOPE = ["mcp:admin:read"]

# Request / result / frequency limits (H-07): a leaked read token must not be
# able to exhaust memory or fan out an unbounded number of calls.
MCP_MAX_BODY_BYTES = int(os.environ.get("MCP_MAX_BODY_BYTES", str(256 * 1024)))
MCP_RATE_LIMIT = int(os.environ.get("MCP_RATE_LIMIT", "300"))       # requests / window
MCP_RATE_WINDOW = int(os.environ.get("MCP_RATE_WINDOW", "60"))      # seconds

# Per-subject sliding-window rate limiter (in-memory, process-local).
_mcp_rate: dict[str, deque] = defaultdict(deque)
_mcp_rate_lock = threading.Lock()


def _rate_limited(subject: str) -> bool:
    """True when ``subject`` has exceeded the per-window request budget."""
    now = time.time()
    with _mcp_rate_lock:
        dq = _mcp_rate[subject]
        while dq and now - dq[0] > MCP_RATE_WINDOW:
            dq.popleft()
        if len(dq) >= MCP_RATE_LIMIT:
            return True
        dq.append(now)
        # Bound memory: drop expired buckets once the map grows large (e.g. a
        # leaked token spraying many distinct subjects).  Only runs past 10k
        # distinct subjects, so the cost is negligible in normal operation.
        if len(_mcp_rate) > 10000:
            for key, q in list(_mcp_rate.items()):
                while q and now - q[0] > MCP_RATE_WINDOW:
                    q.popleft()
                if not q:
                    del _mcp_rate[key]
        return False


# The authenticated subject + scope are captured per-request here so the FastMCP
# tool functions (which run in the same request's contextvars) can enforce
# ownership filtering.  Local stdio MCP calls never enter the middleware, so the
# default ``(None, [])`` is what the tools see — and they treat it as the
# trusted local operator (full access).
_mcp_auth_ctx: contextvars.ContextVar[tuple[Optional[str], list[str]]] = (
    contextvars.ContextVar("mcp_auth", default=(None, []))
)


def get_mcp_auth() -> tuple[Optional[str], list[str]]:
    """Return ``(subject, scopes)`` for the current MCP request.

    Remote Streamable HTTP calls carry the authenticated JWT subject injected by
    :class:`MCPAuthMiddleware`.  Local stdio calls return ``(None, [])``.
    """
    return _mcp_auth_ctx.get()


def is_admin_read(scopes: Optional[list[str]]) -> bool:
    """True when ``scopes`` grants instance-level (admin) read access."""
    return bool(scopes) and any(
        s in ("mcp:admin:read", "mcp:admin", "admin") for s in scopes
    )

# Path prefix on the FastAPI app that this middleware guards.
MCP_PATH_PREFIX = "/mcp"


def create_mcp_token(
    username: str,
    scope: Optional[list[str]] = None,
    ttl_seconds: int = MCP_TOKEN_TTL_SECONDS,
) -> str:
    """Issue a short-lived, read-only MCP JWT (``aud=metano-mcp``).

    Reuses metano's HS256 secret from :mod:`metano.auth` so the guard can
    verify it with the same key.  Raises ``RuntimeError`` if the JWT secret is
    not configured (config error — surfaced as a 500 by the calling endpoint).
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "aud": MCP_AUDIENCE,
        "scope": scope or list(MCP_READ_SCOPE),
        "type": "mcp",
        "jti": secrets.token_urlsafe(16),
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def verify_mcp_token(token: str) -> Optional[dict]:
    """Validate an MCP bearer token.  Returns the payload, or ``None``.

    Enforces the HS256 signature, ``aud == "metano-mcp"`` and ``exp`` (PyJWT
    raises for bad signature / wrong-or-missing audience / expired — all are
    subclasses of ``PyJWTError`` and collapse to ``None`` → 401).  A missing or
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
            audience=MCP_AUDIENCE,
        )
    except jwt.PyJWTError:
        return None


def _extract_bearer(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if auth_header[:7].lower() == "bearer ":
        return auth_header[7:].strip()
    return ""


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _rpc_meta(request: Request) -> dict:
    """Best-effort JSON-RPC method/tool names for the audit line.

    Starlette caches ``request.body()`` so the downstream handler can still read
    the body after we touch it.  Never raises — audit is best-effort.
    """
    if request.method != "POST":
        return {}
    try:
        raw = await request.body()
        if not raw or len(raw) > MCP_MAX_BODY_BYTES:
            return {}
        data = json.loads(raw)
        items = data if isinstance(data, list) else [data]
        methods: list[str] = []
        tools: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            method = item.get("method")
            if method:
                methods.append(str(method))
            params = item.get("params")
            if isinstance(params, dict) and params.get("name"):
                tools.append(str(params["name"]))
        return {"rpc_methods": methods[:5], "rpc_tools": tools[:5]}
    except Exception:
        return {}


class MCPAuthMiddleware(BaseHTTPMiddleware):
    """Guard every ``/mcp`` request with a Bearer JWT (``aud=metano-mcp``).

    Runs outermost (added last in :mod:`metano.web_server`).  OPTIONS preflight
    is passed through so the CORS middleware below can answer it; every other
    ``/mcp`` request (GET for the SSE stream, POST for JSON-RPC) must carry a
    valid MCP bearer token.  Each authenticated call is written to the audit
    log with the JSON-RPC method/tool when it can be parsed.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Guard only the exact /mcp mount and its subtree. startswith("/mcp")
        # would also trap unrelated paths like /mcp-tools (a frontend route)
        # and bounce them with a 401 JSON instead of the SPA.
        if path != MCP_PATH_PREFIX and not path.startswith(MCP_PATH_PREFIX + "/"):
            return await call_next(request)
        # Let CORS preflight through to the CORS middleware.
        if request.method == "OPTIONS":
            return await call_next(request)

        ip = _client_ip(request)
        token = _extract_bearer(request)
        if not token:
            _audit(
                "mcp_auth_denied", "unknown",
                {"path": path, "reason": "missing_token", "ip": ip},
            )
            return JSONResponse(status_code=401,
                                content={"detail": "Missing bearer token"})

        payload = verify_mcp_token(token)
        if not payload:
            _audit(
                "mcp_auth_denied", "unknown",
                {"path": path, "reason": "invalid_or_expired_token", "ip": ip},
            )
            return JSONResponse(status_code=401,
                                content={"detail": "Invalid or expired token"})

        request.state.mcp_user = payload.get("sub") or "mcp"
        request.state.mcp_scope = payload.get("scope") or []

        # Reject oversized JSON-RPC bodies before FastMCP buffers them.
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > MCP_MAX_BODY_BYTES:
            _audit("mcp_auth_denied", request.state.mcp_user,
                   {"path": path, "reason": "body_too_large", "ip": ip})
            return JSONResponse(status_code=413,
                                content={"detail": "Request body too large"})

        # Per-subject rate limit (in-memory).
        if _rate_limited(request.state.mcp_user):
            _audit("mcp_rate_limited", request.state.mcp_user,
                   {"path": path, "ip": ip})
            return JSONResponse(status_code=429,
                                content={"detail": "Rate limit exceeded"})

        meta = await _rpc_meta(request)
        _audit("mcp_call", request.state.mcp_user, {
            "path": path,
            "http_method": request.method,
            "ip": ip,
            "jti": payload.get("jti"),
            **meta,
        })
        # Inject the authenticated subject + scope into the FastMCP tool layer
        # (H-07): the tool functions read this via mcp_server's helpers so every
        # session/memory/analytics query is confined to the subject's data.
        token_ctx = _mcp_auth_ctx.set((request.state.mcp_user, request.state.mcp_scope))
        try:
            return await call_next(request)
        finally:
            _mcp_auth_ctx.reset(token_ctx)


class FastMCPMount:
    """Bridge a FastMCP ``streamable_http_app()`` Starlette app into the host app.

    Two problems make a naive ``app.mount("/mcp", fastmcp_app)`` fail, both
    worked around here:

    * **Path** — ``FastMCP.streamable_http_app()`` registers its endpoint at
      the full ``settings.streamable_http_path`` (default ``/mcp``).  A
      Starlette ``Mount`` strips the ``/mcp`` prefix before the sub-app sees the
      request, so the sub-app would 404.  We register the sub-app as plain
      ``Route`` entries that forward the full path unchanged.
    * **Lifespan** — a mounted sub-app's lifespan is *not* run by Starlette, so
      FastMCP's ``StreamableHTTPSessionManager`` would never start and every
      request would raise ``RuntimeError: Task group is not initialized``.  We
      run the sub-app's lifespan from the host app's lifespan (merged, so any
      pre-existing host lifespan still runs).

    The host FastAPI app keeps the FastMCP endpoint at ``/mcp`` (per the D-plan
    contract); the FastMCP app serves that exact path itself.
    """

    def __init__(self, fastmcp_app):
        self.fastmcp_app = fastmcp_app
        self._lifespan = fastmcp_app.router.lifespan_context(fastmcp_app)

    @asynccontextmanager
    async def lifespan(self):
        async with self._lifespan:
            yield

    async def __call__(self, scope, receive, send):
        await self.fastmcp_app(scope, receive, send)

    def install(self, host_app, path: str = MCP_PATH_PREFIX):
        """Register the FastMCP app at ``path`` on the host FastAPI app.

        Routes are inserted at the front of the router so they win over the SPA
        catch-all.  The FastMCP lifespan is merged into the host app's lifespan
        (previous lifespan, if any, still runs).
        """
        host_app.router.routes.insert(
            0, Route(path, endpoint=self, methods=["GET", "POST", "OPTIONS"])
        )
        host_app.router.routes.insert(
            0, Route(f"{path}/{{rest:path}}", endpoint=self,
                     methods=["GET", "POST", "OPTIONS"])
        )

        previous_lifespan = host_app.router.lifespan_context

        @asynccontextmanager
        async def merged_lifespan(app):
            async with self.lifespan():
                async with previous_lifespan(app):
                    yield

        host_app.router.lifespan_context = merged_lifespan
