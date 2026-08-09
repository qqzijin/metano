"""WeChat adapter using wcferry (WeChatFerry).

Note: WeChat bridge requires a running WeChat desktop client on Windows or Wine.
This module provides the interface; the actual WeChat client needs to be set up separately.
"""
import asyncio
import json
import logging
import threading
import time
from .router import router
from metano.log import logger
log = logging.getLogger(__name__)

class WeChatBot:

    def __init__(self, method: str='wcferry'):
        self.method = method
        self._running = False
        self._wcf = None

    def start(self):
        """Start WeChat bridge in a background thread."""
        self._running = True
        if self.method == 'wcferry':
            self._start_wcferry()
        else:
            log.error(f'Unsupported WeChat method: {self.method}')

    def stop(self):
        self._running = False
        if self._wcf:
            try:
                self._wcf.disable_recv_msg()
            except Exception:
                logger.exception()

    def _start_wcferry(self):
        try:
            from wcferry import Wcf
        except ImportError:
            log.error('wcferry not installed. Run: pip install wcferry')
            log.error('Also need WeChat desktop client running. See: https://github.com/lich0821/WeChatFerry')
            return
        self._wcf = Wcf()
        if not self._wcf.is_login():
            log.error('WeChat not logged in. Please login first.')
            return
        log.info(f'WeChat connected: {self._wcf.get_self_wxid()}')
        self._wcf.enable_receiving_msg()
        thread = threading.Thread(target=self._message_loop, daemon=True)
        thread.start()

    def _message_loop(self):
        """Process incoming WeChat messages."""
        while self._running:
            try:
                msg = self._wcf.get_msg()
                if not msg:
                    time.sleep(0.1)
                    continue
                if msg.type != 1:
                    continue
                content = msg.content.strip()
                if not content:
                    continue
                if msg.from_user().endswith('@chatuser'):
                    continue
                wxid = msg.from_user()
                platform_user = f'wechat:{wxid}'
                response = asyncio.run(router.route_message('wechat', platform_user, content))
                self._wcf.send_text(response, wxid)
                time.sleep(2)
            except Exception as e:
                log.error(f'WeChat message error: {e}')
                time.sleep(1)

    def send_message(self, wxid: str, text: str):
        """Send a message to a WeChat user."""
        if self._wcf:
            self._wcf.send_text(text, wxid)