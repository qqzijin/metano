"""Project-wide structured logging. All modules should use this instead of print()."""

import logging
import os
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

LOG_FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# M12: rotation + retention — bounded 20MB live file, 5 numbered backups.
_MAX_BYTES = 20 * 1024 * 1024
_BACKUP_COUNT = 5


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
        handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
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
                fh.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
                logger.addHandler(fh)
            except Exception:
                pass
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