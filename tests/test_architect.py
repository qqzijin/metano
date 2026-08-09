"""Tests for architect module — model building, bottleneck detection, proposals.

LLM-dependent functions are tested with mocks.
"""

import json
import time


def test_build_architecture_model(tmp_path, monkeypatch):
    """Smoke test: model builds without crashing."""
    monkeypatch.setattr("metano.architect.PROJECT_DIR", tmp_path)
    monkeypatch.setattr("metano.architect.ARCH_SNAP_DIR", tmp_path / "snapshots")
    (tmp_path / "metano").mkdir(parents=True, exist_ok=True)
    (tmp_path / "metano" / "test_mod.py").write_text("x = 1")

    from metano.architect import build_architecture_model
    model = build_architecture_model()
    assert "components" in model
    assert "timestamp" in model


def test_detect_bottlenecks_empty():
    from metano.architect import detect_bottlenecks
    findings = detect_bottlenecks({"components": [], "mcp_tools": [], "cron_jobs": [], "rules": []})
    assert isinstance(findings, list)


def test_detect_bottlenecks_oversized(monkeypatch):
    from metano.architect import detect_bottlenecks
    monkeypatch.setattr("metano.architect.get_action_stats", lambda: {"total": 0, "by_outcome": {}})
    monkeypatch.setattr("metano.architect.get_rules", lambda active_only=True: [])
    model = {
        "components": [{"name": "huge.py", "size_bytes": 100000}],
        "mcp_tools": [], "cron_jobs": [], "rules": []
    }
    findings = detect_bottlenecks(model)
    types = [f["type"] for f in findings]
    assert "oversized_module" in types


def test_detect_bottlenecks_cron_error(monkeypatch):
    from metano.architect import detect_bottlenecks
    monkeypatch.setattr("metano.architect.get_action_stats", lambda: {"total": 0, "by_outcome": {}})
    monkeypatch.setattr("metano.architect.get_rules", lambda active_only=True: [])
    model = {
        "components": [],
        "cron_jobs": [{"id": "j1", "enabled": True, "last_error": "something broke"}],
        "mcp_tools": [], "rules": []
    }
    findings = detect_bottlenecks(model)
    types = [f["type"] for f in findings]
    assert "cron_error" in types


def test_propose_restructure():
    from metano.architect import propose_restructure
    findings = [{"type": "cron_error", "severity": "high", "modifiable_target": "cron", "suggestion": "fix cron job"}]
    proposals = propose_restructure(findings)
    assert len(proposals) >= 1
    assert proposals[0]["target"] == "cron"


def test_propose_restructure_skips_low_severity():
    from metano.architect import propose_restructure
    findings = [{"type": "info", "severity": "low", "modifiable_target": "cron", "suggestion": "minor"}]
    proposals = propose_restructure(findings)
    assert len(proposals) == 0


def test_apply_cron_change(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.architect.CRON_FILE", tmp_path / "jobs.json")
    jobs = [{"id": "test_job", "name": "test", "enabled": True}]
    (tmp_path / "jobs.json").write_text(json.dumps(jobs))

    from metano.architect import _apply_cron_change
    r = _apply_cron_change("disable test_job because it's failing")
    assert r["status"] == "toggled"

    reloaded = json.loads((tmp_path / "jobs.json").read_text())
    assert reloaded[0]["enabled"] is False


def test_apply_meta_change(monkeypatch):
    from metano.architect import _apply_meta_change
    results = []

    def fake_set_meta(key, value):
        results.append((key, value))

    monkeypatch.setattr("metano.architect.set_meta", fake_set_meta)
    r = _apply_meta_change("learning_rate=0.01")
    assert r["status"] == "set_meta"
    assert results == [("learning_rate", 0.01)]


def test_apply_skills_change():
    from metano.architect import _apply_skills_change
    r = _apply_skills_change("update trigger for web search")
    assert r["status"] == "skills_change_requires_manual_review"


def test_apply_gateway_change():
    from metano.architect import _apply_gateway_change
    r = _apply_gateway_change("change routing priority")
    assert r["status"] == "gateway_change_requires_manual_review"
