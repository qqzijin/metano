"""Test that the harvester persists tool_error observations (audit P1-4).

The harvester used to count tool errors without storing them, so
behavior_analyzer's ``category='tool_error'`` query never saw data and behavior
learning spun empty. These tests assert a simulated tool error produces a
``tool_error`` row in honcho.db that the analyzer's query can read.
"""

import time


def _make_session(conn, session_id: str, assistant_content: str):
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, project, started_at, last_active, user_key) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, "test", now, now, "user1"),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (session_id, "user", "please run the deploy script", now),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (session_id, "assistant", assistant_content, now + 0.001),
    )
    conn.commit()


def test_harvester_stores_tool_error_observation(monkeypatch):
    from metano import db as metano_db
    from metano.harvester import harvest_session
    from metano.honcho.models import get_honcho_db, get_user, create_user

    # Keep harvest deterministic: skip the LLM observation-extraction branch.
    monkeypatch.setattr("metano.harvester.extract_observations", lambda *a, **k: [])

    conn = metano_db.init_db()
    session_id = "sess-tool-err-1"
    _make_session(
        conn,
        session_id,
        "[tool:bash] deploy.sh failed with exit code 2: Error: connection refused",
    )

    hconn = get_honcho_db()
    if not get_user(hconn, "user1"):
        create_user(hconn, user_id="user1")

    result = harvest_session(conn, session_id)

    # The counting result is preserved.
    assert result["tool_errors"] == 1, result

    # ... and the observation is now actually persisted.
    rows = hconn.execute(
        "SELECT * FROM observations WHERE user_id = ? AND category = 'tool_error'",
        ("user1",),
    ).fetchall()
    assert len(rows) == 1, f"expected 1 tool_error observation, got {len(rows)}"
    obs = dict(rows[0])
    assert obs["category"] == "tool_error"
    assert obs["session_id"] == session_id
    assert obs["confidence"] == 1.0
    assert "bash" in obs["content"]
    assert "failed" in obs["content"]
    # Freshly written within the analyzer's 7-day window.
    assert obs["timestamp"] >= time.time() - 7 * 86400
    hconn.close()
    conn.close()


def test_behavior_analyzer_query_sees_tool_error(monkeypatch):
    """The exact SQL behavior_analyzer runs must return the new tool_error row."""
    from metano import db as metano_db
    from metano.harvester import harvest_session
    from metano.honcho.models import get_honcho_db, get_user, create_user

    monkeypatch.setattr("metano.harvester.extract_observations", lambda *a, **k: [])

    conn = metano_db.init_db()
    session_id = "sess-tool-err-2"
    _make_session(
        conn,
        session_id,
        "[tool:code_exec] Traceback (most recent call last): module not found",
    )

    hconn = get_honcho_db()
    if not get_user(hconn, "user1"):
        create_user(hconn, user_id="user1")

    harvest_session(conn, session_id)

    # Mirror behavior_analyzer.analyze_behavior_patterns's observation query.
    cutoff = time.time() - 7 * 86400
    rows = hconn.execute(
        "SELECT * FROM observations WHERE user_id = ? AND timestamp >= ? "
        "AND (category = 'correction' OR category = 'tool_error') "
        "ORDER BY timestamp DESC LIMIT 50",
        ("user1", cutoff),
    ).fetchall()
    obs = [dict(r) for r in rows]
    tool_errors = [o for o in obs if o["category"] == "tool_error"]
    assert len(tool_errors) == 1, tool_errors
    assert "code_exec" in tool_errors[0]["content"]
    hconn.close()
    conn.close()


def test_harvester_skips_ok_tool_call(monkeypatch):
    """A tool call WITHOUT an error indicator must not produce an observation."""
    from metano import db as metano_db
    from metano.harvester import harvest_session
    from metano.honcho.models import get_honcho_db, get_user, create_user

    monkeypatch.setattr("metano.harvester.extract_observations", lambda *a, **k: [])

    conn = metano_db.init_db()
    session_id = "sess-tool-ok-3"
    _make_session(
        conn,
        session_id,
        "[tool:bash] deploy.sh completed successfully, all checks passed",
    )

    hconn = get_honcho_db()
    if not get_user(hconn, "user1"):
        create_user(hconn, user_id="user1")

    result = harvest_session(conn, session_id)

    assert result["tool_errors"] == 0, result
    rows = hconn.execute(
        "SELECT * FROM observations WHERE user_id = ? AND category = 'tool_error'",
        ("user1",),
    ).fetchall()
    assert len(rows) == 0
    hconn.close()
    conn.close()
