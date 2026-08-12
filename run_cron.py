"""Entry point for pm2/systemd-managed cron daemon.

The daemon registers SIGTERM/SIGINT handlers for a graceful shutdown (H-06):
in-flight jobs are allowed to finish before the pid file is removed and the
process exits, so a restart never re-runs an interrupted schedule slot.
"""
from metano.cron_daemon import run_daemon

if __name__ == '__main__':
    run_daemon()
