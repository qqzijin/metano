"""Discord Bot adapter for metano gateway."""

import logging
from .router import router

log = logging.getLogger(__name__)


class DiscordBot:
    def __init__(self, token: str, guild_id: int | None = None, allowed_channels: list[int] | None = None):
        self.token = token
        self.guild_id = guild_id
        self.allowed_channels = allowed_channels or []
        self._bot = None

    async def start(self):
        try:
            import discord
            from discord.ext import commands
        except ImportError:
            log.error("discord.py not installed. Run: pip install discord.py")
            return

        intents = discord.Intents.default()
        intents.message_content = True
        self._bot = commands.Bot(command_prefix="!", intents=intents)

        @self._bot.event
        async def on_ready():
            log.info(f"Discord bot ready: {self._bot.user}")

        @self._bot.command()
        async def new(ctx):
            user_id = str(ctx.author.id)
            router.reset_session("discord", user_id)
            await ctx.reply("Session reset. Starting fresh conversation.")

        @self._bot.command()
        async def profile(ctx):
            from ..honcho.models import get_honcho_db, get_profile
            conn = get_honcho_db()
            try:
                profile = get_profile(conn, "default")
                summary = profile.get("belief_summary", "No profile yet.")
            finally:
                conn.close()
            await ctx.reply(f"Your profile:\n{summary}")

        @self._bot.event
        async def on_message(message):
            if message.author.bot:
                return
            # Fail-closed channel whitelist (H-08): empty whitelist == deny all.
            if message.channel.id not in self.allowed_channels:
                return
            # SECURITY (H-08): when a guild_id is configured, strictly scope the
            # bot to that guild (rejects DMs and other guilds' messages).
            if self.guild_id is not None:
                if message.guild is None or message.guild.id != self.guild_id:
                    return
            content = message.content or ''
            is_dm = message.guild is None
            is_mention = self._bot.user.mentioned_in(message)
            # F-16: route slash commands, DMs and @mentions to the central router
            # so /help /search /memory /auto /perms work on Discord too.
            if content.startswith('/') or is_dm or is_mention:
                cleaned = content.replace(f"<@{self._bot.user.id}>", "").strip()
                if not cleaned:
                    return
                async with message.channel.typing():
                    user_id = str(message.author.id)
                    response = await router.route_message("discord", user_id, cleaned)
                # Discord limit: 2000 chars
                for chunk in self._chunk(response, 2000):
                    await message.reply(chunk)
                return
            # Non-command, non-mention guild chatter: handle registered !commands.
            await self._bot.process_commands(message)

        await self._bot.start(self.token)

    async def stop(self):
        if self._bot:
            await self._bot.close()

    @staticmethod
    def _chunk(text: str, limit: int) -> list[str]:
        if len(text) <= limit:
            return [text]
        chunks = []
        while text:
            if len(text) <= limit:
                chunks.append(text)
                break
            split_at = text.rfind("\n", 0, limit)
            if split_at == -1:
                split_at = limit
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        return chunks