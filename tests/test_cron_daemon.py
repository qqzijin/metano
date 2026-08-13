"""Tests for cron_daemon module — job loading, scheduling, tick logic."""

import json
import time
from pathlib import Path


def test_compute_next_run_interval():
    from metano.cron_daemon import compute_next_run
    r = compute_next_run({"kind": "interval", "expr": "30"}, None)
    assert r is not None
    # Aligned to the next :00/:30 boundary: strictly in the future, within one
    # interval of now (regardless of where in the slot we are).
    import datetime
    ts = datetime.datetime.fromisoformat(r).timestamp()
    assert time.time() < ts <= time.time() + 30 * 60


def test_compute_next_run_interval_with_last():
    from metano.cron_daemon import compute_next_run
    from datetime import datetime, timezone
    last = datetime.now(timezone.utc).isoformat()
    r = compute_next_run({"kind": "interval", "expr": "10"}, last)
    assert r is not None
    ts = datetime.fromisoformat(r).timestamp()
    assert time.time() < ts <= time.time() + 10 * 60


def test_interval_ignores_last_run_at():
    """Interval schedules align to wall-clock slots, so last_run_at (a stale
    finish time) must not shift the next run — that shift is what caused the
    P0-3 double-trigger (a late-finishing job kept next_run_at in the past)."""
    from metano.cron_daemon import compute_next_run
    from datetime import datetime, timezone
    old_last = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    recent_last = datetime.now(timezone.utc).isoformat()
    r1 = compute_next_run({"kind": "interval", "expr": "30"}, old_last)
    r2 = compute_next_run({"kind": "interval", "expr": "30"}, recent_last)
    assert r1 == r2


def test_interval_next_run_always_future_after_late_finish():
    """P0-3 regression: after a job finishes late, the next slot must still be
    in the future so the daemon cannot re-run the same slot on the next tick."""
    from metano.cron_daemon import compute_next_run
    from datetime import datetime, timezone
    # Previous run finished 35 min ago on a 30-min interval — under the old
    # logic next_run = last_run + interval would be 5 min in the past.
    late_last = datetime.fromtimestamp(
        time.time() - 35 * 60, tz=timezone.utc).isoformat()
    r = compute_next_run({"kind": "interval", "expr": "30"}, late_last)
    ts = datetime.fromisoformat(r.replace('Z', '+00:00')).timestamp()
    assert ts > time.time(), "next_run must be strictly in the future"


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
    # First run (no jobs file) seeds the default evolution schedules and
    # persists them, so the self-evolving engine works out of the box.
    monkeypatch.setattr("metano.cron_daemon.JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr("metano.cron_daemon.CRON_DIR", tmp_path)
    from metano.cron_daemon import load_jobs, DEFAULT_JOBS
    jobs = load_jobs()
    assert len(jobs) == len(DEFAULT_JOBS)
    assert jobs[0]["name"] == "harvest"
    # Seeded file persisted
    assert (tmp_path / "jobs.json").exists()


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


def test_save_jobs_concurrent_no_torn_writes(tmp_path, monkeypatch):
    """P1-3 acceptance: concurrent writers through save_jobs (the unified,
    atomic store path) never leave a torn jobs.json — every read parses to a
    complete, valid list."""
    monkeypatch.setattr("metano.cron_daemon.CRON_DIR", tmp_path)
    monkeypatch.setattr("metano.cron_daemon.JOBS_FILE", tmp_path / "jobs.json")
    from metano.cron_daemon import save_jobs, load_jobs
    import threading

    payloads = [[{"id": f"j{i}", "name": f"job{i}", "enabled": True}] for i in range(20)]
    errors = []

    def writer(p):
        for _ in range(40):
            try:
                save_jobs(p)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    def reader():
        for _ in range(200):
            try:
                got = load_jobs()
                assert isinstance(got, list)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    threads.append(threading.Thread(target=reader))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    # Final file is exactly one complete payload, never a torn mix.
    assert load_jobs() in payloads


def test_save_then_load(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.cron_daemon.CRON_DIR", tmp_path)
    monkeypatch.setattr("metano.cron_daemon.JOBS_FILE", tmp_path / "jobs.json")
    from metano.cron_daemon import save_jobs, load_jobs

    original = [{"id": "j1", "name": "test", "schedule": {"kind": "interval", "expr": "30"}}]
    save_jobs(original)
    loaded = load_jobs()
    assert len(loaded) == 1
    assert loaded[0]["name"] == "test"


def test_tick_no_double_trigger_after_late_finish(tmp_path, monkeypatch):
    """P0-3 regression: a 30-min interval job that runs for 3 minutes must
    execute exactly once per wall-clock slot. Under the old logic (next_run =
    last_run + interval) the late finish left next_run_at in the past, so the
    very next tick re-ran the same slot — observed as the 2-3-min-apart pairs
    in cron/output/harvest/."""
    from datetime import datetime, timezone
    from metano import cron_daemon

    monkeypatch.setattr(cron_daemon, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_daemon, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_daemon, "LOCK_FILE", tmp_path / "cron" / "tick.lock")
    monkeypatch.setattr(cron_daemon, "OUTPUT_DIR", tmp_path / "cron" / "output")

    clock = [datetime(2026, 8, 13, 17, 50, 0, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr("time.time", lambda: clock[0])

    runs = []

    def fake_run_job(job, timeout=None):
        runs.append(datetime.fromtimestamp(clock[0], tz=timezone.utc).strftime("%H:%M"))
        clock[0] += 180  # a 3-minute job finishes 180s after its slot start
        job["last_run_at"] = datetime.fromtimestamp(clock[0], tz=timezone.utc).isoformat()
        return {"status": "ok", "output": "ok", "error": None}

    monkeypatch.setattr(cron_daemon, "run_job", fake_run_job)

    job = {
        "id": "h1", "name": "harvest",
        "action": "evolution.harvest", "enabled": True,
        "schedule": {"kind": "interval", "expr": "30"},
        "next_run_at": datetime.fromtimestamp(clock[0], tz=timezone.utc).isoformat(),
        "last_run_at": datetime(2026, 8, 13, 17, 20, 0, tzinfo=timezone.utc).isoformat(),
    }
    cron_daemon.save_jobs([job])

    # Slot 17:50 due → runs once, claims next slot (18:00).
    cron_daemon.tick()
    assert runs == ["17:50"]
    # Next tick right after the late finish: same slot must NOT re-run.
    cron_daemon.tick()
    assert runs == ["17:50"]
    # Next slot 18:00 → runs again, exactly once.
    clock[0] = datetime(2026, 8, 13, 18, 0, 0, tzinfo=timezone.utc).timestamp()
    cron_daemon.tick()
    assert runs == ["17:50", "18:00"]
    # No immediate re-run of the 18:00 slot either.
    cron_daemon.tick()
    assert runs == ["17:50", "18:00"]


def test_no_bare_print_statements():
    """P2-6: the daemon must log through ``logger`` (stderr, line-buffered) so
    journald receives real timestamps.  A bare ``print`` to stdout is
    block-buffered when piped and produced the same-second log bunches with fake
    timestamps that the audit observed."""
    import inspect
    from metano import cron_daemon

    src = inspect.getsource(cron_daemon)
    for line in src.splitlines():
        stripped = line.lstrip()
        assert not stripped.startswith('print('), \
            f'bare print() in cron_daemon: {stripped!r}'
        assert not stripped.startswith('print '), \
            f'bare print statement in cron_daemon: {stripped!r}'


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


def test_no_prints_in_cron_daemon_source():
    """P2-6: cron_daemon must never use print().

    stdout is block-buffered when piped to journald, which produced the
    same-second log bunches (with fake timestamps) that made the daemon
    unobservable. Every diagnostic must go through the module logger (stderr,
    line-buffered under journald), which this grep-style assertion enforces.
    """
    src = Path(__file__).resolve().parents[1] / "metano" / "cron_daemon.py"
    text = src.read_text(encoding="utf-8")
    assert "print(" not in text


def test_run_job_logs_start_and_finish(tmp_path, monkeypatch):
    """P2-6: run_job emits a structured start + finish line (with timing)
    through the module logger, so the daemon is observable per-job in the
    journal."""
    from metano import cron_daemon

    monkeypatch.setattr(cron_daemon, "OUTPUT_DIR", tmp_path / "cron" / "output")
    logged: list[str] = []
    monkeypatch.setattr(
        cron_daemon.logger, "info",
        lambda msg, *args: logged.append(msg % args if args else str(msg)),
    )
    monkeypatch.setitem(cron_daemon.ACTIONS, "test.logged_action", lambda: {"ok": True})

    job = {
        "id": "j1", "name": "testjob", "action": "test.logged_action",
        "type": "claude", "enabled": True, "timeout": 30,
        "schedule": {"kind": "interval", "expr": "60"},
    }
    res = cron_daemon.run_job(job)
    assert res["status"] == "ok"
    assert any("Running cron job: testjob" in m for m in logged), logged
    assert any("Cron job testjob finished: status=ok" in m for m in logged), logged
