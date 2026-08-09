"""Message router: routes messages between platforms and Claude Code."""
import asyncio
import json
import time
import hashlib
from pathlib import Path
from dataclasses import dataclass, field
from metano.log import logger
GATEWAY_CONFIG = Path.home() / '.claude' / 'metano' / 'gateway_config.yaml'
SESSIONS_DIR = Path.home() / '.claude' / 'metano' / 'gateway_sessions'

@dataclass
class GatewaySession:
    """Tracks a conversation session with a user on a platform."""
    platform: str
    user_id: str
    session_id: str = ''
    last_active: float = 0.0
    message_count: int = 0
    history: list[dict] = field(default_factory=list)

class MessageRouter:

    def __init__(self):
        self.sessions: dict[str, GatewaySession] = {}
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self.max_idle_minutes = 30
        self.max_history = 50

    def _session_key(self, platform: str, user_id: str) -> str:
        return f'{platform}:{user_id}'

    def get_or_create_session(self, platform: str, user_id: str) -> GatewaySession:
        key = self._session_key(platform, user_id)
        if key in self.sessions:
            session = self.sessions[key]
            if time.time() - session.last_active > self.max_idle_minutes * 60:
                session.session_id = ''
                session.history = []
                session.message_count = 0
            return session
        session = GatewaySession(platform=platform, user_id=user_id)
        self.sessions[key] = session
        return session

    def inject_history(self, platform: str, user_id: str, history: list[dict]):
        """Inject conversation history into a session (for session resume from web)."""
        session = self.get_or_create_session(platform, user_id)
        if not session.history:
            session.history = history[-self.max_history * 2:]

    async def route_message(self, platform: str, user_id: str, message: str) -> str:
        """Route a message from a platform user to Claude Code and return the response."""
        from ..security import security
        check = security.check_message(user_id, message)
        if not check['allowed']:
            return f"⚠️ {check['reason']}"
        session = self.get_or_create_session(platform, user_id)
        skill_prefix = ''
        remaining_message = message
        if message.startswith('/'):
            skill_prefix, remaining_message = self._resolve_skill_command(message)
        prompt = self._build_prompt(session, remaining_message)
        response = await self._call_claude(prompt, session, skill_prefix=skill_prefix)
        session.last_active = time.time()
        session.message_count += 1
        session.history.append({'role': 'user', 'content': message})
        session.history.append({'role': 'assistant', 'content': response})
        if len(session.history) > self.max_history * 2:
            session.history = session.history[-self.max_history * 2:]
        try:
            from ..evolution import track_rule_adherence
            track_rule_adherence(session.history)
        except Exception:
            logger.exception()
        return response

    def _resolve_skill_command(self, message: str) -> tuple[str, str]:
        """Parse slash command, load skill, return (skill_content, remaining_message)."""
        parts = message.split(None, 1)
        command = parts[0]
        args = parts[1] if len(parts) > 1 else ''
        try:
            from ..skills.loader import SkillLoader
            loader = SkillLoader()
            skill = loader.find_by_trigger(command)
            if not skill:
                return ('', message)
            content = loader.get_content(skill.name, variables={'SKILL_DIR': str(skill.path.parent)})
            prefix = f'[Skill Activated: {skill.name}]\n{content}\n\n---\n\n'
            user_msg = args if args else f"(Skill '{skill.name}' activated. Following its instructions.)"
            return (prefix, user_msg)
        except Exception:
            logger.exception()
            return ('', message)

    def _build_prompt(self, session: GatewaySession, message: str) -> str:
        """Build a prompt with conversation context."""
        if not session.history:
            return message
        context_parts = []
        for msg in session.history[-10:]:
            role = 'User' if msg['role'] == 'user' else 'Assistant'
            context_parts.append(f"{role}: {msg['content'][:500]}")
        context = '\n'.join(context_parts)
        return f'Previous conversation:\n{context}\n\nUser: {message}'

    # Categories that should never be injected into system context
    _SKIP_CATEGORIES = {'tool_error', 'correction', 'code_quality', 'self_reflection'}
    _MAX_CONTEXT_CHARS = 3000

    def _build_system_context(self) -> str:
        """Build system context from Honcho profile and memory for claude -p."""
        context_parts = []
        try:
            from ..honcho.models import get_honcho_db, get_profile, get_user, create_user
            conn = get_honcho_db()
            try:
                if not get_user(conn, 'default'):
                    create_user(conn, user_id='default')
                profile = get_profile(conn, 'default')
                beliefs = profile.get('beliefs', [])
                if beliefs:
                    lines = ['## User Profile']
                    for b in beliefs:
                        if b.get('contradicted'):
                            continue
                        cat = b.get('category', '')
                        if cat in self._SKIP_CATEGORIES:
                            continue
                        content = b['content']
                        if len(content) > 200:
                            content = content[:197] + '...'
                        lines.append(f"- [{cat}] {content}")
                    if len(lines) > 1:
                        context_parts.append('\n'.join(lines))
                try:
                    from ..honcho.models import get_observations
                    obs = get_observations(conn, 'default', limit=5)
                    if obs:
                        lines = ['## Recent Observations']
                        for o in obs:
                            cat = o.get('category', '')
                            if cat in self._SKIP_CATEGORIES:
                                continue
                            content = o['content']
                            if len(content) > 150:
                                content = content[:147] + '...'
                            lines.append(f"- [{cat}] {content}")
                        if len(lines) > 1:
                            context_parts.append('\n'.join(lines))
                except Exception:
                    logger.exception("router: get_observations failed")
            finally:
                conn.close()
        except Exception:
            logger.exception("router: honcho context build failed")
        try:
            from pathlib import Path
            memory_index = Path.home() / '.claude' / 'projects' / '-home-dk' / 'memory' / 'MEMORY.md'
            if memory_index.exists():
                content = memory_index.read_text().strip()
                if content and len(content) < 800:
                    context_parts.append(f'[Memory Index]\n{content}')
        except Exception:
            logger.exception()
        try:
            from pathlib import Path
            claude_md = Path.home() / 'CLAUDE.md'
            if claude_md.exists():
                content = claude_md.read_text().strip()
                if content and len(content) < 1000:
                    context_parts.append(f'[Project Instructions]\n{content}')
        except Exception:
            logger.exception()
        result = '\n\n'.join(context_parts)
        if len(result) > self._MAX_CONTEXT_CHARS:
            result = result[:self._MAX_CONTEXT_CHARS - 3] + '...'
        return result

    # Tools allowed for gateway (non-interactive) sessions
    _GATEWAY_ALLOWED_TOOLS = [
        'Bash', 'Read', 'Edit', 'Write', 'Glob', 'Grep',
        'WebSearch', 'WebFetch',
        'mcp__tavily__tavily_search', 'mcp__tavily__tavily_extract',
        'mcp__tavily__tavily_crawl', 'mcp__tavily__tavily_research',
        'mcp__tavily__tavily_map',
        'mcp__metano__browser_navigate',
        'mcp__metano__browser_screenshot',
        'mcp__metano__browser_click',
        'mcp__metano__browser_type',
        'mcp__metano__hot_list',
        'mcp__metano__memory_search',
        'mcp__metano__memory_add',
        'mcp__metano__knowledge_search',
    ]

    async def _call_claude(self, prompt: str, session: GatewaySession, skill_prefix: str='') -> str:
        """Call Claude Code CLI with the prompt (async, non-blocking).

        Gateway sessions are non-interactive (no TTY), so we bypass
        permission prompts and pre-approve a tool allowlist.
        """
        import shutil
        claude_bin = shutil.which('claude') or '/usr/local/bin/claude'
        system_ctx = self._build_system_context()
        context_layers = []
        if skill_prefix:
            context_layers.append(skill_prefix)
        if system_ctx:
            context_layers.append(system_ctx)
        if context_layers:
            combined = '\n\n'.join(context_layers)
            prompt = f"{combined}\n\n---\n\nUser message: {prompt}\n\nRespond in Chinese."
        cmd = [
            claude_bin, '-p', prompt,
            '--dangerously-skip-permissions',
            '--allowedTools', ','.join(self._GATEWAY_ALLOWED_TOOLS),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
            response = stdout.decode().strip()
            if not response and stderr:
                response = f'Error: {stderr.decode()[:200]}'
            return response or '(no response)'
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 'Response timed out. Please try again.'
        except Exception as e:
            logger.exception()
            return f'Error: {str(e)}'

    def reset_session(self, platform: str, user_id: str):
        """Reset a user's conversation session."""
        key = self._session_key(platform, user_id)
        if key in self.sessions:
            self.sessions[key].session_id = ''
            self.sessions[key].history = []
            self.sessions[key].message_count = 0
router = MessageRouter()