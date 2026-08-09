"""CLI entry point for metano web server."""

import uvicorn

def main():
    from .auth import ensure_jwt_secret, ensure_default_admin
    ensure_jwt_secret()
    ensure_default_admin()

    uvicorn.run(
        "metano.web_server:app",
        host="0.0.0.0",
        port=9120,
        log_level="info",
    )

if __name__ == "__main__":
    main()
