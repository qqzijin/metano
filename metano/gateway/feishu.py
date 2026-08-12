"""Feishu/Lark bot adapter for metano gateway.

Uses lark-oapi SDK with WebSocket (Long Connection) mode.
Supports: private chat, group @mention, markdown responses.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    P2ImMessageReceiveV1,
    GetImageRequest,
    GetFileRequest,
)

from ..paths import UPLOADS_DIR, HOME
from .router import MessageRouter

logger = logging.getLogger(__name__)

# Feishu message content size limit (per message)
MAX_MESSAGE_LENGTH = 4000


def _split_message(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split long text into chunks at paragraph boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to split at paragraph or sentence boundary
        split_at = text.rfind("\n\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = text.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = text.rfind("。", 0, max_len)
        if split_at < max_len // 2:
            split_at = text.rfind(" ", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len
        # F-17: no-boundary fallback must slice at exactly max_len, not +1, so a
        # chunk never exceeds the platform limit.
        chunks.append(text[:split_at])
        text = text[split_at:]
    return chunks


def _text_to_feishu_md(text: str) -> str:
    """Convert plain text / markdown to Feishu-compatible markdown.

    Feishu supports a subset of markdown. Strip unsupported syntax.
    """
    # Feishu doesn't support ``` with language hints, keep simple code blocks
    text = re.sub(r"```\w+\n", "```\n", text)
    # Strip HTML-style tags that Feishu doesn't render
    text = re.sub(r"</?(div|span|br|p)[^>]*>", "", text)
    return text


class FeishuBot:
    """Feishu/Lark bot using lark-oapi SDK with WebSocket mode."""

    def __init__(self, config: dict, router: MessageRouter):
        self.app_id = os.environ.get("FEISHU_APP_ID") or config.get("app_id", "")
        self.app_secret = os.environ.get("FEISHU_APP_SECRET") or config.get("app_secret", "")
        self.encryption_key = os.environ.get("FEISHU_ENCRYPTION_KEY") or config.get("encryption_key", "")
        self.verification_token = os.environ.get("FEISHU_VERIFICATION_TOKEN") or config.get("verification_token", "")
        self.allowed_users = config.get("allowed_users", [])
        # S2：审批人白名单——优先独立配置 feishu.approval_users，未配置时回退到
        # allowed_users。审批回复（批准#N/拒绝#N）只有白名单内的发送者才能生效，
        # 防止任意能私聊 bot 的人改写 operator 配置 / CLAUDE.md。
        self.approval_users = config.get("approval_users") or config.get("allowed_users") or []
        self.router = router
        self.client: Optional[lark.Client] = None
        self._processed = set()  # message dedup
        self._typing_backoff_until = 0.0  # timestamp: suppress typing reactions after rate limit

    def start(self):
        """Start the Feishu bot with WebSocket (Long Connection) mode."""
        if not self.app_id or not self.app_secret:
            logger.error("Feishu bot requires app_id and app_secret in gateway_config.yaml")
            return

        # Create lark client (SDK default timeout is 30s, too short for API calls)
        self.client = lark.Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .timeout(120) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        # Build event handler
        handler = lark.EventDispatcherHandler.builder(
            encrypt_key=self.encryption_key,
            verification_token=self.verification_token,
        ) \
            .register_p2_im_message_receive_v1(self._on_message) \
            .register_p2_im_message_message_read_v1(self._on_message_read) \
            .build()

        # Start WebSocket client
        ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )

        logger.info("Feishu bot starting (WebSocket mode)...")
        try:
            ws_client.start()
        except Exception as e:
            logger.error(f"Feishu bot error: {e}")

    def _on_message_read(self, data) -> None:
        """Benign 'message read' receipt — no action needed, but registering it
        stops the SDK from logging 'processor not found' on every read event."""
        pass

    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """Handle incoming Feishu message event (schedules async processing)."""
        try:
            event = data.event
            msg = event.message
            sender = event.sender

            # Dedup by message_id
            msg_id = msg.message_id
            if msg_id in self._processed:
                return
            self._processed.add(msg_id)
            # Keep dedup set bounded
            if len(self._processed) > 1000:
                self._processed = set(list(self._processed)[-500:])

            # Auth check — fail closed (H-08): empty whitelist denies everyone.
            sender_id = sender.sender_id.open_id if sender.sender_id else ""
            if sender_id not in self.allowed_users:
                logger.info(f"Ignoring message from unauthorized user: {sender_id}")
                return

            chat_type = msg.chat_type  # "p2p" or "group"
            chat_id = msg.chat_id

            # Extract text content
            content = msg.content
            msg_type = msg.message_type

            # For image/file messages, download the attachment and mark its path;
            # _extract_text would otherwise return the raw JSON content string.
            if msg_type in ('image', 'file'):
                user_text = self._download_attachment(content, msg_type)
            else:
                user_text = self._extract_text(content, msg_type)

            # For group messages, only respond to @bot mentions
            if chat_type == "group":
                # Check if bot is mentioned
                mentions = msg.mentions
                if not mentions:
                    return
                # Strip @bot prefix from text
                for mention in mentions:
                    if mention.name:
                        user_text = re.sub(rf"@{re.escape(mention.name)}\s*", "", user_text).strip()
                if not user_text:
                    return

            if not user_text:
                return

            # Mirror incoming user messages to the operator inbox so Claude Code
            # can read them asynchronously (user replies via Feishu without
            # returning to the terminal).
            try:
                self._mirror_to_inbox(sender_id, chat_id, chat_type, user_text, msg_id)
            except Exception:
                logger.exception('feishu: mirror to inbox failed')

            logger.info(f"Feishu message from {sender_id} in {chat_type}: {user_text[:80]}")

            # Check if this is an evolution proposal approval/rejection reply.
            # SECURITY (S2/C4): only configured approvers may act on proposal
            # replies. feishu.approval_users（未单独配置时回退到 allowed_users）
            # 必须非空且包含发送者 open_id；否则审批指令被拒绝、不处理
            # （处理会改写 operator 配置 / CLAUDE.md）。同时把 sender_id 传给
            # process_approval_reply 做第二道身份校验（纵深防御）。
            if chat_type == "p2p" and re.match(r'(批准|拒绝)\s*#?\d+', user_text.strip()):
                if not self.approval_users or sender_id not in self.approval_users:
                    self._send_text_message(chat_id, '⚠️ 无权处理提案审批（仅限配置的审批人）。', "")
                    return
                try:
                    from ..evolution_notify import process_approval_reply
                    approval_result = process_approval_reply(
                        user_text, sender_id=sender_id, allowed_senders=self.approval_users
                    )
                    if approval_result:
                        action = approval_result['action']
                        if action == 'denied':
                            logger.warning(f"feishu: 审批被拒绝，sender={sender_id}")
                        else:
                            pid = approval_result['proposal_id']
                            self._send_text_message(chat_id, f"Proposal #{pid} {action}", "")
                            return
                except Exception:
                    pass  # Not an approval reply, continue normal processing

            # Mark message as read (emoji reaction)
            self._add_read_indicator(msg_id)

            # Add typing indicator (emoji reaction)
            self._add_typing_indicator(msg_id)

            # Schedule async processing so we don't block the WS event loop
            import concurrent.futures
            if not hasattr(self, '_executor'):
                self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
            self._executor.submit(
                self._process_and_reply_sync, chat_id, msg_id, sender_id, chat_type, user_text
            )

        except Exception as e:
            logger.error(f"Error handling Feishu message: {e}", exc_info=True)

    def _mirror_to_inbox(self, sender_id, chat_id, chat_type, user_text, msg_id):
        """Write the user's Feishu message to the operator inbox dir. Claude Code
        monitors this directory and treats new files as async instructions, so
        the user can reply to a report via Feishu without returning to the
        terminal session."""
        try:
            inbox = HOME / 'feishu_inbox'
            inbox.mkdir(parents=True, exist_ok=True)
            fname = inbox / f"msg_{int(time.time() * 1000)}_{abs(hash(msg_id)) % 10000}.json"
            fname.write_text(json.dumps({
                'from': sender_id,
                'chat_id': chat_id,
                'chat_type': chat_type,
                'text': user_text,
                'time': time.time(),
                'msg_id': msg_id,
            }, ensure_ascii=False, indent=2))
        except Exception:
            logger.exception('feishu: write inbox failed')

    def _process_and_reply_sync(self, chat_id: str, msg_id: str, sender_id: str, chat_type: str, user_text: str):
        """Sync wrapper: runs async route_message in a new event loop within the thread pool."""
        try:
            # F-16: pass the RAW sender open_id; the Router adds the platform prefix.
            response = asyncio.run(
                self.router.route_message(
                    platform="feishu",
                    user_id=sender_id,
                    message=user_text,
                )
            )
            self._remove_typing_indicator(msg_id)
            self._remove_read_indicator(msg_id)
            self._send_reply(chat_id, response, msg_id)
        except Exception as e:
            # F-17: a processing exception must reach the user — never leave them
            # with no reply and no failure state.
            logger.error(f"Error in async message processing: {e}", exc_info=True)
            self._remove_typing_indicator(msg_id)
            self._remove_read_indicator(msg_id)
            try:
                self._send_reply(chat_id, "抱歉，处理你的消息时出错了，请稍后再试。", msg_id)
            except Exception:
                logger.exception('feishu: failed to send error receipt')

    def _extract_text(self, content: str, msg_type: str) -> str:
        """Extract plain text from Feishu message content."""
        if msg_type == "text":
            try:
                data = json.loads(content)
                return data.get("text", "")
            except json.JSONDecodeError:
                return content
        elif msg_type == "post":
            # Rich text: extract all text from content blocks
            try:
                data = json.loads(content)
                texts = []
                for line in data.get("content", []):
                    for elem in line:
                        if isinstance(elem, dict) and elem.get("tag") == "text":
                            texts.append(elem.get("text", ""))
                        elif isinstance(elem, dict) and elem.get("tag") == "at":
                            texts.append(elem.get("user_name", ""))
                return " ".join(texts)
            except (json.JSONDecodeError, KeyError):
                return content
        return content

    @staticmethod
    def _read_response_bytes(resp) -> Optional[bytes]:
        """Extract raw bytes from a Feishu image/file get response.

        lark-oapi SDK (verified on the installed version): GetImageResponse /
        GetFileResponse expose ``file`` (IO[Any] / bytes) directly on the
        response object, plus ``file_name``. Fall back to ``data.file`` /
        ``data.image`` in case another SDK version nests it under ``data``.
        """
        if resp is None:
            return None
        data = getattr(resp, 'file', None)
        if data is None and getattr(resp, 'data', None) is not None:
            data = getattr(resp.data, 'file', None) or getattr(resp.data, 'image', None)
        if data is None:
            return None
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        try:
            return data.read()
        except Exception:
            return None

    def _download_attachment(self, content: str, msg_type: str) -> str:
        """Download a Feishu image/file message to UPLOADS_DIR.

        Returns a string like ``[附件: /abs/path]`` on success (with any caption
        text if present), or ``''`` on failure so the message is not routed with
        the raw JSON content and the handler is not blocked.
        """
        try:
            data = json.loads(content)
            UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
            if msg_type == "image":
                key = data.get("image_key", "")
                if not key:
                    return ""
                req = GetImageRequest.builder().image_key(key).build()
                resp = self.client.im.v1.image.get(req)
                raw = self._read_response_bytes(resp)
                if resp.success() and raw:
                    dest = UPLOADS_DIR / f'img_{key[-10:]}.png'
                    dest.write_bytes(raw)
                    return f"[附件: {dest}]"
                logger.error(f"Feishu image download failed: code={resp.code}, msg={resp.msg}")
                return ""
            if msg_type == "file":
                key = data.get("file_key", "")
                if not key:
                    return ""
                req = GetFileRequest.builder().file_key(key).build()
                resp = self.client.im.v1.file.get(req)
                raw = self._read_response_bytes(resp)
                if resp.success() and raw:
                    dest = UPLOADS_DIR / f'file_{key[-10:]}.bin'
                    dest.write_bytes(raw)
                    return f"[附件: {dest}]"
                logger.error(f"Feishu file download failed: code={resp.code}, msg={resp.msg}")
                return ""
        except Exception as e:
            logger.error(f"Feishu attachment download failed: {e}", exc_info=True)
            return ""
        return ""

    def _send_reply(self, chat_id: str, text: str, reply_to: str = ""):
        """Send a reply message to a Feishu chat."""
        if not self.client:
            return

        md_text = _text_to_feishu_md(text)
        chunks = _split_message(md_text)
        sent_any = False

        for i, chunk in enumerate(chunks):
            try:
                self._send_text_message(chat_id, chunk, reply_to if i == 0 else "")
                sent_any = True
            except Exception as e:
                logger.error(f"Failed to send Feishu message: {e}")

        if not sent_any:
            # F-17: retries exhausted — send a short error receipt so the user
            # gets a visible failure instead of silence.
            try:
                self._send_text_message(chat_id, "⚠️ 回复发送失败，请稍后再试。", reply_to)
            except Exception:
                logger.exception('feishu: error receipt send failed')

    def _send_text_message(self, chat_id: str, text: str, reply_to: str = ""):
        """Send a text message to Feishu chat with retry."""
        content = json.dumps({"text": text}, ensure_ascii=False)

        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(content)
                .build()
            ) \
            .build()

        if reply_to:
            from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
            reply_req = ReplyMessageRequest.builder() \
                .message_id(reply_to) \
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    .content(content)
                    .build()
                ) \
                .build()
            for attempt in range(3):
                try:
                    resp = self.client.im.v1.message.reply(reply_req)
                    if resp.success():
                        return
                    logger.error(f"Feishu reply failed: code={resp.code}, msg={resp.msg}")
                    if resp.code in (99991400, 99991401):
                        time.sleep(2 ** attempt)
                        continue
                    return
                except Exception as e:
                    logger.error(f"Feishu reply attempt {attempt+1} error: {e}")
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                    else:
                        raise
            return

        for attempt in range(3):
            try:
                resp = self.client.im.v1.message.create(request)
                if resp.success():
                    return
                logger.error(f"Feishu send failed: code={resp.code}, msg={resp.msg}")
                if resp.code in (99991400, 99991401):
                    # Rate limited — back off
                    time.sleep(2 ** attempt)
                    continue
                return  # Non-retryable error
            except Exception as e:
                logger.error(f"Feishu send attempt {attempt+1} error: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
                else:
                    raise

    def _build_content(self, text: str) -> str:
        """Build message content JSON."""
        return json.dumps({"text": text}, ensure_ascii=False)

    # Valid Feishu emoji_type values (tested against API)
    EMOJI_READ = "THUMBSUP"     # 👍 marks message as read/acknowledged
    EMOJI_TYPING = "SWEAT"      # 😅 indicates bot is thinking/processing

    def _add_typing_indicator(self, message_id: str):
        """Add an emoji reaction to show the bot is processing."""
        if not self.client:
            return
        if time.time() < self._typing_backoff_until:
            logger.debug("Skipping typing indicator due to rate-limit backoff")
            return
        try:
            from lark_oapi.api.im.v1 import CreateMessageReactionRequest, CreateMessageReactionRequestBody
            from lark_oapi.api.im.v1.model.emoji import Emoji
            emoji = Emoji.builder().emoji_type(self.EMOJI_TYPING).build()
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(emoji)
                    .build()
                ) \
                .build()
            resp = self.client.im.v1.message_reaction.create(request)
            if not resp.success():
                logger.debug(f"Add typing indicator failed: code={resp.code}, msg={resp.msg}")
                if resp.code in (99991400, 99991401, 99991402, 99991403):
                    self._typing_backoff_until = time.time() + 300
                    logger.warning(f"Typing indicator rate-limited (code={resp.code}), backing off 5min")
        except Exception as e:
            logger.debug(f"Add typing indicator failed (non-critical): {e}")

    def _remove_typing_indicator(self, message_id: str):
        """Remove the typing emoji reaction after response is sent."""
        if not self.client:
            return
        try:
            self._remove_reaction_by_type(message_id, self.EMOJI_TYPING)
        except Exception as e:
            logger.debug(f"Remove typing indicator failed (non-critical): {e}")

    def _add_read_indicator(self, message_id: str):
        """Add a thumbs-up emoji reaction to mark message as read."""
        if not self.client:
            return
        if time.time() < self._typing_backoff_until:
            return
        try:
            from lark_oapi.api.im.v1 import CreateMessageReactionRequest, CreateMessageReactionRequestBody
            from lark_oapi.api.im.v1.model.emoji import Emoji
            emoji = Emoji.builder().emoji_type(self.EMOJI_READ).build()
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(emoji)
                    .build()
                ) \
                .build()
            resp = self.client.im.v1.message_reaction.create(request)
            if not resp.success():
                logger.debug(f"Add read indicator failed: code={resp.code}, msg={resp.msg}")
                if resp.code in (99991400, 99991401, 99991402, 99991403):
                    self._typing_backoff_until = time.time() + 300
                    logger.warning(f"Read indicator rate-limited (code={resp.code}), backing off 5min")
        except Exception as e:
            logger.debug(f"Add read indicator failed (non-critical): {e}")

    def _remove_read_indicator(self, message_id: str):
        """Remove the read emoji reaction after response is sent."""
        if not self.client:
            return
        try:
            self._remove_reaction_by_type(message_id, self.EMOJI_READ)
        except Exception as e:
            logger.debug(f"Remove read indicator failed (non-critical): {e}")

    def _remove_reaction_by_type(self, message_id: str, emoji_type: str):
        """Find and remove a specific emoji reaction from a message."""
        from lark_oapi.api.im.v1 import ListMessageReactionRequest, DeleteMessageReactionRequest
        list_req = ListMessageReactionRequest.builder() \
            .message_id(message_id) \
            .page_size(50) \
            .build()
        list_resp = self.client.im.v1.message_reaction.list(list_req)
        if not list_resp.success():
            return
        items = list_resp.data.items if list_resp.data and list_resp.data.items else []
        for item in items:
            rt = getattr(item, 'reaction_type', None)
            et = rt.emoji_type if rt and hasattr(rt, 'emoji_type') else None
            if et == emoji_type:
                del_req = DeleteMessageReactionRequest.builder() \
                    .message_id(message_id) \
                    .reaction_id(item.reaction_id) \
                    .build()
                self.client.im.v1.message_reaction.delete(del_req)
                break


def _has_markdown(text: str) -> bool:
    """Check if text contains markdown syntax."""
    return bool(re.search(r"[*_`#\[\]|]", text))
