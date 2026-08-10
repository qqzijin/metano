#!/usr/bin/env python3
"""Sink low-frequency / scenario-specific CLAUDE.md rules into metano memory with tags.

Moves the "低频经验" out of /home/dk/CLAUDE.md (LEARNED-PREFS section) into the
metano memory DB, tagged by scenario so a SessionStart hook can re-inject them
only when relevant.

Usage:
    PYTHONPATH="${METANO_HOME:-$HOME/.claude/metano}" python3 "${METANO_HOME:-$HOME/.claude/metano}/sink_claude_prefs.py"
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from metano.memory import add_memory  # noqa: E402

# (content, category, importance, tags)
# category: 'feedback' = learned behavioral rule, 'reference' = lookup info
RULES = [
    # --- Habit: project evolution / continuation planning ---
    ("项目演进中主动进行架构整洁与技术债清理：重命名统一（如cc-hermes-bridge→metano），持续整理代码/项目结构，果断删除0字节死DB、冗余服务、桥接代码等无用资产，倾向清除而非保留",
     'feedback', 0.6, ['workflow', 'refactor', 'cleanup', 'evolution']),
    ("跨会话工作衔接与续接规划：本地优先构建独立于AI的持久化记忆体系（Markdown+YAML frontmatter含node_type/originSessionId/modified等元数据，UTC ISO时间戳记录修改），显式记录'明天的续接点'待办进行跨日规划；续接工作时以极简指令触发，完全依赖已持久化状态而非重新交代上下文；与AI内置记忆做交叉验证时不盲信，采用短小、分阶段的测试风格（初始探测→分隔符→二次确认），先验证载体可靠再依赖",
     'feedback', 0.7, ['workflow', 'continuation']),

    # --- Edit tool discipline ---
    ("使用Edit工具修改代码前，必须先读取目标文件的相关代码段，确认old_string在文件中完全匹配且未被修改，避免因内容不一致导致编辑失败",
     'feedback', 0.7, ['tooling', 'edit']),
    ("使用Edit工具修改文件时，若编辑失败（old_string不匹配），必须重新读取文件获取最新内容，用最新的代码片段重试编辑，绝不能盲目猜测文件当前状态",
     'feedback', 0.7, ['tooling', 'edit']),
    ("使用Edit工具修改代码时，old_string必须包含足够的上下文（如完整的函数签名、类定义或唯一标识行），避免使用过短的代码片段导致多处匹配或匹配失败",
     'feedback', 0.7, ['tooling', 'edit']),
    ("使用Edit工具修改代码前，必须先执行cat或grep确认目标文件中确实存在要替换的代码段，若替换失败必须重新读取文件最新内容，禁止在未确认文件当前状态的情况下反复重试",
     'feedback', 0.7, ['tooling', 'edit']),

    # --- Context continuation ---
    ("上下文续接时，必须先完整阅读前次会话摘要中的关键决策和未完成任务，明确当前进度后再执行操作，不可跳过上下文直接开始新操作",
     'feedback', 0.7, ['workflow', 'continuation']),
    ("上下文续接时，必须先完整阅读前次会话摘要，梳理已完成的修改、未完成的任务和关键决策，明确当前代码库状态后再执行操作，禁止跳过上下文直接操作",
     'feedback', 0.7, ['workflow', 'continuation']),

    # --- code_scan discipline (evolution system) ---
    ("If a code_scan returns 0 new observations and 0 new proposals, do not repeat code_scan for at least N turns or until a code modification is made.",
     'feedback', 0.5, ['code_scan', 'evolution']),
    ("Only perform code_scan if the underlying codebase has been modified since the last scan, or if the issue count has changed.",
     'feedback', 0.5, ['code_scan', 'evolution']),
    ("Limit consecutive code_scan actions without an intervening code modification to a maximum of 2. Force the agent to propose or apply a fix after 2 consecutive empty scans.",
     'feedback', 0.5, ['code_scan', 'evolution']),

    # --- Cost / budget ---
    ("成本估算需区分引擎成本和对话成本，避免用户日常使用费用触发进化系统暂停",
     'feedback', 0.6, ['cost', 'budget']),

    # --- Tool fallback ---
    ("工具调用失败时自动降级：主工具→替代工具→模拟工具→用户协助，每层降级时透明告知用户",
     'feedback', 0.6, ['tooling', 'fallback']),

    # --- Frustration detection / autonomy ---
    ("检测用户挫败感信号（重复纠正、连续拒绝、极简指令），自动降低自主性并增加确认点",
     'feedback', 0.6, ['workflow', 'frustration', 'autonomy']),

    # --- Undercover mode ---
    ("长时间自主任务采用隐蔽模式：静默执行、关键节点汇报、保留中断权和审计权",
     'feedback', 0.6, ['workflow', 'undercover', 'autonomy']),

    # --- Memory system design prefs ---
    ("记忆系统需加入时间维度：追踪何时学到了什么，支持时序查询和趋势分析",
     'feedback', 0.5, ['memory', 'evolution']),
    ("记忆质量需量化评估：增加召回率指标，定期验证记忆有效性",
     'feedback', 0.5, ['memory', 'evolution']),

    # --- Security boundary ---
    ("安全边界需更谨慎：宽授权需配合关键操作确认机制，避免自动执行高风险命令",
     'feedback', 0.7, ['security', 'safety']),

    # --- Reference: Scrapling project (project CLAUDE.md content) ---
    ("Scrapling 爬虫项目：目录 /home/dk/scrapling-project/。工具：static_fetcher.py 静态抓取、dynamic_fetcher.py 动态抓取(Playwright)、antibot_fetcher.py 反爬绕过(StealthyFetcher)、proxy_manager.py 代理轮换。也可直接用 from scrapling import Fetcher, DynamicFetcher, StealthyFetcher",
     'reference', 0.5, ['reference', 'scrapling']),
    ("Scrapling 用法：StaticFetcher().get(url) 静态页；DynamicPageFetcher().get(url, wait_for='.content') 需JS渲染的SPA页；AntiBotFetcher().bypass_cloudflare(url) 强反爬。输出保存到 /home/dk/scrapling-project/output/",
     'reference', 0.5, ['reference', 'scrapling']),
    ("Scrapling 注意事项：强反爬站点（小红书、BOSS直聘等）直接抓页面返回空内容，优先走公开 API；动态抓取需要 Playwright + Chromium（已安装）",
     'reference', 0.5, ['reference', 'scrapling']),

    # --- Reference: CocoIndex code index ---
    ("CocoIndex 跨项目语义代码搜索（已全局注册为 MCP）：cd 项目目录 && ccc init 初始化；ccc index 建索引；ccc search \"查询\" 搜索；ccc status 状态；ccc doctor 诊断。已索引项目：cc-hermes-bridge、DailyHotApi",
     'reference', 0.5, ['reference', 'cocoindex']),

    # --- Reference: metano CORS / DB path ---
    ("metano 前端 CORS 允许：https://nn.250823.xyz、http://localhost:5173、http://localhost:9120；数据库根目录由 METANO_HOME 指定；前端 dev :5173 代理 /api -> :9120",
     'reference', 0.5, ['reference', 'metano']),
]


def main() -> None:
    added = duplicate = 0
    print(f"Total rules to sink: {len(RULES)}")
    for content, category, importance, tags in RULES:
        r = add_memory(content, category=category, importance=importance, tags=tags)
        status = r.get('status')
        if status == 'added':
            added += 1
            print(f"  [added] id={r.get('id')} tags={r.get('tags')} :: {content[:40]}...")
        else:
            duplicate += 1
            print(f"  [duplicate] id={r.get('id')} tags={tags} :: {content[:40]}...")
    print(f"\nDone. added={added}, duplicate(skipped)={duplicate}")


if __name__ == '__main__':
    main()
