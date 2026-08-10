#!/usr/bin/env bash
# metano healthcheck — check the health of the metano services:
#   web (HTTP /health), gateway (process), cron (process), cocoindex (daemon).
#
# Usage:
#   bash healthcheck.sh              # report only (default)
#   bash healthcheck.sh --repair     # also try to restart DOWN services
#   REPAIR=1 bash healthcheck.sh     # same as --repair
#
# Exit code:
#   0  all services OK
#   1  at least one service is DEGRADED or DOWN
set -u

BRIDGE_DIR="${METANO_HOME:-${METANO_DIR:-$HOME/.claude/metano}}"
WEB_URL="${METANO_WEB_URL:-http://localhost:9120/health}"
WEB_TIMEOUT="${METANO_WEB_TIMEOUT:-5}"

REPAIR=0
for arg in "$@"; do
  case "$arg" in
    --repair|-r) REPAIR=1 ;;
  esac
done
if [ "${REPAIR:-0}" != "1" ] && [ "${REPAIR_ENV:-0}" = "1" ]; then
  REPAIR=1
fi

# Per-service status: OK | DEGRADED | DOWN
declare -A STATUS
declare -A DETAIL

set_status() {
  local name="$1" st="$2" detail="$3"
  STATUS["$name"]="$st"
  DETAIL["$name"]="$detail"
}

# pidfile_alive <pidfile> [cmdline-fragment]
# True if the pidfile exists, the pid is a live process, and (optionally)
# the process cmdline contains the expected fragment.
pidfile_alive() {
  local pidfile="$1" match="${2:-}"
  [ -f "$pidfile" ] || return 1
  local pid
  pid=$(cat "$pidfile" 2>/dev/null) || return 1
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  if [ -n "$match" ]; then
    ps -p "$pid" -o args= 2>/dev/null | grep -q "$match" || return 1
  fi
  return 0
}

# ---- web: curl /health ----
check_web() {
  local out body code
  out=$(curl -s --noproxy '*' --max-time "$WEB_TIMEOUT" -w $'\n%{http_code}' "$WEB_URL" 2>/dev/null)
  code=$(printf '%s' "$out" | tail -n1)
  body=$(printf '%s' "$out" | sed '$d')
  if [ "$code" = "200" ]; then
    if printf '%s' "$body" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
      set_status web OK "HTTP 200, /health ok"
    elif printf '%s' "$body" | grep -q '"status"[[:space:]]*:[[:space:]]*"degraded"'; then
      set_status web DEGRADED "HTTP 200, /health degraded"
    else
      set_status web OK "HTTP 200 (unexpected body)"
    fi
  else
    set_status web DOWN "HTTP ${code:-no response} from $WEB_URL"
  fi
}

# ---- gateway: pidfile + process match ----
check_gateway() {
  if pidfile_alive "$BRIDGE_DIR/gateway.pid" "metano.gateway"; then
    set_status gateway OK "PID $(cat "$BRIDGE_DIR/gateway.pid")"
  elif pidfile_alive "$BRIDGE_DIR/gateway.pid"; then
    set_status gateway DEGRADED "PID $(cat "$BRIDGE_DIR/gateway.pid") alive but wrong process"
  else
    set_status gateway DOWN "process not running"
  fi
}

# ---- cron: pidfile + process match ----
check_cron() {
  if pidfile_alive "$BRIDGE_DIR/cron.pid" "metano.cron_daemon"; then
    set_status cron OK "PID $(cat "$BRIDGE_DIR/cron.pid")"
  elif pidfile_alive "$BRIDGE_DIR/cron.pid"; then
    set_status cron DEGRADED "PID $(cat "$BRIDGE_DIR/cron.pid") alive but wrong process"
  else
    set_status cron DOWN "process not running"
  fi
}

# ---- cocoindex: process grep.
# NOTE: `ccc daemon status` auto-starts the daemon if it is not running, so we
# probe the actual process instead (same as metano.sh does).
check_cocoindex() {
  if ps aux | grep -q "[c]cc run-daemon"; then
    set_status cocoindex OK "daemon running"
  else
    set_status cocoindex DOWN "daemon not running"
  fi
}

# ---- run all checks ----
check_web
check_gateway
check_cron
check_cocoindex

overall="OK"
for name in web gateway cron cocoindex; do
  st="${STATUS[$name]:-DOWN}"
  if [ "$st" = "DOWN" ]; then
    overall="DOWN"
  elif [ "$st" = "DEGRADED" ] && [ "$overall" != "DOWN" ]; then
    overall="DEGRADED"
  fi
done

# ---- report ----
echo "metano healthcheck — $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
for name in web gateway cron cocoindex; do
  printf '%-9s %-10s %s\n' "[${STATUS[$name]:-?}]" "$name" "${DETAIL[$name]:-}"
done
echo "Summary: $overall"

# ---- log non-OK states ----
if [ "$overall" != "OK" ]; then
  LOG_FILE="${METANO_HEALTH_LOG:-$BRIDGE_DIR/cron/output/healthcheck.log}"
  mkdir -p "$(dirname "$LOG_FILE")"
  {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] summary=$overall"
    for name in web gateway cron cocoindex; do
      echo "  $name=${STATUS[$name]:-?} ${DETAIL[$name]:-}"
    done
  } >> "$LOG_FILE"
fi

# ---- optional repair (only DOWN services) ----
if [ "$overall" != "OK" ] && [ "$REPAIR" = "1" ]; then
  for name in web gateway cron cocoindex; do
    if [ "${STATUS[$name]:-}" = "DOWN" ]; then
      echo "Repairing $name ..."
      bash "$BRIDGE_DIR/metano.sh" start "$name" 2>&1 | sed 's/^/  /'
    fi
  done
fi

[ "$overall" = "OK" ] && exit 0 || exit 1
