"""Auth: JWT token generation/verification, login rate limiting, password hashing."""

import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import jwt
from fastapi import HTTPException, Request, Response

from metano.log import logger

CONFIG_PATH = Path.home() / ".claude" / "metano" / "gateway_config.yaml"
AUDIT_LOG = Path.home() / ".claude" / "metano" / "security" / "audit.jsonl"

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


def get_users() -> list[dict]:
    config = _load_config()
    return config.get("auth", {}).get("users", [])


def verify_password(plain: str, hashed: str) -> bool:
    import bcrypt
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        "type": "access",
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        "iat": now,
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def create_refresh_token(username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        "type": "refresh",
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


def set_auth_cookies(response: Response, username: str, role: str) -> dict:
    access_token = create_access_token(username, role)
    refresh_token = create_refresh_token(username, role)

    response.set_cookie(
        "access_token", access_token,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True, samesite="lax", path="/",
    )
    response.set_cookie(
        "refresh_token", refresh_token,
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        httponly=True, samesite="lax", path="/api/auth/refresh",
    )
    return {"username": username, "role": role}


def clear_auth_cookies(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/api/auth/refresh")


def get_current_user_from_request(request: Request) -> Optional[dict]:
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        return None
    return {"username": payload["sub"], "role": payload.get("role", "user")}


def try_refresh_from_request(request: Request) -> Optional[str]:
    token = request.cookies.get("refresh_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload or payload.get("type") != "refresh":
        return None
    return create_access_token(payload["sub"], payload.get("role", "user"))


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
            _audit("password_changed", username, {})
            return True
    return False


AUTH_WHITELIST = {
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/logout",
    "/health",
}