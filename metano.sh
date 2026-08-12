#!/usr/bin/env bash
# metano: 服务管理
#
# web/cron/gateway 优先用 systemd 用户服务（自动创建 unit，他人克隆后 start 即用）；
# 环境无 systemd 时回退到 nohup 后台进程（pid 文件）。
# cocoindex 用脚本方式（ccc daemon）。
set -u

BRIDGE_DIR="${METANO_HOME:-$HOME/.claude/metano}"
PYTHON="$(command -v python3 || echo /usr/bin/python3)"

# ── systemd unit 自动创建（开箱即用：新用户无 unit 时自动生成）──
_ensure_unit() {
    local svc="$1"
    command -v systemctl >/dev/null 2>&1 || return 1
    local unit="$HOME/.config/systemd/user/metano-$svc.service"
    [ -f "$unit" ] && return 0
    mkdir -p "$HOME/.config/systemd/user"
    local cmd
    case "$svc" in
        web) cmd="from metano.serve import main; main()" ;;
        cron) cmd="from metano.cron_daemon import run_daemon; run_daemon()" ;;
        gateway) cmd="from metano.gateway.launcher import main; main()" ;;
    esac
    cat > "$unit" <<EOF
[Unit]
Description=metano $svc
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BRIDGE_DIR
Environment=METANO_HOME=$BRIDGE_DIR
ExecStart=$PYTHON -c "$cmd"
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable "metano-$svc" 2>/dev/null || true
    return 0
}

# ── systemd user timer：每日维护在 bwrap 外执行 ──
# cron daemon 的 shell 任务（backup.sh / maintain_daily.sh / healthcheck.sh）
# 在 bwrap(--tmpfs $HOME) 内运行，真实 METANO_HOME 不可见 → 全部无法执行。
# 改由 systemd user timer 直接运行 maintain_daily.sh（不经 bwrap），保证
# 备份与健康检查真实可跑。失败自动补跑（Persistent=true）。
_ensure_maintain_timer() {
    command -v systemctl >/dev/null 2>&1 || return 1
    local unit="$HOME/.config/systemd/user/metano-maintain.service"
    local timer="$HOME/.config/systemd/user/metano-maintain.timer"
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$unit" <<EOF
[Unit]
Description=metano daily maintenance (backup + healthcheck --repair + vacuum) — runs outside bwrap

[Service]
Type=oneshot
WorkingDirectory=$BRIDGE_DIR
Environment=METANO_HOME=$BRIDGE_DIR
ExecStart=/bin/bash $BRIDGE_DIR/maintain_daily.sh
EOF
    cat > "$timer" <<EOF
[Unit]
Description=Daily trigger for metano-maintain (02:30)

[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable "metano-maintain.timer" 2>/dev/null || true
    systemctl --user start "metano-maintain.timer" 2>/dev/null || true
    echo "maintain timer ready (metano-maintain.timer → 每日 02:30, 在 bwrap 外运行)"
    return 0
}

# ── 无 systemd 时的传统后台方式（pid 文件）──
_start_bg() {
    local svc="$1"
    if [ -f "$BRIDGE_DIR/$svc.pid" ] && kill -0 "$(cat "$BRIDGE_DIR/$svc.pid")" 2>/dev/null; then
        echo "$svc already running (PID $(cat "$BRIDGE_DIR/$svc.pid"))"
        return
    fi
    local cmd
    case "$svc" in
        web) cmd="from metano.serve import main; main()" ;;
        cron) cmd="from metano.cron_daemon import run_daemon; run_daemon()" ;;
        gateway) cmd="from metano.gateway.launcher import main; main()" ;;
    esac
    mkdir -p "$BRIDGE_DIR/logs"
    cd "$BRIDGE_DIR"
    nohup python3 -c "$cmd" >> "$BRIDGE_DIR/logs/$svc.log" 2>&1 &
    echo $! > "$BRIDGE_DIR/$svc.pid"
    echo "$svc started (bg PID $(cat "$BRIDGE_DIR/$svc.pid"))"
}

_stop_bg() {
    local svc="$1"
    if [ -f "$BRIDGE_DIR/$svc.pid" ]; then
        kill "$(cat "$BRIDGE_DIR/$svc.pid")" 2>/dev/null || true
        rm -f "$BRIDGE_DIR/$svc.pid"
        echo "$svc stopped"
    fi
}

# 统一服务操作：有 systemd → 用 systemd；无 → 回退 bg
_op() { # <web|cron|gateway> <start|stop|restart|status>
    local svc="$1" action="$2"
    if _ensure_unit "$svc"; then
        case "$action" in
            start) systemctl --user start "metano-$svc" ;;
            stop) systemctl --user stop "metano-$svc" ;;
            restart) systemctl --user restart "metano-$svc" ;;
            status) systemctl --user is-active "metano-$svc" 2>/dev/null || echo inactive ;;
        esac
    else
        case "$action" in
            start) _start_bg "$svc" ;;
            stop) _stop_bg "$svc" ;;
            restart) _stop_bg "$svc"; sleep 1; _start_bg "$svc" ;;
            status) { [ -f "$BRIDGE_DIR/$svc.pid" ] && kill -0 "$(cat "$BRIDGE_DIR/$svc.pid")" 2>/dev/null && echo active; } || echo inactive ;;
        esac
    fi
}

start_backup() {
    # 启动时做一次数据库备份，防误操作丢失数据。失败不阻断启动。
    echo "Running startup backup..."
    bash "$BRIDGE_DIR/backup.sh" || echo "Warning: startup backup failed (continuing)"
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
                web|cron|gateway) _op "$2" start ;;
                cocoindex) start_ccc_daemon ;;
                *) echo "Unknown service: $2 (web|cron|gateway|cocoindex)"; exit 1 ;;
            esac
            exit 0
        fi
        start_backup
        echo "Starting metano services..."
        _op web start
        _op cron start
        _op gateway start
        _ensure_maintain_timer
        start_ccc_daemon
        echo ""
        echo "Dashboard:  http://0.0.0.0:9120"
        ;;
    stop)
        if [ -n "${2:-}" ]; then
            case "$2" in
                web|cron|gateway) _op "$2" stop ;;
                cocoindex) ccc daemon stop 2>/dev/null || true ;;
                *) echo "Unknown service: $2"; exit 1 ;;
            esac
            exit 0
        fi
        _op web stop
        _op cron stop
        _op gateway stop
        ccc daemon stop 2>/dev/null || true
        ;;
    restart)
        if [ -n "${2:-}" ]; then
            case "$2" in
                web|cron|gateway) _op "$2" restart ;;
                cocoindex) ccc daemon restart 2>/dev/null || true ;;
                *) echo "Unknown service: $2"; exit 1 ;;
            esac
            exit 0
        fi
        _op web restart
        _op cron restart
        _op gateway restart
        start_ccc_daemon
        ;;
    status)
        for svc in web cron gateway; do
            st=$(_op "$svc" status)
            echo "$svc: $st"
        done
        if ps aux | grep -q "[c]cc run-daemon"; then
            echo "cocoindex: running"
        else
            echo "cocoindex: stopped"
        fi
        ;;
    setup)
        if [ -f "$BRIDGE_DIR/gen_config.py" ]; then
            python3 "$BRIDGE_DIR/gen_config.py"
        else
            echo "等待 gen_config.py（尚未就绪，跳过配置生成）"
        fi
        echo "Running setup start..."
        "$0" start
        ;;
    maintain-timer)
        _ensure_maintain_timer
        systemctl --user list-timers metano-maintain.timer 2>/dev/null || true
        ;;
    *) echo "Usage: $0 {start|stop|restart|status|setup|maintain-timer} [web|cron|gateway|cocoindex]" ;;
esac
