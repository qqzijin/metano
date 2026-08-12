#!/usr/bin/env bash
# metano: 服务管理（systemd 用户服务）
#
# web/cron/gateway 由 systemd 用户服务托管（metano-web/cron/gateway），
# 可单独重启任意服务而不影响其他——重启 web 不会中断进化定时任务。
# cocoindex 仍用脚本方式（ccc daemon）。
set -u

BRIDGE_DIR="${METANO_HOME:-$HOME/.claude/metano}"

start_backup() {
    # 启动时做一次数据库备份，防误操作丢失数据。失败不阻断启动。
    echo "Running startup backup..."
    bash "$BRIDGE_DIR/backup.sh" || echo "Warning: startup backup failed (continuing)"
}

_svc() { # _svc <web|cron|gateway> <start|stop|restart|status>
    local name="metano-$1"
    local action="$2"
    systemctl --user "$action" "$name"
}

start_ccc_daemon() {
    if ps aux | grep -q "[c]cc run-daemon"; then
        echo "CocoIndex daemon already running"
        return
    fi
    echo "Starting CocoIndex daemon (offline embedding)..."
    HF_HUB_OFFLINE=1 ccc daemon restart
}

case "${1:-start}" in
    start)
        if [ -n "${2:-}" ]; then
            case "$2" in
                web|cron|gateway) _svc "$2" start ;;
                cocoindex) start_ccc_daemon ;;
                *) echo "Unknown service: $2 (web|cron|gateway|cocoindex)"; exit 1 ;;
            esac
            exit 0
        fi
        start_backup
        echo "Starting metano services (systemd)..."
        systemctl --user start metano-web metano-cron metano-gateway
        start_ccc_daemon
        echo ""
        echo "Dashboard:  http://0.0.0.0:9120"
        ;;
    stop)
        if [ -n "${2:-}" ]; then
            case "$2" in
                web|cron|gateway) _svc "$2" stop ;;
                cocoindex) ccc daemon stop 2>/dev/null || true ;;
                *) echo "Unknown service: $2"; exit 1 ;;
            esac
            exit 0
        fi
        systemctl --user stop metano-web metano-cron metano-gateway
        ccc daemon stop 2>/dev/null || true
        ;;
    restart)
        if [ -n "${2:-}" ]; then
            case "$2" in
                web|cron|gateway) _svc "$2" restart ;;
                cocoindex) ccc daemon restart 2>/dev/null || true ;;
                *) echo "Unknown service: $2"; exit 1 ;;
            esac
            exit 0
        fi
        systemctl --user restart metano-web metano-cron metano-gateway
        start_ccc_daemon
        ;;
    status)
        for svc in web cron gateway; do
            st=$(systemctl --user is-active "metano-$svc" 2>/dev/null)
            echo "$svc: $st"
        done
        if ps aux | grep -q "[c]cc run-daemon"; then
            echo "cocoindex: running"
        else
            echo "cocoindex: stopped"
        fi
        ;;
    setup)
        # 生成初始配置（gen_config.py 不存在时静默跳过），然后启动全部服务。
        if [ -f "$BRIDGE_DIR/gen_config.py" ]; then
            python3 "$BRIDGE_DIR/gen_config.py"
        else
            echo "等待 gen_config.py（尚未就绪，跳过配置生成）"
        fi
        echo "Running setup start..."
        "$0" start
        ;;
    *) echo "Usage: $0 {start|stop|restart|status|setup} [web|cron|gateway|cocoindex]" ;;
esac
