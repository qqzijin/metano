#!/usr/bin/env python3
"""Login to WeChat ClawBot (iLink) and persist credentials for the gateway.

Run this in a foreground terminal BEFORE starting the gateway:

    python3 wechat_ilink_login.py

It shows a QR code; scan it with WeChat (8.0.70+, or use the ClawBot entry
in WeChat 设置 -> 插件) and confirm. Credentials are saved to
``~/.claude/metano/gateway/wechat_ilink_credentials.json`` (mode 0600) and
reused automatically on later startups.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from metano.gateway.router import MessageRouter
from metano.gateway.wechat_ilink import WeChatIlinkBot


def main() -> None:
    force = '--force' in sys.argv or '-f' in sys.argv
    bot = WeChatIlinkBot({'method': 'ilink'}, router=MessageRouter())
    if bot.token and not force:
        print(f'Already logged in as account {bot.account_id}. Re-login? [y/N] ', end='', flush=True)
        if input().strip().lower() != 'y':
            print('Nothing changed.')
            return
    ok = bot._qr_login()
    print('WeChat login OK. You can now start the gateway.' if ok else 'WeChat login failed.')


if __name__ == '__main__':
    main()
