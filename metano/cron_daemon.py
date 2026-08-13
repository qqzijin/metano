"""Persistent cron daemon for metano.

Runs as a background process, checking jobs.json every 60 seconds,
and executing registered actions or `claude -p "<prompt>"` when a job is due.

Reliability hardening (H-06 / C-01):
- jobs.json is written atomically (temp file + ``os.replace``) under a
  process-wide write lock, so concurrent writers (daemon tick, Web CRUD, MCP
  CRUD) can never corrupt the canonical store. ``load_jobs`` / ``save_jobs``
  are the single code path for reading/writing ``cron/jobs.json``.
- The daemon claims a due job *before* executing it by advancing and
  persisting ``next_run_at``, so a crash / SIGKILL / restart in the middle of a
  run can never re-run the same slot. An in-process ``_ACTIVE_JOBS`` set guards
  against re-entrant same-name executions within one process.
- SIGTERM / SIGINT trigger a graceful shutdown: in-flight jobs finish, the
  pid file is removed, then the process exits.
"""
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from metano.log import logger
from .paths import CRON_DIR

JOBS_FILE = CRON_DIR / 'jobs.json'
LOCK_FILE = CRON_DIR / '.tick.lock'
PID_FILE = CRON_DIR / 'daemon.pid'
OUTPUT_DIR = CRON_DIR / 'output'

# Resource / safety limits for cron jobs (M-03).
MAX_JOBS = 200                 # max jobs in jobs.json
MAX_JOB_TIMEOUT = 3600         # hard wall-clock cap (seconds)
MAX_JOB_OUTPUT_BYTES = 50000   # output file size cap
MAX_CONCURRENT_JOBS = 4        # concurrent run_job() executions
_JOB_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')

# Action registry: maps action strings to Python functions
ACTIONS = {}

# Concurrency limiter shared by daemon tick and Web/MCP trigger endpoints.
_RUN_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_JOBS)

# In-process write lock for jobs.json (cross-process safety uses flock on a
# dedicated lock file, see ``save_jobs``). Serializes readers/writers so a
# read-modify-write cycle performed by one thread is not interleaved by another
# thread in the same process.
_JOBS_LOCK = threading.Lock()

# In-process "claim" set: job names currently being executed by run_job(). Used
# to refuse re-entrant same-name executions (H-06 idempotency).
_ACTIVE_JOBS: set = set()
_ACTIVE_JOBS_LOCK = threading.Lock()

# Default evolution schedules, auto-seeded on first run (when cron/jobs.json
# does not exist yet) so the self-evolving engine works out of the box.
# maintain runs at 4:15 (not 0 4) to avoid colliding with reflect at 0 4.
DEFAULT_JOBS = [
    {"name": "harvest", "action": "evolution.harvest", "schedule": {"kind": "interval", "expr": "30"}, "enabled": True, "timeout": 180},
    {"name": "introspect", "action": "evolution.introspect", "schedule": {"kind": "cron", "expr": "0 */2 * * *"}, "enabled": True, "timeout": 120},
    {"name": "maintenance", "action": "evolution.maintenance", "schedule": {"kind": "cron", "expr": "3 3 * * *"}, "enabled": True, "timeout": 900},
    # self-modify is DISABLED by default (H-05): it runs LLM-generated code
    # that is committed into the running instance, so it requires an explicit
    # operator switch (set enabled to true) and human approval.
    {"name": "self-modify", "action": "self_modify.daily", "schedule": {"kind": "cron", "expr": "30 4 * * *"}, "enabled": False, "timeout": 600},
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
        cron_evolution_maintenance,
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
    # Combined daily belief-lifecycle pass (maintain → reflect → adapt)
    register_action('evolution.maintenance', cron_evolution_maintenance)
    register_action('evolution.explore', cron_explore)
    register_action('evolution.architect', cron_architect)
    register_action('evolution.introspect', cron_introspect)
    register_action('evolution.evaluate', cron_evaluate)
    register_action('retention.purge_sessions', cron_purge_sessions)
    register_action('self_modify.daily', self_modify_daily)


def _jobs_lock_path() -> Path:
    """Path of the cross-process write-lock file (computed lazily so tests can
    monkeypatch ``CRON_DIR``)."""
    return CRON_DIR / '.jobs.lock'


def load_jobs() -> list[dict]:
    """Load jobs from the canonical store (``cron/jobs.json``).

    Shared by the daemon tick and the Web/MCP CRUD layer (F-01): jobs.json is
    the single source of truth. Returns a list of job dicts; on first run
    (no file yet) the default schedules are seeded and persisted.

    Reads are lock-free and safe because ``save_jobs`` only ever replaces the
    file atomically — a reader can never observe a partially-written file.
    """
    if JOBS_FILE.exists():
        data = json.loads(JOBS_FILE.read_text())
        jobs = data.get('jobs', []) if isinstance(data, dict) else data
        if not isinstance(jobs, list):
            raise ValueError('jobs.json must contain a JSON list (or {"jobs": [...]})')
        return [j for j in jobs if isinstance(j, dict)][:MAX_JOBS]
    # First run: seed the default evolution schedules so the self-evolving
    # engine is active immediately. Persist them for user editing.
    save_jobs(DEFAULT_JOBS)
    return list(DEFAULT_JOBS)

def save_jobs(jobs: list[dict]):
    """Persist jobs to the canonical store (``cron/jobs.json``).

    Atomic and serialized (H-06): the payload is written to a temp file in the
    same directory, fsynced, then ``os.replace``-d over the real file so a
    crash at any point leaves either the old complete file or the new complete
    file — never a torn write. An in-process mutex plus a cross-process flock
    serialize concurrent writers (daemon tick, Web CRUD, MCP CRUD).
    """
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(jobs[:MAX_JOBS], ensure_ascii=False, indent=2).encode('utf-8')
    with _JOBS_LOCK:
        lock_fd = open(_jobs_lock_path(), 'w')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            fd, tmp_path = tempfile.mkstemp(dir=str(CRON_DIR), prefix='.jobs.json.', suffix='.tmp')
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, JOBS_FILE)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                lock_fd.close()


def compute_next_run(schedule, last_run_at: str | None) -> str | None:
    """Compute next run time from schedule config.

    Supports:
    - {"kind": "cron", "expr": "0 */6 * * *"} (structured)
    - {"kind": "interval", "expr": "30"} (minutes)
    - "0 */6 * * *" (plain cron string)

    Interval schedules align to wall-clock slot boundaries (e.g. :00/:30 for a
    30-min interval) based on the current time, not ``last_run_at``. This keeps
    the schedule from drifting and guarantees ``next_run_at`` is always in the
    future — a job that finishes *after* its slot can never land the next run
    back in the past and re-trigger the same slot (P0-3 double-trigger
    regression).
    """
    if isinstance(schedule, str):
        schedule = {'kind': 'cron', 'expr': schedule}
    kind = schedule.get('kind', 'cron')
    expr = schedule.get('expr', '')
    if kind == 'interval':
        minutes = int(expr)
        interval = max(1, minutes) * 60
        now = time.time()
        next_time = (int(now) // interval) * interval + interval
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


def validate_job(job: dict) -> str | None:
    """Validate a job dict; return an error string, or None if valid.

    Enforces the M-03 safety constraints: strict job-name whitelist, bounded
    timeout, at least one of action/prompt, and a supported type. Jobs that
    fail validation are never executed.
    """
    name = job.get('name') or job.get('id') or ''
    if not isinstance(name, str) or not _JOB_NAME_RE.match(name):
        return f'Invalid job name: {name!r} (must match {_JOB_NAME_RE.pattern})'
    timeout = job.get('timeout', 120)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        return f'Invalid job timeout: {job.get("timeout")!r}'
    if timeout < 1 or timeout > MAX_JOB_TIMEOUT:
        return f'Job timeout out of range (1-{MAX_JOB_TIMEOUT}s): {timeout}'
    action = job.get('action', '')
    prompt = job.get('prompt', '')
    job_type = job.get('type', 'claude')
    if not action and not prompt:
        return f'Job {name!r} has neither an action nor a prompt'
    if job_type not in ('claude', 'shell'):
        return f'Job {name!r} has unsupported type: {job_type!r}'
    return None


def _truncate_output(text: str) -> str:
    """Cap job output to MAX_JOB_OUTPUT_BYTES with a truncation marker."""
    if len(text) > MAX_JOB_OUTPUT_BYTES:
        return text[:MAX_JOB_OUTPUT_BYTES] + f'\n... (output truncated, exceeded {MAX_JOB_OUTPUT_BYTES} bytes)'
    return text


def _run_shell_job(prompt: str, timeout: int) -> str:
    """Run a shell job through the bwrap-isolated executor (M-03).

    Arbitrary ``bash -c`` on the host is removed: shell jobs go through
    ``code_exec.code_run`` which runs inside a bubblewrap sandbox and FAILS
    CLOSED when no sandbox is available.
    """
    from .code_exec import code_run
    if len(prompt) > 2000:
        return 'Shell command too long (max 2000 chars)'
    if '\x00' in prompt:
        return 'Shell command contains null bytes'
    # No working_dir: the sandbox starts the snippet at its tmpfs HOME, so a
    # host path is never needed (and a METANO_HOME path would be hidden by the
    # tmpfs HOME / /tmp mounts).
    r = code_run(prompt, language='shell', timeout=timeout)
    if r.get('error'):
        return f'shell job failed: {r["error"]}'
    return r.get('stdout') or r.get('stderr') or '(no output)'


def _run_claude_job(prompt: str, timeout: int) -> str:
    """Run a ``claude -p`` prompt job in its own process group.

    The subprocess runs with ``start_new_session`` so a timeout kills the
    whole process tree; output is streamed and bounded (M-03).
    """
    from .code_exec import _run_popen
    env = os.environ.copy()
    env['HOME'] = str(Path.home())
    # Keep claude / node reachable regardless of how the daemon was started.
    path = env.get('PATH', '/usr/local/bin:/usr/bin:/bin')
    for tool in ('claude', 'node'):
        t = shutil.which(tool)
        if t:
            d = os.path.dirname(t)
            if d not in path.split(':'):
                path = f'{d}:{path}'
    env['PATH'] = path
    r = _run_popen(['claude', '-p', prompt], None, env, timeout, 'claude')
    if r.get('error'):
        return f'claude job failed: {r["error"]}'
    return r.get('stdout') or r.get('stderr') or '(no output)'


def _claim_job(job_name: str) -> bool:
    """Claim a job name for execution. Returns False if already active."""
    with _ACTIVE_JOBS_LOCK:
        if job_name in _ACTIVE_JOBS:
            return False
        _ACTIVE_JOBS.add(job_name)
        return True

def _release_job(job_name: str):
    """Release a previously claimed job name."""
    with _ACTIVE_JOBS_LOCK:
        _ACTIVE_JOBS.discard(job_name)


def run_job(job: dict, timeout: int | None = None) -> dict:
    """Execute a single cron job and return ``{status, output, error}``.

    This is the single execution path shared by the daemon tick and the
    Web/MCP trigger endpoints (F-01), so a triggered job behaves exactly like a
    scheduled one. The subprocess runs in its own process group and is killed
    as a whole tree on timeout; concurrency is capped (``MAX_CONCURRENT_JOBS``)
    and output size is bounded. ``job['last_run_at']`` / ``job['last_error']``
    are updated in place.

    An in-process claim guard (H-06) refuses to run the same job name
    concurrently, so a manual trigger while the daemon is running the job (or a
    re-entrant tick) can never double-execute.

    status: 'ok' | 'error' | 'timeout' | 'busy' | 'rejected'
    """
    job_name = job.get('name', job.get('id', 'unknown'))
    err = validate_job(job)
    if err:
        return {'status': 'rejected', 'output': '', 'error': err}
    effective_timeout = timeout if timeout is not None else job.get('timeout', 120)
    job_timeout = min(max(int(effective_timeout), 1), MAX_JOB_TIMEOUT)
    action = job.get('action', '')
    prompt = job.get('prompt', '')
    job_type = job.get('type', 'claude')

    # Claim guard: never run the same job concurrently (H-06 idempotency).
    if not _claim_job(job_name):
        return {'status': 'busy', 'output': '', 'error': f'job already running: {job_name}'}

    result: dict = {'status': 'ok', 'output': '(no output)', 'error': None}
    try:
        print(f"Running cron job: {job_name} [action={action}]")

        if not _RUN_SEMAPHORE.acquire(blocking=False):
            result = {'status': 'busy', 'output': '', 'error': 'too many concurrent cron jobs'}
        else:
            try:
                if action and action in ACTIONS:
                    r = ACTIONS[action]()
                    output = json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else str(r or '(no output)')
                elif job_type == 'shell':
                    output = _run_shell_job(prompt, job_timeout)
                elif prompt:
                    output = _run_claude_job(prompt, job_timeout)
                else:
                    result = {'status': 'error', 'output': '', 'error': f'No action/prompt configured for job {job_name}'}
                    output = ''
                if result['status'] == 'ok':
                    result['output'] = output
            except subprocess.TimeoutExpired:
                result = {'status': 'timeout', 'output': '', 'error': f'Timeout after {job_timeout}s'}
            except Exception:
                logger.exception("cron job %s failed", job_name)
                result = {'status': 'error', 'output': '', 'error': 'execution error'}
            finally:
                _RUN_SEMAPHORE.release()

        # Write output file (defense-in-depth: name whitelist + containment).
        try:
            if not _JOB_NAME_RE.match(job_name):
                raise ValueError(f'Invalid job name: {job_name!r}')
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            out_dir = (OUTPUT_DIR / job_name).resolve()
            if not out_dir.is_relative_to(OUTPUT_DIR.resolve()):
                raise ValueError(f'Job name escapes output dir: {job_name!r}')
            out_dir.mkdir(parents=True, exist_ok=True)
            ts_str = datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')
            out_text = _truncate_output(str(result.get('output') or ''))
            (out_dir / f'{ts_str}.md').write_text(out_text)
        except Exception as e:
            logger.warning('cron: could not write job output for %s: %s', job_name, e)

        job['last_run_at'] = datetime.now(tz=timezone.utc).isoformat()
        job['last_error'] = result.get('error')
        return result
    finally:
        _release_job(job_name)


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
        due: list[dict] = []
        patched: list[dict] = []   # jobs whose schedule metadata changed, not running
        for job in jobs:
            if not job.get('enabled', True):
                continue
            next_run = job.get('next_run_at')
            if not next_run:
                new_nr = compute_next_run(job.get('schedule', {}), job.get('last_run_at'))
                if new_nr != job.get('next_run_at'):
                    job['next_run_at'] = new_nr
                    patched.append(job)
                continue
            try:
                next_ts = datetime.fromisoformat(next_run.replace('Z', '+00:00')).timestamp()
            except (ValueError, AttributeError):
                next_ts = 0
            if next_ts <= now:
                # Validate before executing; an invalid job is skipped with the
                # error recorded, never run (M-03).
                validate_err = validate_job(job)
                if validate_err:
                    logger.warning('cron: skipping invalid job: %s', validate_err)
                    job['last_error'] = validate_err
                    new_nr = compute_next_run(job.get('schedule', {}), job.get('last_run_at'))
                    if new_nr != job.get('next_run_at'):
                        job['next_run_at'] = new_nr
                    patched.append(job)
                    continue
                due.append(job)
        if due:
            # Claim before execution (H-06 idempotency): advance next_run_at and
            # persist it atomically *before* running. If the process is killed
            # mid-run (or restarts), the slot is already claimed, so the job can
            # never re-run the same schedule slot.
            for job in due:
                nxt = compute_next_run(job.get('schedule', {}), job.get('last_run_at'))
                if nxt:
                    if nxt != job.get('next_run_at'):
                        job['next_run_at'] = nxt
                else:
                    # Unparseable schedule: surface and disable rather than spin.
                    job['last_error'] = 'could not compute next run; job disabled'
                    job['enabled'] = False
            # Persist the claims, merged onto the latest store so concurrent
            # Web/MCP CRUD during execution is preserved (C-01 single source).
            _merge_job_updates(due, ('next_run_at', 'enabled', 'last_error'))
            for job in due:
                if job.get('enabled') is False and str(job.get('last_error', '')).startswith('could not compute'):
                    continue
                run_job(job)         # updates last_run_at / last_error in place
            # Persist run results, again merged onto the latest store.
            _merge_job_updates(due, ('last_run_at', 'last_error'))
        elif patched:
            _merge_job_updates(patched, ('next_run_at', 'last_error'))
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def run_daemon():
    """Run the cron daemon as a foreground process.

    On SIGTERM / SIGINT the daemon stops scheduling new work and lets any
    in-flight job finish (grace period), then removes the pid file and exits —
    so a restart never finds an un-claimed due job that was interrupted
    mid-execution (H-06).
    """
    os.umask(0o077)
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    print(f'Cron daemon started (PID {os.getpid()})')

    stop = threading.Event()

    def handle_signal(signum, frame):
        print(f'Received signal {signum}, shutting down gracefully')
        stop.set()
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    def handle_hup(signum, frame):
        print('Received SIGHUP, will reload jobs on next tick')
    signal.signal(signal.SIGHUP, handle_hup)

    while not stop.is_set():
        tick()
        if stop.is_set():
            break
        # Interruptible sleep: a signal during the wait returns immediately.
        stop.wait(timeout=60)

    # Graceful exit: clean up the pid file.
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    print('Cron daemon stopped')

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'start':
        run_daemon()
    else:
        tick()
        print('Single tick completed')


# ── CRUD primitives (shared by the Web / MCP cron layers) ────────────────────
#
# These are the public mutation entry points for the cron store. The Web and MCP
# servers call these instead of writing jobs.json (or evo.db) directly, so the
# canonical store stays single-source (C-01) and every write is atomic+locked.

def _merge_job_updates(updates: list[dict], fields: tuple[str, ...]) -> None:
    """Merge per-job field updates onto the latest store and save.

    Each ``updates`` entry is matched to the store by id (fallback: name) and
    the listed ``fields`` are copied onto it. The save is atomic+locked and
    happens once at the end, so concurrent Web/MCP CRUD performed while the
    daemon is running a job is preserved instead of being clobbered by the
    daemon's own save (C-01 single source of truth).
    """
    if not updates:
        return
    fresh = load_jobs()
    for u in updates:
        target = _resolve_job(fresh, u.get('id') or u.get('name') or '')
        if target is None:
            continue
        for f in fields:
            if f in u:
                target[f] = u[f]
    save_jobs(fresh)


def _resolve_job(jobs: list[dict], job_id: str) -> dict | None:
    """Find a job by id; fall back to name only when no job carries that id.

    The id-first rule prevents a job whose *name* happens to equal another
    job's *id* from being addressed ambiguously.
    """
    ids = {j.get('id') for j in jobs if j.get('id')}
    for j in jobs:
        if j.get('id') == job_id:
            return j
    if job_id not in ids:
        for j in jobs:
            if j.get('name') == job_id:
                return j
    return None


def add_cron_job(job: dict) -> dict:
    """Append a new cron job to the canonical store.

    Assigns an ``id`` if missing, normalizes a plain-string ``schedule`` into
    ``{'kind': 'cron', 'expr': ...}``, fills defaults, validates (``validate_job``)
    and persists. Raises ``ValueError`` if the job is invalid (e.g. bad name,
    bad type, no action/prompt). Returns the stored job dict.
    """
    import uuid
    new = dict(job)
    new.setdefault('id', uuid.uuid4().hex[:12])
    if 'schedule' in new and isinstance(new['schedule'], str):
        new['schedule'] = {'kind': 'cron', 'expr': new['schedule']}
    new.setdefault('schedule', {'kind': 'cron', 'expr': '0 0 * * *'})
    new.setdefault('prompt', new.get('action', ''))
    new.setdefault('type', 'claude')
    new.setdefault('enabled', True)
    new.setdefault('last_run_at', None)
    new.setdefault('next_run_at', None)
    new.setdefault('last_error', None)
    err = validate_job(new)
    if err:
        raise ValueError(err)
    jobs = load_jobs()
    jobs.append(new)
    save_jobs(jobs)
    return new


def delete_cron_job(job_id: str) -> bool:
    """Delete a cron job by id (fallback: name). Returns True if removed.

    Shared by the Web ``DELETE /api/cron/jobs/{job_id}`` and MCP
    ``cron_remove`` handlers.
    """
    jobs = load_jobs()
    target = _resolve_job(jobs, job_id)
    if target is None:
        return False
    kept = [j for j in jobs if j is not target]
    save_jobs(kept)
    return True


def update_cron_job(job_id: str, **fields) -> dict | None:
    """Update a cron job by id (fallback: name). Returns the updated job, or
    None if not found.

    Editable fields: ``name``, ``prompt``, ``action``, ``type``, ``enabled``,
    ``timeout``, ``schedule``. When ``schedule`` is updated, ``next_run_at`` is
    recomputed immediately from the new schedule (and the last run) so the
    change takes effect without waiting for the daemon's next tick.
    """
    jobs = load_jobs()
    target = _resolve_job(jobs, job_id)
    if target is None:
        return None
    allowed = {'name', 'prompt', 'action', 'type', 'enabled', 'timeout', 'schedule'}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == 'schedule' and isinstance(v, str):
            v = {'kind': 'cron', 'expr': v}
        target[k] = v
    if 'schedule' in fields and fields.get('schedule') is not None:
        target['next_run_at'] = compute_next_run(target.get('schedule', {}), target.get('last_run_at'))
    save_jobs(jobs)
    return dict(target)
