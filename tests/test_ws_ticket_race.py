"""Race / replay / expiry regression tests for WS ticket consumption (audit P1-10).

The old implementation stored consumed jtis in a plain ``set`` and, once the set
grew past 1000 entries, wholesale-cleared it — letting any ticket consumed
within the same 30s TTL window be replayed. The check-then-add was also
non-atomic, so two concurrent consumers of the same jti could both be admitted.
These tests pin the fixed behaviour: single-use within TTL, exactly-once under
concurrency, and lazy expiry instead of a permanent blacklist.
"""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from metano import auth as auth_mod
from metano.auth import (
    _WS_TICKET_TTL,
    _used_ws_tickets,
    consume_ws_ticket,
    create_ws_ticket,
)


@pytest.fixture(autouse=True)
def _isolated_ws_tickets():
    """Keep the module-level consumed-ticket state isolated between tests."""
    _used_ws_tickets.clear()
    yield
    _used_ws_tickets.clear()


@pytest.fixture()
def jwt_secret(monkeypatch):
    """Deterministic JWT secret so ticket create/consume round-trips."""
    monkeypatch.setenv("HERMES_JWT_SECRET", "t" * 48)


def test_ws_ticket_replay_refused(jwt_secret):
    """Same jti consumed twice within TTL → second consume returns None."""
    ticket = create_ws_ticket("alice", "user")
    assert consume_ws_ticket(ticket) is not None
    # Replay of the same ticket is refused (one-time).
    assert consume_ws_ticket(ticket) is None


def test_ws_ticket_concurrent_same_jti_allows_once(jwt_secret):
    """10 threads consuming the *same* jti → exactly one succeeds."""
    ticket = create_ws_ticket("alice", "user")

    def try_consume(_):
        return consume_ws_ticket(ticket) is not None

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(try_consume, range(10)))

    assert sum(results) == 1, f"expected exactly 1 success, got {sum(results)}"
    # And a follow-up replay is still refused.
    assert consume_ws_ticket(ticket) is None


def test_ws_ticket_expired_entry_reenables(jwt_secret):
    """A stale (>TTL) entry is pruned lazily, so the same jti is re-admitted."""
    ticket = create_ws_ticket("alice", "user")
    assert consume_ws_ticket(ticket) is not None
    assert consume_ws_ticket(ticket) is None

    # Forge the recorded consumption timestamp so the entry is past its TTL.
    jti = auth_mod.decode_token(ticket)["jti"]
    assert jti in _used_ws_tickets
    _used_ws_tickets[jti] = time.time() - _WS_TICKET_TTL - 5

    # The stale entry is pruned on access, so the same jti is re-admitted.
    assert consume_ws_ticket(ticket) is not None
    # And it is single-use again afterwards.
    assert consume_ws_ticket(ticket) is None
