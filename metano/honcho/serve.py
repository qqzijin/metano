"""CLI entry point for Honcho service."""

import uvicorn

def main():
    uvicorn.run(
        "metano.honcho.server:app",
        host="0.0.0.0",
        port=9121,
        log_level="info",
    )

if __name__ == "__main__":
    main()