#!/usr/bin/env bash
# metano: Start all services
set -e

BRIDGE_DIR="$HOME/.claude/metano"
PID_DIR="$BRIDGE_DIR"

start_backup() {
    # 启动时做一次数据库备份，防误操作丢失数据。失败不阻断启动。
    echo "Running startup backup..."
    bash "$BRIDGE_DIR/backup.sh" || echo "Warning: startup backup failed (continuing)"
}

start_web() {
    if [ -f "$PID_DIR/web.pid" ] && kill -0 "$(cat "$PID_DIR/web.pid")" 2>/dev/null; then
        echo "Web dashboard already running (PID $(cat "$PID_DIR/web.pid"))"
        return
    fi
    echo "Starting web dashboard on http://0.0.0.0:9120 ..."
    cd "$BRIDGE_DIR"
    python3 -c "from metano.serve import main; main()" &
    echo $! > "$PID_DIR/web.pid"
}

start_ccc_daemon() {
    # Note: `ccc daemon status` auto-starts the daemon if it is not running,
    # so guard on the actual process instead.
    if ps aux | grep -q "[c]cc run-daemon"; then
        echo "CocoIndex daemon already running"
        return
    fi
    echo "Starting CocoIndex daemon (offline embedding)..."
    HF_HUB_OFFLINE=1 ccc daemon restart
}

start_cron() {
    if [ -f "$PID_DIR/cron.pid" ] && kill -0 "$(cat "$PID_DIR/cron.pid")" 2>/dev/null; then
        echo "Cron daemon already running (PID $(cat "$PID_DIR/cron.pid"))"
        return
    fi
    echo "Starting cron daemon..."
    cd "$BRIDGE_DIR"
    python3 -c "from metano.cron_daemon import run_daemon; run_daemon()" &
    echo $! > "$PID_DIR/cron.pid"
}

start_gateway() {
    if [ -f "$PID_DIR/gateway.pid" ] && kill -0 "$(cat "$PID_DIR/gateway.pid")" 2>/dev/null; then
        echo "Gateway already running (PID $(cat "$PID_DIR/gateway.pid"))"
        return
    fi
    echo "Starting message gateway..."
    cd "$BRIDGE_DIR"
    python3 -c "from metano.gateway.launcher import main; main()" &
    echo $! > "$PID_DIR/gateway.pid"
}

stop_all() {
    for svc in web cron gateway; do
        if [ -f "$PID_DIR/$svc.pid" ]; then
            PID=$(cat "$PID_DIR/$svc.pid")
            if kill -0 "$PID" 2>/dev/null; then
                echo "Stopping $svc (PID $PID)..."
                kill "$PID"
            fi
            rm -f "$PID_DIR/$svc.pid"
        fi
    done
    ccc daemon stop 2>/dev/null || true
}

status() {
    for svc in web cron gateway; do
        if [ -f "$PID_DIR/$svc.pid" ] && kill -0 "$(cat "$PID_DIR/$svc.pid")" 2>/dev/null; then
            echo "$svc: running (PID $(cat "$PID_DIR/$svc.pid"))"
        else
            echo "$svc: stopped"
        fi
    done
    if ps aux | grep -q "[c]cc run-daemon"; then
        echo "cocoindex: running"
    else
        echo "cocoindex: stopped"
    fi
}

case "${1:-start}" in
    start)
        # Optional per-service start: `metano.sh start web|cron|gateway|cocoindex`
        if [ -n "${2:-}" ]; then
            case "$2" in
                web)       start_web ;;
                cron)      start_cron ;;
                gateway)   start_gateway ;;
                cocoindex) start_ccc_daemon ;;
                *) echo "Unknown service: $2 (web|cron|gateway|cocoindex)"; exit 1 ;;
            esac
            exit 0
        fi
        start_backup
        start_web
        start_ccc_daemon
        start_cron
        start_gateway
        echo ""
        echo "Dashboard:  http://0.0.0.0:9120"
        ;;
    stop)   stop_all ;;
    status) status ;;
    restart) stop_all; sleep 1; start_backup; start_web; start_ccc_daemon; start_cron; start_gateway ;;
    *) echo "Usage: $0 {start|stop|status|restart}" ;;
esac
