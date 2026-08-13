"""Self-modification (self-bootstrapping) for the metano evolution system.

The evolution system can not only maintain its data/rules — it can modify its
own Python code, the way a species mutates and is selected by its environment.

Pipeline: SCAN → GENERATE → VERIFY → (APPROVE) → APPLY → LOG

- **SCAN**      find anti-patterns (code_introspector) and architectural issues
- **GENERATE**  ask the LLM to produce a unified diff (candidate mutation)
- **VERIFY**    apply the diff in an isolated git worktree, run the import check
                and full test suite INSIDE a bubblewrap sandbox (no network,
                tmpfs HOME, read-only source tree, scrubbed environment). This
                is the "environment": a mutation that breaks tests is selected
                out and never touches the main system — and one that tries to
                read secrets / touch the network during import/test cannot.
- **APPROVE**   "tests passed" is deliberately NOT the only safety boundary:
                an automatically-verified candidate is NOT applied unless an
                operator approves it (``approve_mutation``) — unless
                ``SELF_MODIFY_REQUIRE_APPROVAL=0`` is explicitly set.
- **APPLY**     an approved mutation is committed to the source repo, synced to
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
import sys
import tempfile
import threading
import time
from pathlib import Path

from metano.log import logger

# Serializes apply_candidate so two concurrent mutations (e.g. a cron run and a
# web approve) cannot race the same working tree (TOCTOU — audit N5).
_apply_lock = threading.Lock()

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
    text, cost = call_llm(_GEN_SYSTEM, prompt, max_tokens=1200, timeout=45, session_id='self-modify')
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
    """Constitution check: is this file allowed to be mutated?

    Normalizes via ``Path.resolve`` so ``.``/``..`` segments cannot smuggle a
    file past the allow/deny list (e.g. ``metano/../metano/self_modify.py``
    resolves back to the forbidden ``metano/self_modify.py`` — audit H2/N5).
    """
    raw0 = rel_file.replace('\\', '/')
    if raw0.startswith('/'):
        return False                 # absolute paths are never mutable
    raw = raw0.lstrip('/')
    if raw.startswith('tests'):
        return False
    p = _normalize_rel(raw)          # bare introspector path -> metano/<file>
    path = Path(p)
    if not path.is_absolute():
        path = REPO_ROOT / path
    try:
        resolved = path.resolve()
        rel = resolved.relative_to(REPO_ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return False
    if rel.startswith('tests') or rel in FORBIDDEN_PATHS:
        return False
    return rel.startswith(ALLOWED_MUTATION_PREFIXES)


def _git(args: list[str], cwd: Path) -> str:
    """Run git, return stdout. Raises on failure."""
    proc = subprocess.run(
        ['git', *args], capture_output=True, text=True, cwd=str(cwd), timeout=60
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr[:300]}")
    return proc.stdout.strip()


class _SandboxUnavailable(Exception):
    """Raised when the verify sandbox cannot provide OS-level isolation."""


def _interpreter_prefixes() -> list[str]:
    """Host interpreter prefixes to expose read-only inside the verify sandbox.

    Includes the venv root (the directory containing ``pyvenv.cfg``) and the
    base Python prefix, so the sandboxed interpreter can import the project's
    dependencies without exposing METANO_HOME data or credentials.
    """
    prefixes: list[str] = []
    exe = Path(sys.executable).resolve()
    for cand in (exe.parent, exe.parent.parent):
        if (cand / 'pyvenv.cfg').exists():
            prefixes.append(str(cand))
            break
    prefixes.append(str(Path(sys.base_prefix)))
    return list(dict.fromkeys(prefixes))


def _user_site() -> str | None:
    """The host user site-packages dir (e.g. ~/.local/lib/python3.x/site-packages).

    In a venv-less dev checkout the project's deps (pytest, yaml, ...) live
    here; it is hidden by the sandbox's tmpfs HOME, so it is bound read-only
    and added to PYTHONPATH for the verify run. Returns None when absent.
    """
    try:
        import site
        p = site.getusersitepackages()
        if p and Path(p).is_dir():
            return p
    except Exception:
        pass
    return None


def _run_verify_isolated(cmd: list, cwd: Path, timeout: int,
                         extra_env: dict | None = None,
                         rw_binds: tuple = ()) -> subprocess.CompletedProcess:
    """Run a verification command inside a bubblewrap sandbox.

    Isolation: no network (``--unshare-net``), a throwaway tmpfs ``$HOME``, a
    read-only root filesystem, and a scrubbed minimal environment — no
    ``os.environ`` is inherited, so API keys / secrets in the daemon's
    environment never reach the code under test. The mutated source tree and
    the host interpreter's prefixes are bound read-only so the import / test
    suite can actually run.

    Raises ``_SandboxUnavailable`` if bwrap cannot provide isolation (non-Linux
    or bwrap missing) — verification FAILS CLOSED rather than falling back to
    running untrusted code on the host.
    """
    cwd = Path(os.path.abspath(cwd))  # bwrap binds need absolute paths
    if sys.platform != 'linux':
        raise _SandboxUnavailable('bwrap sandbox requires Linux')
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise _SandboxUnavailable('bwrap not found on PATH')
    home = str(Path.home())
    argv = [
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
    ]
    # The mutated source tree must be readable but never writable.
    argv += ['--ro-bind', str(cwd), str(cwd)]
    # Re-expose only the interpreter prefixes that the tmpfs mounts would
    # otherwise hide (a venv under $HOME, a pyenv base under $HOME, ...).
    # Paths already under the read-only `/` are visible and must NOT be
    # re-mounted — bwrap cannot mkdir a mount point on a ro root.
    def _tmpfs_hidden(p: str) -> bool:
        return p == home or p.startswith(home + os.sep) or p.startswith('/tmp/')
    extra_pythonpath: list[str] = []
    for prefix in _interpreter_prefixes():
        if _tmpfs_hidden(prefix):
            argv += ['--ro-bind', prefix, prefix]
    usite = _user_site()
    if usite and _tmpfs_hidden(usite):
        # Make the host user site-packages (project deps in a venv-less dev
        # checkout) visible read-only and reachable via PYTHONPATH.
        argv += ['--ro-bind', usite, usite]
        extra_pythonpath.append(usite)
    # The sandbox python may not resolve the same system site-packages as the
    # host interpreter (e.g. /usr/local/lib/python3.x/site-packages on Fedora
    # is derived from sysconfig, not from site.getsitepackages()). Expose the
    # host's purelib/platlib explicitly so the project's deps are importable.
    try:
        import sysconfig
        for key in ('purelib', 'platlib'):
            p = sysconfig.get_path(key)
            if p and Path(p).is_dir() and p.startswith('/') and not _tmpfs_hidden(p):
                extra_pythonpath.append(p)
    except Exception:
        pass
    # Read-write binds: the throwaway scratch METANO_HOME so the test suite
    # can create its DBs without touching real host data.
    for host, sandbox in rw_binds:
        argv += ['--bind', host, sandbox]
    argv += ['--setenv', 'HOME', home]
    argv += ['--'] + list(cmd)
    env = {
        'PATH': '/usr/local/bin:/usr/bin:/bin',
        'HOME': home,
        'LANG': 'en_US.UTF-8',
        'LC_ALL': 'en_US.UTF-8',
        'TERM': 'dumb',
        'TMPDIR': '/tmp',
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONNOUSERSITE': '1',
    }
    if extra_pythonpath:
        env['PYTHONPATH'] = ':'.join(extra_pythonpath)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        argv, capture_output=True, text=True, cwd=str(cwd), timeout=timeout, env=env,
    )


def _approval_required() -> bool:
    """Whether applying a verified mutation requires explicit human approval.

    'Tests passed' is deliberately NOT the only safety boundary for a mutation
    that then gets committed and synced into the running instance. Default on;
    set ``SELF_MODIFY_REQUIRE_APPROVAL=0`` (or ``false``/``no``/``off``) to skip
    the human checkpoint — an explicit operator decision, not recommended for
    unattended deployments.

    Fail-closed (N3): an EMPTY string — or any unrecognised value — leaves
    approval required.  Approval can only be disabled with an explicit non-empty
    truthy-off value, never by an empty environment variable.
    """
    val = os.environ.get('SELF_MODIFY_REQUIRE_APPROVAL', '1').strip().lower()
    if not val:
        return True  # empty env value must not silently disable the gate
    return val not in ('0', 'false', 'no', 'off')


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
    scratch_home: str | None = None
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

        # The import/test run uses a scratch METANO_HOME (a throwaway host dir
        # bound read-write into the sandbox at the default ~/.claude/metano
        # location), so the code under test never sees the real METANO_HOME
        # (DBs, .env, config, ...). A minimal config + empty schemas are
        # initialized once so the test suite has what it expects.
        scratch_home = tempfile.mkdtemp(prefix='metano-verify-home-')
        sandbox_home = str(Path.home() / '.claude' / 'metano')  # matches the suite's default METANO_HOME
        verify_env = {'METANO_HOME': sandbox_home}
        rw_binds = ((scratch_home, sandbox_home),)
        _init_verify_home = (
            "import os, pathlib, yaml\n"
            "home = pathlib.Path(os.environ['METANO_HOME'])\n"
            "home.mkdir(parents=True, exist_ok=True)\n"
            "cfg = {'auth': {'jwt_secret': 'metano-verify-scratch-secret-0123456789abcdef'}}\n"
            "(home / 'gateway_config.yaml').write_text(yaml.safe_dump(cfg))\n"
            "from metano.db import init_db as _d\n_d()\n"
            "from metano.evo_models import init_db as _e\n_e()\n"
            "from metano.memory import _get_conn as _m\n"
            "_c = _m(); _c.__enter__(); _c.__exit__(None, None, None)\n"
        )
        init_script = worktree / '_verify_init.py'
        init_script.write_text(_init_verify_home)
        try:
            try:
                init_proc = _run_verify_isolated(
                    [sys.executable, str(init_script)], worktree, timeout=120,
                    extra_env=verify_env, rw_binds=rw_binds,
                )
            except _SandboxUnavailable as e:
                return {'verdict': 'rejected', 'reason': f'verify sandbox unavailable: {e}'}
        finally:
            init_script.unlink(missing_ok=True)
        if init_proc.returncode != 0:
            return {'verdict': 'rejected',
                    'reason': f'verify DB init failed: {(init_proc.stdout or "")[-200:]}{(init_proc.stderr or "")[-200:]}'}

        # Import check: every metano module must import under the mutated tree.
        # Runs INSIDE a bubblewrap sandbox (no network, tmpfs HOME, read-only
        # source, scrubbed env) so a hostile mutation cannot read secrets, hit
        # the network, or write to the host while it is being imported/tested.
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
            try:
                import_proc = _run_verify_isolated(
                    [sys.executable, check_script], worktree, timeout=120,
                    extra_env=verify_env, rw_binds=rw_binds,
                )
            except _SandboxUnavailable as e:
                return {'verdict': 'rejected', 'reason': f'verify sandbox unavailable: {e}'}
        finally:
            os.unlink(check_script)
        if import_proc.returncode != 0:
            return {'verdict': 'rejected', 'reason': f'import failed: {import_proc.stdout[:300]}{import_proc.stderr[:300]}'}

        # Full test suite (also sandboxed).
        try:
            test_proc = _run_verify_isolated(
                [sys.executable, '-m', 'pytest', 'tests/', '-q',
                 '-p', 'no:cacheprovider', '--ignore=tests/test_cron_daemon.py'],
                worktree, timeout=300, extra_env=verify_env, rw_binds=rw_binds,
            )
        except _SandboxUnavailable as e:
            return {'verdict': 'rejected', 'reason': f'verify sandbox unavailable: {e}'}
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
        if scratch_home:
            shutil.rmtree(scratch_home, ignore_errors=True)


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
        with _apply_lock:
            # N5 (TOCTOU): re-check the diff still applies to the *current*
            # tree right before mutating it — a file that changed after verify
            # will be caught here and rejected instead of clobbered.
            check_proc = subprocess.run(
                ['git', 'apply', '--check', diff_path],
                capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30,
            )
            if check_proc.returncode != 0:
                return {'status': 'rejected',
                        'reason': f'apply no longer applies cleanly: {check_proc.stderr[:300]}'}
            apply_proc = subprocess.run(
                ['git', 'apply', '--whitespace=nowarn', diff_path],
                capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30,
            )
            if apply_proc.returncode != 0:
                return {'status': 'rejected', 'reason': f'apply failed: {apply_proc.stderr[:300]}'}
            # N5 (TOCTOU): after mutating, re-verify the resolved target is
            # still a constitution-allowed regular file under REPO_ROOT (guards
            # against the file being swapped for a symlink / escape mid-apply).
            if not _allowed_to_mutate(rel_file):
                subprocess.run(['git', 'checkout', '--', rel_file],
                               capture_output=True, cwd=str(REPO_ROOT), timeout=30)
                return {'status': 'rejected',
                        'reason': f'post-apply constitution check failed: {rel_file}'}
            src = (REPO_ROOT / rel_file).resolve()
            if not (src.is_file() and src.is_relative_to(REPO_ROOT.resolve())):
                subprocess.run(['git', 'checkout', '--', rel_file],
                               capture_output=True, cwd=str(REPO_ROOT), timeout=30)
                return {'status': 'rejected',
                        'reason': f'post-apply target is not a file under repo: {rel_file}'}
            # Commit.
            msg = f"self-modify: {candidate.get('issue', {}).get('pattern', 'fix')}"
            _git(['add', rel_file], REPO_ROOT)
            _git(['commit', '-m', msg], REPO_ROOT)
            commit_hash = _git(['rev-parse', 'HEAD'], REPO_ROOT)
            # Sync the mutated file to the runtime instance.
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
    status = {'verified': 'verified', 'applied': 'applied', 'rejected': 'rejected',
              'reverted': 'reverted', 'pending_approval': 'pending_approval'}.get(verdict, 'candidate')
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
        if _approval_required():
            # Human approval is the final safety gate: the candidate passed
            # verification but is NOT auto-applied. An operator reviews and
            # calls approve_mutation(event_id) to apply (or reject) it.
            record_event(candidate, 'pending_approval', event_id=event_id)
            result['pending_approval'] = result.get('pending_approval', 0) + 1
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


def approve_mutation(event_id: int, approved: bool = True) -> dict:
    """Human approval checkpoint for a verified, pending mutation.

    A candidate that passed verification is not applied automatically (see
    ``_approval_required``). This is the explicit operator step that turns a
    ``pending_approval`` event into an applied mutation (commit + sync to the
    runtime instance). ``approved=False`` rejects it instead.

    Returns {'status': 'applied'|'rejected'|'not_found'|'wrong_state', ...}.
    """
    from .evo_models import get_self_modify_event, update_self_modify_event
    event = get_self_modify_event(event_id)
    if not event:
        return {'status': 'not_found'}
    if not approved:
        update_self_modify_event(event_id, status='rejected')
        return {'status': 'rejected'}
    if event['status'] != 'pending_approval':
        return {'status': 'wrong_state', 'current': event['status']}
    issue = event.get('issue') or ''
    candidate = {
        'file': event.get('file', ''),
        'diff': event.get('diff', ''),
        'issue': {'pattern': issue} if isinstance(issue, str) else (issue or {}),
    }
    if not candidate['file'] or not candidate['diff']:
        return {'status': 'wrong_state', 'current': 'event missing file/diff'}
    applied = apply_candidate(candidate, event_id)
    if applied['status'] == 'applied':
        record_event(candidate, 'applied', commit_hash=applied.get('commit_hash', ''), event_id=event_id)
        return {'status': 'applied', 'commit_hash': applied['commit_hash']}
    record_event(candidate, 'rejected', event_id=event_id)
    return applied


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
