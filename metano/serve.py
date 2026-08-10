"""CLI entry point for metano web server."""

import uvicorn

def main():
    from .auth import ensure_jwt_secret, ensure_default_admin
    from .db import init_db
    ensure_jwt_secret()
    ensure_default_admin()
    init_db()  # ensure bridge.db tables exist on fresh deploys / first run

    uvicorn.run(
        "metano.web_server:app",
        host="0.0.0.0",
        port=9120,
        log_level="info",
    )

if __name__ == "__main__":
    main()
