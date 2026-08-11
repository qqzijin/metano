#!/usr/bin/env bash
# 从源码仓库同步 metano 后端代码到运行实例(METANO_HOME)
# 用法: bash sync_runtime.sh   (默认同步到 ~/.claude/metano)
set -u
SRC="${1:-/home/dk/metano}"
DST="${METANO_HOME:-$HOME/.claude/metano}"
echo "同步 $SRC/metano → $DST/metano"
mkdir -p "$DST/metano"
for f in "$SRC"/metano/*.py; do
  [ -f "$f" ] && cp "$f" "$DST/metano/" && echo "  ✓ $(basename "$f")"
done
# gateway / honcho / skills 子目录
for sub in gateway honcho skills voice; do
  mkdir -p "$DST/metano/$sub"
  for f in "$SRC/metano/$sub"/*.py; do
    [ -f "$f" ] && cp "$f" "$DST/metano/$sub/" && echo "  ✓ $sub/$(basename "$f")"
  done
done
echo "后端同步完成"
