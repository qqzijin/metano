#!/usr/bin/env bash
# =============================================================================
# metano 每日维护脚本 — 由 cron 每日执行
# -----------------------------------------------------------------------------
# 内容：
#   1. 数据库备份 (backup.sh，原独立 db-backup 任务已并入)
#   2. 健康检查 + 自动修复 (healthcheck.sh --repair)
#   3. 数据库 VACUUM (回收空闲页)
#   4. 清理过旧临时/日志文件
#   5. 汇总输出
#
# 失败不阻断：每步独立，某步失败继续后续。
# =============================================================================
set -u

BRIDGE_DIR="${METANO_HOME:-$HOME/.claude/metano}"
LOG_FILE="$BRIDGE_DIR/logs/maintain-daily.log"

# ── bwrap 沙箱守卫 ──
# cron daemon 的 shell 任务在 bwrap(--tmpfs $HOME) 内运行，真实 BRIDGE_DIR 不可见。
# 每日维护必须在 bwrap 外执行（systemd timer metano-maintain.timer / 手动）。
# 检测到沙箱直接跳过并返回 0，避免在空 tmpfs 上跑出一堆假失败日志。
if [ ! -d "$BRIDGE_DIR" ]; then
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] SKIP: $BRIDGE_DIR 不可见（bwrap 沙箱屏蔽了 \$HOME?）。"
    echo "        请通过 systemd timer（metano-maintain.timer）或手动执行本脚本。"
    exit 0
fi

mkdir -p "$(dirname "$LOG_FILE")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "═══ 每日维护开始 ═══"

# ── 1. 数据库备份 ──────────────────────────────────────────────────────
log "[1/5] 数据库备份 (backup.sh)"
if bash "$BRIDGE_DIR/backup.sh" >> "$LOG_FILE" 2>&1; then
  log "  备份完成"
else
  log "  ⚠️ 备份失败(继续)"
fi

# ── 2. 健康检查 + 自动修复 ─────────────────────────────────────────────
log "[2/5] 健康检查 (healthcheck.sh --repair)"
if bash "$BRIDGE_DIR/healthcheck.sh" --repair >> "$LOG_FILE" 2>&1; then
  log "  健康检查通过"
else
  log "  ⚠️ 健康检查发现问题(见上)，已尝试修复"
fi

# ── 3. 数据库 VACUUM (回收空闲页) ───────────────────────────────────────
log "[3/5] 数据库 VACUUM"
for db in bridge evo memory; do
  if [ -f "$BRIDGE_DIR/$db.db" ]; then
    if command -v sqlite3 >/dev/null 2>&1; then
      sqlite3 "$BRIDGE_DIR/$db.db" "VACUUM;" 2>/dev/null \
        && log "  ✓ $db.db 已 VACUUM" \
        || log "  ⚠️ $db.db VACUUM 失败(可能被占用，跳过)"
    fi
  fi
done

# ── 4. 清理过旧临时/日志文件 ───────────────────────────────────────────
log "[4/5] 清理过旧文件"
# 清理 7 天前的旧 dist.old* 构建产物
find "$BRIDGE_DIR" -maxdepth 1 -name "dist.old*" -mtime +7 -type d -exec rm -rf {} + 2>/dev/null
# 清理 30 天前的备份（backup.sh 自带 7 天保留，这里兜底）
find "$BRIDGE_DIR/backups" -name "*.bak.*" -mtime +30 -delete 2>/dev/null
# 清理 30 天前的 audit-* 审计备份目录（backup.sh 的 YYYY-MM-DD 匹配不覆盖 audit-*，这里兜底）
find "$BRIDGE_DIR/backups" -maxdepth 1 -type d -name 'audit-*' -mtime +30 -exec rm -rf {} + 2>/dev/null
# 清理 7 天前的 .pyc 缓存
find "$BRIDGE_DIR" -name "__pycache__" -type d -mtime +7 -exec rm -rf {} + 2>/dev/null
log "  清理完成"

# ── 5. 汇总 ────────────────────────────────────────────────────────────
log "[5/5] 汇总"
if curl -s --noproxy '*' --max-time 8 -o /dev/null -w '%{http_code}' http://localhost:9120/health 2>/dev/null | grep -q 200; then
  log "  Web 面板: 正常 (HTTP 200)"
else
  log "  ⚠️ Web 面板: 异常"
fi
DB_SIZE=$(du -sh "$BRIDGE_DIR"/bridge.db 2>/dev/null | awk '{print $1}')
log "  bridge.db 大小: ${DB_SIZE:-N/A}"
log "═══ 每日维护结束 ═══"
echo "maintain-daily done: $(tail -1 "$LOG_FILE")"
