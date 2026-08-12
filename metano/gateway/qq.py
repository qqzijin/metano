"""QQ Bot adapter using OneBot v11 protocol (NapCat/LLOneBot)."""

import json
import logging
import asyncio
import urllib.request
from .router import router

log = logging.getLogger(__name__)


class QQBot:
    def __init__(self, ws_url: str = "ws://127.0.0.1:3001",
                 allowed_groups: list[int] | None = None,
                 allowed_users: list[str] | None = None,
                 token: str = "", self_id: str = ""):
        self.ws_url = ws_url
        self.allowed_groups = allowed_groups or []
        self.allowed_users = allowed_users or []
        self.token = token or ""
        self.self_id = self_id or ""
        self._running = False

    @staticmethod
    def _ws_host(url: str) -> str:
        """Return the host part of a ws(s):// URL."""
        try:
            from urllib.parse import urlparse
            return (urlparse(url).hostname or "").lower()
        except Exception:
            return ""

    async def start(self):
        try:
            import websockets
        except ImportError:
            log.error("websockets not installed. Run: pip install websockets")
            return

        # SECURITY (H-08): QQ may only connect to a loopback OneBot endpoint by
        # default. A remote endpoint must use wss:// AND carry an access token.
        host = self._ws_host(self.ws_url)
        if host not in ("127.0.0.1", "localhost", "::1"):
            if not self.ws_url.startswith("wss://"):
                log.error(f"QQ remote OneBot connection must use wss:// (got {self.ws_url}); refusing to start")
                return
            if not self.token:
                log.error("QQ remote OneBot connection requires a token; refusing to start")
                return

        self._running = True
        log.info(f"QQ Bot connecting to {self.ws_url}...")
        connect_kw = {}
        if self.token:
            connect_kw["extra_headers"] = {"Authorization": f"Bearer {self.token}"}

        while self._running:
            try:
                async with websockets.connect(self.ws_url, **connect_kw) as ws:
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

        # SECURITY (H-08): when a fixed bot self_id is configured, reject events
        # whose reported identity does not match — the peer cannot claim to be
        # our bot.
        if self.self_id and self_id != self.self_id:
            return

        if not raw_message or not user_id:
            return

        # Group message: only respond to @bot, and only in whitelisted groups.
        if message_type == "group":
            # Empty whitelist == deny all (fail closed).
            if group_id not in self.allowed_groups:
                return
            # Check for @bot mention (CQ code)
            if f"[CQ:at,qq={self_id}]" not in raw_message:
                return
            content = raw_message.replace(f"[CQ:at,qq={self_id}]", "").strip()
            if not content:
                return
        elif message_type == "private":
            # Fail-closed user whitelist for private messages.
            if user_id not in self.allowed_users:
                return
            content = raw_message
        else:
            return

        # Route to Claude — pass the RAW user id; the Router adds the platform
        # prefix. (F-16: previously qq:qq:<id> double prefix.)
        response = await router.route_message("qq", user_id, content)

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