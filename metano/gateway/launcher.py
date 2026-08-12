"""Gateway launcher: starts all enabled platform bots."""

import asyncio
import logging
import os
import yaml

from .telegram import TelegramBot
from .discord_bot import DiscordBot
from .qq import QQBot
from .wechat import WeChatBot
from .feishu import FeishuBot
from ..paths import CONFIG_PATH

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

DEFAULT_CONFIG = {
    "telegram": {"enabled": False, "bot_token": "", "allowed_users": []},
    "discord": {"enabled": False, "bot_token": "", "guild_id": None, "allowed_channels": []},
    "qq": {"enabled": False, "ws_url": "ws://127.0.0.1:3001", "token": "", "self_id": "",
           "allowed_groups": [], "allowed_users": []},
    "wechat": {"enabled": False, "method": "wcferry", "allowed_users": []},
    "feishu": {"enabled": False, "app_id": "", "app_secret": "", "encryption_key": "", "verification_token": "", "allowed_users": []},
    "session": {"max_idle_minutes": 30, "max_history_messages": 50},
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or DEFAULT_CONFIG
    # Write default config
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False)
    return DEFAULT_CONFIG


async def run_gateway():
    config = load_config()
    from .router import MessageRouter
    # M-08(func): session sizing from config must actually reach the router.
    router = MessageRouter(session_cfg=config.get("session", {}) or {})
    bots = []

    # Telegram — SECURITY (H-08): fail-closed whitelist. An enabled channel with
    # an empty allowlist is refused at startup (empty whitelist == deny all).
    tg_cfg = config.get("telegram", {}) or {}
    if tg_cfg.get("enabled"):
        if not tg_cfg.get("bot_token"):
            log.error("telegram enabled but bot_token missing; refusing to start telegram")
        elif not tg_cfg.get("allowed_users"):
            log.error("telegram enabled but allowed_users is empty; refusing to start telegram "
                      "(empty whitelist = deny all)")
        else:
            bots.append(("telegram", TelegramBot(
                token=tg_cfg["bot_token"],
                allowed_users=tg_cfg.get("allowed_users", []),
            )))

    # Discord
    dc_cfg = config.get("discord", {}) or {}
    if dc_cfg.get("enabled"):
        if not dc_cfg.get("bot_token"):
            log.error("discord enabled but bot_token missing; refusing to start discord")
        elif not dc_cfg.get("allowed_channels"):
            log.error("discord enabled but allowed_channels is empty; refusing to start discord "
                      "(empty whitelist = deny all)")
        else:
            bots.append(("discord", DiscordBot(
                token=dc_cfg["bot_token"],
                guild_id=dc_cfg.get("guild_id"),
                allowed_channels=dc_cfg.get("allowed_channels", []),
            )))

    # QQ — only loopback by default; remote requires wss + token (validated in QQBot).
    qq_cfg = config.get("qq", {}) or {}
    if qq_cfg.get("enabled"):
        if not qq_cfg.get("allowed_groups") and not qq_cfg.get("allowed_users"):
            log.error("qq enabled but allowed_groups/allowed_users are both empty; "
                      "refusing to start qq (empty whitelist = deny all)")
        else:
            bots.append(("qq", QQBot(
                ws_url=qq_cfg.get("ws_url", "ws://127.0.0.1:3001"),
                allowed_groups=qq_cfg.get("allowed_groups", []),
                allowed_users=qq_cfg.get("allowed_users", []),
                token=qq_cfg.get("token", ""),
                self_id=qq_cfg.get("self_id", ""),
            )))

    # WeChat
    wx_cfg = config.get("wechat", {}) or {}
    if wx_cfg.get("enabled"):
        if wx_cfg.get("method", "wcferry") == "ilink":
            if not wx_cfg.get("allowed_users"):
                log.error("wechat (ilink) enabled but allowed_users is empty; "
                          "refusing to start wechat (empty whitelist = deny all)")
            else:
                from .wechat_ilink import WeChatIlinkBot
                bots.append(("wechat", WeChatIlinkBot(config=wx_cfg, router=router)))
        else:
            if not wx_cfg.get("allowed_users"):
                log.error("wechat (wcferry) enabled but allowed_users is empty; "
                          "refusing to start wechat (empty whitelist = deny all)")
            else:
                bots.append(("wechat", WeChatBot(
                    method=wx_cfg.get("method", "wcferry"),
                    allowed_users=wx_cfg.get("allowed_users", []),
                )))

    # Feishu/Lark
    fs_cfg = config.get("feishu", {}) or {}
    if fs_cfg.get("enabled"):
        if not fs_cfg.get("app_id") or not fs_cfg.get("app_secret"):
            log.error("feishu enabled but app_id/app_secret missing; refusing to start feishu")
        elif not fs_cfg.get("allowed_users"):
            log.error("feishu enabled but allowed_users is empty; refusing to start feishu "
                      "(empty whitelist = deny all)")
        else:
            bots.append(("feishu", FeishuBot(config=fs_cfg, router=router)))

    if not bots:
        log.info("No platforms enabled. Edit gateway_config.yaml to enable one.")
        return

    # Start all bots
    tasks = []
    for name, bot in bots:
        log.info(f"Starting {name} bot...")
        if name in ("wechat", "feishu"):
            import threading
            t = threading.Thread(target=bot.start, daemon=False)
            t.start()
        else:
            tasks.append(bot.start())

    # Keep running: if only thread-based bots, block on the thread
    if not tasks:
        import signal
        log.info("Gateway running (thread-based bots only). Press Ctrl+C to stop.")
        signal.pause()
    elif tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def main():
    os.umask(0o077)
    asyncio.run(run_gateway())


if __name__ == "__main__":
    main()