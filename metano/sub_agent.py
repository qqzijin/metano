"""Sub-agent delegation: spawn parallel Claude Code tasks."""
import asyncio
import json
import subprocess
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from metano.log import logger
AGENT_DIR = Path.home() / '.claude' / 'metano' / 'agents'

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

class AgentDelegator:

    def __init__(self):
        AGENT_DIR.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, AgentTask] = {}
        self._max_concurrent = 3
        self._default_timeout = 120

    def spawn(self, task: str, model: str='', timeout: int=120) -> dict:
        """Spawn a sub-agent to handle a task."""
        import shutil
        claude_bin = shutil.which('claude') or '/usr/local/bin/claude'
        task_id = uuid.uuid4().hex[:12]
        agent_task = AgentTask(id=task_id, task=task, model=model, status='running', started_at=time.time())
        self._tasks[task_id] = agent_task
        cmd = [claude_bin, '-p', task]
        if model:
            cmd = [claude_bin, '-p', task, '--model', model]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout or self._default_timeout)
            agent_task.result = result.stdout.strip()
            agent_task.status = 'completed'
            agent_task.completed_at = time.time()
            if not agent_task.result and result.stderr:
                agent_task.error = result.stderr[:500]
                agent_task.status = 'failed'
        except subprocess.TimeoutExpired:
            agent_task.status = 'timeout'
            agent_task.error = f'Timed out after {timeout}s'
            agent_task.completed_at = time.time()
        except Exception as e:
            logger.exception()
            agent_task.status = 'failed'
            agent_task.error = str(e)
            agent_task.completed_at = time.time()
        self._save_task(agent_task)
        return {'id': task_id, 'status': agent_task.status, 'result': agent_task.result[:2000] if agent_task.result else '', 'error': agent_task.error, 'duration_seconds': agent_task.completed_at - agent_task.started_at if agent_task.completed_at else 0}

    async def spawn_async(self, task: str, model: str='', timeout: int=120) -> dict:
        """Spawn a sub-agent asynchronously."""
        import shutil
        claude_bin = shutil.which('claude') or '/usr/local/bin/claude'
        task_id = uuid.uuid4().hex[:12]
        agent_task = AgentTask(id=task_id, task=task, model=model, status='running', started_at=time.time())
        self._tasks[task_id] = agent_task
        cmd = [claude_bin, '-p', task]
        if model:
            cmd = [claude_bin, '-p', task, '--model', model]
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout or self._default_timeout)
            agent_task.result = stdout.decode().strip()
            agent_task.status = 'completed'
            agent_task.completed_at = time.time()
            if not agent_task.result and stderr:
                agent_task.error = stderr.decode()[:500]
                agent_task.status = 'failed'
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            agent_task.status = 'timeout'
            agent_task.error = f'Timed out after {timeout}s'
            agent_task.completed_at = time.time()
        except Exception as e:
            logger.exception()
            agent_task.status = 'failed'
            agent_task.error = str(e)
            agent_task.completed_at = time.time()
        self._save_task(agent_task)
        return {'id': task_id, 'status': agent_task.status, 'result': agent_task.result[:2000] if agent_task.result else '', 'error': agent_task.error, 'duration_seconds': agent_task.completed_at - agent_task.started_at if agent_task.completed_at else 0}

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

    def clean_old_tasks(self, max_hours: int = 24) -> dict:
        """Remove persisted task files older than max_hours."""
        import os, time
        cutoff = time.time() - max_hours * 3600
        removed = 0
        for f in AGENT_DIR.glob('*.json'):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
                # Also remove from in-memory dict
                tid = f.stem
                self._tasks.pop(tid, None)
        return {'removed': removed}

    def _save_task(self, task: AgentTask):
        path = AGENT_DIR / f'{task.id}.json'
        path.write_text(json.dumps({'id': task.id, 'task': task.task, 'model': task.model, 'status': task.status, 'result': task.result, 'error': task.error, 'started_at': task.started_at, 'completed_at': task.completed_at}, ensure_ascii=False))

    def _load_task(self, task_id: str) -> AgentTask | None:
        path = AGENT_DIR / f'{task_id}.json'
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        task = AgentTask(id=data['id'], task=data['task'], model=data.get('model', ''), status=data['status'], result=data.get('result', ''), error=data.get('error', ''), started_at=data.get('started_at', 0), completed_at=data.get('completed_at', 0))
        self._tasks[task_id] = task
        return task
delegator = AgentDelegator()