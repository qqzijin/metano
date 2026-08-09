"""Tests for cron_daemon module — job loading, scheduling, tick logic."""

import json
import time
from pathlib import Path


def test_compute_next_run_interval():
    from metano.cron_daemon import compute_next_run
    r = compute_next_run({"kind": "interval", "expr": "30"}, None)
    assert r is not None
    # Should be ~30 min from now
    import datetime
    ts = datetime.datetime.fromisoformat(r).timestamp()
    assert ts > time.time() + 20 * 60


def test_compute_next_run_interval_with_last():
    from metano.cron_daemon import compute_next_run
    from datetime import datetime, timezone
    last = datetime.now(timezone.utc).isoformat()
    r = compute_next_run({"kind": "interval", "expr": "10"}, last)
    assert r is not None
    ts = datetime.fromisoformat(r).timestamp()
    assert ts > time.time() + 5 * 60


def test_compute_next_run_cron():
    from metano.cron_daemon import compute_next_run
    r = compute_next_run({"kind": "cron", "expr": "0 0 * * *"}, None)
    assert r is not None


def test_compute_next_run_cron_no_croniter():
    from metano.cron_daemon import compute_next_run
    # When croniter unavailable, falls back to interval 60min
    import builtins
    orig_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == 'croniter':
            raise ImportError
        return orig_import(name, *args, **kwargs)

    builtins.__import__ = mock_import
    try:
        r = compute_next_run({"kind": "cron", "expr": "0 0 * * *"}, None)
        assert r is not None
    finally:
        builtins.__import__ = orig_import


def test_compute_next_run_str_schedule():
    from metano.cron_daemon import compute_next_run
    r = compute_next_run("0 */6 * * *", None)
    assert r is not None


def test_compute_next_run_unknown_kind():
    from metano.cron_daemon import compute_next_run
    r = compute_next_run({"kind": "unknown", "expr": "30"}, None)
    assert r is None


def test_load_jobs_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.cron_daemon.JOBS_FILE", tmp_path / "nonexistent.json")
    from metano.cron_daemon import load_jobs
    assert load_jobs() == []


def test_load_jobs_with_data(tmp_path, monkeypatch):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps([{"id": "j1", "name": "test", "enabled": True}]))
    monkeypatch.setattr("metano.cron_daemon.JOBS_FILE", jobs_file)
    from metano.cron_daemon import load_jobs
    jobs = load_jobs()
    assert len(jobs) == 1
    assert jobs[0]["name"] == "test"


def test_load_jobs_dict_format(tmp_path, monkeypatch):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps({"jobs": [{"id": "j1", "name": "test"}]}))
    monkeypatch.setattr("metano.cron_daemon.JOBS_FILE", jobs_file)
    from metano.cron_daemon import load_jobs
    jobs = load_jobs()
    assert len(jobs) == 1


def test_save_then_load(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.cron_daemon.CRON_DIR", tmp_path)
    monkeypatch.setattr("metano.cron_daemon.JOBS_FILE", tmp_path / "jobs.json")
    from metano.cron_daemon import save_jobs, load_jobs

    original = [{"id": "j1", "name": "test", "schedule": {"kind": "interval", "expr": "30"}}]
    save_jobs(original)
    loaded = load_jobs()
    assert len(loaded) == 1
    assert loaded[0]["name"] == "test"


def test_tick_no_lock_contention(tmp_path, monkeypatch):
    """tick should gracefully handle lock contention."""
    monkeypatch.setattr("metano.cron_daemon.CRON_DIR", tmp_path)
    monkeypatch.setattr("metano.cron_daemon.JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr("metano.cron_daemon.LOCK_FILE", tmp_path / "tick.lock")

    # Pre-acquire the lock
    import fcntl
    lock = open(tmp_path / "tick.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)

    from metano.cron_daemon import tick
    # Should not crash - just skip with a debug log
    tick()

    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
