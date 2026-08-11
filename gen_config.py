#!/usr/bin/env python3
"""Generate the initial metano gateway_config.yaml (first-run setup).

Creates (or repairs) the config at $METANO_HOME/gateway_config.yaml
(default: ~/.claude/metano/gateway_config.yaml) with:

  - a random JWT secret  (secrets.token_urlsafe(48), >= 32 chars)
  - a random 16-char admin password (secrets module), stored as a bcrypt
    hash in auth.users so metano.auth can verify logins
  - all message-gateway platforms disabled (discord/feishu/qq/telegram/wechat)
    plus session defaults -- mirroring metano/gateway/launcher.py DEFAULT_CONFIG

The generated admin password is printed to stdout and also saved to
$METANO_HOME/initial_admin_password.txt (chmod 600) so the user can recover it.

Idempotent: if the target config already exists with a complete auth section
(JWT secret >= 32 chars + an admin user), it reports "already exists" and
changes nothing.  --force regenerates the admin password (other sections are
preserved).

``--wizard`` starts an interactive first-run wizard that walks through the LLM
channel (models.default), the optional message channels (feishu / qq / wechat /
telegram / discord) and the optional features (embedding / browser / LAN remote
MCP).  Every step is written to the config immediately, so the wizard is
resumable: Ctrl-C and re-run ``--wizard`` to continue where you left off.  Auth
(JWT secret + admin) is never overwritten unless --force is also given.

Usage:
    python3 gen_config.py            # create if missing / fill missing auth
    python3 gen_config.py --force    # regenerate the admin password
    python3 gen_config.py --wizard   # interactive first-run configuration wizard
    python3 gen_config.py --wizard --home /path/to/data   # custom data dir
"""

import argparse
import copy
import os
import secrets
import string
import sys
from pathlib import Path

try:
    import bcrypt
    import yaml
except ImportError as exc:  # pragma: no cover
    print(f"[gen_config] 缺少依赖: {exc}", file=sys.stderr)
    print("[gen_config] 请先安装: pip install bcrypt pyyaml", file=sys.stderr)
    sys.exit(1)

DEFAULT_METANO_HOME = Path.home() / ".claude" / "metano"
CONFIG_FILENAME = "gateway_config.yaml"
PASSWORD_FILENAME = "initial_admin_password.txt"
ADMIN_USERNAME = "admin"

# Mirrors metano/gateway/launcher.py DEFAULT_CONFIG -- every platform off.
GATEWAY_DEFAULTS = {
    "telegram": {"enabled": False, "bot_token": "", "allowed_users": []},
    "discord": {"enabled": False, "bot_token": "", "guild_id": None, "allowed_channels": []},
    "qq": {"enabled": False, "ws_url": "ws://127.0.0.1:3001", "allowed_groups": []},
    "wechat": {"enabled": False, "method": "wcferry"},
    "feishu": {
        "enabled": False,
        "app_id": "",
        "app_secret": "",
        "encryption_key": "",
        "verification_token": "",
        "allowed_users": [],
    },
    "session": {"max_idle_minutes": 30, "max_history_messages": 50},
}

# Defaults used by the interactive wizard (--wizard).
WIZARD_LLM_DEFAULTS = {
    "base_url": "https://opencode.ai/zen/go",
    "model": "claude-sonnet-4-6",
}
WIZARD_QQ_WS_URL = "ws://127.0.0.1:3001"
WIZARD_MCP_HOST_DEFAULT = "192.168.*.*:*"
WIZARD_EMBED_MODEL = "Snowflake/snowflake-arctic-embed-xs"


def metano_home(override: str = "") -> Path:
    """Return the metano data root (--home > $METANO_HOME > ~/.claude/metano)."""
    return Path(override or os.environ.get("METANO_HOME") or DEFAULT_METANO_HOME).expanduser()


def config_path(home: Path) -> Path:
    return home / CONFIG_FILENAME


def auth_complete(cfg) -> bool:
    """True when the config already has a usable JWT secret + an admin user."""
    if not isinstance(cfg, dict):
        return False
    auth = cfg.get("auth")
    if not isinstance(auth, dict):
        return False
    secret = auth.get("jwt_secret") or ""
    if not isinstance(secret, str) or len(secret) < 32:
        return False
    users = auth.get("users") or []
    return any(
        isinstance(u, dict) and u.get("role") == "admin" and u.get("password")
        for u in users
    )


def _read_yaml(path: Path) -> dict:
    """Read a YAML file as dict; abort on parse error (never touch a broken file)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        print(f"[gen_config] 读取 {path} 失败: {exc}", file=sys.stderr)
        print("[gen_config] 为避免破坏现有配置，已退出，请手工检查该文件。", file=sys.stderr)
        sys.exit(1)
    return data if isinstance(data, dict) else {}


def new_jwt_secret() -> str:
    return secrets.token_urlsafe(48)


def new_admin_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def write_password_file(home: Path, password: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / PASSWORD_FILENAME
    path.write_text(f"{ADMIN_USERNAME} / {password}\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _order_config(cfg: dict) -> dict:
    """Return a copy of cfg with the auth section placed first in the YAML output."""
    if not isinstance(cfg, dict) or "auth" not in cfg:
        return cfg
    return {"auth": cfg["auth"], **{k: v for k, v in cfg.items() if k != "auth"}}


def _write_config(cfg: dict, home: Path, path: Path) -> None:
    """Write cfg to path (auth section first); idempotent."""
    home.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(_order_config(cfg), allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    # SECURITY (S2): config holds secrets (JWT secret, bcrypt password hashes,
    # api_key) — force owner-only permissions instead of relying on umask.
    os.chmod(path, 0o600)


def _set_env(home: Path, key: str, value: str) -> Path:
    """Idempotently set ``key=value`` in ``$METANO_HOME/.env`` (created if missing).

    Preserves existing lines/comments; only the matching ``key=`` line is
    replaced.  Returns the path to the .env file.
    """
    home.mkdir(parents=True, exist_ok=True)
    env_path = home / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out: list[str] = []
    found = False
    for ln in lines:
        if ln.startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    # SECURITY (S2): .env holds secrets (api_key etc.) — force owner-only perms.
    os.chmod(env_path, 0o600)
    return env_path


def _ask(prompt: str, default: str = "") -> str:
    """Ask a free-text question. Empty input returns ``default``."""
    hint = f" [默认: {default}]" if default else ""
    try:
        val = input(f"{prompt}{hint} ").strip()
    except EOFError:
        return default
    except KeyboardInterrupt:
        raise
    return val or default


def _ask_bool(prompt: str, default: bool = False) -> bool:
    """Ask a yes/no question ([y/N] or [Y/n]). Empty input returns ``default``."""
    hint = "[Y/n]" if default else "[y/N]"
    try:
        val = input(f"{prompt} {hint} ").strip().lower()
    except EOFError:
        return default
    except KeyboardInterrupt:
        raise
    if not val:
        return default
    return val in ("y", "yes", "是", "1", "true")


def _ask_llm_api_key(dflt: dict) -> str:
    """Prompt for the LLM api_key (required); bounded retry, never blocks forever."""
    api_key = _ask("API Key（必填）", dflt.get("api_key") or "")
    attempts = 0
    while not api_key and attempts < 2:
        print("  ⚠️  API Key 不能为空（AI 对话/记忆/进化都需要它）。")
        api_key = _ask("API Key（再次输入，仍留空则跳过）", "")
        attempts += 1
    return api_key


def run_wizard(args) -> int:
    """Interactive first-run configuration wizard (--wizard).

    Walks through:
      a. LLM 通道 (required)   -> models.default
      b. 消息渠道 (optional)   -> feishu / qq / wechat / telegram / discord
      c. 可选功能              -> embedding / browser / LAN remote MCP

    Every section is written to gateway_config.yaml immediately (idempotent),
    so interrupting with Ctrl-C and re-running ``--wizard`` continues from the
    last saved section.  Existing auth (JWT secret + admin) is preserved unless
    ``--force`` is also given.
    """
    home = metano_home(args.home)
    path = config_path(home)
    exists = path.exists()
    existing = _read_yaml(path) if exists else {}
    cfg = copy.deepcopy(existing) if isinstance(existing, dict) else {}

    # Ensure every platform/session section exists; keep existing values.
    for key, defaults in GATEWAY_DEFAULTS.items():
        if not isinstance(cfg.get(key), dict):
            cfg[key] = copy.deepcopy(defaults)

    # ── Auth: keep existing unless missing/incomplete or --force ───────────
    auth = cfg.get("auth")
    if not isinstance(auth, dict):
        auth = {}
    cfg["auth"] = auth
    new_password = None
    if auth_complete(cfg) and not args.force:
        auth_preserved = True
    else:
        secret = auth.get("jwt_secret") or ""
        if not isinstance(secret, str) or len(secret) < 32:
            auth["jwt_secret"] = new_jwt_secret()
        new_password = new_admin_password()
        hashed = hash_password(new_password)
        users = [
            u for u in (auth.get("users") or [])
            if not (isinstance(u, dict) and u.get("role") == "admin")
        ]
        users.append({"username": ADMIN_USERNAME, "password": hashed, "role": "admin"})
        auth["users"] = users
        auth_preserved = False

    print()
    print("=" * 62)
    print("  metano 配置引导向导")
    print(f"  配置文件: {path}")
    print("=" * 62)
    print("  逐项交互配置；回车即用默认值，随时 Ctrl-C 中断后重跑不丢进度。")

    # ── a. LLM 通道（必填）───────────────────────────────────────────────
    print()
    print("── ① LLM 通道（必填，AI 对话 / 记忆 / 进化都依赖它）──")
    models = cfg.get("models")
    if not isinstance(models, dict):
        models = {}
    dflt = models.get("default")
    if not isinstance(dflt, dict):
        dflt = {}
    base_url = _ask("Base URL", dflt.get("base_url") or WIZARD_LLM_DEFAULTS["base_url"])
    api_key = _ask_llm_api_key(dflt)
    model = _ask("模型名称 (model)", dflt.get("model") or WIZARD_LLM_DEFAULTS["model"])
    models["default"] = {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "max_tokens": int(dflt.get("max_tokens") or 4096),
        "default": True,
        "enabled": True,
    }
    cfg["models"] = models
    _write_config(cfg, home, path)
    if not api_key:
        print("  ⚠️ 未填写 API Key，AI 功能需 key 后才可用；可重跑向导补填。")
    else:
        print(f"  ✓ models.default 已写入: base_url={base_url}  model={model}")

    # ── b. 消息渠道（每项按需启用）───────────────────────────────────────
    print()
    print("── ② 消息渠道（每项按需启用，全部可跳过）──")

    # 飞书
    feishu = cfg.get("feishu") or {}
    print()
    print("[飞书 Feishu] 接入飞书机器人。需先在飞书开放平台创建应用并配置事件订阅。")
    if _ask_bool("是否启用飞书？", feishu.get("enabled", False)):
        app_id = _ask("App ID", feishu.get("app_id") or "")
        app_secret = _ask("App Secret", feishu.get("app_secret") or "")
        encryption_key = _ask("Encryption Key（可选）", feishu.get("encryption_key") or "")
        verification_token = _ask("Verification Token（可选）", feishu.get("verification_token") or "")
        cfg["feishu"] = {
            "enabled": True,
            "app_id": app_id,
            "app_secret": app_secret,
            "encryption_key": encryption_key,
            "verification_token": verification_token,
            "allowed_users": feishu.get("allowed_users", []),
        }
        print("  ✓ feishu 已启用")
    else:
        cfg["feishu"] = {**feishu, "enabled": False}
    _write_config(cfg, home, path)

    # QQ
    qq = cfg.get("qq") or {}
    print()
    print("[QQ] 基于 OneBot v11 WebSocket 协议，需先运行 NapCat（或其它 OneBot 实现）监听该地址。")
    if _ask_bool("是否启用 QQ？", qq.get("enabled", False)):
        ws_url = _ask("WebSocket 地址", qq.get("ws_url") or WIZARD_QQ_WS_URL)
        cfg["qq"] = {"enabled": True, "ws_url": ws_url, "allowed_groups": qq.get("allowed_groups", [])}
        print(f"  ✓ qq 已启用 (ws_url={ws_url})")
    else:
        cfg["qq"] = {**qq, "enabled": False}
    _write_config(cfg, home, path)

    # 微信
    wechat = cfg.get("wechat") or {}
    print()
    print("[微信 WeChat] 接入方式: wcferry（Windows 微信机器人框架）或 ilink（iPad 协议）。")
    if _ask_bool("是否启用微信？", wechat.get("enabled", False)):
        method = _ask("接入方式 (wcferry / ilink)", wechat.get("method") or "wcferry").strip().lower()
        if method not in ("wcferry", "ilink"):
            method = "wcferry"
        cfg["wechat"] = {"enabled": True, "method": method}
        print(f"  ✓ wechat 已启用 (method={method})")
    else:
        cfg["wechat"] = {**wechat, "enabled": False}
    _write_config(cfg, home, path)

    # Telegram
    telegram = cfg.get("telegram") or {}
    print()
    print("[Telegram] 需先通过 @BotFather 创建机器人获取 bot_token。")
    if _ask_bool("是否启用 Telegram？", telegram.get("enabled", False)):
        bot_token = _ask("Bot Token", telegram.get("bot_token") or "")
        cfg["telegram"] = {
            "enabled": True,
            "bot_token": bot_token,
            "allowed_users": telegram.get("allowed_users", []),
        }
        if not bot_token:
            print("  ⚠️ bot_token 为空，Telegram 无法连接；可稍后补填。")
        else:
            print("  ✓ telegram 已启用")
    else:
        cfg["telegram"] = {**telegram, "enabled": False}
    _write_config(cfg, home, path)

    # Discord
    discord = cfg.get("discord") or {}
    print()
    print("[Discord] 需在 Discord Developer Portal 创建应用并邀请机器人入服。")
    if _ask_bool("是否启用 Discord？", discord.get("enabled", False)):
        bot_token = _ask("Bot Token", discord.get("bot_token") or "")
        guild_id = _ask("Guild ID（可选）", str(discord.get("guild_id") or ""))
        cfg["discord"] = {
            "enabled": True,
            "bot_token": bot_token,
            "guild_id": int(guild_id) if guild_id.strip().isdigit() else None,
            "allowed_channels": discord.get("allowed_channels", []),
        }
        print("  ✓ discord 已启用")
    else:
        cfg["discord"] = {**discord, "enabled": False}
    _write_config(cfg, home, path)

    # ── c. 可选功能 ───────────────────────────────────────────────────────
    print()
    print("── ③ 可选功能（不改变核心运行）──")

    embedding_yes = _ask_bool(
        "是否安装本地向量嵌入（知识库语义检索，需 sentence-transformers + torch，体积大）？",
        False,
    )
    if embedding_yes:
        _set_env(home, "HF_HUB_OFFLINE", "1")
        _set_env(home, "METANO_EMBED_MODEL", WIZARD_EMBED_MODEL)
        print("  已写入 $METANO_HOME/.env: HF_HUB_OFFLINE=1, METANO_EMBED_MODEL=" + WIZARD_EMBED_MODEL)
        print("  提示: 退出向导后运行  ./install.sh --with-embedding  安装依赖（torch 体积大）。")

    browser_yes = _ask_bool(
        "是否安装 Playwright 浏览器自动化（网页浏览 / 截图工具）？",
        False,
    )
    if browser_yes:
        print("  提示: 退出向导后运行  ./install.sh --with-browser  安装并下载 chromium。")

    mcp_yes = _ask_bool(
        "是否允许局域网访问只读远程 MCP（跨设备调用本机工具，/mcp JWT 鉴权）？",
        False,
    )
    mcp_pattern = ""
    if mcp_yes:
        mcp_pattern = _ask(
            "允许的 Host 模式（逗号分隔，每项含端口通配，如 192.168.1.50:*,nas.local:*）",
            WIZARD_MCP_HOST_DEFAULT,
        )
        env_file = _set_env(home, "METANO_ALLOWED_HOSTS", mcp_pattern)
        print(f"  已写入 {env_file}: METANO_ALLOWED_HOSTS={mcp_pattern}")
        print("  提示: 重启服务后生效；另一台机器在 Claude Code 中配置 metano-local MCP server 即可调用。")

    _write_config(cfg, home, path)

    # ── 收尾：保存 admin 密码 + 总结 ────────────────────────────────────
    if new_password:
        pw_file = write_password_file(home, new_password)
        print()
        print("  初始管理员账号")
        print(f"    用户名:  {ADMIN_USERNAME}")
        print(f"    密码:    {new_password}")
        print(f"    密码已保存至: {pw_file} (权限 600)")
    else:
        print()
        print("  认证配置已存在，保留原 JWT secret / admin 密码。")
        print("  （如需重置 admin 密码，可运行: python3 gen_config.py --force）")

    print()
    print("=" * 62)
    print(f"[gen_config] 配置完成: {path}")
    print()
    print("  已配置项:")
    print(f"    • LLM:      base_url={base_url}  model={model}  api_key={'***' if api_key else '(空)'}")
    for key in ("feishu", "qq", "wechat", "telegram", "discord"):
        if cfg.get(key, {}).get("enabled"):
            print(f"    • {key}: 已启用")
    if embedding_yes:
        print("    • 本地向量嵌入: 计划安装（记得运行 ./install.sh --with-embedding）")
    if browser_yes:
        print("    • Playwright 浏览器: 计划安装（记得运行 ./install.sh --with-browser）")
    if mcp_yes:
        print(f"    • 远程只读 MCP: 允许局域网 ({mcp_pattern})")
    print()
    print("  下一步:")
    print("    1. 启动/重启服务使配置生效:  bash metano.sh start")
    print("    2. 浏览器打开 http://localhost:9120 并登录（admin / 上述密码）")
    print("    3. 消息渠道前置: 飞书开放平台 / NapCat / @BotFather / Discord Developer Portal")
    print("    4. 查看 README.md 的「配置引导」节做自查清单")
    print("=" * 62)
    return 0


def build_config(existing: dict, exists: bool, force: bool):
    """Return (config, mode, admin_password).

    mode is one of:
      - "fresh"   the target config did not exist
      - "filled"  config existed but auth was incomplete (only auth is touched)
      - "forced"  --force regenerated the admin password (other sections kept)
    """
    cfg = copy.deepcopy(existing) if isinstance(existing, dict) else {}

    # Ensure every platform/session section exists; keep any existing values.
    for key, defaults in GATEWAY_DEFAULTS.items():
        if not isinstance(cfg.get(key), dict):
            cfg[key] = copy.deepcopy(defaults)

    # Auth: keep a valid existing JWT secret; otherwise mint a new one.
    auth = cfg.get("auth")
    if not isinstance(auth, dict):
        auth = {}
    secret = auth.get("jwt_secret") or ""
    if not isinstance(secret, str) or len(secret) < 32:
        auth["jwt_secret"] = new_jwt_secret()

    # Admin user: always (re)generate here (missing admin, or --force).
    password = new_admin_password()
    hashed = hash_password(password)
    users = [
        u for u in (auth.get("users") or [])
        if not (isinstance(u, dict) and u.get("role") == "admin")
    ]
    users.append({"username": ADMIN_USERNAME, "password": hashed, "role": "admin"})
    auth["users"] = users

    # Put auth first in the YAML output.
    cfg = {"auth": auth, **{k: v for k, v in cfg.items() if k != "auth"}}

    if not exists:
        mode = "fresh"
    elif force:
        mode = "forced"
    else:
        mode = "filled"

    return cfg, mode, password


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate/repair metano gateway_config.yaml (JWT secret + admin password; --wizard interactive setup)."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="重新生成 admin 密码（保留配置中其它内容）",
    )
    parser.add_argument(
        "--wizard",
        action="store_true",
        help="交互式配置向导：LLM 通道 + 消息渠道 + 可选功能（幂等，可中断重跑）",
    )
    parser.add_argument(
        "--home",
        metavar="DIR",
        help="覆盖 METANO_HOME 数据目录（默认 $METANO_HOME 或 ~/.claude/metano）",
    )
    args = parser.parse_args(argv)

    if args.wizard:
        try:
            return run_wizard(args)
        except KeyboardInterrupt:
            print("\n[gen_config] 向导已中断。已完成的部分已写入配置，重跑 --wizard 可继续。")
            return 130

    home = metano_home(args.home)
    path = config_path(home)
    exists = path.exists()

    if exists:
        existing = _read_yaml(path)
        if auth_complete(existing) and not args.force:
            print("[gen_config] 已存在且认证配置完整，未做任何修改:")
            print(f"  {path}")
            print("[gen_config] 如需重新生成 admin 密码，请加 --force。")
            return 0
    else:
        existing = {}

    cfg, mode, password = build_config(existing, exists=exists, force=args.force)

    _write_config(cfg, home, path)

    pw_file = write_password_file(home, password)

    label = {
        "fresh": "已生成全新配置",
        "filled": "已补齐缺失的认证配置",
        "forced": "已重新生成 admin 密码",
    }[mode]
    print("=" * 62)
    print(f"[gen_config] {label}: {path}")
    print()
    print("  初始管理员账号")
    print(f"    用户名:  {ADMIN_USERNAME}")
    print(f"    密码:    {password}")
    print()
    print(f"  密码已保存至: {pw_file} (权限 600)")
    print()
    print("  ⚠️  首次登录后请立即修改默认密码。")
    print("      各消息网关默认处于 disabled 状态，可在 gateway_config.yaml 中启用。")
    print("      如需交互式配置全部核心功能，可运行: python3 gen_config.py --wizard")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
