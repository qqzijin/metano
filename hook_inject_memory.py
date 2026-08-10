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
    echo '{}' | PYTHONPATH=/home/dk/.claude/metano python3 /home/dk/.claude/metano/hook_inject_memory.py
"""
import json
import os
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

# Per-tag result cap. Lower keeps the injected context tight.
PER_TAG_LIMIT = 4
# Hard cap on total injected context length (chars). Avoids bloating the model
# context window every session.
MAX_CHARS = 2000


def _load_stdin_event() -> dict:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ''
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def main() -> None:
    event = _load_stdin_event()
    seen: set[str] = set()
    lines: list[str] = []
    for tag in INJECT_TAGS:
        try:
            res = search_memories('', tag=tag, limit=PER_TAG_LIMIT)
        except Exception:
            # Never let a memory DB error break session startup.
            continue
        for m in res.get('results', []):
            content = (m.get('content') or '').strip()
            if not content or content in seen:
                continue
            seen.add(content)
            lines.append(f"[{tag}] {content}")

    context = '\n'.join(lines)
    if len(context) > MAX_CHARS:
        context = context[:MAX_CHARS] + '\n...(截断，可按 tag 检索完整规则)'

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == '__main__':
    # Deferred import so the module can be syntax-checked / documented standalone.
    from metano.memory import search_memories  # noqa: E402
    main()
