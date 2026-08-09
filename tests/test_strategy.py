"""Tests for strategy module — action recording, outcome tracking, rule effectiveness."""

from unittest.mock import MagicMock, patch


def test_record_action(monkeypatch):
    from metano.strategy import record_action

    mock_conn = MagicMock()
    mock_conn.execute.return_value.lastrowid = 42

    with patch("metano.evo_models._get_conn", return_value=mock_conn):
        action_id = record_action("sess1", "test_action", "detail")
        assert action_id == 42


def test_record_outcome_success(monkeypatch):
    from metano.strategy import record_outcome

    mock_conn = MagicMock()
    with patch("metano.evo_models._get_conn", return_value=mock_conn):
        r = record_outcome(1, "success")
        assert r["status"] == "recorded"
        assert r["outcome"] == "success"


def test_record_outcome_failure(monkeypatch):
    from metano.strategy import record_outcome

    mock_conn = MagicMock()
    with patch("metano.evo_models._get_conn", return_value=mock_conn):
        r = record_outcome(1, "failure", "timeout")
        assert r["status"] == "recorded"
        assert r["outcome"] == "failure"


class MockRow(dict):
    """Pseudo-row that supports both dict key access and attribute access."""
    pass


def test_get_effectiveness(monkeypatch):
    from metano.strategy import get_effectiveness

    mock_row = MockRow({"id": "r1", "content": "test rule", "effectiveness": 0.7,
                        "times_applied": 10, "times_succeeded": 7, "times_failed": 3,
                        "active": 1})
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = mock_row

    with patch("metano.evo_models._get_conn", return_value=mock_conn):
        r = get_effectiveness("r1")
        assert r["effectiveness"] == 0.7


def test_get_effectiveness_not_found(monkeypatch):
    from metano.strategy import get_effectiveness

    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = None

    with patch("metano.evo_models._get_conn", return_value=mock_conn):
        r = get_effectiveness("nonexistent")
        assert r["found"] is False


def test_select_strategy(monkeypatch):
    from metano.strategy import select_strategy

    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = [
        {"id": "r1", "kind": "behavior", "content": "test rule",
         "effectiveness": 0.8, "times_applied": 5}
    ]

    with patch("metano.evo_models._get_conn", return_value=mock_conn):
        rules = select_strategy("testing context")
        assert isinstance(rules, list)


def test_detect_strategy_patterns_empty(monkeypatch):
    from metano.strategy import detect_strategy_patterns

    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = []

    with patch("metano.evo_models._get_conn", return_value=mock_conn):
        patterns = detect_strategy_patterns()
        assert isinstance(patterns, list)
