"""WeChat adapter using Tencent's official iLink Bot API (WeChat ClawBot).

Official WeChat personal-account Bot API, opened 2026-03. Pure HTTP/JSON and
cross-platform (Linux included) — this replaces the old ``wechat.py`` wcferry
adapter, which requires a Windows WeChat desktop client. Uses a Telegram-style
long-poll loop (no public webhook / inbound port needed).

Flow: QR login once -> persist credentials -> long-poll ``getUpdates`` ->
route each message through ``MessageRouter`` -> ``sendMessage`` reply carrying
the per-user ``context_token``.

Protocol reference: https://github.com/hao-ji-xing/cc-weixin (weixin-bot-api.md)
"""
import asyncio
import base64
import concurrent.futures
import json
import logging
import random
import threading
import time
import uuid

import requests

from ..paths import GATEWAY_DIR
from .router import MessageRouter

log = logging.getLogger(__name__)

BASE_URL = 'https://ilinkai.weixin.qq.com/'
BOT_TYPE = '3'                   # official channel build type
CHANNEL_VERSION = '2.4.6'
ILINK_APP_ID = 'bot'
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 6)
BOT_AGENT = 'OpenClaw'
LONG_POLL_TIMEOUT = 35           # long-poll client timeout (server holds the request)
API_TIMEOUT = 15
SESSION_EXPIRED_ERRCODE = -14    # session expired -> pause before retrying
SESSION_PAUSE_SEC = 300
RETRY_DELAY_SEC = 3
BACKOFF_DELAY_SEC = 30
MAX_CONSECUTIVE_FAILURES = 10

# proto enums
MT_USER, MT_BOT = 1, 2
MS_FINISH = 2
MIT_TEXT = 1

CRED_FILE = GATEWAY_DIR / 'wechat_ilink_credentials.json'
BUF_FILE = GATEWAY_DIR / 'wechat_ilink_sync_buf.txt'
QR_FILE = GATEWAY_DIR / 'wechat_ilink_login_qr.txt'


class WeChatIlinkBot:
    """WeChat ClawBot adapter (iLink Bot API) with a long-poll message loop."""

    def __init__(self, config: dict, router: MessageRouter):
        self.base_url = (config.get('base_url') or BASE_URL).rstrip('/') + '/'
        self.token = config.get('token') or ''
        self.account_id = config.get('account_id') or ''
        self.user_id = config.get('user_id') or ''
        self.allowed_users = config.get('allowed_users') or []
        self._router = router
        self._get_updates_buf = ''
        self._ctx_tokens: dict[str, str] = {}
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self._stop = threading.Event()
        self._session = requests.Session()
        self._session.trust_env = False   # direct connection to a domestic endpoint
        self._consecutive_failures = 0    # must init before _long_poll_loop increments it
        self._load_state()

    # --------------------------------------------------------------- public API
    def start(self):
        if not self.token:
            if not self._qr_login():
                log.error('WeChat iLink: QR login failed, bot not started')
                return
        log.info('WeChat iLink bot starting (long-poll loop)...')
        self._long_poll_loop()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------- state
    def _load_state(self):
        try:
            if CRED_FILE.exists():
                d = json.loads(CRED_FILE.read_text())
                self.base_url = (d.get('base_url') or self.base_url).rstrip('/') + '/'
                self.token = d.get('token') or self.token
                self.account_id = d.get('account_id') or self.account_id
                self.user_id = d.get('user_id') or self.user_id
        except Exception:
            log.exception('load wechat credentials failed')
        try:
            if BUF_FILE.exists():
                self._get_updates_buf = BUF_FILE.read_text().strip()
        except Exception:
            pass

    def _save_credentials(self, base_url: str, token: str, account_id: str, user_id: str):
        GATEWAY_DIR.mkdir(parents=True, exist_ok=True)
        CRED_FILE.write_text(json.dumps({
            'base_url': base_url, 'token': token,
            'account_id': account_id, 'user_id': user_id,
        }, ensure_ascii=False))
        CRED_FILE.chmod(0o600)
        self.base_url = base_url.rstrip('/') + '/'
        self.token, self.account_id, self.user_id = token, account_id, user_id

    def _save_buf(self):
        try:
            BUF_FILE.write_text(self._get_updates_buf)
        except Exception:
            pass

    # ------------------------------------------------------------------- login
    def _fetch_qrcode(self, base: str) -> tuple[str, str]:
        local_tokens = [self.token] if self.token else []
        try:
            body = json.dumps({'local_token_list': local_tokens}, ensure_ascii=False)
            r = self._session.post(f'{base}ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}',
                                   data=body, headers=self._headers(body), timeout=API_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if not data.get('qrcode'):
                log.info('WeChat iLink: POST qrcode returned no qrcode, falling back to GET')
                r = self._session.get(f'{base}ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}',
                                      timeout=API_TIMEOUT)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.error(f'WeChat iLink: fetch QR code failed: {e}')
            return '', ''
        return data.get('qrcode') or '', data.get('qrcode_img_content') or ''

    def _poll_qr_status(self, base: str, qrcode: str,
                        verify_code: str | None = None) -> dict:
        endpoint = f'ilink/bot/get_qrcode_status?qrcode={qrcode}'
        if verify_code:
            endpoint += f'&verify_code={verify_code}'
        try:
            resp = self._session.get(base + endpoint,
                                     headers={'iLink-App-ClientVersion': '1'},
                                     timeout=LONG_POLL_TIMEOUT)
            resp.raise_for_status()
            st = resp.json()
        except requests.exceptions.Timeout:
            return {}
        except Exception as e:
            log.error(f'WeChat iLink: QR status poll error: {e}')
            return {'error': str(e)}
        state = st.get('status')
        if state == 'confirmed' or st.get('bot_token'):
            return {
                'bot_token': st.get('bot_token') or '',
                'baseurl': st.get('baseurl') or BASE_URL,
                'account_id': st.get('ilink_bot_id') or '',
                'user_id': st.get('ilink_user_id') or '',
            }
        if state == 'binded_redirect' or st.get('binded_redirect'):
            return {'already_connected': True}
        if state == 'expired':
            return {'expired': True}
        if state == 'scaned_but_redirect':
            host = st.get('redirect_host')
            return {'redirect_base': f'https://{host}'} if host else {}
        if state in ('need_verifycode', 'verify_code_blocked') or st.get('need_verifycode'):
            return {'need_verifycode': True,
                    'blocked': state == 'verify_code_blocked',
                    'retry': bool(verify_code)}
        return {'waiting': True}

    def _qr_login(self) -> bool:
        base = self.base_url
        qrcode, img_url = self._fetch_qrcode(base)
        if not qrcode:
            return False
        qr_ascii = self._render_qr(qrcode)
        GATEWAY_DIR.mkdir(parents=True, exist_ok=True)
        QR_FILE.write_text(f'{qr_ascii}\n\nScan URL (open in browser): {img_url}\n')
        log.info('WeChat iLink: scan the QR with WeChat to authorize ClawBot:')
        log.info('\n' + qr_ascii)
        log.info(f'Scan URL: {img_url}')

        current_base = base
        verify_code: str | None = None
        deadline = time.time() + 5 * 60
        while not self._stop.is_set() and time.time() < deadline:
            result = self._poll_qr_status(current_base, qrcode, verify_code)
            if result.get('bot_token'):
                self._save_credentials(
                    base_url=result['baseurl'], token=result['bot_token'],
                    account_id=result['account_id'], user_id=result['user_id'],
                )
                log.info(f'WeChat iLink: login confirmed (account={self.account_id})')
                return True
            if result.get('already_connected'):
                log.error('WeChat iLink: this WeChat account is already bound to another bot '
                          'instance (e.g. OpenClaw). Unbind it in WeChat first, or use another account.')
                return False
            if result.get('expired'):
                log.error('WeChat iLink: QR expired, please retry')
                return False
            if result.get('redirect_base'):
                log.info(f'WeChat iLink: redirecting login polling to {result["redirect_base"]}')
                current_base = result['redirect_base']
                continue
            if result.get('need_verifycode'):
                if result.get('blocked'):
                    log.error('WeChat iLink: verify code blocked (too many attempts)')
                    return False
                log.info('WeChat iLink: enter the numeric pairing code shown on your phone: ')
                try:
                    verify_code = input().strip()
                except EOFError:
                    log.error('WeChat iLink: cannot read the pairing code here. Run '
                              '`python3 wechat_ilink_login.py` in a terminal to log in.')
                    return False
                continue
            if result.get('error'):
                time.sleep(RETRY_DELAY_SEC)
                continue
            time.sleep(1)
        log.error('WeChat iLink: QR login timed out')
        return False

    @staticmethod
    def _render_qr(content: str) -> str:
        try:
            import qrcode as qrlib
            qr = qrlib.QRCode(border=1)
            qr.add_data(content)
            qr.make(fit=True)
            matrix = qr.get_matrix()
            return '\n'.join(''.join('##' if c else '  ' for c in row) for row in matrix)
        except Exception:
            return f'QR content: {content}'

    # -------------------------------------------------------------------- HTTP
    @staticmethod
    def _base_info() -> dict:
        return {'channel_version': CHANNEL_VERSION, 'bot_agent': BOT_AGENT}

    def _headers(self, body: str) -> dict:
        uin = base64.b64encode(str(random.randint(0, 2 ** 32 - 1)).encode('utf-8')).decode()
        headers = {
            'Content-Type': 'application/json',
            'AuthorizationType': 'ilink_bot_token',
            'X-WECHAT-UIN': uin,
            'iLink-App-Id': ILINK_APP_ID,
            'iLink-App-ClientVersion': ILINK_APP_CLIENT_VERSION,
        }
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        return headers

    def _post(self, endpoint: str, payload: dict, timeout: int = API_TIMEOUT) -> dict:
        body = json.dumps(payload, ensure_ascii=False)
        r = self._session.post(self.base_url + endpoint, data=body,
                               headers=self._headers(body), timeout=timeout)
        r.raise_for_status()
        return r.json()

    # --------------------------------------------------------------- long poll
    def _notify_start(self):
        """Tell the iLink server this bot is online and ready to receive messages.

        Required by the official plugin before the long-poll loop; without it the
        server does not route inbound messages to this bot.
        """
        try:
            resp = self._post('ilink/bot/msg/notifystart', {'base_info': self._base_info()})
            log.info(f'WeChat iLink: notifyStart ok (ret={resp.get("ret")})')
        except Exception as e:
            log.warning(f'WeChat iLink: notifyStart failed: {e}')

    def _long_poll_loop(self):
        self._notify_start()
        while not self._stop.is_set():
            try:
                resp = self._post('ilink/bot/getupdates', {
                    'get_updates_buf': self._get_updates_buf,
                    'base_info': self._base_info(),
                }, timeout=LONG_POLL_TIMEOUT)
            except requests.exceptions.Timeout:
                continue   # normal for long-poll
            except Exception as e:
                self._consecutive_failures += 1
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error(f'WeChat iLink: {MAX_CONSECUTIVE_FAILURES} consecutive failures, '
                              f'backing off {BACKOFF_DELAY_SEC}s')
                    time.sleep(BACKOFF_DELAY_SEC)
                    self._consecutive_failures = 0
                else:
                    time.sleep(RETRY_DELAY_SEC)
                log.warning(f'WeChat iLink: getupdates error: {e}')
                continue

            self._consecutive_failures = 0
            ret = resp.get('ret')
            errcode = resp.get('errcode')
            if errcode == SESSION_EXPIRED_ERRCODE or ret == SESSION_EXPIRED_ERRCODE:
                log.error(f'WeChat iLink: session expired, pausing {SESSION_PAUSE_SEC // 60} min')
                time.sleep(SESSION_PAUSE_SEC)
                continue
            # Success is signalled by ret/errcode being absent or 0; any other
            # value is an error. (The server omits `ret` on a normal long-poll.)
            if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
                log.error(f'WeChat iLink: getupdates ret={ret} errcode={errcode} '
                          f'errmsg={resp.get("errmsg")}')
                time.sleep(RETRY_DELAY_SEC)
                continue

            if resp.get('get_updates_buf'):
                self._get_updates_buf = resp['get_updates_buf']
                self._save_buf()
            for msg in resp.get('msgs') or []:
                try:
                    self._handle_msg(msg)
                except Exception:
                    log.exception('WeChat iLink: handle msg failed')

    # ---------------------------------------------------------------- message
    def _handle_msg(self, msg: dict):
        if msg.get('message_type') != MT_USER:
            return
        user_id = msg.get('from_user_id') or ''
        if not user_id:
            return
        if self.allowed_users and user_id not in self.allowed_users:
            log.info(f'WeChat iLink: ignoring message from unauthorized user {user_id}')
            return
        text = self._extract_text(msg.get('item_list') or [])
        if not text:
            log.debug(f'WeChat iLink: non-text message from {user_id}, skipped')
            return
        ctx = msg.get('context_token') or ''
        if ctx:
            self._ctx_tokens[user_id] = ctx
        log.info(f'WeChat iLink: message from {user_id}: {text[:80]}')
        self._executor.submit(self._process_and_reply, user_id, text, ctx)

    @staticmethod
    def _extract_text(item_list: list) -> str:
        for item in item_list:
            if item.get('type') == MIT_TEXT and item.get('text_item', {}).get('text'):
                return str(item['text_item']['text'])
        return ''

    def _process_and_reply(self, user_id: str, text: str, ctx: str):
        try:
            reply = asyncio.run(self._router.route_message('wechat_ilink', user_id, text))
        except Exception:
            log.exception('WeChat iLink: route_message failed')
            reply = '抱歉，处理你的消息时出错了，请稍后再试。'
        self._send_text(user_id, reply, ctx or self._ctx_tokens.get(user_id, ''))

    def _send_text(self, user_id: str, text: str, ctx: str):
        if not ctx:
            log.warning(f'WeChat iLink: no context_token for {user_id}, reply dropped')
            return
        payload = {
            'msg': {
                'from_user_id': '',
                'to_user_id': user_id,
                'client_id': uuid.uuid4().hex,
                'message_type': MT_BOT,
                'message_state': MS_FINISH,
                'item_list': [{'type': MIT_TEXT, 'text_item': {'text': text}}],
                'context_token': ctx,
            },
            'base_info': self._base_info(),
        }
        try:
            self._post('ilink/bot/sendmessage', payload)
            log.info(f'WeChat iLink: reply sent to {user_id} ({len(text)} chars)')
        except Exception:
            log.exception('WeChat iLink: sendmessage failed')
