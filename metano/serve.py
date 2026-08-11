"""CLI entry point for metano web server."""

import uvicorn

def _reload_config(config: dict):
    """Hot-reload callback for gateway_config.yaml changes.

    ``model_router`` is the only component that caches config in memory (models,
    prices, default). Auth users are re-read from the file on every request
    (see auth._load_config), so they pick up changes without a reload.
    """
    try:
        from .model_router import model_router
        model_router.refresh()
    except Exception:
        from metano.log import logger
        logger.exception('[config_watcher] 刷新 model_router 失败（保留旧配置）')


def _start_config_watcher():
    try:
        from .config_watcher import start_config_watcher
        start_config_watcher(reload_fn=_reload_config)
    except Exception:
        from metano.log import logger
        logger.exception('[config_watcher] 启动失败（配置热重载不可用，继续以静态配置运行）')


def main():
    from .auth import ensure_jwt_secret, ensure_default_admin
    from .db import init_db
    ensure_jwt_secret()
    ensure_default_admin()
    init_db()  # ensure bridge.db tables exist on fresh deploys / first run
    _start_config_watcher()

    uvicorn.run(
        "metano.web_server:app",
        host="0.0.0.0",
        port=9120,
        log_level="info",
    )

if __name__ == "__main__":
    main()
