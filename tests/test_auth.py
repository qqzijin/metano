"""Tests for auth module: S1/S2 regression tests."""
import pytest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


def test_verify_password_no_plaintext_fallback():
    """S1: verify_password must NOT fall back to plaintext comparison."""
    from metano.auth import verify_password
    # 'test' is not a valid bcrypt hash — bcrypt should reject it, NOT compare plaintext
    with pytest.raises((ValueError, TypeError)):
        verify_password("test", "test")


def test_verify_password_correct_hash():
    """verify_password works correctly with a valid bcrypt hash."""
    import bcrypt
    from metano.auth import verify_password
    hashed = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode()
    assert verify_password("secret123", hashed) is True
    assert verify_password("wrong", hashed) is False


def test_get_jwt_secret_no_hardcoded_fallback(tmp_path):
    """S2: get_jwt_secret must NOT return hardcoded fallback."""
    import yaml
    from metano.auth import CONFIG_PATH, get_jwt_secret
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # A too-short secret in config must be refused, never silently used and
    # never replaced by a hardcoded fallback.
    CONFIG_PATH.write_text(yaml.dump({"auth": {"jwt_secret": "short"}}))
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError) as exc_info:
            get_jwt_secret()
        assert "jwt_secret" in str(exc_info.value).lower()
    # A valid config secret is returned as-is (still never the fallback).
    CONFIG_PATH.write_text(yaml.dump({"auth": {"jwt_secret": "v" * 40}}))
    with patch.dict(os.environ, {}, clear=True):
        secret = get_jwt_secret()
        assert secret == "v" * 40
        assert secret != "fallback-secret-change-me"


def test_get_jwt_secret_env_var_takes_priority():
    """JWT secret from env var should take priority over config file."""
    from metano.auth import get_jwt_secret
    with patch.dict(os.environ, {"HERMES_JWT_SECRET": "a" * 40}):
        secret = get_jwt_secret()
        assert secret == "a" * 40


def test_auth_whitelist_includes_health():
    """B1 regression: /health must be in AUTH_WHITELIST."""
    from metano.auth import AUTH_WHITELIST
    assert "/health" in AUTH_WHITELIST


def test_require_role_admin():
    """require_role('admin') should reject non-admin users."""
    from metano.auth import require_role
    from fastapi import HTTPException
    from unittest.mock import MagicMock

    checker = require_role("admin")

    # Admin should pass
    request = MagicMock()
    request.state.user = {"username": "admin", "role": "admin"}
    result = checker(request)
    assert result["role"] == "admin"

    # Guest should be rejected
    request2 = MagicMock()
    request2.state.user = {"username": "guest", "role": "guest"}
    with pytest.raises(HTTPException) as exc_info:
        checker(request2)
    assert exc_info.value.status_code == 403


# ── P1-9: deleted users must never pass stale-token verification ───────────
#
# Audit P1-9: get_token_version() returned 0 for a user that no longer exists,
# so an A2A/MCP token minted while the user existed (tv=0) compared 0 == 0
# against the deleted user and stayed valid forever.  The fix makes
# get_token_version() return -1 for a missing user and makes the A2A/MCP
# verifiers explicitly reject that sentinel.

import yaml


def _rewrite_config(config_path, users=None, token_versions=None):
    """Overwrite the gateway_config.yaml at config_path.

    If ``users``/``token_versions`` are given they replace those sections; the
    rest of the config (jwt_secret, ...) is preserved.
    """
    cfg = yaml.safe_load(config_path.read_text()) or {}
    auth = cfg.setdefault("auth", {})
    if users is not None:
        auth["users"] = users
    if token_versions is not None:
        auth["token_versions"] = token_versions
    config_path.write_text(yaml.dump(cfg, allow_unicode=True))


def test_get_token_version_nonexistent_user_returns_minus_one(auth_config):
    """P1-9(a): a user that does not exist reports tv == -1, never 0."""
    from metano.auth import CONFIG_PATH, get_token_version

    _rewrite_config(CONFIG_PATH, users=[{"username": "only-user", "password": "x", "role": "user"}])
    assert get_token_version("only-user") == 0        # existing, unversioned → 0
    assert get_token_version("ghost") == -1           # never existed → -1


def test_get_token_version_reflects_bump(auth_config):
    """P1-9(a): an existing user reports their current token_version."""
    from metano.auth import CONFIG_PATH, bump_token_version, get_token_version

    _rewrite_config(CONFIG_PATH, users=[{"username": "alice", "password": "x", "role": "user"}])
    assert get_token_version("alice") == 0
    bumped = bump_token_version("alice")
    assert bumped == 1
    assert get_token_version("alice") == 1


def test_a2a_token_rejected_after_user_deleted(auth_config):
    """P1-9(b): a tv=0 A2A token minted for a user is rejected after deletion."""
    from metano.auth import CONFIG_PATH
    from metano.a2a_server import create_a2a_token, verify_a2a_token

    _rewrite_config(CONFIG_PATH, users=[{"username": "alice", "password": "x", "role": "user"}])
    # Mint while alice exists — she has no token_version, so tv == 0.
    token = create_a2a_token("alice", scope=["a2a:read"])
    # Verify while alice exists → valid.
    assert verify_a2a_token(token) is not None
    # Simulate the user being deleted from auth.users.
    _rewrite_config(CONFIG_PATH, users=[{"username": "admin", "password": "x", "role": "admin"}])
    assert verify_a2a_token(token) is None


def test_a2a_token_for_never_existing_user_rejected(auth_config):
    """P1-9(b): a forged A2A token claiming a deleted/unknown subject is rejected."""
    from metano.auth import CONFIG_PATH
    from metano.a2a_server import create_a2a_token, verify_a2a_token

    _rewrite_config(CONFIG_PATH, users=[{"username": "admin", "password": "x", "role": "admin"}])
    # Even if someone mints for a subject that never existed, the verifier must
    # refuse it (get_token_version('ghost') == -1 → explicit reject).
    token = create_a2a_token("ghost", scope=["a2a:read"])
    assert verify_a2a_token(token) is None


def test_a2a_token_valid_for_existing_user(auth_config):
    """P1-9(c): a normal user's A2A token still verifies."""
    from metano.auth import CONFIG_PATH, bump_token_version
    from metano.a2a_server import create_a2a_token, verify_a2a_token

    _rewrite_config(CONFIG_PATH, users=[{"username": "admin", "password": "x", "role": "admin"}],
                    token_versions={"admin": 5})
    token = create_a2a_token("admin", scope=["a2a:read"])
    payload = verify_a2a_token(token)
    assert payload is not None
    assert payload["sub"] == "admin"
    assert payload.get("tv") == 5


def test_mcp_token_rejected_after_user_deleted(auth_config):
    """P1-9(b): a tv=0 MCP token minted for a user is rejected after deletion."""
    from metano.auth import CONFIG_PATH
    from metano.mcp_gateway import create_mcp_token, verify_mcp_token

    _rewrite_config(CONFIG_PATH, users=[{"username": "alice", "password": "x", "role": "user"}])
    token = create_mcp_token("alice", scope=["mcp:read"])
    assert verify_mcp_token(token) is not None
    _rewrite_config(CONFIG_PATH, users=[{"username": "admin", "password": "x", "role": "admin"}])
    assert verify_mcp_token(token) is None


def test_mcp_token_valid_for_existing_user(auth_config):
    """P1-9(c): a normal user's MCP token still verifies."""
    from metano.auth import CONFIG_PATH
    from metano.mcp_gateway import create_mcp_token, verify_mcp_token

    _rewrite_config(CONFIG_PATH, users=[{"username": "admin", "password": "x", "role": "admin"}],
                    token_versions={"admin": 3})
    token = create_mcp_token("admin", scope=["mcp:read"])
    payload = verify_mcp_token(token)
    assert payload is not None
    assert payload["sub"] == "admin"
    assert payload.get("tv") == 3
