---
trust: bundled
name: hermes-agent-skill-authoring
description: "Author metano SKILL.md: frontmatter, validator, structure, skill_manage."
version: 1.1.0
author: ""
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [skills, authoring, metano, conventions, skill-md]
    related_skills: [writing-plans, requesting-code-review]
---


# 编写 metano 技能（SKILL.md）

## Overview

metano 的技能就是一个带 YAML frontmatter 的 `SKILL.md` 文件。技能有两个存放位置：

1. **内置技能（bundled）**：`metano/skills_data/<category>/<name>/SKILL.md` —— 随仓库/发行版提供，只读保护（`source='bundled'`），autonomous 后台进程不能修改。
2. **用户技能（user）**：`~/.claude/metano/skills/<category>/<name>/SKILL.md`（`METANO_HOME` 环境变量可覆盖根目录）—— 个人使用，用 `skill_manage(action='create')` 创建。

本技能面向 **用户技能** 的编写。

## 何时使用

- 用户要求"新建一个技能 / 保存这个工作流"
- 把可复用的工作流固化成一个 slash 命令（`/<name>`）
- 修改已有技能内容

## Required Frontmatter

校验入口：`metano/skills/validator.py`（`validate_frontmatter` / `validate_content`）。硬性要求：

- 以 `---` 开头（首字节，无前导空行）
- 以 `\n---\n` 闭合
- 解析为合法 YAML mapping
- 必须有 `name` 字段
- 必须有 `description` 字段
- 闭合 `---` 之后必须非空正文

标准模板：

```yaml
---
name: my-skill-name               # 小写 + 连字符
description: 何时触发用 <trigger>。一句话行为描述。
version: 1.0.0
author: ""
trigger: /my-skill-name
category: productivity            # 归入 skills_data 下的某个分类
---
```

## 常用操作（skill_manage MCP 工具）

| 动作 | 命令 | 说明 |
|------|------|------|
| 创建 | `skill_manage(action="create", name=..., category=..., description=..., content=...)` | 写入 `~/.claude/metano/skills/<category>/<name>/SKILL.md` |
| 修改 | `skill_manage(action="edit", name=..., content=...)` | 整体替换正文 |
| 局部改 | `skill_manage(action="patch", name=..., old_string=..., new_string=...)` | 查找替换正文片段 |
| 删除 | `skill_manage(action="delete", name=...)` | 删除用户技能（内置技能受保护不可删） |
| 查看 | `skill_view(name=..., full=True)` | 查看完整内容 |

内置（bundled）技能受保护，`edit/patch/delete` 会被拒绝，除非带 `force=True`（仅用于用户已同意的提案）。

## 结构约定

```
# <Title>

## 概述 / When to Use
- 何时触发的要点
- 反例：何时不要用

## 正文小节
- 快速参考表格
- 代码块给出精确命令

## Common Pitfalls
编号列出易错点与修复

## 验证清单
- [ ] 发布后的自查项
```

不是每节都必须，但「触发条件 + 可执行正文 + 易错点」是底线。

## 验证

创建后用 `skills_list` 确认出现，用 `skill_view(name=..., full=True)` 检查渲染结果。
注意：当前会话的技能加载器有缓存（TTL 约 60s）——新建技能后稍等再查，或重开会话；这不是 bug。

## 跨技能引用

`description` 里可用 `related` 思路提及相邻技能名，但不要依赖不存在的技能。

## Common Pitfalls

1. **用 `skill_manage(action='create')` 想写内置技能** —— create 只会写用户目录，内置技能要改仓库文件（`metano/skills_data/`），并且受保护。
2. **frontmatter 前有空白** —— 校验要求 `---` 是首字节；前导空行/BOM 直接失败。
3. **description 太泛** —— 以"何时触发"开头，描述触发类别而非单个任务。
4. **正文缺失** —— 闭合 `---` 后必须有内容。
5. **写完立刻断言可见** —— 加载器有缓存，需等 TTL 或重开。
6. **引用不存在的辅助文件** —— 正文里 `scripts/`、`references/`、`templates/` 若文件不存在会误导模型；要么补文件，要么从正文删除引用。

## Verification Checklist

- [ ] 文件位于用户技能目录 `~/.claude/metano/skills/<category>/<name>/SKILL.md`
- [ ] frontmatter 从字节 0 开始 `---`，`\n---\n` 闭合
- [ ] `name` / `description` / `version` / `author` / `trigger` / `category` 齐全
- [ ] description 以触发条件开头，≤ 1024 字符
- [ ] 正文非空，结构：标题 → 触发条件 → 正文 → pitfalls
- [ ] 无指向不存在 `scripts/` / `references/` / `templates/` 文件的引用
