"""Audit N16: ``llm_call`` cost rows carry the calling ``session_id``.

Previously ``_record_cost`` never wrote ``session_id`` into the ``audit_log``
row, so every ``phase='llm'`` row had ``session_id=''`` (0/389 attributed).
Now the caller's ``session_id`` is threaded through ``call_llm`` →
``_record_cost`` → ``add_audit``. This pins the two behaviors:

(a) a call made with ``session_id='sess123'`` produces an audit row with that id;
(b) a call made without a session (default ``''``) still writes the field
    (empty), so the column is never dropped.
"""

import pytest

from metano import llm_call
from metano.evo_models import get_audit

pytestmark = pytest.mark.usefixtures("isolated_env")


@pytest.fixture()
def fake_llm_env(monkeypatch):
    """Pin the provider + API call + pricing so no real network is touched."""
    monkeypatch.setattr(
        llm_call, "_resolve_provider",
        lambda: ("https://fake.invalid", "sk-test", "test-model", "anthropic"),
    )
    monkeypatch.setattr(
        llm_call, "_call_anthropic",
        lambda *a, **k: ("fake response", {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_tokens": 0,
        }),
    )
    monkeypatch.setattr(llm_call, "_estimate_cost", lambda *a, **k: 0.5)
    return llm_call


def test_call_llm_records_session_id(fake_llm_env):
    text, cost = llm_call.call_llm("sys", "user", session_id="sess123")
    assert text == "fake response"
    assert cost == 0.5

    rows = get_audit(limit=5, phase="llm")
    assert rows, "expected an llm audit row"
    newest = rows[0]
    assert newest["session_id"] == "sess123"
    assert newest["model"] == "test-model"


def test_call_llm_default_session_empty_but_field_present(fake_llm_env):
    llm_call.call_llm("sys", "user")

    rows = get_audit(limit=5, phase="llm")
    assert rows, "expected an llm audit row"
    newest = rows[0]
    # Field must exist (never dropped) and carry the empty-string default.
    assert "session_id" in newest
    assert newest["session_id"] == ""
