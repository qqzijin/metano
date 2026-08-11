"""Code execution sandbox: run Python, JavaScript, and Shell code safely.

Security model
--------------
Python / JavaScript / Shell snippets are executed inside a bubblewrap
(``bwrap``) OS-level sandbox whenever one is available:

* ``--ro-bind / /`` — the root filesystem is mounted read-only, so a snippet
  cannot modify the host (no ``rm -rf /``, no writing under /etc, ...).
* ``--tmpfs $HOME`` — the caller's home directory is replaced by an empty,
  throw-away tmpfs, so ``~/.ssh``, ``~/.claude/metano/gateway_config.yaml``
  and every other private file under the real home is invisible.
* ``--unshare-net`` — a fresh, empty network namespace; any network access
  (SSRF, data exfiltration) fails at connect()/socket() time.
* ``--unshare-pid`` + ``--die-with-parent`` — a dedicated PID namespace makes
  it impossible for a forked/daemonised child to escape cleanup: when the
  sandbox leader dies the kernel reaps the whole namespace.
* Resource limits (RLIMIT_AS, RLIMIT_CPU, RLIMIT_NPROC) are applied inside the
  sandbox before the snippet runs, with both soft and hard caps so the snippet
  cannot raise them.
* Output is streamed through bounded pipes and truncated; it is never buffered
  fully in memory.
* On timeout the whole process group is SIGKILLed (``start_new_session``), so
  no orphan survives.

If ``bwrap`` is not usable (non-Linux, binary missing, user namespaces
disabled) execution falls back to direct spawning — still with process-group
killing on timeout and the same resource limits, but without the read-only
root / network isolation of the full sandbox.
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

from metano.log import logger

MAX_OUTPUT_BYTES = 50000
MAX_TIMEOUT_SECONDS = 300
MAX_TOOL_CALLS = 50

# ---- Sandbox resource limits (applied inside the sandbox, soft + hard) ----
RLIMIT_AS_BYTES = 1024 * 1024 * 1024        # 1 GiB address space (OOM guard)
RLIMIT_AS_KB = RLIMIT_AS_BYTES // 1024       # ulimit -v takes KiB
RLIMIT_CPU_SECONDS = 60                       # hard CPU-time cap
RLIMIT_NPROC = 256                            # fork-bomb guard (per real uid)

# Path inside the sandbox where the snippet's temp directory is bound.
_SANDBOX_SCRIPT_DIR = '/tmp/sandbox'

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

# ---------------------------------------------------------------------------
# bubblewrap availability probe (cached)
# ---------------------------------------------------------------------------

_bwrap_cache: Optional[bool] = None
_bwrap_cache_lock = threading.Lock()


def _bwrap_available() -> bool:
    """Return True if bwrap works on this machine (cached after first check)."""
    global _bwrap_cache
    if _bwrap_cache is None:
        with _bwrap_cache_lock:
            if _bwrap_cache is None:
                _bwrap_cache = _probe_bwrap()
    return _bwrap_cache


def _probe_bwrap() -> bool:
    """Actually run a minimal sandbox to confirm bwrap is usable.

    bwrap is present on most Linux boxes but may still fail at runtime (e.g.
    user namespaces disabled). Probing with the exact flags used by the real
    sandbox makes the "sandbox vs. fallback" decision reliable.
    """
    if sys.platform != 'linux':
        return False
    bwrap = shutil.which('bwrap')
    if not bwrap:
        return False
    home = str(Path.home())
    probe = [
        bwrap,
        '--ro-bind', '/', '/',
        '--tmpfs', home,
        '--unshare-net',
        '--unshare-pid',
        '--unshare-ipc',
        '--unshare-uts',
        '--proc', '/proc',
        '--dev', '/dev',
        '--tmpfs', '/tmp',
        '--tmpfs', '/run',
        '--tmpfs', '/dev/shm',
        '--die-with-parent',
        '--setenv', 'HOME', home,
        '--', '/bin/true',
    ]
    try:
        r = subprocess.run(probe, capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# bwrap command construction
# ---------------------------------------------------------------------------

def _bwrap_argv(argv: list, rw_binds=(), ro_binds=()) -> list:
    """Prepend bubblewrap sandbox options to ``argv``.

    ``rw_binds`` is an iterable of ``(host_path, sandbox_path)`` read-write
    bind mounts (used to expose the snippet's throw-away script directory).
    ``ro_binds`` is a read-only variant, used for host directories that must be
    visible but never modifiable (a working dir under $HOME, a user-local
    interpreter prefix such as ~/local/node).
    """
    home = str(Path.home())
    cmd = [
        'bwrap',
        '--ro-bind', '/', '/',
        '--tmpfs', home,
        '--unshare-net',
        '--unshare-pid',
        '--unshare-ipc',
        '--unshare-uts',
        '--proc', '/proc',
        '--dev', '/dev',
        '--tmpfs', '/tmp',
        '--tmpfs', '/run',
        '--tmpfs', '/dev/shm',
        '--die-with-parent',
    ]
    for host, sandbox in ro_binds:
        cmd += ['--ro-bind', host, sandbox]
    for host, sandbox in rw_binds:
        cmd += ['--bind', host, sandbox]
    cmd += ['--setenv', 'HOME', home]
    cmd += ['--'] + list(argv)
    return cmd


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------

def _ulimit_wrap(inner_cmd: list, cd_target: Optional[str] = None) -> list:
    """Wrap ``inner_cmd`` in a bash process that applies resource limits.

    Both the soft and hard limit are set (``ulimit -X`` then ``ulimit -HX``)
    so the sandboxed code cannot raise them. Limits are best-effort: if one
    cannot be set (e.g. a pre-existing lower hard cap) the others still apply.

    In bwrap mode the sandbox always starts with cwd ``/`` (to avoid inheriting
    a host path that the tmpfs-home overlay would hide); ``cd_target`` tells the
    wrapper where to change directory before exec'ing the real command.
    """
    limits = (
        f'ulimit -v {RLIMIT_AS_KB}; ulimit -Hv {RLIMIT_AS_KB}; '
        f'ulimit -t {RLIMIT_CPU_SECONDS}; ulimit -Ht {RLIMIT_CPU_SECONDS}; '
        f'ulimit -u {RLIMIT_NPROC}; ulimit -Hu {RLIMIT_NPROC}; '
    )
    if cd_target is not None:
        script = (
            limits
            + 'cd -- "$1" 2>/dev/null || { echo "error: cannot cd to \'$1\'"; exit 1; }; '
            + 'shift; exec "$@"'
        )
        return ['/bin/bash', '-c', script, 'bash', cd_target] + list(inner_cmd)
    script = limits + 'exec "$@"'
    return ['/bin/bash', '-c', script, 'bash'] + list(inner_cmd)


# ---------------------------------------------------------------------------
# Streaming output + process-group timeout handling
# ---------------------------------------------------------------------------

def _stream_reader(stream, bucket: list, limit: int):
    """Drain ``stream`` into ``bucket[0]`` (a bytearray), capping at ``limit``.

    ``bucket`` is ``[bytearray, truncated_flag]``. Data past the cap is still
    drained so the child never blocks on a full pipe buffer, but it is not
    retained — memory stays bounded regardless of how much the snippet emits
    (even a single multi-gigabyte line).
    """
    try:
        while True:
            data = stream.read1(8192)
            if not data:
                break
            room = limit - len(bucket[0])
            if room > 0:
                bucket[0] += data[:room]
            if len(data) > room:
                bucket[1] = True
    except (ValueError, OSError, AttributeError):
        # Stream closed under us (e.g. after a timeout kill) — nothing to do.
        pass


def _kill_process_group(proc):
    """SIGKILL the whole process group (sandbox + every descendant)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _run_popen(argv: list, cwd: Optional[str], env: dict, timeout: int,
               language: str) -> dict:
    """Spawn ``argv``, streaming output, enforcing wall-clock timeout.

    Raises OSError if the process cannot be started at all.
    """
    stdout_bucket: list = [bytearray(), False]
    stderr_bucket: list = [bytearray(), False]

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    t_out = threading.Thread(
        target=_stream_reader, args=(proc.stdout, stdout_bucket, MAX_OUTPUT_BYTES),
        daemon=True,
    )
    t_err = threading.Thread(
        target=_stream_reader, args=(proc.stderr, stderr_bucket, MAX_OUTPUT_BYTES),
        daemon=True,
    )
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Kill the whole process group; with --unshare-pid the namespace is
        # torn down by the kernel when its leader dies, so no orphan survives.
        _kill_process_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                proc.wait()
    finally:
        for s in (proc.stdout, proc.stderr):
            try:
                s.close()
            except Exception:
                pass
        t_out.join(timeout=10)
        t_err.join(timeout=10)

    stdout = bytes(stdout_bucket[0]).decode('utf-8', errors='replace')
    stderr = bytes(stderr_bucket[0]).decode('utf-8', errors='replace')
    if stdout_bucket[1]:
        stdout += f'\n... (output truncated, exceeded {MAX_OUTPUT_BYTES} bytes)'
    if stderr_bucket[1]:
        stderr += f'\n... (output truncated, exceeded {MAX_OUTPUT_BYTES} bytes)'

    result = {
        'language': language,
        'exit_code': -1 if timed_out else proc.returncode,
        'stdout': _truncate_output(stdout),
        'stderr': _truncate_output(stderr),
    }
    if timed_out:
        result['error'] = f'Execution timed out after {timeout}s'
    return result


def _execute(argv: list, cwd: str, env: dict, timeout: int, language: str,
             script_host: Optional[str] = None,
             script_sandbox: Optional[str] = None, binds=(),
             ro_binds=()) -> dict:
    """Run ``argv`` under the sandbox, streaming output, enforcing limits.

    ``argv`` references the snippet script by its *host* path (``script_host``);
    in bwrap mode that argument is rewritten to ``script_sandbox`` and the temp
    directory is bind-mounted into the sandbox. ``binds`` are read-write mounts
    (script dir), ``ro_binds`` read-only (working dir under $HOME, interpreter
    prefixes).
    """
    rw_binds = list(binds)
    ro_binds = list(ro_binds)
    use_bwrap = _bwrap_available()

    if use_bwrap:
        home = str(Path.home())
        cd_target = home
        if cwd:
            abs_cwd = os.path.abspath(cwd)
            # The real home is tmpfs-hidden inside the sandbox. Re-expose only
            # the requested working directory (read-only), not the whole home.
            if abs_cwd.startswith(home + os.sep):
                ro_binds.append((abs_cwd, abs_cwd))
            cd_target = abs_cwd
        inner = [script_sandbox if (script_host and a == script_host) else a
                 for a in argv]
        wrapped = _ulimit_wrap(inner, cd_target)
        full_argv = _bwrap_argv(wrapped, rw_binds=rw_binds, ro_binds=ro_binds)
        proc_cwd = '/'  # never inherit a host cwd that the tmpfs-home hides
    else:
        wrapped = _ulimit_wrap(argv, None)
        full_argv = wrapped
        proc_cwd = cwd or None

    try:
        return _run_popen(full_argv, proc_cwd, env, timeout, language)
    except OSError as e:
        if use_bwrap:
            logger.warning('bwrap execution failed (%s); falling back to direct execution', e)
            try:
                return _run_popen(_ulimit_wrap(argv, None), cwd or None, env,
                                  timeout, language)
            except OSError as e2:
                return {'language': language, 'exit_code': -1,
                        'error': f'Failed to start process: {e2}'}
        return {'language': language, 'exit_code': -1,
                'error': f'Failed to start process: {e}'}


# ---------------------------------------------------------------------------
# Script staging helpers
# ---------------------------------------------------------------------------

def _make_script(code: str, suffix: str):
    """Write ``code`` to a fresh temp file; return (host_dir, host_path, sandbox_path)."""
    host_dir = tempfile.mkdtemp(prefix='metano-sandbox-')
    host_script = os.path.join(host_dir, 'script' + suffix)
    with open(host_script, 'w') as f:
        f.write(code)
    sandbox_script = _SANDBOX_SCRIPT_DIR + '/script' + suffix
    return host_dir, host_script, sandbox_script


def _cleanup(host_dir: str):
    try:
        shutil.rmtree(host_dir, ignore_errors=True)
    except Exception:
        logger.exception('code_exec.py:397 exception')


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

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

def _check_shell_dangerous(code: str) -> Optional[str]:
    for pattern in DANGEROUS_SHELL_PATTERNS:
        m = re.search(pattern, code, re.IGNORECASE)
        if m:
            return f"Blocked dangerous command: {m.group(0)}"
    return None


def _run_python(code: str, timeout: int, working_dir: str) -> dict:
    workdir, script_host, script_sandbox = _make_script(code, '.py')
    try:
        interp = shutil.which('python3') or 'python3'
        env = {**SAFE_ENV, 'PYTHONPATH': '', 'PYTHONNOUSERSITE': '1'}
        result = _execute(
            [interp, script_host], working_dir, env, timeout, 'python',
            script_host=script_host, script_sandbox=script_sandbox,
            binds=[(workdir, _SANDBOX_SCRIPT_DIR)],
        )
        result['timeout_used'] = timeout
        return result
    finally:
        _cleanup(workdir)


def _run_javascript(code: str, timeout: int, working_dir: str) -> dict:
    node_bin = shutil.which('node')
    if not node_bin:
        for p in ('/usr/bin/node', '/usr/local/bin/node'):
            if Path(p).exists():
                node_bin = p
                break
    if not node_bin:
        return {'language': 'javascript', 'exit_code': -1, 'error': 'Node.js not found'}
    node_bin = os.path.realpath(node_bin)

    workdir, script_host, script_sandbox = _make_script(code, '.js')
    try:
        binds = [(workdir, _SANDBOX_SCRIPT_DIR)]
        ro_binds = []
        # Node here is a user-local install under $HOME; bind its prefix dir
        # read-only so the binary + bundled modules are visible inside the
        # tmpfs-hidden home. The rest of home stays hidden.
        node_prefix = str(Path(node_bin).resolve().parent.parent)
        home = str(Path.home())
        if node_prefix.startswith(home + os.sep):
            ro_binds.append((node_prefix, node_prefix))
        env = {**SAFE_ENV}
        return _execute(
            [node_bin, script_host], working_dir, env, timeout, 'javascript',
            script_host=script_host, script_sandbox=script_sandbox,
            binds=binds, ro_binds=ro_binds,
        )
    finally:
        _cleanup(workdir)


def _run_shell(code: str, timeout: int, working_dir: str) -> dict:
    danger = _check_shell_dangerous(code)
    if danger:
        return {'language': 'shell', 'exit_code': -1, 'error': danger}
    workdir, script_host, script_sandbox = _make_script(code, '.sh')
    try:
        bash = shutil.which('bash') or '/bin/bash'
        env = {**SAFE_ENV}
        return _execute(
            [bash, script_host], working_dir, env, timeout, 'shell',
            script_host=script_host, script_sandbox=script_sandbox,
            binds=[(workdir, _SANDBOX_SCRIPT_DIR)],
        )
    finally:
        _cleanup(workdir)
