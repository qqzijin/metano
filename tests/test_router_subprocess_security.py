"""Security tests for audit P1-1: sub-process env scrubbing + process-group kill.

Covers the two claude -p derived-process paths that were NOT previously
hardened (the third, sub_agent.py, was already scrubbed):

* ``metano.gateway.router._call_claude`` — the gateway claude -p child.
* ``metano.model_router.ModelRouter.call_claude`` — the model-router claude -p
  child.

Both must (a) pass a whitelist-scrubbed env to the child (so metano's
operational secrets — JWT secret, feishu/lark credentials, A2A/MCP tokens,
DB paths — never reach it) and (b) spawn with ``start_new_session=True`` so a
timeout can SIGKILL the whole process group, not just the direct child.
"""
import asyncio
import signal
import subprocess

import pytest


# ---------------------------------------------------------------------------
# scrub_subprocess_env drops metano's operational secrets
# ---------------------------------------------------------------------------

def test_scrub_subprocess_env_drops_sensitive_keys():
    """Non-allowlisted keys (JWT secret, ssh-agent, feishu/lark, MCP/A2A
    tokens, DB paths) must never survive the scrub; runtime vars the claude
    child needs (PATH/HOME/CLAUDE_BIN/...) are preserved."""
    from metano import code_exec

    ambient = {
        'PATH': '/usr/local/bin:/usr/bin:/bin',
        'HOME': '/home/dk',
        'CLAUDE_BIN': '/fake/claude',
        'LANG': 'en_US.UTF-8',
        'TERM': 'xterm',
        # ---- operational secrets that MUST never reach a claude -p child ----
        'JWT_SECRET': 'super-secret-jwt',
        'JWT_VERIFY_KEY': 'verify-secret',
        'SSH_AUTH_SOCK': '/run/user/1000/ssh-agent.sock',
        'METANO_HOME': '/home/dk/.claude/metano',
        'FEISHU_APP_SECRET': 'feishu-secret',
        'LARK_APP_SECRET': 'lark-secret',
        'MCP_SERVER_TOKEN': 'mcp-token',
        'A2A_HUB_TOKEN': 'a2a-token',
        'DB_PATH': '/home/dk/.claude/metano/bridge.db',
    }
    scrubbed = code_exec.scrub_subprocess_env(ambient)

    for sensitive in ('JWT_SECRET', 'JWT_VERIFY_KEY', 'SSH_AUTH_SOCK',
                      'METANO_HOME', 'FEISHU_APP_SECRET', 'LARK_APP_SECRET',
                      'MCP_SERVER_TOKEN', 'A2A_HUB_TOKEN', 'DB_PATH'):
        assert sensitive not in scrubbed, f'{sensitive} leaked into sub-process env'

    # Allowlisted runtime vars the claude child needs are preserved.
    assert scrubbed['PATH'] == ambient['PATH']
    assert scrubbed['HOME'] == ambient['HOME']
    assert scrubbed['CLAUDE_BIN'] == ambient['CLAUDE_BIN']


def test_scrub_subprocess_env_no_arg_scrubs_os_environ(monkeypatch):
    """``scrub_subprocess_env()`` with no args reads and scrubs os.environ."""
    from metano import code_exec

    monkeypatch.setenv('JWT_SECRET', 'super-secret-jwt')
    monkeypatch.setenv('SSH_AUTH_SOCK', '/run/user/1000/ssh-agent.sock')
    monkeypatch.setenv('CLAUDE_BIN', '/fake/claude')

    scrubbed = code_exec.scrub_subprocess_env()

    assert 'JWT_SECRET' not in scrubbed
    assert 'SSH_AUTH_SOCK' not in scrubbed
    assert scrubbed.get('CLAUDE_BIN') == '/fake/claude'


# ---------------------------------------------------------------------------
# _kill_process_group (router + model_router) targets the whole group
# ---------------------------------------------------------------------------

def test_router_kill_process_group_uses_killpg(monkeypatch):
    from metano.gateway import router

    calls = []
    monkeypatch.setattr(router.os, 'getpgid', lambda pid: pid + 10)
    monkeypatch.setattr(router.os, 'killpg', lambda pgid, sig: calls.append((pgid, sig)))

    class FakeProc:
        pid = 5

    router._kill_process_group(FakeProc())
    assert calls == [(15, signal.SIGKILL)]


def test_model_router_kill_process_group_uses_killpg(monkeypatch):
    from metano import model_router

    calls = []
    monkeypatch.setattr(model_router.os, 'getpgid', lambda pid: pid + 20)
    monkeypatch.setattr(model_router.os, 'killpg', lambda pgid, sig: calls.append((pgid, sig)))

    class FakeProc:
        pid = 7

    model_router._kill_process_group(FakeProc())
    assert calls == [(27, signal.SIGKILL)]


def test_kill_process_group_survives_lookup_error(monkeypatch):
    """If the group is already gone (ProcessLookupError), no exception escapes —
    neither from killpg nor from the proc.kill() fallback."""
    from metano.gateway import router

    monkeypatch.setattr(router.os, 'getpgid', lambda pid: 1)

    def boom_killpg(pgid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(router.os, 'killpg', boom_killpg)

    class FakeProc:
        pid = 5

        def kill(self):
            raise ProcessLookupError

    router._kill_process_group(FakeProc())  # must not raise


# ---------------------------------------------------------------------------
# model_router.call_claude: timeout kills the whole group, env stays scrubbed
# ---------------------------------------------------------------------------

def test_model_router_call_claude_timeout_kills_group(monkeypatch):
    from metano import model_router as mr

    killed = []
    monkeypatch.setattr(mr.os, 'getpgid', lambda pid: pid + 5000)
    monkeypatch.setattr(mr.os, 'killpg', lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setenv('CLAUDE_BIN', '/fake/claude')
    monkeypatch.setenv('JWT_SECRET', 'super-secret-jwt')

    captured = {}

    class FakeProc:
        pid = 123

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired('claude -p hi', 0.001)

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(*args, **kwargs):
        captured['env'] = kwargs.get('env')
        captured['start_new_session'] = kwargs.get('start_new_session')
        return FakeProc()

    monkeypatch.setattr(mr.subprocess, 'Popen', fake_popen)

    router = mr.ModelRouter()
    provider = mr.ModelProvider(name='t', base_url='', api_key='', model='')
    monkeypatch.setattr(router, 'get_provider', lambda name: provider)

    result = router.call_claude('hello', timeout=5)

    assert result == 'Response timed out.'
    assert captured['start_new_session'] is True
    assert 'JWT_SECRET' not in captured['env']
    # Popen was given a scrubbed env + process-group kill on timeout.
    assert killed == [(5123, signal.SIGKILL)]


# ---------------------------------------------------------------------------
# router._call_claude: env scrubbed, child gets its own process group
# ---------------------------------------------------------------------------

def test_router_call_claude_scrubs_env_and_new_session(monkeypatch):
    from metano.gateway import router as router_mod
    from metano.gateway.router import GatewaySession, MessageRouter

    monkeypatch.setattr(MessageRouter, '_build_system_context', lambda self, p, u: '')
    monkeypatch.setenv('CLAUDE_BIN', '/fake/claude')
    monkeypatch.setenv('JWT_SECRET', 'super-secret-jwt')
    monkeypatch.setenv('SSH_AUTH_SOCK', '/run/user/1000/ssh-agent.sock')

    from metano import model_router as mr
    provider = mr.ModelProvider(name='t', base_url='', api_key='', model='')
    monkeypatch.setattr(mr.model_router, 'get_provider', lambda name: provider)

    captured = {}

    class FakeProc:
        async def communicate(self):
            return b'', b''

        def kill(self):
            pass

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured['cmd'] = cmd
        captured['env'] = kwargs.get('env')
        captured['start_new_session'] = kwargs.get('start_new_session')
        return FakeProc()

    monkeypatch.setattr(router_mod.asyncio, 'create_subprocess_exec', fake_create_subprocess_exec)

    r = MessageRouter()
    session = GatewaySession(platform='web', user_id='admin')
    asyncio.run(r._call_claude('hello', session, skip_permissions=False))

    assert captured['start_new_session'] is True
    assert 'JWT_SECRET' not in captured['env']
    assert 'SSH_AUTH_SOCK' not in captured['env']
    assert captured['env'].get('CLAUDE_BIN') == '/fake/claude'
    # PATH/HOME survive the scrub so the claude child can start.
    assert captured['env'].get('PATH')
    assert captured['env'].get('HOME')
