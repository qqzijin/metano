"""Tests for code introspector: verify anti-pattern detection."""
import ast
import pytest
from metano.code_introspector import (
    detect_silent_except,
    detect_hardcoded_secrets,
    detect_sql_concat,
    detect_shell_exec,
)


def test_detect_silent_except_pass():
    code = """
try:
    x = 1
except Exception:
    pass
"""
    tree = ast.parse(code)
    findings = detect_silent_except(tree, "test.py")
    assert len(findings) == 1
    assert findings[0]["detail"] == "Exception silently swallowed with pass"


def test_detect_silent_except_return_default():
    code = """
try:
    x = 1
except Exception:
    return []
"""
    tree = ast.parse(code)
    findings = detect_silent_except(tree, "test.py")
    assert len(findings) == 1
    assert "return []" in findings[0]["code"]


def test_no_false_positive_on_logged_except():
    code = """
try:
    x = 1
except Exception:
    logger.exception()
    return None
"""
    tree = ast.parse(code)
    findings = detect_silent_except(tree, "test.py")
    assert len(findings) == 0


def test_detect_hardcoded_secret(tmp_path):
    code = '''
secret = "fallback-secret-change-me"
'''
    p = tmp_path / "test.py"
    p.write_text(code)
    tree = ast.parse(code)
    findings = detect_hardcoded_secrets(tree, str(p))
    assert len(findings) >= 1
    assert any("fallback" in f["detail"].lower() for f in findings)


def test_detect_sql_concat():
    code = """
def query():
    conn.execute(f"SELECT * FROM users WHERE id = {user_id}")
"""
    tree = ast.parse(code)
    findings = detect_sql_concat(tree, "test.py")
    assert len(findings) == 1
    assert "f-string" in findings[0]["detail"]


def test_detect_shell_exec_with_variable():
    code = """
import subprocess
subprocess.run(user_input, shell=True)
"""
    tree = ast.parse(code)
    findings = detect_shell_exec(tree, "test.py")
    assert len(findings) == 1
    assert "injection" in findings[0]["detail"].lower()
