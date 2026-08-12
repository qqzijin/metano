"""Sub-agent delegation: spawn parallel Claude Code tasks.

Concurrency & process-tree safety
---------------------------------
Spawned ``claude -p`` sub-processes share a global concurrency budget
(``AgentDelegator._max_concurrent``, default 3) enforced with a
``threading.BoundedSemaphore``, so an unbounded number of sub-agents can never
be forked (DoS / fork-bomb).  ``spawn`` (sync) and ``spawn_async`` (async) both
acquire a slot before launching; callers can also pre-acquire a slot via
``acquire()`` / ``acquire_async()`` + ``release()`` (the A2A server does this so
it can reject a request with a clean "concurrency limit reached" error instead
of silently queuing).  A slot is released when the sub-agent finishes, times
out, errors, or is cancelled.

Every sub-agent is started in its own process group (``start_new_session=True``)
so cancelling or timing-out can SIGKILL the *whole* tree with
``os.killpg(pid, SIGKILL)`` rather than leaving orphaned grandchildren behind.
Each task records the sub-process pid (``AgentTask.pid``) so ``cancel_task`` can
look it up.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from metano.log import logger
from .paths import AGENT_DIR


class ConcurrencyLimitError(Exception):
    """Raised when the sub-agent concurrency limit is already reached."""


@dataclass
class AgentTask:
    id: str
    task: str
    model: str = ''
    status: str = 'pending'
    result: str = ''
    error: str = ''
    started_at: float = 0.0
    completed_at: float = 0.0
    tokens_used: int = 0
    pid: int = 0   # OS pid of the claude sub-process (0 = not spawned yet)


class AgentDelegator:

    def __init__(self):
        AGENT_DIR.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, AgentTask] = {}
        self._max_concurrent = 3
        self._default_timeout = 120
        self._acquire_timeout = 5.0          # seconds to wait for a free slot
        self._sem = threading.BoundedSemaphore(self._max_concurrent)
        self._active = 0
        self._count_lock = threading.Lock()

    # ── Concurrency gate (one shared budget across sync + async paths) ──────
    def try_acquire(self) -> bool:
        """Non-blocking: acquire one slot immediately.  Returns True when the
        caller must later call :meth:`release`."""
        if self._sem.acquire(blocking=False):
            self._bump_active(1)
            return True
        return False

    def acquire(self, timeout: float | None = None) -> bool:
        """Block until a slot is free (default timeout ``_acquire_timeout``)."""
        t = self._acquire_timeout if timeout is None else timeout
        if self._sem.acquire(timeout=t):
            self._bump_active(1)
            return True
        return False

    async def acquire_async(self, timeout: float | None = None) -> bool:
        """Async wait for a slot.  Cancellation-safe (polls ``try_acquire``
        with small sleeps instead of blocking a worker thread)."""
        deadline = time.monotonic() + (self._acquire_timeout if timeout is None else timeout)
        while True:
            if self.try_acquire():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(0.05)

    def release(self) -> None:
        """Free one acquired slot (call exactly once per successful acquire).

        Defensive: over-releasing is silently ignored so a double-release in a
        cancellation race can never corrupt the semaphore counter.
        """
        with self._count_lock:
            if self._active <= 0:
                return
            self._active -= 1
        try:
            self._sem.release()
        except ValueError:
            pass

    def active_count(self) -> int:
        """Number of sub-agent processes currently running (0.._max_concurrent)."""
        with self._count_lock:
            return self._active

    def _bump_active(self, delta: int):
        with self._count_lock:
            self._active += delta

    # ── Process-group kill helpers ──────────────────────────────────────────
    @staticmethod
    def _kill_pgid(pgid: int) -> None:
        """SIGKILL an entire process group (children of a start_new_session)."""
        if not pgid:
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    @staticmethod
    def _kill_process(proc) -> None:
        """SIGKILL the process group of an asyncio sub-process (if running)."""
        if proc is None or proc.returncode is not None:
            return
        AgentDelegator._kill_pgid(proc.pid)

    def cancel_task(self, task_id: str) -> bool:
        """Best-effort SIGKILL of a running sub-agent's process group.

        Returns True when a pid was known and the group was signaled.  The
        caller (A2A tasks/cancel) additionally cancels the background
        asyncio.Task, whose ``finally``/cancellation handler also kills the
        tree — so a race where the sub-process is spawned just after this
        lookup is still covered.
        """
        task = self._tasks.get(task_id)
        if task is None:
            task = self._load_task(task_id)
        pid = task.pid if task is not None else 0
        if not pid:
            return False
        try:
            os.killpg(pid, signal.SIGKILL)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    # ── spawn (sync) ────────────────────────────────────────────────────────
    def spawn(self, task: str, model: str='', timeout: int=120) -> dict:
        """Spawn a sub-agent to handle a task (blocks the calling thread).

        When the concurrency limit is reached the call waits up to
        ``_acquire_timeout`` seconds, then returns a ``status='failed'`` task
        whose ``error`` explains the limit instead of forking another process.
        """
        if not self.acquire():
            task_id = uuid.uuid4().hex[:12]
            agent_task = AgentTask(
                id=task_id, task=task, model=model, status='failed',
                error=(f'Concurrency limit reached ({self._max_concurrent} '
                       f'concurrent sub-agents); retry later'),
                started_at=time.time(), completed_at=time.time())
            self._tasks[task_id] = agent_task
            self._save_task(agent_task)
            return {'id': task_id, 'status': 'failed', 'result': '',
                    'error': agent_task.error, 'duration_seconds': 0}
        import shutil
        claude_bin = shutil.which('claude') or '/home/dk/local/node/bin/claude'
        task_id = uuid.uuid4().hex[:12]
        agent_task = AgentTask(id=task_id, task=task, model=model, status='running',
                               started_at=time.time())
        self._tasks[task_id] = agent_task
        cmd = [claude_bin, '-p', task]
        if model:
            cmd = [claude_bin, '-p', task, '--model', model]
        try:
            # start_new_session=True -> own process group so a timeout can
            # SIGKILL the whole tree (not just the direct child).
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=timeout or self._default_timeout,
                                    start_new_session=True)
            agent_task.result = result.stdout.strip()
            agent_task.status = 'completed'
            agent_task.completed_at = time.time()
            if not agent_task.result and result.stderr:
                agent_task.error = result.stderr[:500]
                agent_task.status = 'failed'
        except subprocess.TimeoutExpired as e:
            proc = getattr(e, 'process', None)
            self._kill_pgid(proc.pid if proc is not None else 0)
            agent_task.status = 'timeout'
            agent_task.error = f'Timed out after {timeout}s'
            agent_task.completed_at = time.time()
        except Exception as e:
            logger.exception()
            agent_task.status = 'failed'
            agent_task.error = str(e)
            agent_task.completed_at = time.time()
        finally:
            self.release()
        self._save_task(agent_task)
        return {'id': task_id, 'status': agent_task.status,
                'result': agent_task.result[:2000] if agent_task.result else '',
                'error': agent_task.error,
                'duration_seconds': agent_task.completed_at - agent_task.started_at
                if agent_task.completed_at else 0}

    # ── spawn_async ─────────────────────────────────────────────────────────
    async def spawn_async(self, task: str, model: str='', timeout: int=120) -> dict:
        """Spawn a sub-agent asynchronously.

        Acquires a concurrency slot first; raises
        :class:`ConcurrencyLimitError` if the limit is still full after
        ``_acquire_timeout`` seconds.
        """
        if not await self.acquire_async():
            raise ConcurrencyLimitError(
                f'Concurrency limit reached ({self._max_concurrent} concurrent '
                f'sub-agents); retry later')
        return await self._spawn_async_impl(task, model, timeout)

    async def _spawn_async_impl(self, task: str, model: str='', timeout: int=120) -> dict:
        """Run one claude sub-process and wait for its result.

        The caller must already hold a concurrency slot (via ``acquire_async``
        or ``spawn_async``); this method releases it when the sub-process
        finishes, times out, errors, or is cancelled.  On cancellation /
        timeout it SIGKILLs the whole process group so no grandchildren are
        orphaned.  A task is registered in ``self._tasks`` synchronously at
        the start so an A2A caller can discover its id immediately.
        """
        import shutil
        claude_bin = shutil.which('claude') or '/home/dk/local/node/bin/claude'
        task_id = uuid.uuid4().hex[:12]
        agent_task = AgentTask(id=task_id, task=task, model=model, status='running',
                               started_at=time.time())
        self._tasks[task_id] = agent_task
        cmd = [claude_bin, '-p', task]
        if model:
            cmd = [claude_bin, '-p', task, '--model', model]
        proc = None
        try:
            # start_new_session=True -> own process group so cancel/timeout can
            # SIGKILL the whole tree with os.killpg.
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            agent_task.pid = proc.pid
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout or self._default_timeout)
            agent_task.result = stdout.decode().strip()
            agent_task.status = 'completed'
            agent_task.completed_at = time.time()
            if not agent_task.result and stderr:
                agent_task.error = stderr.decode()[:500]
                agent_task.status = 'failed'
        except asyncio.TimeoutError:
            self._kill_process(proc)
            if proc is not None:
                try:
                    await proc.wait()
                except Exception:
                    pass
            agent_task.status = 'timeout'
            agent_task.error = f'Timed out after {timeout}s'
            agent_task.completed_at = time.time()
        except asyncio.CancelledError:
            # A2A tasks/cancel path: SIGKILL the process group, reap, re-raise
            # (the task record is left in the 'canceled' state the caller set).
            self._kill_process(proc)
            if proc is not None:
                try:
                    await proc.wait()
                except Exception:
                    pass
            raise
        except Exception as e:
            logger.exception()
            agent_task.status = 'failed'
            agent_task.error = str(e)
            agent_task.completed_at = time.time()
        finally:
            self.release()
        self._save_task(agent_task)
        return {'id': task_id, 'status': agent_task.status,
                'result': agent_task.result[:2000] if agent_task.result else '',
                'error': agent_task.error,
                'duration_seconds': agent_task.completed_at - agent_task.started_at
                if agent_task.completed_at else 0}

    def status(self, task_id: str) -> dict:
        """Check the status of a sub-agent task."""
        task = self._tasks.get(task_id)
        if not task:
            task = self._load_task(task_id)
            if not task:
                return {'error': f'Task {task_id} not found'}
        return {'id': task.id, 'task': task.task[:100], 'status': task.status, 'result': task.result[:2000] if task.result else '', 'error': task.error, 'started_at': task.started_at, 'completed_at': task.completed_at}

    def result(self, task_id: str) -> dict:
        """Get the full result of a completed sub-agent task."""
        task = self._tasks.get(task_id) or self._load_task(task_id)
        if not task:
            return {'error': f'Task {task_id} not found'}
        return {'id': task.id, 'task': task.task, 'status': task.status, 'result': task.result, 'error': task.error, 'duration_seconds': task.completed_at - task.started_at if task.completed_at else 0}

    def list_tasks(self) -> list[dict]:
        """List all sub-agent tasks."""
        return [{'id': t.id, 'task': t.task[:80], 'status': t.status, 'started_at': t.started_at, 'completed_at': t.completed_at} for t in self._tasks.values()]


    def _save_task(self, task: AgentTask):
        path = AGENT_DIR / f'{task.id}.json'
        path.write_text(json.dumps({'id': task.id, 'task': task.task, 'model': task.model, 'status': task.status, 'result': task.result, 'error': task.error, 'started_at': task.started_at, 'completed_at': task.completed_at, 'pid': task.pid}, ensure_ascii=False))

    def _load_task(self, task_id: str) -> AgentTask | None:
        path = AGENT_DIR / f'{task_id}.json'
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        task = AgentTask(id=data['id'], task=data['task'], model=data.get('model', ''), status=data['status'], result=data.get('result', ''), error=data.get('error', ''), started_at=data.get('started_at', 0), completed_at=data.get('completed_at', 0), pid=data.get('pid', 0))
        self._tasks[task_id] = task
        return task


delegator = AgentDelegator()
