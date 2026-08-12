"""Auth: JWT token generation/verification, login rate limiting, password hashing."""

import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import HTTPException, Request, Response

from metano.log import logger
from .paths import CONFIG_PATH, AUDIT_LOG

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 7

_login_attempts: dict[str, list[float]] = {}
LOGIN_RATE_LIMIT = 10
LOGIN_RATE_WINDOW = 300

ALLOWED_ROLES = {"admin", "user", "guest"}


def require_role(min_role: str):
    """FastAPI dependency that enforces minimum role level.
    Role hierarchy: admin > user > guest."""
    role_levels = {"admin": 3, "user": 2, "guest": 1}
    min_level = role_levels.get(min_role, 0)

    def checker(request: Request):
        user = getattr(request.state, "user", None)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        user_level = role_levels.get(user.get("role", "guest"), 0)
        if user_level < min_level:
            raise HTTPException(status_code=403, detail=f"Requires {min_role} role")
        return user

    return checker


def _load_config() -> dict:
    try:
        import yaml
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                return yaml.safe_load(f) or {}
    except Exception:
        logger.exception()
        return {}


def _save_config(config: dict):
    import yaml
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.dump(config, allow_unicode=True, default_flow_style=False))
    # SECURITY (S9): config holds secrets (JWT secret, user password hashes);
    # force owner-only permissions instead of relying on the process umask.
    os.chmod(CONFIG_PATH, 0o600)


def ensure_jwt_secret() -> str:
    config = _load_config()
    auth = config.setdefault("auth", {})
    secret = auth.get("jwt_secret", "")
    if not secret or len(secret) < 32:
        secret = secrets.token_urlsafe(48)
        auth["jwt_secret"] = secret
        _save_config(config)
    return secret


def ensure_default_admin() -> None:
    config = _load_config()
    auth = config.setdefault("auth", {})
    users = auth.setdefault("users", [])
    has_admin = any(u.get("role") == "admin" for u in users)
    if not has_admin:
        import bcrypt, secrets, string
        default_pw = os.environ.get("HERMES_DEFAULT_PASSWORD", "")
        if not default_pw:
            default_pw = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
            logger.warning("No HERMES_DEFAULT_PASSWORD set; generated random admin password — check logs or set env var")
        hashed = bcrypt.hashpw(default_pw.encode(), bcrypt.gensalt()).decode()
        users.append({"username": "admin", "password": hashed, "role": "admin"})
        auth["users"] = users
        _save_config(config)
        logger.info(f"Default admin created with password: {default_pw[:2]}{'*' * (len(default_pw)-2)}")


def get_jwt_secret() -> str:
    secret = os.environ.get("HERMES_JWT_SECRET", "")
    if secret and len(secret) >= 32:
        return secret
    config = _load_config()
    secret = config.get("auth", {}).get("jwt_secret", "")
    if not secret or len(secret) < 32:
        raise RuntimeError("jwt_secret missing or too short — set HERMES_JWT_SECRET env var or configure gateway_config.yaml")
    return secret


def get_domain_secret(domain: str) -> str:
    """Return a per-domain signing secret for MCP/A2A bearer tokens.

    A single shared JWT secret means a leak of the web secret lets an attacker
    forge MCP/A2A tokens too (audit M4).  This prefers an explicitly configured
    ``auth.<domain>_secret`` in gateway_config.yaml — a genuinely independent
    key — and otherwise derives a deterministic secret from the master JWT
    secret via HMAC-SHA256, so a fresh deploy still works without extra config.

    Once a per-domain secret is configured, the web JWT secret alone can no
    longer mint valid MCP/A2A tokens.
    """
    config = _load_config()
    explicit = config.get("auth", {}).get(f"{domain}_secret", "")
    if isinstance(explicit, str) and len(explicit) >= 32:
        return explicit
    import hashlib
    import hmac
    master = get_jwt_secret()
    return hmac.new(
        master.encode(), f"metano:{domain}".encode(), hashlib.sha256
    ).hexdigest()


def get_users() -> list[dict]:
    config = _load_config()
    return config.get("auth", {}).get("users", [])


def verify_password(plain: str, hashed: str) -> bool:
    import bcrypt
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _current_user_record(username: str) -> Optional[dict]:
    """Return {username, role, token_version} for a configured user, else None.

    The JWT ``tv`` claim must equal the user's current ``token_version`` for a
    token to be valid. ``token_version`` is bumped on logout / password change
    so stolen or old tokens are revoked immediately.
    """
    config = _load_config()
    auth = config.get('auth', {})
    for user in auth.get('users', []):
        if user.get('username') == username:
            return {
                'username': username,
                'role': user.get('role', 'user'),
                'token_version': int(auth.get('token_versions', {}).get(username, 0) or 0),
            }
    return None


def bump_token_version(username: str) -> int:
    """Increment a user's token_version, invalidating all previously-issued tokens."""
    config = _load_config()
    auth = config.setdefault('auth', {})
    versions = auth.setdefault('token_versions', {})
    versions[username] = int(versions.get(username, 0) or 0) + 1
    _save_config(config)
    return versions[username]


def get_user_by_username(username: str) -> Optional[dict]:
    """Return {username, role} for a configured user, else None."""
    rec = _current_user_record(username)
    if not rec:
        return None
    return {'username': rec['username'], 'role': rec['role']}


def get_token_version(username: str) -> int:
    """Current token_version for a user (0 when unset)."""
    rec = _current_user_record(username)
    return rec['token_version'] if rec else 0


def create_access_token(username: str, role: str, token_version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        "type": "access",
        "tv": token_version,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        "iat": now,
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def create_refresh_token(username: str, role: str, token_version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        "type": "refresh",
        "tv": token_version,
        "exp": now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        "iat": now,
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def authenticate_user(username: str, password: str) -> Optional[dict]:
    for user in get_users():
        if user.get("username") == username and verify_password(password, user.get("password", "")):
            return {"username": user["username"], "role": user.get("role", "user")}
    return None


def check_login_rate(ip: str) -> bool:
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < LOGIN_RATE_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) < LOGIN_RATE_LIMIT


def record_login_attempt(ip: str):
    now = time.time()
    _login_attempts.setdefault(ip, []).append(now)


def set_auth_cookies(response: Response, username: str, role: str, secure: bool = True) -> dict:
    rec = _current_user_record(username)
    tv = rec['token_version'] if rec else 0
    access_token = create_access_token(username, role, token_version=tv)
    refresh_token = create_refresh_token(username, role, token_version=tv)

    # SECURITY (H-06): Secure flag so the token is never sent over plain HTTP.
    # Loopback (127.0.0.1/localhost) is still a secure context in modern
    # browsers; non-loopback deployments MUST use HTTPS.
    # Callers may pass secure=False when the connection is plain HTTP (e.g. the
    # 138-device port forward) — otherwise the browser drops the Secure cookie
    # and the user is logged out on the very next request ("登录秒登出").
    response.set_cookie(
        "access_token", access_token,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True, samesite="lax", path="/", secure=secure,
    )
    response.set_cookie(
        "refresh_token", refresh_token,
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        httponly=True, samesite="lax", path="/api/auth/refresh", secure=secure,
    )
    return {"username": username, "role": role}


def clear_auth_cookies(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/api/auth/refresh")


def validate_access_token(token: str) -> Optional[dict]:
    """Validate an access-token string against the current user record.

    Checks the token type, the user's token_version (revokes on logout /
    password change) and re-reads the user's current role from config so a
    role downgrade takes effect immediately.
    """
    if not token:
        return None
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        return None
    rec = _current_user_record(payload["sub"])
    if not rec:
        return None
    if payload.get("tv", 0) != rec["token_version"]:
        return None
    return {"username": rec["username"], "role": rec["role"]}


def get_current_user_from_request(request: Request) -> Optional[dict]:
    token = request.cookies.get("access_token")
    return validate_access_token(token)


def try_refresh_from_request(request: Request) -> Optional[str]:
    token = request.cookies.get("refresh_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload or payload.get("type") != "refresh":
        return None
    rec = _current_user_record(payload["sub"])
    if not rec:
        return None
    if payload.get("tv", 0) != rec["token_version"]:
        return None
    return create_access_token(rec["username"], rec["role"], token_version=rec["token_version"])


def rotate_refresh_tokens(response: Response, username: str, role: str, secure: bool = True) -> dict:
    """Rotate an access/refresh pair on refresh AND bump the token_version.

    ``set_auth_cookies`` alone re-issues both cookies but leaves the old refresh
    token valid until its 7-day expiry, so a stolen refresh token stays
    replayable (audit M1).  Bumping ``token_version`` first revokes the just-used
    refresh token (and every other outstanding access/refresh/MCP/A2A token for
    the user) so a rotated refresh token can only be used once.

    Call this from the explicit ``POST /api/auth/refresh`` handler instead of
    ``set_auth_cookies`` (``web_server.auth_refresh``).
    """
    bump_token_version(username)
    return set_auth_cookies(response, username, role, secure=secure)


# ── One-time WebSocket ticket (F-12) ──────────────────────────────────────
# The access token is HttpOnly, so the front-end cannot read it to send as the
# WS first message. Instead the front-end fetches a short-lived (30s) one-time
# ticket from POST /api/auth/ws-ticket (using its normal cookie auth) and sends
# it as the first WS message. The ticket's jti is consumed once server-side.
_WS_TICKET_TTL = 30
_used_ws_tickets: set[str] = set()


def create_ws_ticket(username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        "type": "ws_ticket",
        "tv": 0,
        "jti": secrets.token_urlsafe(12),
        "exp": now + timedelta(seconds=_WS_TICKET_TTL),
        "iat": now,
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def consume_ws_ticket(token: str) -> Optional[dict]:
    """Validate and single-use consume a WS ticket. Returns the user or None."""
    if not token:
        return None
    payload = decode_token(token)
    if not payload or payload.get("type") != "ws_ticket":
        return None
    jti = payload.get("jti")
    if jti and jti in _used_ws_tickets:
        return None
    if jti:
        _used_ws_tickets.add(jti)
        # Opportunistic prune so the set never grows unboundedly.
        if len(_used_ws_tickets) > 1000:
            _used_ws_tickets.clear()
    return {"username": payload["sub"], "role": payload.get("role", "user")}


def _audit(action: str, user_id: str, details: dict):
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": time.time(),
        "action": action,
        "user_id": user_id,
        "details": details,
    }
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def change_password(username: str, old_password: str, new_password: str) -> bool:
    import bcrypt
    config = _load_config()
    auth = config.get("auth", {})
    users = auth.get("users", [])
    for user in users:
        if user.get("username") == username:
            if not verify_password(old_password, user.get("password", "")):
                return False
            user["password"] = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
            _save_config(config)
            # SECURITY (H-06): revoke all previously-issued tokens for this user.
            bump_token_version(username)
            _audit("password_changed", username, {})
            return True
    return False


AUTH_WHITELIST = {
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/logout",
    "/health",
}