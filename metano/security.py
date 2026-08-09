"""Security: rate limiting, content filtering, permission tiers, audit logging."""

import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

AUDIT_LOG = Path.home() / ".claude" / "metano" / "security" / "audit.jsonl"

# Permission tiers
TIER_ADMIN = "admin"
TIER_USER = "user"
TIER_GUEST = "guest"

TIER_CAPABILITIES = {
    TIER_ADMIN: {"all"},
    TIER_USER: {"chat", "skills", "code_run", "browser", "image", "knowledge", "voice"},
    TIER_GUEST: {"chat", "skills"},
}

# Rate limits: {tier: (max_requests, window_seconds)}
RATE_LIMITS = {
    TIER_ADMIN: (1000, 60),
    TIER_USER: (60, 60),
    TIER_GUEST: (20, 60),
}

# Content filter patterns
BLOCKED_PATTERNS = [
    r"(?i)(hack|exploit|attack)\s+(into|server|system|database)",
    r"(?i)(generate|create|write)\s+(malware|virus|trojan|ransomware)",
    r"(?i)(bypass|circumvent)\s+(security|firewall|authentication)",
]


@dataclass
class UserSecurity:
    user_id: str
    tier: str = TIER_USER
    request_times: list[float] = field(default_factory=list)
    blocked_count: int = 0
    last_violation: str = ""


class SecurityManager:
    def __init__(self):
        self._users: dict[str, UserSecurity] = {}
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)

    def _get_user(self, user_id: str) -> UserSecurity:
        if user_id not in self._users:
            self._users[user_id] = UserSecurity(user_id=user_id)
        return self._users[user_id]

    def set_tier(self, user_id: str, tier: str) -> bool:
        if tier not in TIER_CAPABILITIES:
            return False
        user = self._get_user(user_id)
        user.tier = tier
        self._audit("set_tier", user_id, {"tier": tier})
        return True

    def check_permission(self, user_id: str, capability: str) -> bool:
        """Check if a user has permission for a capability."""
        user = self._get_user(user_id)
        caps = TIER_CAPABILITIES.get(user.tier, set())
        return "all" in caps or capability in caps

    def check_rate_limit(self, user_id: str) -> bool:
        """Check if a user is within rate limits. Returns True if allowed."""
        user = self._get_user(user_id)
        max_req, window = RATE_LIMITS.get(user.tier, (20, 60))
        now = time.time()

        # Clean old entries; cap list to prevent unbounded growth
        user.request_times = [t for t in user.request_times if now - t < window]
        if len(user.request_times) > 1000:
            user.request_times = user.request_times[-1000:]

        if len(user.request_times) >= max_req:
            self._audit("rate_limited", user_id, {"requests": len(user.request_times), "window": window})
            return False

        user.request_times.append(now)
        return True

    def filter_content(self, text: str) -> tuple[bool, str]:
        """Check content against filter rules. Returns (is_safe, reason)."""
        for pattern in BLOCKED_PATTERNS:
            if re.search(pattern, text):
                return False, f"Content matches blocked pattern"
        return True, ""

    def check_message(self, user_id: str, message: str, capability: str = "chat") -> dict:
        """Full security check for an incoming message. Returns {allowed, reason}."""
        # Check permission
        if not self.check_permission(user_id, capability):
            return {"allowed": False, "reason": f"Permission denied: {capability} requires higher tier"}

        # Check rate limit
        if not self.check_rate_limit(user_id):
            user = self._get_user(user_id)
            return {"allowed": False, "reason": f"Rate limit exceeded ({user.tier} tier)"}

        # Check content
        is_safe, reason = self.filter_content(message)
        if not is_safe:
            user = self._get_user(user_id)
            user.blocked_count += 1
            user.last_violation = reason
            self._audit("content_blocked", user_id, {"reason": reason, "message_preview": message[:100]})
            return {"allowed": False, "reason": reason}

        return {"allowed": True, "reason": ""}

    def get_user_status(self, user_id: str) -> dict:
        """Get security status for a user."""
        user = self._get_user(user_id)
        max_req, window = RATE_LIMITS.get(user.tier, (20, 60))
        now = time.time()
        recent = [t for t in user.request_times if now - t < window]
        return {
            "user_id": user_id,
            "tier": user.tier,
            "capabilities": list(TIER_CAPABILITIES.get(user.tier, set())),
            "rate_limit": f"{len(recent)}/{max_req} per {window}s",
            "blocked_count": user.blocked_count,
        }

    def _audit(self, action: str, user_id: str, details: dict):
        """Write an audit log entry."""
        entry = {
            "timestamp": time.time(),
            "action": action,
            "user_id": user_id,
            "details": details,
        }
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


    def list_users(self) -> list[dict]:
        """List all known users with their security status."""
        return [
            {
                "user_id": uid,
                "tier": u.tier,
                "rate_limit_remaining": max(0, RATE_LIMITS[u.tier][0] - len(u.request_times)),
                "blocked_count": u.blocked_count,
            }
            for uid, u in self._users.items()
        ]


def role_to_tier(role: str) -> str:
    """Map JWT role to security tier."""
    return {"admin": TIER_ADMIN, "user": TIER_USER, "guest": TIER_GUEST}.get(role, TIER_GUEST)


security = SecurityManager()