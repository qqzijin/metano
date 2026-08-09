"""Telegram Bot adapter for metano gateway."""

import asyncio
import logging
from .router import router

log = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, token: str, allowed_users: list[int] | None = None):
        self.token = token
        self.allowed_users = allowed_users or []
        self._app = None

    async def start(self):
        try:
            from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
        except ImportError:
            log.error("python-telegram-bot not installed. Run: pip install python-telegram-bot")
            return

        self._app = ApplicationBuilder().token(self.token).build()

        # Handlers
        self._app.add_handler(CommandHandler("new", self._cmd_new))
        self._app.add_handler(CommandHandler("profile", self._cmd_profile))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

        log.info("Telegram bot starting...")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    async def stop(self):
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def _cmd_new(self, update, context):
        user_id = str(update.effective_user.id)
        router.reset_session("telegram", user_id)
        await update.message.reply_text("Session reset. Starting fresh conversation.")

    async def _cmd_profile(self, update, context):
        from ..honcho.models import get_honcho_db, get_profile
        conn = get_honcho_db()
        try:
            profile = get_profile(conn, "default")
            summary = profile.get("belief_summary", "No profile yet.")
        finally:
            conn.close()
        await update.message.reply_text(f"Your profile:\n{summary}")

    async def _handle_message(self, update, context):
        user_id = str(update.effective_user.id)
        username = update.effective_user.username or user_id

        # Auth check
        if self.allowed_users and update.effective_user.id not in self.allowed_users:
            await update.message.reply_text("Not authorized.")
            return

        text = update.message.text
        if not text:
            return

        # Show typing indicator
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        # Route to Claude
        response = await router.route_message("telegram", user_id, text)

        # Telegram message limit: 4096 chars
        for chunk in self._chunk(response, 4096):
            await update.message.reply_text(chunk)

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