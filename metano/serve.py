"""CLI entry point for metano web server."""

import os
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
    # SECURITY (M-08): default to owner-only permissions for every file/dir the
    # process creates (DBs, config, logs, screenshots, uploads) instead of
    # relying on a loose umask.
    os.umask(0o077)
    ensure_jwt_secret()
    ensure_default_admin()
    init_db()  # ensure bridge.db tables exist on fresh deploys / first run
    _start_config_watcher()

    host = os.environ.get('METANO_HOST', '127.0.0.1')
    port = int(os.environ.get('METANO_PORT', '9120'))
    if host not in ('127.0.0.1', '::1', 'localhost'):
        # SECURITY (H-06): default is loopback-only. Public exposure must go
        # through an HTTPS reverse proxy; bind elsewhere only when explicitly
        # requested via METANO_HOST.
        from metano.log import logger
        logger.warning('METANO_HOST=%s is non-loopback — plaintext HTTP is NOT safe on a network. Use an HTTPS reverse proxy.', host)
    uvicorn.run(
        "metano.web_server:app",
        host=host,
        port=port,
        log_level="info",
    )

if __name__ == "__main__":
    main()
