"""Code introspector: scan own source code for anti-patterns and feed findings as observations
into the evolution system. This bridges the gap where the harvester only reads chat messages
and cannot detect systemic code quality issues."""

import ast
import json
import re
import time
from pathlib import Path

from .honcho.models import get_honcho_db, add_observation, get_user, create_user
from .evo_models import log_action, EVO_DB_PATH
from metano.log import logger

AGENT_USER_ID = "hermes-introspector"

SOURCE_ROOT = Path(__file__).parent

# Anti-pattern definitions: (name, severity, detector_function)
PATTERNS = []

def register_pattern(name: str, severity: str):
    def decorator(fn):
        PATTERNS.append((name, severity, fn))
        return fn
    return decorator


@register_pattern("silent-except", "high")
def detect_silent_except(tree: ast.Module, filepath: str) -> list[dict]:
    """Find `except Exception` blocks with only `pass` or bare default returns."""
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        is_broad = (node.type is None or
            (isinstance(node.type, ast.Name) and node.type.id == 'Exception'))
        if not is_broad:
            continue
        body = node.body
        # Check for logging calls
        has_logging = any(
            isinstance(s, ast.Expr) and isinstance(s.value, ast.Call) and
            isinstance(s.value.func, ast.Attribute) and
            s.value.func.attr in ('exception', 'error', 'warning', 'critical')
            for s in body
        )
        if has_logging:
            continue
        # Determine if it's just pass or a bare return
        if len(body) == 1 and isinstance(body[0], ast.Pass):
            findings.append({
                'line': node.lineno,
                'code': 'except Exception: pass',
                'detail': 'Exception silently swallowed with pass',
            })
        elif len(body) == 1 and isinstance(body[0], ast.Return):
            try:
                returned = ast.unparse(body[0].value)
            except Exception:
                returned = '?'
            if returned in ('"[]"', "'[]'", '[]', 'None', '{}', '""', "''"):
                findings.append({
                    'line': node.lineno,
                    'code': f'except Exception: return {returned}',
                    'detail': f'Exception silently swallowed, returns {returned}',
                })
    return findings


@register_pattern("hardcoded-secret", "critical")
def detect_hardcoded_secrets(tree: ast.Module, filepath: str) -> list[dict]:
    """Find hardcoded secret strings (JWT fallbacks, default passwords)."""
    findings = []
    # Read source from file for regex scanning (AST can't find string patterns reliably)
    try:
        with open(filepath) as f:
            source = f.read()
    except Exception:
        return findings
    patterns = [
        (r'["\']fallback-secret-change-me["\']', 'JWT hardcoded fallback secret'),
        (r'["\']admin123["\']', 'Hardcoded default admin password'),
        (r'["\']change-me["\']', 'Generic change-me secret'),
    ]
    for regex, desc in patterns:
        for m in re.finditer(regex, source):
            findings.append({
                'line': source[:m.start()].count('\n') + 1,
                'code': m.group(0),
                'detail': desc,
            })
    return findings


@register_pattern("dangerous-html-render", "high")
def detect_dangerous_html(tree: ast.Module, filepath: str) -> list[dict]:
    """Find dangerouslySetInnerHTML in TSX files (checked via text scan)."""
    if not filepath.endswith('.tsx') and not filepath.endswith('.jsx'):
        return []
    findings = []
    source = open(filepath).read()
    for m in re.finditer(r'dangerouslySetInnerHTML', source):
        line = source[:m.start()].count('\n') + 1
        findings.append({
            'line': line,
            'code': 'dangerouslySetInnerHTML',
            'detail': 'XSS risk: raw HTML rendered without sanitization',
        })
    return findings


@register_pattern("sql-concat", "medium")
def detect_sql_concat(tree: ast.Module, filepath: str) -> list[dict]:
    """Find f-string SQL concatenation (potential injection)."""
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != 'execute':
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.JoinedStr):
            findings.append({
                'line': node.lineno,
                'code': 'f-string in execute()',
                'detail': 'SQL via f-string concatenation — use parameterized queries',
            })
    return findings


@register_pattern("bare-shell-exec", "high")
def detect_shell_exec(tree: ast.Module, filepath: str) -> list[dict]:
    """Find subprocess.run with shell=True using variable input."""
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ('run', 'call', 'Popen'):
            continue
        has_shell_true = any(
            isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords if kw.arg == 'shell'
        )
        if has_shell_true and node.args:
            try:
                first_arg = ast.unparse(node.args[0])
            except Exception:
                first_arg = '<variable>'
            if not first_arg.startswith(("'", '"')):
                findings.append({
                    'line': node.lineno,
                    'code': f'subprocess.{node.func.attr}(.., shell=True)',
                    'detail': 'Shell execution with variable input — command injection risk',
                })
    return findings


def _ensure_user():
    """Ensure the introspector user exists in honcho."""
    conn = get_honcho_db()
    try:
        user = get_user(conn, AGENT_USER_ID)
        if not user:
            create_user(conn, AGENT_USER_ID)
    finally:
        conn.close()


def scan_source_tree() -> list[dict]:
    """Scan all Python and TSX source files, return findings."""
    all_findings = []
    for py_file in sorted(SOURCE_ROOT.rglob('*.py')):
        if '__pycache__' in str(py_file):
            continue
        try:
            source = py_file.read_text()
            tree = ast.parse(source)
        except Exception as e:
            logger.warning(f'Skip {py_file}: {e}')
            continue

        for pattern_name, severity, detector in PATTERNS:
            try:
                hits = detector(tree, str(py_file))
                for hit in hits:
                    all_findings.append({
                        'pattern': pattern_name,
                        'severity': severity,
                        'file': str(py_file.relative_to(SOURCE_ROOT)),
                        **hit,
                    })
            except Exception as e:
                logger.warning(f'Detector {pattern_name} failed on {py_file}: {e}')

    # Also scan TSX files for dangerous HTML
    web_dir = SOURCE_ROOT.parent / 'web' / 'src'
    if web_dir.exists():
        for tsx_file in sorted(web_dir.rglob('*.tsx')):
            try:
                source = tsx_file.read_text()
                tree = ast.parse(source)
                hits = detect_dangerous_html(tree, str(tsx_file))
                for hit in hits:
                    all_findings.append({
                        'pattern': 'dangerous-html-render',
                        'severity': 'high',
                        'file': str(tsx_file.relative_to(SOURCE_ROOT.parent)),
                        **hit,
                    })
            except Exception:
                pass  # TSX may not parse as valid Python

    return all_findings


def introspect_and_report() -> dict:
    """Main entry point: scan code, create observations for new findings,
    and directly create proposals so findings enter the approval pipeline."""
    _ensure_user()
    findings = scan_source_tree()

    if not findings:
        log_action('introspect', 'code_scan_clean', detail='No anti-patterns found')
        return {'findings': 0, 'new_observations': 0, 'new_proposals': 0}

    conn = get_honcho_db()
    try:
        new_obs = 0

        # Get existing observations to avoid duplicates
        existing = {
            o['content'] for o in conn.execute(
                "SELECT content FROM observations WHERE user_id = ? AND category = 'code_quality'",
                (AGENT_USER_ID,)
            ).fetchall()
        }

        from .evo_models import add_proposal, get_proposals
        # Deduplicate proposals by content
        existing_proposals = {(p['proposal_type'], p['content']) for p in get_proposals()}
        new_proposals = 0

        for f in findings:
            obs_text = f"[code_quality:{f['severity']}] {f['file']}:{f['line']} — {f['pattern']}: {f['detail']}"
            if obs_text not in existing:
                add_observation(conn, AGENT_USER_ID, obs_text, category='code_quality')
                existing.add(obs_text)
                new_obs += 1

            # Always try to create a proposal for each finding (deduplicated against existing proposals)
            severity_map = {'critical': 'rule_add', 'high': 'behavior_improvement', 'medium': 'behavior_improvement'}
            proposal_type = severity_map.get(f['severity'], 'behavior_improvement')
            proposal_content = f"Fix {f['pattern']}: {f['file']}:{f['line']} — {f['detail']}"
            proposal_detail = json.dumps(f, ensure_ascii=False)
            proposal_key = (proposal_type, proposal_content)
            if proposal_key not in existing_proposals:
                add_proposal(proposal_type, proposal_content, proposal_detail, source='introspector')
                existing_proposals.add(proposal_key)
                new_proposals += 1

        summary = f"Found {len(findings)} issues, {new_obs} new observations, {new_proposals} new proposals"
        log_action('introspect', 'code_scan', action_detail=summary)

        return {'findings': len(findings), 'new_observations': new_obs, 'new_proposals': new_proposals}
    finally:
        conn.close()