"""Tests for behavior_analyzer module — correction clustering, pattern analysis."""


def test_cluster_corrections_empty():
    from metano.behavior_analyzer import _cluster_corrections
    clusters = _cluster_corrections([])
    assert isinstance(clusters, dict)
    assert len(clusters) == 0


def test_cluster_corrections_basic():
    from metano.behavior_analyzer import _cluster_corrections
    corrections = [
        {"type": "correction", "user_content": "don't use f-strings for SQL"},
        {"type": "correction", "user_content": "not like that, use parameterized"},
        {"type": "correction", "user_content": "be more concise in explanations"},
    ]
    clusters = _cluster_corrections(corrections)
    # Should produce some clusters
    assert isinstance(clusters, dict)


def test_get_behavior_patterns_return_format(monkeypatch):
    from metano.behavior_analyzer import get_behavior_patterns

    from unittest.mock import MagicMock
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = []
    monkeypatch.setattr("metano.behavior_analyzer.get_honcho_db", lambda: mock_conn)
    monkeypatch.setattr("metano.behavior_analyzer.get_rules", lambda kind=None: [])

    r = get_behavior_patterns("default")
    assert "patterns" in r
    assert "recent_corrections" in r
