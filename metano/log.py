"""Project-wide structured logging. All modules should use this instead of print()."""

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from logging.handlers import RotatingFileHandler

# audit 7-1: timestamps are ISO-8601 with an explicit local offset everywhere
# (e.g. ``2026-08-13T17:16:54+08:00``) — never M/D/YY, never bare.
LOG_FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
# Kept for backward compatibility; IsoFormatter ignores datefmt so every
# ``%(asctime)s`` carries a zone offset.
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# M12: rotation + retention — bounded 20MB live file, 5 numbered backups.
_MAX_BYTES = 20 * 1024 * 1024
_BACKUP_COUNT = 5


class IsoFormatter(logging.Formatter):
    """Formatter whose ``%(asctime)s`` is ISO-8601 with an explicit zone offset.

    ``record.created`` is a UTC epoch float; ``.astimezone()`` renders it in the
    process's local zone, so the ``+08:00`` suffix is always present and correct.
    """

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone().isoformat(timespec='seconds')


def ensure_iso_root_handler() -> None:
    """Give the root logger an ISO-8601 StreamHandler (idempotent).

    The fastmcp library calls ``logging.basicConfig`` with a rich ``RichHandler``
    (M/D/YY timestamps — audit 7-1) the moment a FastMCP app is created.
    ``basicConfig`` is a no-op once the root logger already has handlers, so
    seeding the root logger *before* the mcp import prevents that M/D/YY handler
    from ever being installed.  Third-party loggers (mcp, asyncio, ...) that
    propagate to the root logger then render with an explicit ISO timestamp too.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(IsoFormatter(LOG_FORMAT))
        root.addHandler(handler)
        root.setLevel(logging.INFO)


def _default_log_file() -> str:
    try:
        from .paths import GATEWAY_DIR
        return str(GATEWAY_DIR / 'metano.log')
    except Exception:
        return ''


def get_logger(name: str = "metano") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(IsoFormatter(LOG_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        # M12: also mirror to a rotating on-disk log (0600 — the audit showed
        # logs can carry credentials, so the file must never be world-readable).
        log_file = os.environ.get('METANO_LOG_FILE') or _default_log_file()
        if log_file:
            try:
                p = Path(log_file)
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    fd = os.open(str(p), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
                    os.close(fd)
                else:
                    os.chmod(str(p), 0o600)
                fh = RotatingFileHandler(log_file, maxBytes=_MAX_BYTES,
                                         backupCount=_BACKUP_COUNT, encoding='utf-8')
                fh.setFormatter(IsoFormatter(LOG_FORMAT))
                logger.addHandler(fh)
            except Exception:
                pass
    # audit 7-1: never let records bubble up to the root logger.  fastmcp installs
    # a rich (M/D/YY) RichHandler on the root logger; without this the same record
    # would be rendered twice — once here (ISO) and once by the root handler.
    logger.propagate = False
    return logger


# Root logger for the project
def _patch_exception():
    """Make logger.exception() work without arguments (default msg='')."""
    _orig_exception = logging.Logger.exception
    def _exception(self, msg='', *args, **kwargs):
        kwargs.setdefault('exc_info', True)
        _orig_exception(self, msg, *args, **kwargs)
    logging.Logger.exception = _exception

_patch_exception()

logger = get_logger("metano")