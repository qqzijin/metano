"""Hot-reload ``gateway_config.yaml`` in the web process.

Polling watcher (daemon thread) that detects configuration changes and calls a
registered ``reload_fn(config)`` callback. Design decisions:

- **mtime + size fast path**: on each poll we only ``stat()`` the file; a read
  happens only when the file actually changed. This keeps the cost near zero on
  slow/network filesystems (remote NAS) while still detecting writes reliably.
- **content-equality skip**: a touch / whitespace / key-order-only edit produces
  an identical *normalized signature*, so no reload is triggered.
- **stable read**: after detecting a change we re-read until two consecutive
  ``stat()`` snapshots agree, so a half-written file (e.g. an in-place truncate
  during write) is never applied.
- **fail-safe**: if ``reload_fn`` raises, the error is logged, the previous
  (in-memory) config is kept, and the same bad content is not retried every
  poll — it is retried only after the file changes again.
"""

import hashlib
import json
import threading
import time
from typing import Callable, Optional

from metano.log import logger
from metano.paths import CONFIG_PATH


class ConfigWatcher:
    """Monitors a YAML config file and reloads on real content changes."""

    def __init__(
        self,
        config_path: Optional[object] = None,
        reload_fn: Optional[Callable[[dict], None]] = None,
        interval: float = 2.0,
    ):
        self.config_path = config_path if config_path is not None else CONFIG_PATH
        self.reload_fn = reload_fn
        self.interval = interval

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # Signature of the config currently in effect.
        self._applied_sig: Optional[str] = None
        # Signature of the last content we tried to apply (success or failure).
        self._attempted_sig: Optional[str] = None
        # Last observed (mtime_ns, size); used as a cheap change detector.
        self._last_seen: Optional[tuple] = None

        self.status = {
            'running': False,
            'watched_path': str(self.config_path),
            'interval': self.interval,
            'last_checked': None,
            'last_reload_attempt': None,
            'last_success': None,
            'last_error': None,
            'reload_count': 0,
            'skipped_identical': 0,
            'file_missing': False,
        }

        # Pre-seed the applied signature with the current file so startup does
        # not trigger a redundant "reload"; only subsequent changes reload.
        try:
            initial = self._read_config_stable()
            if initial is not None:
                self._applied_sig = self._signature(initial)
                st = self.config_path.stat()
                self._last_seen = (st.st_mtime_ns, st.st_size)
        except Exception:
            logger.debug('[config_watcher] 预读初始配置失败（文件可能尚不存在）', exc_info=True)

    # ------------------------------------------------------------------ utils

    @staticmethod
    def _signature(config: dict) -> str:
        """Order-insensitive, comment-insensitive signature of a parsed config."""
        return hashlib.sha256(
            json.dumps(config, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')
        ).hexdigest()

    def _read_config_stable(self, max_retries: int = 5, settle: float = 0.05) -> Optional[dict]:
        """Read the config, retrying until the file is not mid-write.

        Returns ``None`` when the file does not exist. Never raises for a parse
        error -- a malformed file yields ``{}`` so the caller can decide.
        """
        import yaml
        path = self.config_path
        for _ in range(max_retries):
            try:
                before = path.stat()
                raw = path.read_bytes()
                after = path.stat()
            except FileNotFoundError:
                return None
            except OSError:
                return None
            if (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size):
                try:
                    data = yaml.safe_load(raw) or {}
                except Exception:
                    # Malformed YAML -- surface as empty dict, caller logs.
                    return {}
                return data if isinstance(data, dict) else {}
            time.sleep(settle)
        # Best effort after retries.
        try:
            data = yaml.safe_load(path.read_bytes()) or {}
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    # -------------------------------------------------------------- polling

    def _check(self) -> None:
        now = time.time()
        self.status['last_checked'] = now

        try:
            st = self.config_path.stat()
        except FileNotFoundError:
            self.status['file_missing'] = True
            self._last_seen = None
            return
        except OSError as exc:
            self.status['last_error'] = f'stat failed: {exc}'
            return
        self.status['file_missing'] = False

        seen = (st.st_mtime_ns, st.st_size)
        if seen == self._last_seen:
            return  # nothing changed
        self._last_seen = seen

        config = self._read_config_stable()
        if config is None:
            return  # vanished during read; retry next poll

        sig = self._signature(config)
        if sig == self._applied_sig:
            # File touched / rewritten with identical content -- no reload.
            self.status['skipped_identical'] += 1
            return
        if sig == self._attempted_sig:
            # Same content we already tried (and possibly failed on) -- don't
            # spin a retry loop; wait for the file to change again.
            return

        self._attempted_sig = sig
        self.status['last_reload_attempt'] = now
        try:
            if self.reload_fn:
                self.reload_fn(config)
            self._applied_sig = sig
            self.status['last_success'] = now
            self.status['last_error'] = None
            self.status['reload_count'] += 1
            logger.info('[config_watcher] gateway_config.yaml 已热重载（累计 %d 次）', self.status['reload_count'])
        except Exception as exc:
            self.status['last_error'] = f'{type(exc).__name__}: {exc}'
            logger.error('[config_watcher] 配置重载失败，已保留旧配置: %s', exc, exc_info=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._check()
            except Exception:
                logger.exception('[config_watcher] 轮询循环异常（已忽略）')

    # ------------------------------------------------------------- lifecycle

    def start(self) -> 'ConfigWatcher':
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name='config-watcher', daemon=True
            )
            self._thread.start()
            self.status['running'] = True
            logger.info('[config_watcher] 已启动，监听 %s（每 %.1fs）', self.config_path, self.interval)
        return self

    def stop(self) -> None:
        self._stop.set()
        self.status['running'] = False

    def status_dict(self) -> dict:
        with self._lock:
            return dict(self.status)


# Module-level singleton the web process drives. ``start()`` is idempotent, so
# re-imports / test imports never spawn more than one thread.
_watcher: Optional[ConfigWatcher] = None


def start_config_watcher(reload_fn: Callable[[dict], None], interval: float = 2.0) -> ConfigWatcher:
    """Start (or return the existing) singleton watcher with ``reload_fn``."""
    global _watcher
    if _watcher is None:
        _watcher = ConfigWatcher(reload_fn=reload_fn, interval=interval)
    else:
        _watcher.reload_fn = reload_fn
        _watcher.interval = interval
    return _watcher.start()


def get_config_watcher() -> Optional[ConfigWatcher]:
    return _watcher
