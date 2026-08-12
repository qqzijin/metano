#!/usr/bin/env bash
# 从源码仓库同步 metano 后端代码到运行实例(METANO_HOME)
# 用法: bash sync_runtime.sh   (默认同步到 ~/.claude/metano)
set -u
SRC="${1:-/home/dk/metano}"
DST="${METANO_HOME:-$HOME/.claude/metano}"
fail=0

# ── 沙箱守卫：bwrap 内 $HOME 被 tmpfs 屏蔽，无法同步 ──
if [ ! -d "$SRC" ]; then
  echo "ERROR: 源码目录不可见：$SRC" >&2
  exit 1
fi

echo "同步 $SRC/metano → $DST/metano"

# ── 单个文件同步 + 完整性校验 ──
# 用 sha256sum 快照比对（H10）。源文件可能被并行的 self-modify/其他进程改写：
# 每次尝试都重新取源哈希，避免"复制中途源被改"造成假失败；最多重试 2 次。
sync_file() {
  local src="$1" dst="$2" attempt=1 src_sha dst_sha
  while [ "$attempt" -le 2 ]; do
    src_sha=$(sha256sum "$src" 2>/dev/null | awk '{print $1}')
    if cp -f "$src" "$dst" 2>/dev/null; then
      dst_sha=$(sha256sum "$dst" 2>/dev/null | awk '{print $1}')
      if [ -n "$src_sha" ] && [ "$src_sha" = "$dst_sha" ]; then
        return 0
      fi
    fi
    attempt=$((attempt + 1))
  done
  echo "  ✗ 校验失败 $dst（源哈希与副本不一致）"
  fail=1
  return 1
}

# 删除目标端孤儿 .py（源码已删除/移动的旧版残留，避免被 import 到旧代码）。
# 只清理受管控目录下的 .py；目标端数据文件(.db/.jsonl/.md 等)一律不动。
rm_orphans() {
  local src_dir="$1" dst_dir="$2"
  [ -d "$src_dir" ] || return 0
  [ -d "$dst_dir" ] || return 0
  local f base
  for f in "$dst_dir"/*.py; do
    [ -e "$f" ] || continue
    base=$(basename "$f")
    if [ ! -f "$src_dir/$base" ]; then
      echo "  ✗ 移除孤儿 $base"
      rm -f "$f"
    fi
  done
}

mkdir -p "$DST/metano"
rm_orphans "$SRC/metano" "$DST/metano"
for f in "$SRC"/metano/*.py; do
  [ -f "$f" ] && sync_file "$f" "$DST/metano/$(basename "$f")" && echo "  ✓ $(basename "$f")"
done

# gateway / honcho / skills / voice 子目录
for sub in gateway honcho skills voice; do
  mkdir -p "$DST/metano/$sub"
  rm_orphans "$SRC/metano/$sub" "$DST/metano/$sub"
  for f in "$SRC/metano/$sub"/*.py; do
    [ -f "$f" ] && sync_file "$f" "$DST/metano/$sub/$(basename "$f")" && echo "  ✓ $sub/$(basename "$f")"
  done
done

# ── 2. tests/ 同步（H10：运行时测试集与源码脱节，self-modify 验证门用旧测试集）──
if [ -d "$SRC/tests" ]; then
  mkdir -p "$DST/tests"
  rm_orphans "$SRC/tests" "$DST/tests"
  for f in "$SRC"/tests/*.py; do
    [ -f "$f" ] && sync_file "$f" "$DST/tests/$(basename "$f")" && echo "  ✓ tests/$(basename "$f")"
  done
fi

# ── 3. skills_data 同步（技能含 trust 元数据，随代码一起下发）──
if [ -d "$SRC/metano/skills_data" ]; then
  mkdir -p "$DST/metano/skills_data"
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    mkdir -p "$DST/metano/skills_data/$(dirname "$rel")"
    sync_file "$SRC/metano/skills_data/$rel" "$DST/metano/skills_data/$rel" \
      && echo "  ✓ skills_data/$rel"
  done < <(cd "$SRC/metano/skills_data" && find . -type f)
fi

echo "后端同步完成"
if [ "$fail" -eq 0 ]; then
  echo "完整性校验: OK（全部文件一致）"
else
  echo "ERROR: 同步后完整性校验失败（有文件不一致）" >&2
  exit 1
fi
