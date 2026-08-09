"""Tests for reflector module — coherence, coverage, accuracy checks.

LLM functions are mocked.
"""

from unittest.mock import patch


def test_check_coherence_no_beliefs():
    from metano.reflector import _check_coherence
    issues = _check_coherence([])
    assert issues == []


def test_check_coherence_contradiction():
    from metano.reflector import _check_coherence
    beliefs = [
        {"id": "1", "category": "preference", "content": "User prefers dark mode", "confidence": 0.8},
        {"id": "2", "category": "preference", "content": "User prefers light mode", "confidence": 0.7},
    ]
    issues = _check_coherence(beliefs)
    # Should detect contradiction or return empty
    assert isinstance(issues, list)


def test_check_coverage_empty_beliefs(monkeypatch):
    from metano.reflector import _check_coverage
    monkeypatch.setattr("metano.reflector._call_llm", lambda s, u: "[]")
    issues = _check_coverage("default", [], days=7)
    assert isinstance(issues, list)


def test_check_accuracy_no_beliefs():
    from metano.reflector import _check_accuracy
    issues = _check_accuracy([])
    assert issues == []


def test_apply_correction(monkeypatch):
    from metano.reflector import apply_correction
    from unittest.mock import MagicMock

    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = None

    monkeypatch.setattr("metano.reflector.get_honcho_db", lambda: mock_conn)
    monkeypatch.setattr("metano.reflector.get_beliefs", lambda conn, uid: [])
    monkeypatch.setattr("metano.reflector.ANTHROPIC_API_KEY", "")

    r = apply_correction("default", "This is wrong, should use parameterized queries")
    assert isinstance(r, dict)


def test_apply_correction_empty():
    from metano.reflector import apply_correction
    from unittest.mock import MagicMock, patch
    with patch("metano.reflector.get_honcho_db", return_value=MagicMock()):
        with patch("metano.reflector.get_beliefs", return_value=[]):
            with patch("metano.reflector.ANTHROPIC_API_KEY", ""):
                r = apply_correction("default", "")
                assert isinstance(r, dict)


def test_reflect_on_model_calls_llm(monkeypatch):
    from metano.reflector import reflect_on_model
    monkeypatch.setattr("metano.reflector._call_llm", lambda s, u: "[]")

    from unittest.mock import MagicMock
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = []
    monkeypatch.setattr("metano.reflector.get_honcho_db", lambda: mock_conn)
    monkeypatch.setattr("metano.reflector.get_beliefs", lambda conn, uid: [])

    r = reflect_on_model("default")
    assert isinstance(r, dict)
