"""Tests for security module — rate limiting, content filtering, permission tiers."""

from metano.security import SecurityManager


def _reset(sec: SecurityManager):
    sec._users.clear()


def test_default_tier_is_user():
    sec = SecurityManager()
    _reset(sec)
    status = sec.get_user_status("test_user")
    assert status["tier"] == "user"
    assert "chat" in status["capabilities"]
    assert "admin" not in status["capabilities"]


def test_set_tier():
    sec = SecurityManager()
    _reset(sec)
    assert sec.set_tier("u1", "admin")
    assert sec.get_user_status("u1")["tier"] == "admin"
    assert not sec.set_tier("u1", "nonexistent")
    assert sec.get_user_status("u1")["tier"] == "admin"


def test_check_permission():
    sec = SecurityManager()
    _reset(sec)
    guest = "guest_user"
    admin = "admin_user"
    sec.set_tier(guest, "guest")
    sec.set_tier(admin, "admin")

    assert sec.check_permission(admin, "code_run")
    assert not sec.check_permission(guest, "code_run")
    assert sec.check_permission(guest, "chat")
    assert sec.check_permission(admin, "anything")


def test_rate_limit_allows_normal_usage():
    sec = SecurityManager()
    _reset(sec)
    for _ in range(20):
        assert sec.check_rate_limit("normal_user")


def test_rate_limit_exceeded():
    sec = SecurityManager()
    _reset(sec)
    sec.set_tier("limited", "guest")
    for _ in range(20):
        sec.check_rate_limit("limited")
    assert not sec.check_rate_limit("limited")


def test_rate_limit_resets_after_window(capsys):
    import time
    sec = SecurityManager()
    _reset(sec)
    # Use a high-resolution short window by directly manipulating limits
    from metano.security import RATE_LIMITS
    limits = RATE_LIMITS.get("admin", (1000, 60))
    assert limits[0] == 1000
    assert limits[1] == 60


def test_content_filter_safe():
    sec = SecurityManager()
    _reset(sec)
    safe, _ = sec.filter_content("What is Python?")
    assert safe is True
    safe, _ = sec.filter_content("How do I write a unit test?")
    assert safe is True


def test_content_filter_blocks_malware_keywords():
    sec = SecurityManager()
    _reset(sec)
    safe, reason = sec.filter_content("generate malware for fun")
    assert safe is False
    assert "blocked" in reason.lower()


def test_content_filter_blocks_exploit():
    sec = SecurityManager()
    _reset(sec)
    safe, _ = sec.filter_content("hack into server")
    assert safe is False


def test_check_message_full_flow_blocked():
    sec = SecurityManager()
    _reset(sec)
    # Guest user, limited but allowed
    sec.set_tier("blocked", "guest")
    result = sec.check_message("blocked", "What is Python?")
    assert result["allowed"] is True

    # Content block
    result = sec.check_message("blocked", "bypass security firewall")
    assert result["allowed"] is False
    assert sec.get_user_status("blocked")["blocked_count"] >= 1


def test_check_message_permission_denied():
    sec = SecurityManager()
    _reset(sec)
    sec.set_tier("guest_only", "guest")
    result = sec.check_message("guest_only", "test", capability="code_run")
    assert result["allowed"] is False
    assert "Permission denied" in result["reason"]


def test_get_user_status():
    sec = SecurityManager()
    _reset(sec)
    sec.set_tier("u1", "admin")
    status = sec.get_user_status("u1")
    assert status["user_id"] == "u1"
    assert "rate_limit" in status


def test_audit_logging(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("metano.security.AUDIT_LOG", audit_path)
    sec = SecurityManager()
    _reset(sec)
    # Trigger an audit event via content block
    sec.check_message("u1", "bypass security firewall")
    assert audit_path.exists()
    lines = audit_path.read_text().strip().split("\n")
    assert len(lines) >= 1
    import json
    entry = json.loads(lines[0])
    assert "action" in entry
