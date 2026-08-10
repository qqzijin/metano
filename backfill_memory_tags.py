#!/usr/bin/env python3
"""Backfill scenario tags onto existing memories in memory.db.

Task: "记忆系统'技能库化'渐进改造" step 3 — give existing high-value memories
(feedback/user categories) trigger-scenario tags so they can be retrieved by
scenario instead of only by full-text search.

Strategy: rule-based keyword classifier (batch, not hand-edited rows). For each
feedback/user memory whose `tags` is empty, compute the union of tags from every
rule whose keywords appear in the content, then store the normalized result.

Idempotent: rows that already have tags are skipped unless --force is given.

Usage:
    python3 backfill_memory_tags.py              # feedback/user only
    python3 backfill_memory_tags.py --all        # include project/reference/seed too
    python3 backfill_memory_tags.py --dry-run    # show what would change, write nothing
    python3 backfill_memory_tags.py --force      # overwrite existing tags too
"""
import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = os.environ.get(
    'MEMORY_DB', str(Path(__file__).resolve().parent / 'memory.db')
)

# (keyword_list, tag_list) — content is lowercased before matching.
# Short ASCII keywords use word-boundary matching; longer/Chinese keywords use substring.
# Curated tags for known high-value feedback/user memories, derived from reading
# each entry's actual content. These are the "trigger scenarios" where the rule
# should be injected. Composite profile entries get a compact tag set (the
# dominant scenarios), not every keyword present.
CURATED_TAGS = {
    # id -> [tags]
    2:  ['webui', 'ui', 'preference', 'style'],                  # Web UI 偏好 / UI 本地化 / 深色紫色 / 输出简洁
    4:  ['backend', 'mcp', 'architecture', 'proxy', 'scraping'],  # MCP 服务器 / 技术栈 / 代理 / 爬虫 profile
    5:  ['backend', 'frontend', 'sync'],                          # 前后端功能对齐，前端展示缺失
    6:  ['test', 'verify'],                                       # 验证完整依赖链
    7:  ['test', 'verify'],                                       # 声明完成前必须跑验证测试
    8:  ['output', 'style', 'report'],                            # 输出重复冗余零容忍 / 结构化总结报告
    9:  ['identity', 'im', 'feishu'],                             # IM/通知 API 用 bot 身份
    10: ['webui', 'ui', 'preference', 'style', 'report'],         # user: Web UI / 深色紫 / 中文 / 结构化报告
    11: ['backend', 'mcp', 'architecture', 'proxy', 'scraping'],  # MCP 项目 / 飞书 / 代理 / 爬虫 profile
    12: ['memory', 'knowledge'],                                  # 项目状态不写记忆，用 CocoIndex
    13: ['test', 'verify', 'backend', 'frontend', 'sync'],        # 验证 curl + 后端 API 同步前端 TS/hooks
    14: ['workflow', 'ai', 'autonomy', 'frontend', 'sync'],       # AI 辅助迭代 / 自主探索 / 前后端对齐
    15: ['deployment', 'resilience', 'troubleshooting', 'test', 'verify'],  # 非致命先推进 / 代码审计 / 递归敏感
    17: ['backend', 'frontend', 'sync', 'test'],                  # 后端 API 变更同步类型/hooks/页面/build
    18: ['troubleshooting', 'pragmatic', 'security', 'backend'],  # 实用主义 / 先诊断后治疗 / 安全盲区
    19: ['autonomy', 'trust', 'workflow', 'verify', 'frontend', 'sync'],  # 宽授权+严控制 / 验证驱动
}

# Fallback keyword rules for any rows not covered by CURATED_TAGS.
# Short ASCII keywords use word-boundary matching; longer/Chinese use substring.
RULES = [
    # 身份边界 / IM 通知
    (['bot 身份', '冒充用户', '冒充身份', '飞书', 'feishu', '通知 api'],
     ['identity', 'im', 'feishu']),
    # 前后端同步
    (['client.ts', 'hooks.ts', 'ts 类型', '前端类型', '类型定义', '页面组件',
      'build 验证', '前端展示', '同步修改前端', '字段名或类型不一致'],
     ['backend', 'frontend', 'sync']),
    # 验证 / 测试
    (['curl', '依赖链', '验证测试', '验证结果', '实际运行', '跑验证',
      '声明完成前必须跑验证', '不验证不算完成'],
     ['test', 'verify']),
    # Web UI / 界面偏好
    (['web ui', 'claude code web', 'claudecodeui', '网页端', 'web 面板', '界面',
      '深色主题', '紫色', 'aa3bff'],
     ['webui', 'ui', 'preference']),
    # 输出风格
    (['重复', '冗余', '零容忍', '简洁'],
     ['output', 'style']),
    # 结构化报告
    (['结构化总结', '总结报告', '提炼'],
     ['output', 'report', 'style']),
    # MCP / 架构
    (['mcp 服务器', 'cc-hermes-bridge', 'hermes'],
     ['mcp', 'architecture', 'backend']),
    # 技术栈
    (['react', 'typescript', 'vite', 'fastapi', '技术栈'],
     ['architecture', 'backend', 'frontend']),
    # 记忆 / 知识
    (['记忆文件', '记忆系统', 'cocoindex', '语义搜索', '知识库'],
     ['memory', 'knowledge']),
    # 代理 / Claude 连接
    (['代理', '星火', 'anthropic', '7897'],
     ['proxy', 'api', 'claude']),
    # 部署容错
    (['非致命', '继续推进', '容忍', '安装失败'],
     ['deployment', 'resilience']),
    # 排障风格
    (['实用主义', '先诊断', '诊断后治疗'],
     ['troubleshooting', 'pragmatic']),
    # 自主授权 / 工作流
    (['极简指令', '自主探索', '宽授权', '全权', '严控制'],
     ['autonomy', 'trust', 'workflow']),
    # AI 辅助工作流
    (['延续开发迭代', '上下文延续', 'ai 辅助'],
     ['workflow', 'ai']),
    # 反爬
    (['cloakbrowser', '反爬', '爬虫'],
     ['scraping', 'antibot']),
]

TARGET_CATEGORIES_DEFAULT = {'feedback', 'user'}
TARGET_CATEGORIES_ALL = {'feedback', 'user', 'project', 'reference', 'seed'}


def _contains(content_lower: str, kw: str) -> bool:
    kw = kw.lower()
    if len(kw) >= 4 or not kw.isascii():
        return kw in content_lower
    return re.search(r'\b' + re.escape(kw) + r'\b', content_lower) is not None


def classify_tags(mid: int, content: str) -> list:
    """Curated tags for known rows; keyword-rule fallback for unknown rows."""
    if mid in CURATED_TAGS:
        return list(CURATED_TAGS[mid])
    cl = content.lower()
    tags: list[str] = []
    seen: set[str] = set()
    for keywords, rule_tags in RULES:
        if any(_contains(cl, kw) for kw in keywords):
            for t in rule_tags:
                if t not in seen:
                    seen.add(t)
                    tags.append(t)
    return tags


def _ensure_schema(conn):
    cols = {r[1] for r in conn.execute('PRAGMA table_info(memories)')}
    if 'tags' not in cols:
        conn.execute('ALTER TABLE memories ADD COLUMN tags TEXT')
        conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--all', action='store_true', help='tag project/reference/seed too')
    ap.add_argument('--dry-run', action='store_true', help='report only, write nothing')
    ap.add_argument('--force', action='store_true', help='overwrite existing tags')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)

    cats = sorted(TARGET_CATEGORIES_ALL if args.all else TARGET_CATEGORIES_DEFAULT)
    placeholders = ','.join('?' * len(cats))
    if args.force:
        rows = conn.execute(
            f"SELECT id, category, content, tags FROM memories WHERE category IN ({placeholders})",
            cats,
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT id, category, content, tags FROM memories "
            f"WHERE category IN ({placeholders}) AND (tags IS NULL OR tags = '')",
            cats,
        ).fetchall()

    updated = 0
    skipped = 0
    print(f'DB: {DB_PATH}')
    print(f'categories: {cats}  force={args.force}  dry_run={args.dry_run}')
    print(f'candidate rows: {len(rows)}')
    print('-' * 72)
    for r in rows:
        tags = classify_tags(r['id'], r['content'])
        if not tags:
            print(f"  [skip] id={r['id']} [{r['category']}] (no rules matched)")
            skipped += 1
            continue
        tags_str = ','.join(tags)
        print(f"  [{'DRY' if args.dry_run else 'set'}] id={r['id']} [{r['category']}] -> {tags_str}")
        if not args.dry_run:
            conn.execute("UPDATE memories SET tags = ? WHERE id = ?", (tags_str, r['id']))
            updated += 1
    if not args.dry_run:
        conn.commit()
    print('-' * 72)
    print(f'updated={updated} skipped_no_match={skipped} total={len(rows)}')
    conn.close()


if __name__ == '__main__':
    main()
