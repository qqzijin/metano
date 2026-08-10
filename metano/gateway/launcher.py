"""Gateway launcher: starts all enabled platform bots."""

import asyncio
import logging
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
    "qq": {"enabled": False, "ws_url": "ws://127.0.0.1:3001", "allowed_groups": []},
    "wechat": {"enabled": False, "method": "wcferry"},
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
    router = MessageRouter()
    bots = []

    # Telegram
    if config.get("telegram", {}).get("enabled") and config["telegram"].get("bot_token"):
        tg = TelegramBot(
            token=config["telegram"]["bot_token"],
            allowed_users=config["telegram"].get("allowed_users", []),
        )
        bots.append(("telegram", tg))

    # Discord
    if config.get("discord", {}).get("enabled") and config["discord"].get("bot_token"):
        dc = DiscordBot(
            token=config["discord"]["bot_token"],
            guild_id=config["discord"].get("guild_id"),
            allowed_channels=config["discord"].get("allowed_channels", []),
        )
        bots.append(("discord", dc))

    # QQ
    if config.get("qq", {}).get("enabled"):
        qq = QQBot(
            ws_url=config["qq"].get("ws_url", "ws://127.0.0.1:3001"),
            allowed_groups=config["qq"].get("allowed_groups", []),
        )
        bots.append(("qq", qq))

    # WeChat
    wx_cfg = config.get("wechat", {})
    if wx_cfg.get("enabled"):
        if wx_cfg.get("method", "wcferry") == "ilink":
            from .wechat_ilink import WeChatIlinkBot
            wx = WeChatIlinkBot(config=wx_cfg, router=router)
        else:
            wx = WeChatBot(method=wx_cfg.get("method", "wcferry"))
        bots.append(("wechat", wx))

    # Feishu/Lark
    if config.get("feishu", {}).get("enabled") and config["feishu"].get("app_id"):
        fs = FeishuBot(
            config=config["feishu"],
            router=router,
        )
        bots.append(("feishu", fs))

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
    asyncio.run(run_gateway())


if __name__ == "__main__":
    main()