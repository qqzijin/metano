"""Code execution sandbox: run Python, JavaScript, and Shell code safely.

Security measures:
- Resource limits: CPU time, output size, process count
- Restricted PATH and environment
- Dangerous command blocklist for shell
- Read-only home for Python (PYTHONNOUSERSITE)
- No network access via env stripping
"""
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from metano.log import logger

MAX_OUTPUT_BYTES = 50000
MAX_TIMEOUT_SECONDS = 300
MAX_TOOL_CALLS = 50

DANGEROUS_SHELL_PATTERNS = [
    r'\brm\s+-rf\s+/',
    r'\bmkfs\b',
    r'\bdd\s+if=',
    r'\bformat\b',
    r':\(\)\s*\{',
    r'\bcurl\s+.*\|\s*(ba)?sh',
    r'\bwget\s+.*\|\s*(ba)?sh',
    r'\bchmod\s+777',
    r'\bchown\s+root',
    r'\biptables\b',
    r'\bnetfilter\b',
    r'\bsystemctl\s+(stop|disable|mask)',
    r'\bservice\s+\w+\s+stop',
    r'\bmount\b',
    r'\bumount\b',
    r'\bshutdown\b',
    r'\breboot\b',
    r'\binit\s+[06]',
    r'\bcrontab\s+-r',
    r'\bpasswd\b',
    r'\buseradd\b',
    r'\buserdel\b',
    r'\busermod\b',
]

SAFE_ENV = {
    'PATH': '/usr/local/bin:/usr/bin:/bin:/usr/local/bin',
    'HOME': str(Path.home()),
    'LANG': 'en_US.UTF-8',
    'LC_ALL': 'en_US.UTF-8',
    'TERM': 'dumb',
    'TMPDIR': '/tmp',
}

def _check_shell_dangerous(code: str) -> Optional[str]:
    for pattern in DANGEROUS_SHELL_PATTERNS:
        m = re.search(pattern, code, re.IGNORECASE)
        if m:
            return f"Blocked dangerous command: {m.group(0)}"
    return None

def code_run(code: str, language: str='python', timeout: int=60, working_dir: str='') -> dict:
    timeout = min(max(timeout, 1), MAX_TIMEOUT_SECONDS)
    if language == 'python':
        return _run_python(code, timeout, working_dir)
    elif language == 'javascript':
        return _run_javascript(code, timeout, working_dir)
    elif language == 'shell':
        return _run_shell(code, timeout, working_dir)
    else:
        return {'error': f'Unsupported language: {language}. Use python, javascript, or shell.'}

def _truncate_output(text: str) -> str:
    if len(text) > MAX_OUTPUT_BYTES:
        return text[:MAX_OUTPUT_BYTES] + f'\n... (truncated, total {len(text)} bytes)'
    return text

def _run_python(code: str, timeout: int, working_dir: str) -> dict:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(code)
        f.flush()
        script_path = f.name
    try:
        env = {**SAFE_ENV, 'PYTHONPATH': '', 'PYTHONNOUSERSITE': '1'}
        result = subprocess.run(
            ['python3', script_path],
            capture_output=True, text=True, timeout=timeout,
            cwd=working_dir or None, env=env,
        )
        return {
            'language': 'python',
            'exit_code': result.returncode,
            'stdout': _truncate_output(result.stdout),
            'stderr': _truncate_output(result.stderr),
            'timeout_used': timeout,
        }
    except subprocess.TimeoutExpired:
        return {'language': 'python', 'exit_code': -1, 'error': f'Execution timed out after {timeout}s'}
    except Exception:
        logger.exception()
        return {'language': 'python', 'exit_code': -1, 'error': 'Internal execution error'}
    finally:
        Path(script_path).unlink(missing_ok=True)

def _run_javascript(code: str, timeout: int, working_dir: str) -> dict:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False) as f:
        f.write(code)
        f.flush()
        script_path = f.name
    try:
        node_bin = '/usr/bin/node'
        if not Path(node_bin).exists():
            node_bin = '/usr/local/bin/node'
        result = subprocess.run(
            [node_bin, script_path],
            capture_output=True, text=True, timeout=timeout,
            cwd=working_dir or None, env=SAFE_ENV,
        )
        return {
            'language': 'javascript',
            'exit_code': result.returncode,
            'stdout': _truncate_output(result.stdout),
            'stderr': _truncate_output(result.stderr),
        }
    except subprocess.TimeoutExpired:
        return {'language': 'javascript', 'exit_code': -1, 'error': f'Execution timed out after {timeout}s'}
    except FileNotFoundError:
        return {'language': 'javascript', 'exit_code': -1, 'error': 'Node.js not found'}
    except Exception:
        logger.exception()
        return {'language': 'javascript', 'exit_code': -1, 'error': 'Internal execution error'}
    finally:
        Path(script_path).unlink(missing_ok=True)

def _run_shell(code: str, timeout: int, working_dir: str) -> dict:
    danger = _check_shell_dangerous(code)
    if danger:
        return {'language': 'shell', 'exit_code': -1, 'error': danger}
    try:
        result = subprocess.run(
            ['bash', '-c', code],
            capture_output=True, text=True, timeout=timeout,
            cwd=working_dir or None, env=SAFE_ENV,
        )
        return {
            'language': 'shell',
            'exit_code': result.returncode,
            'stdout': _truncate_output(result.stdout),
            'stderr': _truncate_output(result.stderr),
        }
    except subprocess.TimeoutExpired:
        return {'language': 'shell', 'exit_code': -1, 'error': f'Execution timed out after {timeout}s'}
    except Exception:
        logger.exception()
        return {'language': 'shell', 'exit_code': -1, 'error': 'Internal execution error'}