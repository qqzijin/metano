"""QQ Bot adapter using OneBot v11 protocol (NapCat/LLOneBot)."""

import json
import logging
import asyncio
import urllib.request
from .router import router

log = logging.getLogger(__name__)


class QQBot:
    def __init__(self, ws_url: str = "ws://127.0.0.1:3001", allowed_groups: list[int] | None = None):
        self.ws_url = ws_url
        self.allowed_groups = allowed_groups or []
        self._running = False

    async def start(self):
        try:
            import websockets
        except ImportError:
            log.error("websockets not installed. Run: pip install websockets")
            return

        self._running = True
        log.info(f"QQ Bot connecting to {self.ws_url}...")

        while self._running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    log.info("QQ Bot connected")
                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(message)
                            await self._handle_event(data, ws)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                log.warning(f"QQ Bot connection error: {e}, reconnecting in 5s...")
                await asyncio.sleep(5)

    def stop(self):
        self._running = False

    async def _handle_event(self, data: dict, ws):
        """Handle a OneBot v11 event."""
        post_type = data.get("post_type")
        if post_type != "message":
            return

        message_type = data.get("message_type")
        user_id = str(data.get("user_id", ""))
        group_id = data.get("group_id")
        raw_message = data.get("raw_message", "").strip()
        message_id = data.get("message_id")
        self_id = str(data.get("self_id", ""))

        if not raw_message or not user_id:
            return

        # Group message: only respond to @bot
        if message_type == "group":
            if self.allowed_groups and group_id not in self.allowed_groups:
                return
            # Check for @bot mention (CQ code)
            if f"[CQ:at,qq={self_id}]" not in raw_message:
                return
            content = raw_message.replace(f"[CQ:at,qq={self_id}]", "").strip()
            if not content:
                return
        elif message_type == "private":
            content = raw_message
        else:
            return

        # Route to Claude
        platform_user = f"qq:{user_id}"
        response = await router.route_message("qq", platform_user, content)

        # Send response
        await self._send_reply(ws, message_type, user_id, group_id, response, message_id)

    async def _send_reply(self, ws, message_type: str, user_id: str,
                          group_id: int | None, text: str, reply_to: int | None):
        """Send a reply via OneBot v11 API."""
        # Chunk long messages
        chunks = self._chunk(text, 3000) if message_type == "private" else self._chunk(text, 4500)

        for chunk in chunks:
            if message_type == "private":
                action = {"action": "send_private_msg", "params": {"user_id": int(user_id), "message": chunk}}
            else:
                action = {"action": "send_group_msg", "params": {"group_id": group_id, "message": chunk}}

            await ws.send(json.dumps(action))
            await asyncio.sleep(0.5)  # Rate limit

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