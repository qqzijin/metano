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


def test_get_jwt_secret_no_hardcoded_fallback():
    """S2: get_jwt_secret must NOT return hardcoded fallback."""
    from metano.auth import get_jwt_secret
    # Either reads from env var or from config file — never returns 'fallback-secret-change-me'
    with patch.dict(os.environ, {}, clear=True):
        # If config file has a valid secret, it should work
        # If not, it should raise RuntimeError
        try:
            secret = get_jwt_secret()
            assert secret != "fallback-secret-change-me"
            assert len(secret) >= 32
        except RuntimeError as e:
            assert "jwt_secret" in str(e).lower() or "refuse" in str(e).lower()


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
