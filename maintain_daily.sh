#!/usr/bin/env bash
# =============================================================================
# metano 每日维护脚本 — 由 cron 每日执行
# -----------------------------------------------------------------------------
# 内容：
#   1. 健康检查 + 自动修复 (healthcheck.sh --repair)
#   2. 数据库 VACUUM (回收空闲页)
#   3. 清理过旧临时/日志文件
#   4. 汇总输出
#
# 失败不阻断：每步独立，某步失败继续后续。
# =============================================================================
set -u

BRIDGE_DIR="${METANO_HOME:-$HOME/.claude/metano}"
LOG_FILE="$BRIDGE_DIR/logs/maintain-daily.log"
mkdir -p "$(dirname "$LOG_FILE")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "═══ 每日维护开始 ═══"

# ── 1. 健康检查 + 自动修复 ──────────────────────────────────────────────
log "[1/4] 健康检查 (healthcheck.sh --repair)"
if bash "$BRIDGE_DIR/healthcheck.sh" --repair >> "$LOG_FILE" 2>&1; then
  log "  健康检查通过"
else
  log "  ⚠️ 健康检查发现问题(见上)，已尝试修复"
fi

# ── 2. 数据库 VACUUM (回收空闲页) ───────────────────────────────────────
log "[2/4] 数据库 VACUUM"
for db in bridge evo memory; do
  if [ -f "$BRIDGE_DIR/$db.db" ]; then
    if command -v sqlite3 >/dev/null 2>&1; then
      sqlite3 "$BRIDGE_DIR/$db.db" "VACUUM;" 2>/dev/null \
        && log "  ✓ $db.db 已 VACUUM" \
        || log "  ⚠️ $db.db VACUUM 失败(可能被占用，跳过)"
    fi
  fi
done

# ── 3. 清理过旧临时/日志文件 ───────────────────────────────────────────
log "[3/4] 清理过旧文件"
# 清理 7 天前的旧 dist.old* 构建产物
find "$BRIDGE_DIR" -maxdepth 1 -name "dist.old*" -mtime +7 -type d -exec rm -rf {} + 2>/dev/null
# 清理 30 天前的备份（backup.sh 自带 7 天保留，这里兜底）
find "$BRIDGE_DIR/backups" -name "*.bak.*" -mtime +30 -delete 2>/dev/null
# 清理 7 天前的 .pyc 缓存
find "$BRIDGE_DIR" -name "__pycache__" -type d -mtime +7 -exec rm -rf {} + 2>/dev/null
log "  清理完成"

# ── 4. 汇总 ────────────────────────────────────────────────────────────
log "[4/4] 汇总"
if curl -s --noproxy '*' --max-time 8 -o /dev/null -w '%{http_code}' http://localhost:9120/health 2>/dev/null | grep -q 200; then
  log "  Web 面板: 正常 (HTTP 200)"
else
  log "  ⚠️ Web 面板: 异常"
fi
DB_SIZE=$(du -sh "$BRIDGE_DIR"/bridge.db 2>/dev/null | awk '{print $1}')
log "  bridge.db 大小: ${DB_SIZE:-N/A}"
log "═══ 每日维护结束 ═══"
echo "maintain-daily done: $(tail -1 "$LOG_FILE")"
