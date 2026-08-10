"""Message router: routes messages between platforms and Claude Code."""
import asyncio
import json
import os
import time
import hashlib
from dataclasses import dataclass, field
from typing import Optional
from metano.log import logger
from ..paths import CONFIG_PATH as GATEWAY_CONFIG, GATEWAY_SESSIONS_DIR as SESSIONS_DIR, GATEWAY_LOG, HOME

# Per-user authorization state (mode free/safe, granted/revoked tools).
AUTH_STATE = HOME / 'authorizations.json'


def _log_gateway_event(**fields) -> None:
    """Append a structured event line to the gateway log (used by /api/logs)."""
    try:
        GATEWAY_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {'timestamp': time.time(), **fields}
        with open(GATEWAY_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception:
        logger.exception('gateway log write failed')

@dataclass
class GatewaySession:
    """Tracks a conversation session with a user on a platform."""
    platform: str
    user_id: str
    session_id: str = ''
    db_session_id: str = ''
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
        # Commands take priority over skills; handled here so they work on every channel.
        cmd_response = self._handle_command(platform, user_id, message)
        if cmd_response is not None:
            return cmd_response
        session = self.get_or_create_session(platform, user_id)
        skill_prefix = ''
        remaining_message = message
        if message.startswith('/'):
            skill_prefix, remaining_message = self._resolve_skill_command(message)
        prompt = self._build_prompt(session, remaining_message)
        # Per-user authorization: revoked tools dropped from the default allowlist,
        # explicitly granted tools re-added, and permission prompts skipped only in
        # free mode.
        auth = self._get_auth(f'{platform}:{user_id}')
        base_tools = [t for t in self._GATEWAY_ALLOWED_TOOLS if t not in auth['revoked']]
        tools = base_tools + [t for t in auth['granted'] if t not in base_tools]
        skip = (auth['mode'] == 'free')
        response, in_tok, out_tok, cache_tok = await self._call_claude(
            prompt, session, skill_prefix=skill_prefix, allowed_tools=tools, skip_permissions=skip)
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
        try:
            _log_gateway_event(platform=platform, user_id=user_id, action='message', content=message[:200])
            _log_gateway_event(platform=platform, user_id=user_id, action='reply', content=response[:300])
        except Exception:
            logger.exception('gateway event logging failed')
        try:
            from ..db import persist_exchange
            from ..model_router import model_router
            model = None
            try:
                model = model_router.get_provider().model or None
            except Exception:
                logger.exception()
            sid = persist_exchange(
                session_id=session.db_session_id,
                user_key=f'{platform}:{user_id}',
                platform=platform,
                msg=message,
                response=response,
                usage={'input_tokens': in_tok, 'output_tokens': out_tok, 'cache_read_tokens': cache_tok},
                model=model,
            )
            if sid:
                session.db_session_id = sid
        except Exception:
            logger.exception('persist_exchange failed')
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

    def _handle_command(self, platform: str, user_id: str, message: str) -> Optional[str]:
        """Parse a slash command and return its response, or None if not a command.

        Commands are handled centrally here so they work identically across all
        channels (web / feishu / wechat / telegram / QQ / Discord). Returns None
        for non-commands and for unknown slash commands (which fall through to
        skill resolution).
        """
        if not message.startswith('/'):
            return None
        raw = message[1:].strip()
        if not raw:
            return None
        parts = raw.split()
        command = parts[0].lower()
        args = parts[1:]
        user_key = f'{platform}:{user_id}'
        try:
            if command in ('new', 'reset'):
                self.reset_session(platform, user_id)
                return '✅ 已开启新对话'
            if command == 'help':
                return self._cmd_help()
            if command == 'whoami':
                return self._cmd_whoami(platform, user_id)
            if command == 'profile':
                return self._cmd_profile()
            if command == 'memory':
                return self._cmd_memory(args)
            if command == 'search':
                return self._cmd_search(args)
            if command == 'model':
                return self._cmd_model()
            if command == 'stats':
                return self._cmd_stats(platform, user_id)
            if command == 'cost':
                return self._cmd_cost(user_key)
            if command == 'skills':
                return self._cmd_skills()
            if command == 'perms':
                return self._cmd_perms(user_key)
            if command in ('auto', 'safe'):
                return self._cmd_mode(command, args, user_key)
            if command == 'grant':
                return self._cmd_grant(args, user_key)
            if command == 'revoke':
                return self._cmd_revoke(args, user_key)
        except Exception:
            logger.exception(f'command handler failed: /{command}')
            return '⚠️ 命令执行出错，请稍后再试'
        return None

    def _cmd_help(self) -> str:
        return (
            '📋 可用命令：\n'
            '── 会话 ──\n'
            '/new 或 /reset — 开启新对话\n'
            '/stats — 本会话统计\n'
            '/memory [n] — 查看最近记忆（默认 5 条）\n'
            '── 信息 ──\n'
            '/whoami — 查看身份信息\n'
            '/profile — 查看你的画像\n'
            '/model — 查看当前模型\n'
            '/cost — 查看累计费用\n'
            '/skills — 查看可用技能\n'
            '/search <query> — 搜索知识库\n'
            '── 授权 ──\n'
            '/perms — 查看授权状态\n'
            '/auto [on|off] — 开启免授权模式\n'
            '/safe [on|off] — 开启授权模式\n'
            '/grant <tool> — 授权工具（如 Bash、Edit、Write、Read、WebSearch）\n'
            '/revoke <tool> — 收回工具\n'
            '/help — 显示本帮助'
        )

    def _cmd_whoami(self, platform: str, user_id: str) -> str:
        tier = 'guest'
        try:
            from ..security import security
            status = security.get_user_status(user_id)
            tier = status.get('tier', 'guest') or 'guest'
        except Exception:
            logger.exception()
        return f'平台: {platform} | 用户: {user_id} | 等级: {tier}'

    def _cmd_profile(self) -> str:
        try:
            from ..honcho.models import get_honcho_db, get_profile
            conn = get_honcho_db()
            try:
                profile = get_profile(conn, 'default')
                summary = profile.get('belief_summary') or '暂无画像'
            finally:
                conn.close()
            return f'你的画像:\n{summary}'
        except Exception:
            logger.exception()
            return '画像暂不可用'

    def _cmd_memory(self, args: list) -> str:
        try:
            n = 5
            if args and args[0].isdigit():
                n = max(1, min(int(args[0]), 50))
            from ..memory import _get_conn
            with _get_conn() as conn:
                rows = conn.execute(
                    'SELECT category, content FROM memories ORDER BY created_at DESC, id DESC LIMIT ?',
                    (n,)
                ).fetchall()
            if not rows:
                return '暂无记忆'
            return '\n'.join(f'[{r["category"]}] {r["content"]}' for r in rows)
        except Exception:
            logger.exception()
            return '记忆读取失败'

    def _cmd_search(self, args: list) -> str:
        if not args:
            return '用法: /search <query>'
        query = ' '.join(args)
        try:
            from ..knowledge import knowledge_search
            result = knowledge_search(query, limit=3)
            results = result.get('results', []) or []
            if not results:
                return '未找到相关知识'
            lines = []
            for r in results[:3]:
                title = r.get('title') or r.get('file') or '未知标题'
                source = r.get('source') or '知识库'
                content = (r.get('content') or '').replace('\n', ' ')[:100]
                lines.append(f'📄 {title} [{source}]\n   {content}')
            return '\n'.join(lines)
        except Exception:
            logger.exception()
            return '知识库搜索失败'

    def _cmd_model(self) -> str:
        try:
            from ..model_router import model_router
            provider = model_router.get_provider()
            if not provider:
                return '当前模型: 未知'
            model = provider.model or '未知'
            pin = provider.price_input or 0
            pout = provider.price_output or 0
            return f'当前模型: {model} | 价格: 输入${pin}/M 输出${pout}/M'
        except Exception:
            logger.exception()
            return '模型信息不可用'

    def _cmd_stats(self, platform: str, user_id: str) -> str:
        try:
            session = self.get_or_create_session(platform, user_id)
            history = session.history or []
            return f'本会话消息数: {session.message_count} | 历史轮数: {len(history)//2}'
        except Exception:
            logger.exception()
            return '会话统计不可用'

    def _cmd_cost(self, user_key: str) -> str:
        try:
            from ..db import get_db
            conn = get_db()
            try:
                row = conn.execute(
                    'SELECT COALESCE(SUM(estimated_cost_usd),0) c, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o '
                    'FROM sessions WHERE user_key=?',
                    (user_key,)
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                return '累计费用: $0.0 | 输入0 tokens | 输出0 tokens'
            return f'累计费用: ${round(row["c"],4)} | 输入{row["i"]} tokens | 输出{row["o"]} tokens'
        except Exception:
            logger.exception()
            return '费用统计不可用'

    def _cmd_skills(self) -> str:
        try:
            from ..skills.loader import SkillLoader
            loader = SkillLoader()
            skills = loader.discover_all()
            names = [s.name for s in skills]
            if not names:
                return '暂无技能'
            return f'可用技能 ({len(names)}):\n' + '\n'.join(f'• {name}' for name in names)
        except Exception:
            logger.exception()
            return '技能列表不可用'

    def _cmd_perms(self, user_key: str) -> str:
        try:
            auth = self._get_auth(user_key)
            mode = auth.get('mode', 'free')
            granted = auth.get('granted', []) or []
            revoked = auth.get('revoked', []) or []
            granted_str = ', '.join(granted) if granted else '无'
            revoked_str = ', '.join(revoked) if revoked else '无'
            return f'模式: {mode}（free=免授权/safe=需授权）| 授权工具: {granted_str} | 收回工具: {revoked_str}'
        except Exception:
            logger.exception()
            return '授权状态不可用'

    def _cmd_mode(self, command: str, args: list, user_key: str) -> str:
        arg = args[0].lower() if args else ''
        if command == 'auto':
            if arg == 'off':
                self._set_auth(user_key, 'mode', 'safe')
                return '🔒 授权模式已开启，写操作需授权（当前自动拒绝写工具）'
            self._set_auth(user_key, 'mode', 'free')
            return '✅ 免授权模式已开启，AI 可直接执行工具'
        # safe
        if arg == 'off':
            self._set_auth(user_key, 'mode', 'free')
            return '✅ 免授权模式已开启，AI 可直接执行工具'
        self._set_auth(user_key, 'mode', 'safe')
        return '🔒 授权模式已开启，写操作需授权（当前自动拒绝写工具）'

    def _cmd_grant(self, args: list, user_key: str) -> str:
        if not args:
            return '用法: /grant <tool>，tool 如 Bash、Edit、Write、Read、WebSearch'
        tool = args[0]
        auth = self._get_auth(user_key)
        granted = list(auth.get('granted', []) or [])
        revoked = [t for t in (auth.get('revoked', []) or []) if t != tool]
        if tool not in granted:
            granted.append(tool)
        self._set_auth(user_key, 'granted', granted)
        self._set_auth(user_key, 'revoked', revoked)
        return f'已授权工具: {tool}'

    def _cmd_revoke(self, args: list, user_key: str) -> str:
        if not args:
            return '用法: /revoke <tool>，tool 如 Bash、Edit、Write、Read、WebSearch'
        tool = args[0]
        auth = self._get_auth(user_key)
        granted = [t for t in (auth.get('granted', []) or []) if t != tool]
        revoked = list(auth.get('revoked', []) or [])
        if tool not in revoked:
            revoked.append(tool)
        self._set_auth(user_key, 'granted', granted)
        self._set_auth(user_key, 'revoked', revoked)
        return f'已收回工具: {tool}'

    def _get_auth(self, user_key: str) -> dict:
        """Read per-user authorization state, with defaults when absent."""
        default = {'mode': 'free', 'granted': [], 'revoked': []}
        try:
            if AUTH_STATE.exists():
                with open(AUTH_STATE, encoding='utf-8') as f:
                    data = json.load(f)
                entry = data.get(user_key, {})
                mode = entry.get('mode', 'free')
                if mode not in ('free', 'safe'):
                    mode = 'free'
                return {
                    'mode': mode,
                    'granted': list(entry.get('granted', []) or []),
                    'revoked': list(entry.get('revoked', []) or []),
                }
        except Exception:
            logger.exception('auth state read failed')
        return default

    def _set_auth(self, user_key: str, field: str, value) -> None:
        """Persist a per-user authorization field atomically (tmp + rename)."""
        try:
            AUTH_STATE.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if AUTH_STATE.exists():
                with open(AUTH_STATE, encoding='utf-8') as f:
                    data = json.load(f)
            entry = data.setdefault(user_key, {'mode': 'free', 'granted': [], 'revoked': []})
            if field == 'mode':
                if value not in ('free', 'safe'):
                    raise ValueError(f'invalid auth mode: {value}')
                entry['mode'] = value
            elif field in ('granted', 'revoked'):
                seen: list[str] = []
                for item in (value or []):
                    if item and item not in seen:
                        seen.append(item)
                entry[field] = seen
            else:
                raise ValueError(f'unknown auth field: {field}')
            data[user_key] = entry
            tmp = AUTH_STATE.parent / (AUTH_STATE.name + '.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, AUTH_STATE)
        except Exception:
            logger.exception('auth state write failed')

    def _build_prompt(self, session: GatewaySession, message: str) -> str:
        """Build a prompt with conversation context."""
        if not session.history:
            prompt = message
        else:
            context_parts = []
            for msg in session.history[-10:]:
                role = 'User' if msg['role'] == 'user' else 'Assistant'
                context_parts.append(f"{role}: {msg['content'][:500]}")
            context = '\n'.join(context_parts)
            prompt = f'Previous conversation:\n{context}\n\nUser: {message}'
        # Attachment hint: message embeds [附件: /path] markers, tell the model to read them.
        if '[附件:' in message:
            prompt += '\n\n（用户发来了附件，路径已标注为 [附件: 路径]，请用 Read 工具读取附件内容后回答。）'
        return prompt

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
        'mcp__metano__memory_search',
        'mcp__metano__memory_add',
        'mcp__metano__knowledge_search',
    ]

    async def _call_claude(self, prompt: str, session: GatewaySession, skill_prefix: str='',
                           allowed_tools=None, skip_permissions: bool=True) -> tuple:
        """Call Claude Code CLI with the prompt (async, non-blocking).

        Gateway sessions are non-interactive (no TTY), so by default we
        bypass permission prompts and pre-approve a tool allowlist. When a
        user is in ``safe`` mode (``skip_permissions=False``) the dangerous
        skip flag is omitted, so write tools require authorization (currently
        auto-denied in the headless CLI).

        ``allowed_tools`` overrides the default gateway allowlist (per-user
        granted/revoked tools are resolved by the caller).

        The subprocess inherits os.environ but is overridden with the
        ModelRouter default provider so chat honours the model selected in
        the Models page (falls back to process env when no provider set).

        Returns (response, input_tokens, output_tokens, cache_read_tokens)
        parsed from ``--output-format stream-json --verbose``.
        """
        import os
        import shutil
        claude_bin = shutil.which('claude') or '/home/dk/local/node/bin/claude'
        system_ctx = self._build_system_context()
        context_layers = []
        if skill_prefix:
            context_layers.append(skill_prefix)
        if system_ctx:
            context_layers.append(system_ctx)
        if context_layers:
            combined = '\n\n'.join(context_layers)
            prompt = f"{combined}\n\n---\n\nUser message: {prompt}\n\nRespond in Chinese."
        tools = allowed_tools or self._GATEWAY_ALLOWED_TOOLS
        cmd = [claude_bin, '-p', prompt]
        if skip_permissions:
            cmd.append('--dangerously-skip-permissions')
        cmd += [
            '--allowedTools', ','.join(tools),
            '--output-format', 'stream-json',
            '--verbose',
        ]
        env = os.environ.copy()
        try:
            from ..model_router import model_router
            provider = model_router.get_provider()
            if provider:
                if provider.base_url:
                    env['ANTHROPIC_BASE_URL'] = provider.base_url
                if provider.api_key:
                    env['ANTHROPIC_API_KEY'] = provider.api_key
                if provider.model:
                    env['ANTHROPIC_MODEL'] = provider.model
        except Exception:
            logger.exception("router: provider env injection failed")
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
            return self._parse_stream_json(stdout, stderr)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 'Response timed out. Please try again.', 0, 0, 0
        except Exception as e:
            logger.exception()
            return f'Error: {str(e)}', 0, 0, 0

    def _parse_stream_json(self, stdout: bytes, stderr: bytes) -> tuple:
        """Parse ``claude -p --output-format stream-json --verbose`` output.

        Returns (response, input_tokens, output_tokens, cache_read_tokens).

        Assistant ``text`` blocks are concatenated into the response. Usage is
        the last assistant snapshot, overridden by the authoritative ``result``
        line usage when present (streamed assistant usage reports output_tokens
        as 0 until the run finishes).
        """
        text_parts: list[str] = []
        usage: dict = {}
        for line in stdout.decode(errors='replace').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get('type') == 'assistant':
                msg = obj.get('message', obj)
                content = msg.get('content')
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'text':
                            text_parts.append(block.get('text', ''))
                elif isinstance(content, str):
                    text_parts.append(content)
                u = msg.get('usage') if isinstance(msg.get('usage'), dict) else obj.get('usage')
                if isinstance(u, dict):
                    usage = u
            elif obj.get('type') == 'result':
                u = obj.get('usage')
                if isinstance(u, dict):
                    usage = u
        response = ''.join(text_parts).strip()
        if not response:
            if stderr:
                response = f'Error: {stderr.decode(errors="replace")[:200]}'
            else:
                response = '(no response)'
        in_tok = usage.get('input_tokens') or 0
        out_tok = usage.get('output_tokens') or 0
        cache_tok = usage.get('cache_read_input_tokens') or 0
        return response, in_tok, out_tok, cache_tok

    def reset_session(self, platform: str, user_id: str):
        """Reset a user's conversation session."""
        key = self._session_key(platform, user_id)
        if key in self.sessions:
            self.sessions[key].session_id = ''
            self.sessions[key].history = []
            self.sessions[key].message_count = 0
router = MessageRouter()