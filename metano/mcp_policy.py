"""Tool-tiering policy for the remote read-only MCP surface of metano.

Defines which of the tools exposed by :mod:`metano.mcp_server` may be exposed
over the remote Streamable HTTP endpoint (see :mod:`metano.mcp_http`).

Tiers
-----
- ``read``         — pure queries; safe to expose to a remote (possibly
                     untrusted) MCP host.
- ``write``        — mutates local state (DB rows, config files); may only be
                     invoked by the local stdio server.
- ``destructive``  — dangerous / privileged: executes code, spawns agents,
                     drives the browser, mutates knowledge/personality, or
                     triggers side effects. Never exposed remotely.

The list of tool names below is derived from the ``@mcp.tool()`` decorated
functions actually present in ``metano/mcp_server.py`` (do not guess).
"""

# Read-only whitelist exposed to remote MCP hosts. Every name here is a real
# @mcp.tool() function in metano/mcp_server.py. Deliberately excludes anything
# with write/destructive side effects (see DESTRUCTIVE_TOOLS).
READ_TOOLS: list[str] = [
    # session / search
    "session_search",
    "session_list",
    "session_get",
    # analytics queries
    "analytics_summary",
    "analytics_daily",
    # evolution status / log / pending suggestions
    "evolution_status",
    "evolution_log",
    "evolution_suggestions",
    # skills viewing
    "skills_list",
    "skill_view",
    # model provider list
    "model_list",
    # web / search
    "web_search",
    # memory queries
    "memory_search",
    # knowledge base queries
    "knowledge_search",
    "knowledge_list",
    # security status
    "security_status",
]

# Read-only tools that read INSTANCE-wide data (no per-user column): the
# knowledge base, evolution log/suggestions, skill bodies, model config, memory
# and honcho stores.  They stay registered on the remote surface so an
# admin-read token can use them, but the tool functions refuse user-level remote
# tokens (see mcp_server._instance_data_denied).  Kept in one place so the
# policy and the implementation cannot drift (audit H7).
INSTANCE_READ_TOOLS: frozenset[str] = frozenset({
    "knowledge_search", "knowledge_list",
    "evolution_status", "evolution_log", "evolution_suggestions",
    "skill_view",
    "model_list",
    "memory_search", "memory_stats", "memory_timeline", "memory_detail",
    "honcho_profile", "honcho_beliefs",
    "security_status",
})


def is_instance_read(name: str) -> bool:
    """True when ``name`` reads instance-wide data (admin-read scope required)."""
    return name in INSTANCE_READ_TOOLS


# Dangerous / privileged tools. These must NEVER be exposed to a remote host:
# they execute code, spawn sub-agents, drive the browser, mutate the knowledge
# base / personality / beliefs, trigger cron jobs, or cause real-world side
# effects (Home Assistant, TTS audio, image generation billing).
DESTRUCTIVE_TOOLS: list[str] = [
    "code_run",                 # arbitrary code execution
    "agent_spawn",              # spawns parallel Claude Code instances
    "browser_navigate",         # drives the local browser
    "browser_screenshot",       # drives the local browser
    "browser_click",            # drives the local browser (clicking)
    "browser_fill",             # fills forms in the local browser
    "browser_evaluate",         # executes arbitrary JS in the browser
    "browser_get_content",      # navigates the local browser
    "home_control",             # controls Home Assistant entities
    "cron_trigger",             # runs `claude -p` subprocess
    "evolution_run",            # runs a full evolution cycle
    "evolution_approve",        # approves suggestions
    "evolution_reject",         # rejects suggestions
    "image_generate",           # paid image generation side effect
    "voice_speak",              # plays audio on the host
    "skill_manage",             # create/edit/patch/delete skills on disk
    "personality_set",          # overwrites ~/CLAUDE.md
    "personality_apply",        # applies a staged personality (F-1: was unregistered)
    "honcho_compress",          # merges/removes user beliefs
    "memory_compress",          # merges/removes memories
]

# Full tier map for every @mcp.tool() function in metano/mcp_server.py.
ALL_TOOL_TIERS: dict[str, str] = {
    # session / search
    "session_search": "read",
    "session_list": "read",
    "session_get": "read",
    # analytics
    "analytics_summary": "read",
    "analytics_daily": "read",
    # cron (read = list, everything else mutates the jobs file / runs things)
    "cron_list": "read",
    "cron_add": "write",
    "cron_remove": "write",
    "cron_pause": "write",
    "cron_resume": "write",
    "cron_trigger": "destructive",
    "reindex": "write",
    # personality
    "personality_list": "read",
    "personality_set": "destructive",
    "personality_apply": "destructive",
    "personality_current": "read",
    # curator
    "curator_report": "write",  # dry_run=False auto-fixes memory files
    # x / web search
    "x_search": "read",
    "web_search": "read",
    "web_search_tavily": "read",
    # honcho (belief/observation store)
    "honcho_observe": "write",
    "honcho_profile": "read",
    "honcho_dialectic": "write",
    "honcho_beliefs": "read",
    "honcho_compress": "destructive",
    # voice
    "voice_speak": "destructive",
    "voice_list": "read",
    # evolution
    "evolution_status": "read",
    "evolution_run": "destructive",
    "evolution_suggestions": "read",
    "evolution_approve": "destructive",
    "evolution_reject": "destructive",
    "evolution_log": "read",
    # skills
    "skills_list": "read",
    "skill_view": "read",
    "skill_manage": "destructive",
    "skill_bundle": "read",
    # browser (all privileged)
    "browser_navigate": "destructive",
    "browser_screenshot": "destructive",
    "browser_click": "destructive",
    "browser_fill": "destructive",
    "browser_evaluate": "destructive",
    "browser_get_content": "destructive",
    # code execution
    "code_run": "destructive",
    # sub-agents
    "agent_spawn": "destructive",
    "agent_status": "read",
    "agent_result": "read",
    # image
    "image_generate": "destructive",
    "image_describe": "destructive",  # reads arbitrary files (path traversal) — must not be in read-only remote whitelist
    # model router
    "model_list": "read",
    # knowledge base
    "knowledge_ingest": "write",
    "knowledge_search": "read",
    "knowledge_list": "read",
    # security
    "security_check": "read",
    "security_status": "read",
    # kanban
    "kanban_board": "write",  # has create action; list-only via action arg
    "kanban_task": "write",   # has add/move/delete actions
    # home assistant
    "home_control": "destructive",
    "home_status": "read",
    # memory
    "memory_add": "write",
    "memory_search": "read",
    "memory_stats": "read",
    "memory_compress": "destructive",
    "memory_timeline": "read",
    "memory_detail": "read",
}


def tool_tier(name: str) -> str:
    """Return the tier for a tool name, or 'unknown' if not classified."""
    return ALL_TOOL_TIERS.get(name, "unknown")


def is_read_only(name: str) -> bool:
    """True if the tool is classified as read-only (safe for remote exposure)."""
    return tool_tier(name) == "read"
