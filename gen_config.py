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

Usage:
    python3 gen_config.py            # create if missing / fill missing auth
    python3 gen_config.py --force    # regenerate the admin password
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


def metano_home() -> Path:
    return Path(os.environ.get("METANO_HOME") or DEFAULT_METANO_HOME).expanduser()


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
        description="Generate/repair metano gateway_config.yaml (JWT secret + admin password)."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="重新生成 admin 密码（保留配置中其它内容）",
    )
    args = parser.parse_args(argv)

    home = metano_home()
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

    home.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )

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
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
