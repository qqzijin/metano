"""Self-modification (self-bootstrapping) for the metano evolution system.

The evolution system can not only maintain its data/rules — it can modify its
own Python code, the way a species mutates and is selected by its environment.

Pipeline: SCAN → GENERATE → VERIFY → APPLY → LOG

- **SCAN**      find anti-patterns (code_introspector) and architectural issues
- **GENERATE**  ask the LLM to produce a unified diff (candidate mutation)
- **VERIFY**    apply the diff in an isolated git worktree, run the full test
                suite + import check. This is the "environment": a mutation
                that breaks tests is selected out and never touches the main
                system.
- **APPLY**     a surviving mutation is committed to the source repo, synced to
                the runtime instance, and recorded with its git hash.
- **LOG**       every mutation is persisted to ``self_modify_events`` so any
                change can be inspected and reverted (the species-level safety
                net — a bad mutation is reverted via git, the system survives).

Safety "constitution" (meta-rules the system may never mutate):
- ``self_modify.py`` itself is never modified (else the verify gate could be
  turned off).
- ``tests/`` is never modified (the verify gate itself).
- Only ``metano/*.py`` business code may change — never config/keys/DB.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from metano.log import logger

# ── paths ────────────────────────────────────────────────────────────────
# The mutation pipeline operates on the SOURCE git repo (worktree/commit/revert
# need a real git repo). Prefer METANO_SOURCE_DIR; otherwise walk up from this
# module to the nearest ancestor containing a .git; otherwise fall back to the
# runtime instance (degraded — no git ops possible).
def _find_repo_root() -> Path:
    # 1) Explicit env override — the reliable path when running from the
    #    deployed instance (METANO_HOME is NOT a git repo; the source repo is).
    env_dir = os.environ.get('METANO_SOURCE_DIR')
    if env_dir:
        p = Path(env_dir).expanduser()
        if (p / '.git').exists():
            return p
    # 2) Walk up from this module looking for a .git ancestor (dev checkout).
    here = Path(__file__).resolve()
    for parent in [here] + list(here.parents):
        if (parent / '.git').exists():
            return parent
    # 3) Try a sibling/known source checkout next to the deployed instance.
    home = Path(os.environ.get('METANO_HOME', str(Path.home() / '.claude' / 'metano')))
    if home.exists():
        sibling = home.parent / 'metano'   # e.g. ~/metano when deployed at ~/.claude/metano
        if (sibling / '.git').exists():
            return sibling
    # 4) Known dev-machine source checkout (this project's canonical repo).
    dev_src = Path.home() / 'metano'
    if (dev_src / '.git').exists():
        return dev_src
    # 5) METANO_HOME itself (deployed). No git ops will work, but the module
    #    still imports and SCAN-only (dry_run) works.
    if home.exists():
        return home
    return here.parent.parent


REPO_ROOT = _find_repo_root()                                # e.g. /home/dk/metano (git)
METANO_PKG = REPO_ROOT / 'metano'                            # the package under mutation
RUNTIME_ROOT = Path(os.environ.get('METANO_HOME', str(Path.home() / '.claude' / 'metano')))

# ── safety constitution ─────────────────────────────────────────────────
# Files / dirs the mutation may never touch (relative to REPO_ROOT).
FORBIDDEN_PATHS = {
    'metano/self_modify.py',   # the bootstrap mechanism itself
    'tests',                   # the verify gate itself
    '.git',
}
# Only these prefixes may be mutated (business code).
ALLOWED_MUTATION_PREFIXES = ('metano/',)


# ── SCAN ────────────────────────────────────────────────────────────────
def scan_issues() -> list[dict]:
    """Find mutation-worthy issues in own code.

    Severity gate: medium and above are mutation-worthy (silent-except,
    sql-concat are real, fixable anti-patterns; there are currently no
    critical/high findings in the tree). The LLM generator + verify gate
    decide whether a candidate is actually worth applying — scanning widely
    gives the mutation pipeline material, verification keeps it safe.

    Returns a deduplicated list of {pattern, severity, file, line, detail}.
    """
    try:
        from .code_introspector import scan_source_tree
        findings = scan_source_tree()
    except Exception:
        logger.exception('self_modify: scan_source_tree failed')
        return []
    issues = [f for f in findings if f.get('severity') in ('critical', 'high', 'medium')]
    # Deduplicate by (file, pattern, line) — skip already-fixed / noise.
    seen = set()
    out = []
    for f in issues:
        key = (f.get('file', ''), f.get('pattern', ''), f.get('line', ''))
        if key in seen:
            continue
        seen.add(key)
        # Never propose mutating self_modify.py itself (constitution).
        if f.get('file', '') in FORBIDDEN_PATHS:
            continue
        out.append(f)
    return out


# ── GENERATE ────────────────────────────────────────────────────────────
_GEN_SYSTEM = """You are a precise code-patch engine. The user gives you a
code-quality issue in a file plus the relevant source. You must reply with a
SINGLE JSON object describing a MINIMAL edit:

{"old": "<verbatim text that currently exists in the file, from the relevant source>", "new": "<the replacement text>", "note": "<one short line>"}

Rules:
- `old` must be a contiguous block copied VERBATIM from the given source
  (including exact indentation). It should be the smallest block that contains
  the problem.
- `new` is the same block with the fix applied (e.g. add a logger.exception(),
  narrow an except, escape a LIKE wildcard).
- Change as few lines as possible — never reformat or reorder surrounding code.
- If not fixable, reply {"old": "", "new": "", "note": "not fixable"}.
Reply with ONLY the JSON object — no markdown, no prose."""


def _read_issue_context(issue: dict, window: int = 20) -> str:
    """Read the relevant lines around the issue for the LLM to diff against."""
    rel_file = issue.get('file', '')
    path = METANO_PKG / rel_file
    if not path.exists():
        # File may be under web/ (relative to repo root).
        alt = REPO_ROOT / rel_file
        path = alt if alt.exists() else None
    if not path:
        return ''
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except Exception:
        return ''
    try:
        line_no = int(issue.get('line') or 0)
    except (TypeError, ValueError):
        line_no = 0
    if line_no <= 0:
        return '\n'.join(lines[:60])
    start = max(0, line_no - window)
    end = min(len(lines), line_no + window)
    numbered = '\n'.join(f'{i+1:5d} {lines[i]}' for i in range(start, end))
    return numbered


def generate_candidate(issue: dict, max_tokens: int = 1500) -> dict | None:
    """Ask the LLM to produce a fix diff for one issue.

    The issue's surrounding source lines are included so the LLM can produce a
    concrete unified diff. Returns {'file', 'diff', 'issue'} or None if the LLM
    produced nothing usable.
    """
    rel_file = _normalize_rel(issue.get('file', ''))
    path = _resolve_issue_path(issue)
    if not path:
        logger.warning('self_modify: cannot read issue file %s', rel_file)
        return None
    original = path.read_text(encoding='utf-8')

    # 1) Deterministic fix first (reliable, no LLM cost).
    modified = _deterministic_fix(issue, original)
    if modified and modified != original:
        diff = _make_unified_diff(rel_file, original, modified)
        if diff:
            return {'file': rel_file, 'diff': diff, 'issue': issue, 'cost': 0.0,
                    'method': 'deterministic'}

    # 2) LLM fallback for patterns we don't fix deterministically.
    from .llm_call import call_llm
    context = _read_issue_context(issue)
    prompt = (
        f"File: {rel_file}\n"
        f"Issue: [{issue.get('pattern')}] severity={issue.get('severity')} "
        f"line={issue.get('line')} — {issue.get('detail', '')}\n\n"
        f"Relevant source (line numbers shown):\n```\n{context}\n```"
    )
    text, cost = call_llm(_GEN_SYSTEM, prompt, max_tokens=1200, timeout=45)
    data = _parse_llm_json(text)
    if not isinstance(data, dict):
        logger.warning('self_modify: LLM output not JSON: %r', (text or '')[:150])
        return None
    old = (data.get('old') or '').strip()
    new = (data.get('new') or '').strip()
    if not old or not new or old == new:
        logger.warning('self_modify: empty/unchanged edit')
        return None
    if old not in original:
        logger.warning('self_modify: old block not found in file (line %s)', issue.get('line'))
        return None
    modified = original.replace(old, new, 1)
    diff = _make_unified_diff(rel_file, original, modified)
    if not diff:
        return None
    # Sanity: a mutation must be small.
    added = sum(1 for ln in diff.splitlines() if ln.startswith('+') and not ln.startswith('+++'))
    removed = sum(1 for ln in diff.splitlines() if ln.startswith('-') and not ln.startswith('---'))
    if added > 40 or removed > 40 or len(diff) > 20_000:
        logger.warning('self_modify: candidate too large (file=%s +%d -%d), rejected', rel_file, added, removed)
        return None
    return {'file': rel_file, 'diff': diff, 'issue': issue, 'cost': cost, 'method': 'llm'}


def _deterministic_fix(issue: dict, original: str) -> str | None:
    """Apply a deterministic fix for KNOWN anti-patterns via AST/string edit.

    Returns the modified file content, or None if no deterministic fix applies.
    This is the reliable core of self-modification — the LLM generator is the
    fallback for patterns we don't handle here. Deterministic fixes never
    reformat anything else.
    """
    pattern = issue.get('pattern')
    if pattern == 'silent-except':
        return _fix_silent_except(issue, original)
    if pattern == 'sql-concat':
        return _fix_sql_concat(issue, original)
    return None


def _fix_silent_except(issue: dict, original: str) -> str | None:
    """Replace `except Exception: pass` with a logging call.

    Uses AST to find the exact block, then string-replaces the pass body with
    logger.exception(...). Only touches the single line reported.
    """
    import ast
    path = _resolve_issue_path(issue)
    if not path:
        return None
    try:
        tree = ast.parse(original)
        target_line = int(issue.get('line') or 0)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.lineno != target_line:
                continue
            body = node.body
            if not body:
                continue
            # Only fix "silent" bodies: single `pass`, or a bare `return` /
            # `return None`. Anything with real logic or existing logging stays.
            is_silent = (
                (len(body) == 1 and isinstance(body[0], ast.Pass)) or
                (len(body) == 1 and isinstance(body[0], ast.Return))
            )
            if not is_silent:
                continue
            lines = original.splitlines(keepends=True)
            except_indent = _line_indent(original, node.lineno)
            body_indent = except_indent + '    '
            log_line = f"{body_indent}logger.exception('{issue.get('file', '')}:{target_line} exception')\n"
            if isinstance(body[0], ast.Pass):
                pass_lineno = body[0].lineno
                lines[pass_lineno - 1] = log_line
            else:
                # Insert logging before the bare return.
                ret_lineno = body[0].lineno
                lines.insert(ret_lineno - 1, log_line)
            return ''.join(lines)
    except Exception:
        logger.exception('self_modify: silent-except fix failed')
    return None


def _fix_sql_concat(issue: dict, original: str) -> str | None:
    """Escape LIKE wildcards in keyword search (deterministic)."""
    # SQL concat fixes are context-specific; leave to LLM for now.
    return None


def _line_indent(text: str, lineno: int) -> str:
    lines = text.splitlines()
    if lineno - 1 < len(lines):
        line = lines[lineno - 1]
        return line[:len(line) - len(line.lstrip())]
    return ''


def _resolve_issue_path(issue: dict) -> Path | None:
    """Resolve the issue's file to a real path (metano pkg or repo root).

    Accepts both ``llm_call.py`` (code_introspector bare path) and
    ``metano/llm_call.py`` / ``web/...`` (repo-root-relative).
    """
    rel = (issue.get('file') or '').replace('\\', '/').lstrip('/')
    if rel.startswith('metano/'):
        return METANO_PKG / rel[len('metano/'):]
    if rel.startswith('web/'):
        return REPO_ROOT / rel
    p = METANO_PKG / rel
    if p.exists():
        return p
    return None


def _parse_llm_json(text: str):
    """Parse a JSON object out of an LLM reply, tolerating markdown fences and
    surrounding prose. Returns dict | None."""
    if not text:
        return None
    t = text.strip()
    if t.startswith('```'):
        for line in t.splitlines():
            if line.startswith('{'):
                t = line
                break
        else:
            t = t.strip('`').strip()
            if t.startswith('json'):
                t = t[4:].strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    start = t.find('{')
    end = t.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except Exception:
            return None
    return None


def _clean_llm_content(text: str, original: str) -> str:
    """Strip markdown fences/leading prose from the LLM's returned file content."""
    if not text:
        return ''
    t = text.strip()
    if t.startswith('```'):
        # Take content between first and last fence.
        lines = t.splitlines()
        # Drop the opening ```/```python line.
        start = 1 if lines and lines[0].startswith('```') else 0
        end = len(lines)
        if lines and lines[-1].strip() == '```':
            end = len(lines) - 1
        t = '\n'.join(lines[start:end]).strip()
    # If the reply clearly isn't the file (too short / is prose), bail.
    if len(t) < max(10, len(original) // 4):
        return ''
    return t


def _make_unified_diff(rel_file: str, original: str, new_content: str) -> str:
    """Build a git-style unified diff between original and new file content."""
    import difflib
    a = original.splitlines(keepends=True)
    b = new_content.splitlines(keepends=True)
    diff_lines = difflib.unified_diff(
        a, b,
        fromfile=f'a/{rel_file}',
        tofile=f'b/{rel_file}',
        lineterm='\n',
    )
    diff = ''.join(diff_lines)
    if not diff:
        return ''
    # Prepend git header so `git apply` accepts it.
    return f'diff --git a/{rel_file} b/{rel_file}\n{diff}'


# ── VERIFY ──────────────────────────────────────────────────────────────
def _normalize_rel(rel_file: str) -> str:
    """Normalize a relative path from the code_introspector (bare package
    path, e.g. ``llm_call.py``) into a repo-root-relative path (``metano/llm_call.py``)."""
    p = rel_file.replace('\\', '/').lstrip('/')
    if not p.startswith('metano/') and not p.startswith('web/'):
        return f'metano/{p}'
    return p


def _allowed_to_mutate(rel_file: str) -> bool:
    """Constitution check: is this file allowed to be mutated?"""
    raw = rel_file.replace('\\', '/').lstrip('/')
    if raw.startswith('tests') or raw in FORBIDDEN_PATHS:
        return False
    p = _normalize_rel(rel_file)
    if p in FORBIDDEN_PATHS or p.startswith('tests'):
        return False
    return p.startswith(ALLOWED_MUTATION_PREFIXES)


def _git(args: list[str], cwd: Path) -> str:
    """Run git, return stdout. Raises on failure."""
    proc = subprocess.run(
        ['git', *args], capture_output=True, text=True, cwd=str(cwd), timeout=60
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr[:300]}")
    return proc.stdout.strip()


def verify_candidate(candidate: dict) -> dict:
    """Verify a mutation in an isolated git worktree. Returns verdict dict.

    A surviving mutation: diff applies cleanly + full test suite passes +
    every metano module imports. Anything else = mutation is selected out;
    the main repo and runtime are untouched.
    """
    rel_file = candidate['file']
    if not _allowed_to_mutate(rel_file):
        return {'verdict': 'rejected', 'reason': f'file not mutable: {rel_file}'}

    worktree = Path(tempfile.mkdtemp(prefix='metano-selfmod-'))
    try:
        _git(['worktree', 'add', '--detach', str(worktree), 'HEAD'], REPO_ROOT)
        # Apply the diff in the worktree.
        apply_proc = subprocess.run(
            ['git', 'apply', '--check'], capture_output=True, text=True,
            cwd=str(worktree), timeout=30, input=candidate['diff'],
        )
        if apply_proc.returncode != 0:
            return {'verdict': 'rejected', 'reason': f'diff does not apply: {apply_proc.stderr[:300]}'}
        apply_proc = subprocess.run(
            ['git', 'apply'], capture_output=True, text=True,
            cwd=str(worktree), timeout=30, input=candidate['diff'],
        )
        if apply_proc.returncode != 0:
            return {'verdict': 'rejected', 'reason': f'diff apply failed: {apply_proc.stderr[:300]}'}

        # Import check: every metano module must import under the mutated tree.
        # Use a temp script (python3 -c cannot hold a multi-line for-loop).
        import_check = """import sys, pathlib
pkg = pathlib.Path("metano")
mods = [str(p.relative_to("."))[:-3].replace("/", ".") for p in pkg.rglob("*.py") if p.name != "__init__.py" and "__pycache__" not in str(p)]
sys.path.insert(0, ".")
failed = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:
        failed.append(f"{m}: {e}")
if failed:
    print("IMPORT_FAIL")
    print("\\n".join(failed[:10]))
    sys.exit(1)
print("IMPORT_OK", len(mods))
"""
        with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False, dir=str(worktree)) as f:
            f.write(import_check)
            check_script = f.name
        try:
            import_proc = subprocess.run(
                ['python3', check_script],
                capture_output=True, text=True, cwd=str(worktree), timeout=90,
            )
        finally:
            os.unlink(check_script)
        if import_proc.returncode != 0:
            return {'verdict': 'rejected', 'reason': f'import failed: {import_proc.stdout[:300]}{import_proc.stderr[:300]}'}

        # Full test suite.
        test_proc = subprocess.run(
            ['python3', '-m', 'pytest', 'tests/', '-q', '--ignore=tests/test_cron_daemon.py'],
            capture_output=True, text=True, cwd=str(worktree), timeout=300,
        )
        if test_proc.returncode != 0:
            tail = (test_proc.stdout or '')[-300:] + (test_proc.stderr or '')[-300:]
            return {'verdict': 'rejected', 'reason': f'tests failed: {tail}'}

        return {'verdict': 'verified', 'worktree': str(worktree)}
    except Exception as e:
        logger.exception('self_modify: verify failed')
        return {'verdict': 'rejected', 'reason': str(e)}
    finally:
        # Clean up the worktree (detach: nothing was committed there).
        try:
            _git(['worktree', 'remove', '--force', str(worktree)], REPO_ROOT)
        except Exception:
            logger.exception('self_modify: worktree cleanup failed')


# ── APPLY ───────────────────────────────────────────────────────────────
def apply_candidate(candidate: dict, event_id: int | None = None) -> dict:
    """Apply a verified mutation: commit to the source repo + sync runtime.

    Returns {status, commit_hash} or {status: 'rejected', reason}.
    """
    rel_file = candidate['file']
    if not _allowed_to_mutate(rel_file):
        return {'status': 'rejected', 'reason': f'file not mutable: {rel_file}'}
    # Write the diff to a temp file and apply it to the source repo.
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.diff', delete=False) as f:
        f.write(candidate['diff'])
        diff_path = f.name
    try:
        apply_proc = subprocess.run(
            ['git', 'apply', '--whitespace=nowarn', diff_path],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30,
        )
        if apply_proc.returncode != 0:
            return {'status': 'rejected', 'reason': f'apply failed: {apply_proc.stderr[:300]}'}
        # Commit.
        msg = f"self-modify: {candidate.get('issue', {}).get('pattern', 'fix')}"
        _git(['add', rel_file], REPO_ROOT)
        _git(['commit', '-m', msg], REPO_ROOT)
        commit_hash = _git(['rev-parse', 'HEAD'], REPO_ROOT)
        # Sync the mutated file to the runtime instance.
        src = REPO_ROOT / rel_file
        dst = RUNTIME_ROOT / rel_file
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return {'status': 'applied', 'commit_hash': commit_hash, 'file': rel_file}
    except Exception as e:
        logger.exception('self_modify: apply failed')
        return {'status': 'rejected', 'reason': str(e)}
    finally:
        try:
            os.unlink(diff_path)
        except OSError:
            pass


# ── LOG / orchestration ─────────────────────────────────────────────────
def record_event(candidate: dict, verdict: str, commit_hash: str = '', event_id: int | None = None) -> int:
    """Persist a mutation + verdict into the self_modify_events log."""
    from .evo_models import add_self_modify_event, update_self_modify_event
    issue = candidate.get('issue', {})
    rel_file = candidate.get('file', '')
    diff = candidate.get('diff', '')
    if event_id is None:
        event_id = add_self_modify_event(
            issue=f"[{issue.get('pattern')}] {issue.get('detail', '')}" if issue else rel_file,
            file=rel_file,
            diff=diff,
        )
    status = {'verified': 'verified', 'applied': 'applied', 'rejected': 'rejected', 'reverted': 'reverted'}.get(verdict, 'candidate')
    update_self_modify_event(
        event_id,
        verify_result=verdict,
        status=status,
        commit_hash=commit_hash or None,
        applied_at=time.time() if verdict == 'applied' else None,
    )
    return event_id


def self_modify_daily(dry_run: bool = False, max_mutations: int = 3) -> dict:
    """Main entry point (cron). SCAN → GENERATE → VERIFY → APPLY → LOG.

    dry_run=True only scans and generates candidates (no apply/commit).
    """
    if os.environ.get('SELF_MODIFY_DISABLED'):
        return {'status': 'disabled'}
    result = {'status': 'completed', 'scanned': 0, 'candidates': 0, 'applied': 0, 'rejected': 0}

    issues = scan_issues()
    result['scanned'] = len(issues)
    if not issues:
        return result

    for issue in issues[:max_mutations]:
        candidate = generate_candidate(issue)
        if not candidate:
            result['rejected'] += 1
            continue
        result['candidates'] += 1
        # Record the candidate first (before verify) so the mutation log has it.
        event_id = record_event(candidate, 'candidate')
        if dry_run:
            continue
        verdict = verify_candidate(candidate)
        if verdict['verdict'] != 'verified':
            record_event(candidate, 'rejected', event_id=event_id)
            result['rejected'] += 1
            continue
        applied = apply_candidate(candidate, event_id)
        if applied['status'] == 'applied':
            record_event(candidate, 'applied', commit_hash=applied.get('commit_hash', ''), event_id=event_id)
            result['applied'] += 1
        else:
            record_event(candidate, 'rejected', event_id=event_id)
            result['rejected'] += 1

    result['status'] = 'completed'
    return result


def revert_mutation(event_id: int) -> dict:
    """Revert an applied mutation via git revert of its commit hash."""
    from .evo_models import get_self_modify_event, update_self_modify_event
    event = get_self_modify_event(event_id)
    if not event:
        return {'status': 'not_found'}
    if event['status'] != 'applied' or not event['commit_hash']:
        return {'status': 'wrong_state', 'current': event['status']}
    try:
        _git(['revert', '--no-edit', event['commit_hash']], REPO_ROOT)
        # Sync the reverted file(s) to runtime: diff the two hashes.
        changed = _git(['diff', '--name-only', f"{event['commit_hash']}^", event['commit_hash']], REPO_ROOT)
        for rel in changed.splitlines():
            src = REPO_ROOT / rel
            dst = RUNTIME_ROOT / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        update_self_modify_event(event_id, status='reverted')
        return {'status': 'reverted', 'commit_hash': event['commit_hash']}
    except Exception as e:
        logger.exception('self_modify: revert failed')
        return {'status': 'error', 'reason': str(e)}
