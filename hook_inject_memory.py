#!/usr/bin/env python3
"""SessionStart hook: inject low-frequency / scenario-tagged memories into session context.

Retrieves the scenario-specific rules that were sunk out of CLAUDE.md into the
metano memory DB, and emits them as a Claude Code SessionStart hook
`additionalContext` JSON payload:

    {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}

Run by Claude Code hooks on SessionStart. Reads the hook input event JSON from
stdin (optional; ignored), never blocks (wraps everything in try/except so a
memory DB hiccup cannot break session startup).

Usage (manual test):
    echo '{}' | PYTHONPATH="${METANO_HOME:-$HOME/.claude/metano}" python3 "${METANO_HOME:-$HOME/.claude/metano}/hook_inject_memory.py"
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Scenario tags to inject at every session start. These correspond to the tags
# used in sink_claude_prefs.py. 'reference' is intentionally NOT included by
# default (Scrapling/CocoIndex/etc. are already in project CLAUDE.md; pull them
# on demand with `search_memories('', tag='reference')` when a task needs them).
INJECT_TAGS = [
    'tooling',        # Edit-tool discipline, tool-param care, tool-fallback
    'edit',           # Edit-specific rules
    'workflow',       # continuation / autonomy / frustration / undercover
    'continuation',   # context-continuation rules
    'code_scan',      # evolution system code_scan throttle
    'evolution',      # memory-system design prefs + code_scan
    'cost',           # cost/budget rules
    'budget',
    'fallback',       # tool-call fallback ladder
    'frustration',    # frustration-detection -> lower autonomy
    'undercover',     # quiet long-running task mode
    'security',       # wide-authorization confirmation
    'memory',         # memory-system time-dimension / quality metrics
]

# F-4 isolation: general Claude Code discipline is injected into every project
# (that's the memory system's design), but metano-specific learned knowledge
# (evolution/memory) only flows into the metano project itself.
GENERAL_TAGS = ['tooling', 'edit', 'workflow', 'undercover', 'security']
METANO_TAGS = ['evolution', 'memory']

# Per-tag result cap. Lower keeps the injected context tight.
PER_TAG_LIMIT = 4
# Hard cap on total injected context length (chars). Avoids bloating the model
# context window every session.
MAX_CHARS = 2000

# P1-2 / 全检3 F6: fail-closed content policy for hook-injected memory.
#
# The hook is executed via a command that itself does ``cd <METANO_HOME> &&
# python3 hook_inject_memory.py``, so the *execution* cwd is always METANO_HOME
# regardless of the real session project. Any injected memory that carries its
# own ``cd`` directive would therefore override the externally-passed cwd
# constraint if that content were ever run as a command, letting it escape the
# intended working directory and write to arbitrary paths. We do NOT try to
# allow-then-relock — any such content is rejected outright (fail-closed).
CD_RE = re.compile(r'\bcd\b')
PATH_ESCAPE_RE = re.compile(r'(\.\.|/etc/|/root/|/home/)')


def _validate_inject_content(content: str) -> str | None:
    """Return a rejection reason if ``content`` must NOT be injected, else None.

    Fail-closed policy applied to every memory line before it enters the
    session context:

    * any ``cd`` directive (``cd /etc``, ``&& cd``, ``; cd``, ``cd /``,
      ``cd ~``, ``cd ..``, ...) -> rejected: the command would override the
      execution cwd and escape the METANO_HOME lock;
    * any path-escape feature (``../``, ``/etc/``, ``/root/``, ``/home/``)
      -> rejected: writing through an absolute/escaping path bypasses the
      working-directory boundary.

    ``ls -la``-style normal commands / behaviour rules pass through unchanged.
    """
    if not content or not content.strip():
        return None
    if CD_RE.search(content):
        return 'unsafe cd command in hook'
    if PATH_ESCAPE_RE.search(content):
        return 'unsafe path escape in content'
    return None


def _log_reject(reason: str, content: str) -> None:
    """Record an explicit REJECTED log line with the injected-content summary."""
    summary = content[:200] if content else ''
    try:
        from metano.log import logger
        logger.warning('REJECTED: %s — content=%r', reason, summary)
    except Exception:
        # Never let a logging hiccup break session startup.
        sys.stderr.write(f'REJECTED: {reason} — content={summary!r}\n')


def _search_memories(*args, **kwargs) -> dict:
    """Lazy ``metano.memory.search_memories`` so ``main()`` is callable from
    tests and the module stays importable standalone."""
    from metano.memory import search_memories
    return search_memories(*args, **kwargs)


def _load_stdin_event() -> dict:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ''
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _cwd_in_metano(cwd: str) -> bool:
    """F-4: whether the session's project is metano (or an allow-listed one).

    Metano-specific tags (evolution/memory) are only injected into allow-listed
    projects. Default allow-list: ``METANO_HOOK_PROJECTS`` env (comma-separated)
    if set, else METANO_HOME and the source repo. Unknown / unparseable cwd →
    fail-closed → only GENERAL tags are injected.
    """
    if not cwd:
        return False  # fail-closed: unknown project gets only GENERAL tags
    allowed = os.environ.get('METANO_HOOK_PROJECTS', '')
    paths = [p.strip() for p in allowed.split(',') if p.strip()] if allowed else [
        os.environ.get('METANO_HOME', '') or os.path.expanduser('~/.claude/metano'),
        os.path.expanduser('~/metano'),
    ]
    try:
        cwd_res = os.path.realpath(cwd)
        for p in paths:
            if not p:
                continue
            base = os.path.realpath(p)
            if cwd_res == base or cwd_res.startswith(base + os.sep):
                return True
    except Exception:
        # P1-2 fail-closed: on error, never leak metano-specific tags.
        return False
    return False


def main() -> None:
    event = _load_stdin_event()
    # F-4 / P1-2: scope metano-specific tags to the metano project.
    #
    # P1-2 (全检3 F6): NEVER fall back to os.getcwd() to decide the project
    # scope. The hook command is ``cd <METANO_HOME> && python3
    # hook_inject_memory.py``, so the process cwd is ALWAYS METANO_HOME — an
    # empty event would otherwise be treated as "inside metano" and leak all 13
    # tags (incl. evolution/memory) into every project session. Only the
    # explicit ``cwd`` field passed by Claude Code reflects the real session
    # project; absent cwd ⇒ fail-closed ⇒ GENERAL tags only.
    cwd = (event.get('cwd') or '').strip()
    active_tags = INJECT_TAGS if _cwd_in_metano(cwd) else GENERAL_TAGS
    seen: set[str] = set()
    lines: list[str] = []
    for tag in active_tags:
        try:
            res = _search_memories('', tag=tag, limit=PER_TAG_LIMIT)
        except Exception:
            # Never let a memory DB error break session startup.
            continue
        for m in res.get('results', []):
            content = (m.get('content') or '').strip()
            if not content or content in seen:
                continue
            # P1-2 fail-closed content policy: reject cd / path-escape content.
            reason = _validate_inject_content(content)
            if reason:
                _log_reject(reason, content)
                continue
            seen.add(content)
            lines.append(f"[{tag}] {content}")

    context = '\n'.join(lines)
    if len(context) > MAX_CHARS:
        context = context[:MAX_CHARS] + '\n...(截断，可按 tag 检索完整规则)'

    # P1-2: wrap injected memory content as untrusted data so the model treats
    # it as data to consider — not as instructions that can override its own
    # behaviour. A memory that itself arrived via prompt-injection must not be
    # able to steer the session (same contract as the gateway's C6 wrap).
    if context:
        context = f'<untrusted_data>\n{context}\n</untrusted_data>'

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == '__main__':
    main()
