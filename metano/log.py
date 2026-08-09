"""Project-wide structured logging. All modules should use this instead of print()."""

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str = "metano") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
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