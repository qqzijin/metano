"""M16: key-path coverage for metano/a2a_server.py — scope tiers + task owner checks.

The A2A token is a local-RCE trust boundary; a valid token can only act on its
own tasks unless it carries ``a2a:admin``.  These tests drive the scope and
ownership helpers directly (no live delegator needed).
"""

from types import SimpleNamespace

import pytest

from metano import a2a_server
from metano.a2a_server import (
    A2AErrorCode,
    _admin_scope,
    _check_task_access,
    _RPCError,
    _scope_allows,
    _scope_denied,
    _scope_tier,
    _valid_task_id,
)

pytestmark = pytest.mark.usefixtures("isolated_env")


# ── scope tiers ────────────────────────────────────────────────────────────

def test_scope_tier_ranks():
    assert _scope_tier([]) == 0
    assert _scope_tier(['a2a:read']) == 1
    assert _scope_tier(['a2a:task']) == 2
    assert _scope_tier(['a2a:admin']) == 3
    assert _scope_tier(['a2a:read', 'a2a:admin']) == 3
    assert _scope_tier(['unknown']) == 0


def test_scope_allows_min_tier():
    assert _scope_allows(['a2a:task'], 'a2a:task') is True
    assert _scope_allows(['a2a:read'], 'a2a:task') is False
    # admin tier (3) implicitly holds every lower tier.
    assert _scope_allows(['a2a:admin'], 'a2a:task') is True
    assert _scope_allows(['a2a:admin'], 'a2a:read') is True
    assert _scope_allows([], 'a2a:read') is False


def test_scope_denied_by_method():
    assert _scope_denied(['a2a:read'], 'message/send') is True
    assert _scope_denied(['a2a:read'], 'tasks/get') is False
    assert _scope_denied(['a2a:task'], 'tasks/cancel') is False
    # Unknown method is not denied here (dispatcher reports METHOD_NOT_FOUND).
    assert _scope_denied(['a2a:read'], 'no/such/method') is False


# ── admin bypass helper ────────────────────────────────────────────────────

def test_admin_scope():
    assert _admin_scope(['a2a:admin']) is True
    assert _admin_scope(['admin']) is True
    assert _admin_scope(['a2a:task']) is False
    assert _admin_scope(None) is False
    assert _admin_scope([]) is False


# ── task id validation ─────────────────────────────────────────────────────

def test_valid_task_id():
    # The id pattern is ^[A-Za-z0-9_-]{12,64}$ (F-18: rejects path traversal).
    assert _valid_task_id('task-abc123456') is True
    assert _valid_task_id('t-abc123') is False       # too short
    assert _valid_task_id('') is False
    assert _valid_task_id('../../etc/passwd') is False
    assert _valid_task_id('a b c d e f') is False    # spaces / too short


# ── owner check (M-02) ─────────────────────────────────────────────────────

def test_owner_check_admin_bypasses():
    task = SimpleNamespace(owner='alice')
    _check_task_access('mallory', ['a2a:admin'], task)  # must not raise


def test_owner_check_legacy_task_accessible():
    # Legacy tasks (owner == '') remain visible to any authenticated caller.
    task = SimpleNamespace(owner='')
    _check_task_access('mallory', [], task)  # must not raise


def test_owner_check_owner_matches():
    task = SimpleNamespace(owner='alice')
    _check_task_access('alice', [], task)  # must not raise


def test_owner_check_foreign_task_forbidden():
    task = SimpleNamespace(owner='alice')
    with pytest.raises(_RPCError) as exc:
        _check_task_access('mallory', [], task)
    assert exc.value.code == A2AErrorCode.FORBIDDEN


def test_owner_check_task_without_owner_attr_legacy():
    task = SimpleNamespace()  # no .owner attribute → treated as legacy
    _check_task_access('mallory', [], task)  # must not raise
