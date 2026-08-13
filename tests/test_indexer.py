"""Audit N2: indexer must redact secrets before persisting session messages.

The indexer writes extracted Claude Code session text (including
``[tool:Bash]`` command inputs, which can carry live app_secret / sk- keys /
bearer tokens) into bridge.db.messages.  This test pins the
``redact_sensitive()`` call on that path, mirroring the persistence guard
already in ``db.persist_exchange``.

The autouse ``isolated_env`` fixture redirects every DB path to a throwaway
tmp dir, so these assertions never touch production bridge.db.
"""

import pytest

from metano import db as metano_db
from metano.indexer import process_records

pytestmark = pytest.mark.usefixtures("isolated_env")

# Test fixture values only — deliberately NOT a live secret (matches the
# established convention in tests/test_db.py test_redact_sensitive_patterns).
_FAKE_SK = "sk-mWbiLOPVabcdef123456"


def _records():
    """Minimal Claude Code JSONL records: one assistant ``tool_use`` carrying a
    secret-bearing Bash command, plus one user turn with a key/value pair."""
    return [
        {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": f"export KEY={_FAKE_SK}"},
                    }
                ],
            },
            "timestamp": "2026-08-13T00:00:00Z",
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "text",
                     "text": f"please set app_secret={_FAKE_SK}"},
                ]
            },
            "timestamp": "2026-08-13T00:01:00Z",
        },
    ]


def test_process_records_redacts_secrets_in_content():
    conn = metano_db.init_db()
    process_records(conn, "sess-redact-1", "proj", _records())
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id=? ORDER BY timestamp",
        ("sess-redact-1",),
    ).fetchall()
    conn.close()
    by_role = {r["role"]: r["content"] for r in rows}
    # The [tool:Bash] command input must not persist the key.
    assert "sk-mWbiLOPV" not in (by_role["assistant"] or "")
    assert "[REDACTED]" in (by_role["assistant"] or "")
    # User-turn key/value text is redacted too.
    assert "app_secret=[REDACTED]" in (by_role["user"] or "")
    assert "sk-mWbiLOPV" not in (by_role["user"] or "")


def test_process_records_tool_calls_field_contains_no_secret():
    # tool_calls stores only key names today; pin that the field stays
    # secret-free (defensive redaction is a safe no-op on key names).
    conn = metano_db.init_db()
    process_records(conn, "sess-redact-2", "proj", _records())
    row = conn.execute(
        "SELECT tool_calls FROM messages "
        "WHERE session_id=? AND role='assistant'",
        ("sess-redact-2",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert "sk-mWbiLOPV" not in (row["tool_calls"] or "")
