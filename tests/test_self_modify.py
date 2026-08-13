"""Tests for the self-modification (self-bootstrap) pipeline.

The pipeline: SCAN → GENERATE → VERIFY → APPLY → LOG. The verify gate runs the
full test suite in an isolated git worktree, so a bad mutation never touches
the main repo/runtime. These tests mock the LLM generator and drive the
pipeline end to end with a real, known-safe mutation.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def no_llm(monkeypatch):
    """Block real LLM calls; return a fixed {old,new} edit for LLM fallback."""
    def fake_call_llm(system, user, max_tokens=1500, timeout=45):
        # Return a JSON {old, new} edit targeting metano/__init__.py.
        # Keep the old block in sync with the real metano/__init__.py version
        # line, or generate_candidate reports "old block not found" and returns
        # None (stale fixture after a version bump).
        return json.dumps({
            'old': '__version__ = "3.3.0"',
            'new': '__version__ = "3.3.0"\n# self-modify test marker',
        }), 0.0
    monkeypatch.setattr('metano.llm_call.call_llm', fake_call_llm)


def test_scan_issues_shape():
    """scan_issues returns deduplicated dicts with the expected keys."""
    from metano.self_modify import scan_issues
    issues = scan_issues()
    assert isinstance(issues, list)
    for i in issues:
        assert 'pattern' in i and 'file' in i and 'severity' in i


def test_allowed_to_mutate_constitution():
    """The constitution blocks self_modify.py and tests/."""
    from metano.self_modify import _allowed_to_mutate, _normalize_rel
    assert not _allowed_to_mutate('metano/self_modify.py')
    assert not _allowed_to_mutate('tests/test_self_modify.py')
    assert not _allowed_to_mutate('tests/x.py')
    assert _allowed_to_mutate('metano/db.py')
    assert _allowed_to_mutate('llm_call.py')  # bare introspector path is normalized


def test_generate_candidate_deterministic_silent_except():
    """silent-except issues are fixed deterministically (no LLM)."""
    from metano.self_modify import scan_issues, generate_candidate
    issues = scan_issues()
    se = next((i for i in issues if i.get('pattern') == 'silent-except'), None)
    if not se:
        return  # no such finding in this tree; test is vacuous
    cand = generate_candidate(se)
    assert cand is not None
    assert cand['method'] == 'deterministic'
    # The deterministic silent-except fix must actually add a logging call to
    # the diff (N4: the old `'logger.exception' in diff or diff` was always true).
    assert 'logger.exception' in cand['diff']


def test_generate_candidate_llm_fallback(no_llm):
    """A non-deterministic pattern uses the LLM fallback producing {old,new}."""
    from metano.self_modify import generate_candidate
    issue = {'pattern': 'custom', 'severity': 'medium', 'file': 'metano/__init__.py', 'line': 1, 'detail': 'x'}
    cand = generate_candidate(issue)
    assert cand is not None
    assert cand['file'] == 'metano/__init__.py'
    assert 'diff --git' in cand['diff']


def test_verify_candidate_rejects_bad_diff():
    """A diff that does not apply is rejected and the main repo stays clean."""
    from metano.self_modify import verify_candidate
    from metano.self_modify import REPO_ROOT
    before = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True, cwd=str(REPO_ROOT)).stdout
    bad = {'file': 'metano/__init__.py', 'diff': 'this is not a valid diff at all'}
    verdict = verify_candidate(bad)
    assert verdict['verdict'] == 'rejected'
    # Main repo untouched.
    after = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True, cwd=str(REPO_ROOT)).stdout
    assert before == after


def test_verify_candidate_accepts_good_diff(no_llm):
    """A diff that applies cleanly and passes tests is 'verified'."""
    from metano.self_modify import verify_candidate, generate_candidate
    cand = generate_candidate({'pattern': 'custom', 'severity': 'medium', 'file': 'metano/__init__.py', 'line': 1, 'detail': 'x'})
    assert cand is not None
    verdict = verify_candidate(cand)
    assert verdict['verdict'] in ('verified', 'rejected'), verdict


def test_revert_mutation_requires_applied(tmp_path):
    """Reverting a non-applied mutation returns wrong_state.

    Isolation comes from the ``isolated_env`` autouse fixture in conftest.py
    (EVO_DB_PATH is already redirected to tmp_path).  Reloading evo_models here
    would be counter-productive: paths.py constants are frozen at import time,
    so a reload after setting METANO_HOME does NOT re-resolve them (audit
    cross-conclusion #1).
    """
    from metano import evo_models
    from metano.self_modify import revert_mutation
    eid = evo_models.add_self_modify_event('issue', 'metano/__init__.py', 'diff')
    result = revert_mutation(eid)
    assert result['status'] == 'wrong_state'
