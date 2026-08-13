"""P1-2 / 全检3 F6: hook-injection cwd-escape + content-policy tests.

The SessionStart hook (``hook_inject_memory.py``) is executed via a command
that itself does ``cd <METANO_HOME> && python3 hook_inject_memory.py``, so the
process cwd is always METANO_HOME and ``os.getcwd()`` can NEVER be trusted as
the project-scope signal (an empty event used to fall back to it and leak all
tags into every project). These tests lock that fail-closed and verify the
injected-content policy: any memory carrying a ``cd`` directive or a
path-escape feature is rejected with an explicit ``REJECTED`` log line.
"""
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hook_inject_memory as him


def test_inject_with_cd_is_rejected():
    """(a) A command carrying its own cd directive must be rejected (not injected)."""
    cmd = 'cd /etc; touch test.txt'
    assert him._validate_inject_content(cmd) == 'unsafe cd command in hook'
    # variants: && cd, ; cd, cd /, cd ~, cd ..
    for variant in ('foo && cd /tmp', 'foo; cd /root', 'cd /', 'cd ~',
                    'cd ../..', 'x=1 && cd metano && ls'):
        assert him._validate_inject_content(variant) == 'unsafe cd command in hook'


def test_inject_normal_command_still_accepted():
    """(b) A normal command / behaviour rule passes through unchanged."""
    assert him._validate_inject_content('ls -la') is None
    assert him._validate_inject_content('echo hello') is None
    assert him._validate_inject_content('修改后端API后必须同步修改前端TS类型和hooks') is None


def test_path_escape_content_rejected():
    """(c) Content with ../, /etc/, /root/ or /home/ is rejected."""
    for bad in ('cat ../etc/passwd', 'touch /etc/test.txt', 'ls /root/',
                'cat /home/dk/secret', 'rm ../../x'):
        assert him._validate_inject_content(bad) == 'unsafe path escape in content'


def test_rejected_log_marker(caplog):
    """(d) The rejection log line carries the REJECTED marker + content summary."""
    with caplog.at_level('WARNING', logger='metano'):
        him._log_reject('unsafe cd command in hook', 'cd /etc; touch test.txt')
    msgs = [r.getMessage() for r in caplog.records]
    assert any('REJECTED: unsafe cd command in hook' in m for m in msgs)
    assert any('cd /etc; touch test.txt' in m for m in msgs)


def test_main_filters_cd_and_wraps_untrusted(monkeypatch, capsys):
    """main() skips rejected content and wraps the injected context in
    <untrusted_data> tags; safe content is still injected."""
    results = [
        {'content': '修改代码后必须验证'},          # safe behaviour rule
        {'content': 'cd /etc; touch test.txt'},   # unsafe cd -> rejected
        {'content': 'cat ../etc/passwd'},          # path escape -> rejected
        {'content': 'ls -la'},                     # safe command
    ]

    def fake_search(query, tag, limit):
        return {'results': results}

    monkeypatch.setattr(him, '_search_memories', fake_search)
    monkeypatch.setattr('sys.stdin', io.StringIO('{}'))
    him.main()

    ctx = json.loads(capsys.readouterr().out)['hookSpecificOutput']['additionalContext']
    assert '<untrusted_data>' in ctx and '</untrusted_data>' in ctx
    assert '修改代码后必须验证' in ctx
    assert 'ls -la' in ctx
    assert 'cd /etc; touch test.txt' not in ctx
    assert '../etc/passwd' not in ctx


def test_main_fail_closed_without_cwd_is_general_only(monkeypatch, capsys):
    """Empty event (no explicit cwd) must NOT inject metano-specific tags:
    fail-closed even though the process cwd is METANO_HOME."""
    safe = {'content': '通用纪律：修改后必须验证'}
    monkeypatch.setattr(him, '_search_memories',
                        lambda query, tag, limit: {'results': [safe]})
    monkeypatch.setattr('sys.stdin', io.StringIO('{}'))
    him.main()

    ctx = json.loads(capsys.readouterr().out)['hookSpecificOutput']['additionalContext']
    for tag in him.METANO_TAGS:
        assert f'[{tag}]' not in ctx, f'metano tag [{tag}] leaked into a non-metano session'
    # Content dedup means a single safe memory appears under the FIRST general
    # tag only — the invariant is that it IS injected (under a general tag)
    # while no metano tag ever appears.
    assert any(f'[{tag}]' in ctx for tag in him.GENERAL_TAGS)


def test_main_non_metano_cwd_is_general_only(monkeypatch, capsys):
    """Explicit non-metano cwd -> GENERAL tags only (regression guard for F-4)."""
    safe = {'content': '通用纪律：修改后必须验证'}
    monkeypatch.setattr(him, '_search_memories',
                        lambda query, tag, limit: {'results': [safe]})
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'cwd': '/home/dk/some-other-project'})))
    him.main()

    ctx = json.loads(capsys.readouterr().out)['hookSpecificOutput']['additionalContext']
    for tag in him.METANO_TAGS:
        assert f'[{tag}]' not in ctx
    assert any(f'[{tag}]' in ctx for tag in him.GENERAL_TAGS)
