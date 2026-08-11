#!/usr/bin/env bash
# =============================================================================
# metano 自动数据库备份脚本
# -----------------------------------------------------------------------------
# - 用 `sqlite3 .backup` 对全部 SQLite 数据库做一致性备份（WAL 模式下也安全，
#   不会因为进程正在写入而拿到不一致的快照）。
# - 同时备份敏感配置文件 gateway_config.yaml。
# - 备份按日期归档：backups/YYYY-MM-DD/，保留最近 N 天（默认 7 天），旧的自动清理。
# - 幂等：同一天重复执行会覆盖当天备份，可安全重跑（cron 和手动都能跑）。
# - 备份目录 chmod 700、备份的敏感配置 chmod 600，防止其他用户读取。
# =============================================================================
set -euo pipefail

METANO_DIR="${METANO_HOME:-${METANO_DIR:-$HOME/.claude/metano}}"
BACKUP_ROOT="$METANO_DIR/backups"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
LOG_FILE="$BACKUP_ROOT/backup.log"

# 数据库列表（相对 METANO_DIR 的路径）
DATABASES=(
    "bridge.db"
    "evo.db"
    "memory.db"
    "knowledge/knowledge.db"
    "honcho_data/honcho.db"
)

# 敏感配置文件（相对 METANO_DIR 的路径）
CONFIG_FILES=(
    "gateway_config.yaml"
)

log() {
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] $*"
    echo "[$ts] $*" >> "$LOG_FILE"
}

backup_db() {
    local src="$1" dest="$2" base="$3" ok=0 attempt
    if [ ! -f "$src" ]; then
        log "SKIP $base (源文件不存在)"
        return 0
    fi
    for attempt in 1 2; do
        if sqlite3 "$src" ".backup '${dest}'" 2>>"$LOG_FILE"; then
            ok=1
            break
        fi
        log "RETRY $base (第 ${attempt} 次尝试失败)"
        sleep 2
    done
    if [ "$ok" -ne 1 ]; then
        log "FAIL $base (sqlite3 .backup 失败)"
        return 1
    fi
    # 快速校验备份文件是可读的 SQLite 库（读取 sqlite_master，对 147MB 也很快）
    if sqlite3 "$dest" "SELECT count(*) FROM sqlite_master;" >/dev/null 2>&1; then
        local size
        size="$(du -h "$dest" | cut -f1)"
        log "OK   $base -> $dest ($size)"
    else
        log "FAIL $base (备份文件不可读)"
        return 1
    fi
}

backup_config() {
    local src="$1" dest="$2" base="$3"
    if [ ! -f "$src" ]; then
        log "SKIP $base (源文件不存在)"
        return 0
    fi
    if cp "$src" "$dest" 2>>"$LOG_FILE"; then
        chmod 600 "$dest"
        local size
        size="$(du -h "$dest" | cut -f1)"
        log "OK   $base -> $dest ($size)"
    else
        log "FAIL $base (cp 失败)"
        return 1
    fi
}

cleanup_old() {
    # 只保留最近 RETENTION_DAYS 天：删除按日期命名的、比 cutoff 天更旧的备份目录。
    # 仅匹配 YYYY-MM-DD 形式的目录名，绝不触碰 backup.log 等文件。
    if ! command -v find >/dev/null 2>&1; then
        return 0
    fi
    local cutoff=$((RETENTION_DAYS - 1))
    local old_dirs
    old_dirs="$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
        -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' \
        -mtime "+${cutoff}" 2>/dev/null || true)"
    # 逐行读取（IFS= read -r）而非 `for d in $old_dirs` —— 后者会按空白分词，
    # 路径含空格时会把一个目录拆成多个、可能误删不相关目录。
    while IFS= read -r d; do
        [ -z "$d" ] && continue
        [ "$d" = "$DEST_DIR" ] && continue
        if rm -rf "$d" 2>>"$LOG_FILE"; then
            log "CLEAN 删除过期备份 $d"
        else
            log "WARN 删除失败 $d"
        fi
    done <<< "$old_dirs"
}

# ---- 主流程 ---------------------------------------------------------------
command -v sqlite3 >/dev/null 2>&1 || { echo "ERROR: 未找到 sqlite3，请先安装"; exit 1; }

DATE_STAMP="$(date +%Y-%m-%d)"
DEST_DIR="$BACKUP_ROOT/$DATE_STAMP"

mkdir -p "$DEST_DIR"
chmod 700 "$BACKUP_ROOT"
chmod 700 "$DEST_DIR"
touch "$LOG_FILE" && chmod 600 "$LOG_FILE"

log "===== 备份开始: $DATE_STAMP (保留 ${RETENTION_DAYS} 天) ====="

for db in "${DATABASES[@]}"; do
    backup_db "$METANO_DIR/$db" "$DEST_DIR/$(basename "$db")" "$(basename "$db")" || true
done

for f in "${CONFIG_FILES[@]}"; do
    backup_config "$METANO_DIR/$f" "$DEST_DIR/$(basename "$f")" "$(basename "$f")" || true
done

cleanup_old

log "===== 备份完成: $DATE_STAMP ====="
