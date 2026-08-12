"""Claude Code hook dispatcher for metano.

Hooks are configured in settings.local.json and receive event data via stdin.
Each handler extracts relevant information and stores it as a memory observation.
Tracks cross-event tool call state to evaluate behavior rule effectiveness.
"""
import json
import os
import sys
import time

from .paths import EVOLUTION_DIR as _STATE_DIR

_STATE_FILE = _STATE_DIR / 'hook_state.json'

def _load_state() -> dict:
    """Load cross-event hook state (last tool, session_id, edit sequence)."""
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {'last_tool': '', 'last_file': '', 'last_ts': 0, 'session_edits': []}


def _save_state(state: dict):
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


def handle_user_prompt():
    """UserPromptSubmit: capture user message as intent observation."""
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        return
    # F-5 root cause: Claude Code's UserPromptSubmit input uses the ``prompt``
    # field (not ``message``), so reading only ``message`` always returned ''
    # and intent memories were never written. Accept both for compatibility.
    message = data.get('message') or data.get('prompt') or ''
    if not message or len(message) < 10:
        return
    from .memory import add_memory
    add_memory(f'[intent] {message[:500]}', category='intent', importance=0.3)


def handle_post_tool_use():
    """PostToolUse: track tool calls and evaluate rule effectiveness.

    Fires on ALL tool types to enable sequence-based rule checking
    (e.g., "Read before Edit", "verify after Edit").
    """
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        return

    tool_name = data.get('tool_name', '')
    tool_input = data.get('tool_input', {})
    file_path = tool_input.get('file_path', '')

    # Note: raw Edit/Write operations are intentionally NOT logged to memory —
    # they accumulated as low-value '[action]' noise (the majority of the memory
    # store) that the evolution system never consumes. Rule-effectiveness
    # tracking below (agent_rules in evo.db) is the meaningful signal.
    _evaluate_rule_effectiveness(data)


def _evaluate_rule_effectiveness(event_data: dict):
    """Check if behavior rules were followed and update effectiveness scores."""
    try:
        from .evo_models import get_rules, update_rule_effectiveness
        tool_name = event_data.get('tool_name', '')
        tool_input = event_data.get('tool_input', {})
        file_path = tool_input.get('file_path', '')

        state = _load_state()
        prev_tool = state.get('last_tool', '')
        prev_file = state.get('last_file', '')

        # Update state BEFORE checking rules so current event is "previous" for next
        new_state = {
            'last_tool': tool_name,
            'last_file': file_path,
            'last_ts': time.time(),
            'session_edits': state.get('session_edits', []),
        }

        # Track session edits for cross-file sync detection
        edits = state.get('session_edits', [])
        if tool_name in ('Edit', 'Write') and file_path:
            # Reset session edits if >5min since last tool call (new session)
            if time.time() - state.get('last_ts', 0) > 300:
                edits = []
            if file_path not in edits:
                edits.append(file_path)
        new_state['session_edits'] = edits

        # Load active behavior rules
        rules = get_rules(kind='behavior', active_only=True)
        if not rules:
            _save_state(new_state)
            return

        for rule in rules:
            content = rule.get('content', '')
            rule_id = rule.get('id')
            if not rule_id:
                continue
            success = _check_rule_followed(content, tool_name, file_path,
                                           prev_tool, prev_file, edits, event_data)
            # A4: only the FIRST decisive rule per event gets its counter
            # updated. Previously EVERY matching rule was incremented on the
            # same event, so overlapping rules (e.g. several "read before edit"
            # / "verify after edit" variants) accumulated in perfect lockstep
            # (1420/954/466 — one Edit flushed all three). Each event now
            # contributes to at most one rule.
            if success is not None:
                update_rule_effectiveness(rule_id, success=success)
                break

        _save_state(new_state)
    except Exception:
        logger = __import__('metano.log', fromlist=['logger']).logger
        logger.debug("rule effectiveness evaluation failed", exc_info=True)


def _check_rule_followed(rule_content: str, tool_name: str, file_path: str,
                         prev_tool: str, prev_file: str, session_edits: list,
                         event_data: dict) -> bool | None:
    """Check if a behavior rule was followed using cross-event state.

    Returns True, False, or None (not applicable to this event).
    """
    c = rule_content.lower()
    tool_input = event_data.get('tool_input', {})

    # Rule: "使用Edit前必须先读取文件"
    if 'read' in c and ('edit' in c or '修改' in c):
        if tool_name in ('Edit', 'Write'):
            # Was the previous tool a Read on the same file?
            if prev_tool == 'Read' and prev_file == file_path:
                return True
            return False

    # Rule: "修改代码后必须验证" / "必须curl验证" / "必须用curl" / "必须测试"
    if ('验证' in c or 'verify' in c or 'curl' in c or '测试' in c or 'test' in c):
        if tool_name in ('Edit', 'Write'):
            return None  # Defer: next tool call may be verification
        if tool_name in ('Read', 'Bash') and prev_tool in ('Edit', 'Write'):
            return True  # Verification after edit

    # Rule: "修改后端必须同步改前端" — check if the CURRENT tool was an Edit/Write
    # that touches a backend file AND there's also a frontend edit in this session
    if ('前端' in c or 'frontend' in c or 'ts' in c or 'type' in c or 'hook' in c):
        if tool_name in ('Edit', 'Write') and file_path:
            is_backend = file_path.endswith('.py') and 'test_' not in os.path.basename(file_path)
            is_frontend = any(('.ts' in p or '.tsx' in p or '.vue' in p) for p in session_edits)
            if is_backend:
                # Backend edit — check if there's a frontend edit in same session
                return is_frontend
            if '.ts' in file_path or '.tsx' in file_path:
                return True  # Frontend file, rule satisfied

    # Rule: "使用中文回复" — check user message content
    if '中文' in c:
        message = event_data.get('message', '')
        if message:
            import re
            return bool(re.search(r'[一-鿿]', message))

    # Rule: "简洁、结构化"
    if '简洁' in c or '结构化' in c:
        message = event_data.get('message', '')
        if message and len(message) > 2000:
            return False
        if message:
            return True

    # Default: can't evaluate from a single event
    return None


DISPATCH = {
    'UserPromptSubmit': handle_user_prompt,
    'PostToolUse': handle_post_tool_use,
}


if __name__ == '__main__':
    hook_type = sys.argv[1] if len(sys.argv) > 1 else 'unknown'
    handler = DISPATCH.get(hook_type)
    if handler:
        handler()
