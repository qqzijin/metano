"""Persistent cron daemon for metano.

Runs as a background process, checking jobs.json every 60 seconds,
and executing registered actions or `claude -p "<prompt>"` when a job is due.
"""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from metano.log import logger
from .paths import CRON_DIR

JOBS_FILE = CRON_DIR / 'jobs.json'
LOCK_FILE = CRON_DIR / '.tick.lock'
PID_FILE = CRON_DIR / 'daemon.pid'
OUTPUT_DIR = CRON_DIR / 'output'

# Action registry: maps action strings to Python functions
ACTIONS = {}

# Default evolution schedules, auto-seeded on first run (when cron/jobs.json
# does not exist yet) so the self-evolving engine works out of the box.
# maintain runs at 4:15 (not 0 4) to avoid colliding with reflect at 0 4.
DEFAULT_JOBS = [
    {"name": "harvest", "action": "evolution.harvest", "schedule": {"kind": "interval", "expr": "30"}, "enabled": True, "timeout": 180},
    {"name": "introspect", "action": "evolution.introspect", "schedule": {"kind": "cron", "expr": "0 */2 * * *"}, "enabled": True, "timeout": 120},
    {"name": "adapt", "action": "evolution.adapt", "schedule": {"kind": "cron", "expr": "0 3 * * *"}, "enabled": True, "timeout": 180},
    {"name": "reflect", "action": "evolution.reflect", "schedule": {"kind": "cron", "expr": "0 4 * * *"}, "enabled": True, "timeout": 180},
    {"name": "maintain", "action": "evolution.maintain", "schedule": {"kind": "cron", "expr": "15 4 * * *"}, "enabled": True, "timeout": 300},
    {"name": "self-modify", "action": "self_modify.daily", "schedule": {"kind": "cron", "expr": "30 4 * * *"}, "enabled": True, "timeout": 600},
    {"name": "knowledge-sink", "action": "knowledge.sink", "schedule": {"kind": "cron", "expr": "30 5 * * *"}, "enabled": True, "timeout": 120},
    {"name": "architect", "action": "evolution.architect", "schedule": {"kind": "cron", "expr": "0 5 * * 0"}, "enabled": True, "timeout": 180},
    {"name": "explore", "action": "evolution.explore", "schedule": {"kind": "cron", "expr": "0 3 * * 0"}, "enabled": True, "timeout": 300},
    {"name": "evaluate", "action": "evolution.evaluate", "schedule": {"kind": "interval", "expr": "360"}, "enabled": True, "timeout": 120},
    {"name": "session-retention", "action": "retention.purge_sessions", "schedule": {"kind": "cron", "expr": "0 6 * * 0"}, "enabled": True, "timeout": 600},
]

def register_action(name: str, fn):
    """Register a named action function for cron execution."""
    ACTIONS[name] = fn

def _register_default_actions():
    """Register all evolution system cron actions."""
    from .evolution import (
        cron_harvest, cron_reflect, cron_adapt, cron_maintain,
        cron_explore, cron_architect, cron_introspect, cron_evaluate,
    )
    from .db import cron_purge_sessions
    from .self_modify import self_modify_daily
    from .knowledge_explorer import sink_evolution_knowledge
    register_action('knowledge.sink', sink_evolution_knowledge)
    register_action('evolution.harvest', cron_harvest)
    register_action('evolution.reflect', cron_reflect)
    register_action('evolution.adapt', cron_adapt)
    register_action('evolution.maintain', cron_maintain)
    register_action('evolution.explore', cron_explore)
    register_action('evolution.architect', cron_architect)
    register_action('evolution.introspect', cron_introspect)
    register_action('evolution.evaluate', cron_evaluate)
    register_action('retention.purge_sessions', cron_purge_sessions)
    register_action('self_modify.daily', self_modify_daily)

def load_jobs() -> list[dict]:
    if JOBS_FILE.exists():
        data = json.loads(JOBS_FILE.read_text())
        if isinstance(data, dict):
            return data.get('jobs', [])
        return data
    # First run: seed the default evolution schedules so the self-evolving
    # engine is active immediately. Persist them for user editing.
    save_jobs(DEFAULT_JOBS)
    return list(DEFAULT_JOBS)

def save_jobs(jobs: list[dict]):
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))

def compute_next_run(schedule, last_run_at: str | None) -> str | None:
    """Compute next run time from schedule config.

    Supports:
    - {"kind": "cron", "expr": "0 */6 * * *"} (structured)
    - {"kind": "interval", "expr": "30"} (minutes)
    - "0 */6 * * *" (plain cron string)
    """
    if isinstance(schedule, str):
        schedule = {'kind': 'cron', 'expr': schedule}
    kind = schedule.get('kind', 'cron')
    expr = schedule.get('expr', '')
    if kind == 'interval':
        minutes = int(expr)
        if last_run_at:
            last = datetime.fromisoformat(last_run_at.replace('Z', '+00:00'))
            next_time = last.timestamp() + minutes * 60
        else:
            next_time = time.time() + minutes * 60
        return datetime.fromtimestamp(next_time, tz=timezone.utc).isoformat()
    if kind == 'cron':
        try:
            from croniter import croniter
            now = datetime.now(tz=timezone.utc)
            cron = croniter(expr, now)
            return cron.get_next(datetime).isoformat()
        except ImportError:
            return compute_next_run({'kind': 'interval', 'expr': '60'}, last_run_at)
    return None

def tick():
    """One tick of the cron daemon: check all jobs and run any that are due."""
    _register_default_actions()
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        logger.debug("cron tick skipped: lock held by another process")
        return
    try:
        jobs = load_jobs()
        now = time.time()
        for job in jobs:
            if not job.get('enabled', True):
                continue
            next_run = job.get('next_run_at')
            if not next_run:
                job['next_run_at'] = compute_next_run(job.get('schedule', {}), job.get('last_run_at'))
                continue
            try:
                next_ts = datetime.fromisoformat(next_run.replace('Z', '+00:00')).timestamp()
            except (ValueError, AttributeError):
                next_ts = 0
            if next_ts <= now:
                job_name = job.get('name', job.get('id', 'unknown'))
                action = job.get('action', '')
                prompt = job.get('prompt', '')
                job_type = job.get('type', 'claude')
                print(f"Running cron job: {job_name} [action={action}]")
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                out_dir = OUTPUT_DIR / job_name
                out_dir.mkdir(parents=True, exist_ok=True)
                try:
                    import threading
                    job_timeout = job.get('timeout', 120)
                    result_holder = [None, None]  # [result, error]
                    result_text = '(no output)'

                    def _run():
                        try:
                            if action and action in ACTIONS:
                                result_holder[0] = ACTIONS[action]()
                            elif job_type == 'shell':
                                from .code_exec import _check_shell_dangerous
                                if len(prompt) > 2000:
                                    result_holder[0] = 'Shell command too long (max 2000 chars)'
                                elif '\x00' in prompt:
                                    result_holder[0] = 'Shell command contains null bytes'
                                else:
                                    danger = _check_shell_dangerous(prompt)
                                    if danger:
                                        result_holder[0] = danger
                                    else:
                                        sp = subprocess.run(['bash', '-c', prompt], capture_output=True, text=True, timeout=job_timeout, cwd=str(CRON_DIR.parent), env={'PATH': '/usr/local/bin:/usr/bin:/bin:/home/dk/local/node/bin', 'HOME': str(Path.home())})
                                        result_holder[0] = sp.stdout or sp.stderr or '(no output)'
                            elif prompt:
                                sp = subprocess.run(['claude', '-p', prompt], capture_output=True, text=True, timeout=job_timeout)
                                result_holder[0] = sp.stdout or sp.stderr or '(no output)'
                            else:
                                result_holder[0] = f'No action/prompt configured for job {job_name}'
                        except Exception as e:
                            result_holder[1] = e

                    t = threading.Thread(target=_run, daemon=True)
                    t.start()
                    t.join(timeout=job_timeout)
                    if t.is_alive():
                        result_text = f'Timeout after {job_timeout}s'
                        job['last_error'] = result_text
                    elif result_holder[1]:
                        raise result_holder[1]
                    else:
                        r = result_holder[0]
                        result_text = json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else str(r or '(no output)')
                    ts_str = datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')
                    out_file = out_dir / f'{ts_str}.md'
                    out_file.write_text(result_text)
                    job['last_run_at'] = datetime.now(tz=timezone.utc).isoformat()
                    if not job.get('last_error'):
                        job['last_error'] = None
                except subprocess.TimeoutExpired:
                    job['last_error'] = f'Timeout after {job_timeout}s'
                except Exception:
                    logger.exception("cron job %s failed", job_name)
                    job['last_error'] = 'execution error'
                job['next_run_at'] = compute_next_run(job.get('schedule', {}), job.get('last_run_at'))
        save_jobs(jobs)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

def run_daemon():
    """Run the cron daemon as a foreground process."""
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    print(f'Cron daemon started (PID {os.getpid()})')

    def handle_signal(signum, frame):
        print(f'Received signal {signum}, shutting down')
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    def handle_hup(signum, frame):
        print('Received SIGHUP, will reload jobs on next tick')
    signal.signal(signal.SIGHUP, handle_hup)
    while True:
        tick()
        time.sleep(60)
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'start':
        run_daemon()
    else:
        tick()
        print('Single tick completed')