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
