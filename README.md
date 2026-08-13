# metano

<p align="center">
  <img alt="版本" src="https://img.shields.io/badge/版本-v3.3.2-6a4ff3" />
  <img alt="平台" src="https://img.shields.io/badge/平台-Linux%20%7C%20macOS-28a745" />
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" />
  <img alt="Node" src="https://img.shields.io/badge/Node.js-18%2B-339933?logo=nodedotjs&logoColor=white" />
  <img alt="License" src="https://img.shields.io/badge/License-MIT-yellow" />
  <img alt="PRs Welcome" src="https://img.shields.io/badge/PRs-Welcome-brightgreen" />
</p>

> **版本：v3.3.2** · AI 网关桥接层 · 自我进化引擎 · 多平台消息接入 · RAG 知识库 + 知识图谱 · 记忆技能库

> 📜 更新记录见 [CHANGELOG.md](CHANGELOG.md)

**支持平台**：Linux（x86_64 / aarch64，已实测群晖 NAS / x86 服务器 / WSL）· macOS（Python 3.11+）· 需 Python 3.11+、Node.js 18+、npm。Windows 未官方支持（WSL2 可用）。

为 Claude Code 提供多维度扩展能力的桥接层：把个人 AI 助手从"单会话对话"升级为**能记住你、能自我改进、能跨平台触达**的常驻系统。

---

## ✨ 核心特性

| 模块 | 说明 |
|------|------|
| 🧬 **自我进化引擎** | `Observe → Reason → Act → Reflect → Maintain` 五阶段闭环，系统从会话中持续学习你的偏好并内化为行为规则 |
| 💾 **用户建模 (Honcho)** | 信念生命周期：DRAFT → ESTABLISHED → CORE，置信度驱动，观察→信念方言推理 |
| 🧠 **记忆技能库** | 自动收割会话 → 提取观察 → 生成信念 → 注入 CLAUDE.md（原子回滚）；记忆带场景 tag，SessionStart 按 tag 注入上下文 |
| 🔧 **技能系统** | 48+ 内置技能（源自 Hermes Agent 精选），支持技能发现、校验、自定义、**编辑/删除（内置受保护）**，使用频率统计 |
| 📚 **RAG 知识库 + 本地向量** | 文档导入（`.md` 优先）、分块、本地向量检索（chunks embedding，离线）；目录批量导入默认跳过代码文件，知识库专注知识文档 |
| 🕸️ **知识图谱** | 实体-关系图，PPR（Personalized PageRank）相关扩散检索，前端可视化页 |
| 🔌 **MCP 服务器** | 60+ 工具注册给 Claude Code（stdin/stdout 协议）+ 只读远程 MCP（Streamable HTTP，JWT 鉴权，跨设备协同） |
| 💬 **多平台网关** | Discord / Telegram / QQ / 微信 / 飞书 / Web Chat 统一路由 |
| 🖥️ **Web 控制面板** | React 19 + FastAPI，精简导航：**工具中心**（技能+MCP）· **设置中心**（配置+安全）· **记忆中心**（记忆+画像）· 搜索统一入口；聊天支持 **SSE 流式输出 + 思考过程 + 工具调用卡片 + Markdown 渲染**，跨路由流不中断 |
| 🔄 **配置热重载** | `gateway_config.yaml` 变更自动热重载（web 进程轮询，改价格/模型/凭据免重启） |
| 🧭 **经验记忆 + 路由反馈** | 任务签名分类 + ε-greedy bandit 路由 + Reflexion 反思经验注入（`DO:/AVOID:`），让 AI 越用越聪明 |
| 🤝 **多设备协作** | A2A v1.0 AgentCard（JWS 签名）+ 跨设备协同页（CollabPage）；跨设备任务执行（A2A message/send + tasks/get 轮询，本地/远程均可派发） |
| 🧪 **Be-ACTIVE 即时学习** | 检测到用户纠正后立即生成规则/技能提案，不等定时任务 |
| 🔬 **自我进化（Self-Modify）** | 进化系统自主修改自己的代码：扫描反模式 → LLM 生成修复 → 沙箱隔离验证（bwrap，无网络/无真实密钥）→ 人工审批 → 应用 → git 回滚；默认停用，需显式开启 + 审批 |
| 🛠️ **运维** | `healthcheck.sh` 健康检查 · `backup.sh` 数据库备份 · `maintain_daily.sh` 每日维护（备份+健康+VACUUM+清理）· 定时任务 9 个内置（+可选运维 shell 任务）· 会话保留（180天/512MB）· 每周知识主动探索 |

## 🧬 自我进化系统

这是本项目区别于一般"记忆工具"的核心：

**第三层：自我代码修改（Self-Modify）** —— 进化系统不仅能维护数据/规则，还能修改自己的 Python 代码。这是"AI 自举"（self-improvement）的落地：

```
SCAN      发现自身反模式（silent-except/sql-concat 等）与结构问题
  ↓
GENERATE  LLM 生成修复 diff（变异候选）
  ↓
VERIFY    隔离 git worktree 应用 → 全量测试 + 模块导入检查（变异的"环境筛选"）
  ↓
APPLY     存活变异 → git commit + 同步运行实例（记录 commit hash）
  ↓
LOG       写入 self_modify_events 表（变异记录），可 git revert 一键回滚
```

**安全宪法**（自举永远碰不到）：
- `self_modify.py` 自身不可被修改（否则验证门可被关闭）
- `tests/` 不可被修改（验证门自身）
- 只改 `metano/*.py` 业务代码，不碰配置/密钥/DB
- 每次变异必须过验证门（全量测试+导入），无例外
- 验证在隔离 worktree 进行，主系统在验证通过前不受任何影响

坏变异会被验证门淘汰并记录；已应用的变异可经 Web「自我进化」页一键 git revert。这就是软件的"种群"——系统永远能回到活着的版本，允许大胆变异而不会自毁。

```
Observe (收割器)    每 30min + SessionStart/End 钩子，从会话提取观察
   │
Reason (方言推理)   新观察 vs 现有信念 → add/update/contradict/ignore
   │                 信念置信度 0.5→0.8+，分 DRAFT/ESTABLISHED/CORE
   │
Act (适配器)        信念 → 行为变化：
   │                 A. CLAUDE.md 注入（标记区域，可原子回滚）
   │                 B. Memory 文件生成（同类别 ≥3 信念时）
   │                 C. 设置/技能改进提案（需用户审批，不自动应用）
   │
Reflect (反思器)    每周评估模型质量：一致性/覆盖度/陈旧度/行为规则效果
   │
Maintain (维护)     信念衰减、合并、归档、时间趋势抽象、成本熔断器
```

**Be-ACTIVE 即时学习**：SessionEnd 检测到纠正信号（"不对/错了/重复/必须验证..."）→ 立即在后台分析并生成行为规则 + 技能改进提案，无需等待每日 cron。

**内置定时任务**（`cron/jobs.json`，默认 9 个进化任务）：harvest（每 30min）· introspect（每 2h）· maintenance（每日 03:03，信念生命周期）· self-modify（每日 04:30，内置默认停用，需显式开启）· knowledge-sink（每日 05:30）· evaluate（每 6h）· explore（周日 03:00）· architect（周日 05:00）· session-retention（周日 06:00）。

运行实例的 `cron/jobs.json` **可能额外注册运维类 shell 任务**（如 `healthcheck` 每小时、`maintain-daily` 每日 02:30），因此实际任务数可多于 9（例如当前运行实例为 11）。注意：这类 shell 任务经 cron daemon 在 bwrap 沙箱（`--tmpfs $HOME`）内执行，**看不到真实 `METANO_HOME` 会直接失败**——运维脚本（`backup.sh` / `healthcheck.sh` / `maintain_daily.sh`）应改由 `metano.sh` 创建的 systemd user timer（`metano-maintain.timer`，每日 02:30）在沙箱外执行，或手工运行。

**安全机制**：
- CLAUDE.md 注入用 `<!-- LEARNED-PREFS-START/END -->` 标记隔离，可原子回滚
- 设置/技能变更永远走审批门，不自动应用
- 内置/pinned 技能受保护，自主进程不可修改
- 每日进化成本超阈值自动熔断（区分引擎成本与对话成本）

## 🕸️ 知识图谱与本地向量

- **本地向量检索**：知识库 chunks 自带 embedding（BLOB 存于 `knowledge.db`），搜索时本地向量 + 关键词混合召回，**离线可用，无需 CocoIndex 冷启动**。
- **知识图谱**：基于 chunks 自动抽取实体（技术/概念/文件/人等模式匹配）与关系（chunk 内共现，置信度加权）。
- **PPR 检索**：给定实体/关键词运行 Personalized PageRank 扩散（HippoRAG 风格），返回直接邻居 + 多跳相关实体，按 relatedness 排序。
- **API**：`GET /api/knowledge/graph`（查询实体/关系）、`POST /api/knowledge/graph/extract`（重建图谱）、`GET /api/knowledge/graph/stats`；懒构建，首次访问自动重建。
- **前端**：Web 面板"知识图谱"页（`/knowledge-graph`）可视化图谱浏览。

## 🧭 经验记忆 + 路由反馈（自进化闭环）

网关级"越用越聪明"机制（`route_events.py` + `experience.py`，默认关闭，`METANO_EXPERIENCE_ENABLED=1` 或 `gateway_config.yaml` 配 `experience.enabled: true` 开启）：

1. **任务签名**：对用户请求规范化 hash + 关键词分类（code / qa / research / cron / chat）
2. **bandit 路由**：ε-greedy 从已配置 provider 中选策略；reward = 质量 − α×成本 − β×延迟；冷启动 <5 次强制探索，ε 随事件数衰减
3. **经验注入**：检索 top-k 语义相似历史经验，以 `DO:`/`AVOID:` 前缀注入 prompt
4. **Reflexion 反思**：失败时记录原因 + 修复动作；成功判定独立于生成模型（启发式 + 可外接 evaluator）
5. **防退化**：失败经验降权/失活，每 50 事件清理低效经验

## 🔄 配置热重载

`gateway_config.yaml` 由 `config_watcher.py` 轮询监控（2s 间隔），检测到真实内容变化后自动热重载 `model_router`（模型/价格/默认模型）。**改配置不再需要重启 web 进程**——对远程 NAS 部署尤其重要（避免远程重启卡死）。特性：

- mtime + size 快路径，内容签名去重（touch / 注释变化不触发）
- 稳定读：文件写入中途不应用半成品
- fail-safe：reload 失败保留旧配置，不崩溃

## 🧠 记忆技能库

跨会话语义记忆库（`memory.db`），每条记忆可带多个场景 tag（如 `backend`、`frontend`、`sync`、`workflow`、`cost`），按场景检索（多 tag 为 AND 语义）。

- **SessionStart 注入**：`hook_inject_memory.py` 按配置的 `INJECT_TAGS` 把低频经验注入会话上下文（per-tag 上限 4 条，总长上限 2000 字符）
- **下沉**：`sink_claude_prefs.py` 把 CLAUDE.md 中低频/场景化规则带 tag 写入记忆库；`backfill_memory_tags.py` 为存量记忆回填 tag
- **检索/压缩**：FTS5 全文 + LIKE 回退 + 按 tag 浏览；自动合并 30 天以上低重要度（<0.3）记忆
- **导入导出**：JSON 迁移，合并去重

## 🏗️ 架构

```
┌─────────────────────────────────────────────────────────┐
│                    消息平台网关                           │
│  Discord │ Telegram │ QQ │ WeChat │ Feishu │ Web Chat   │
└──────────────────────┬──────────────────────────────────┘
                       │ MessageRouter
                       ▼
┌─────────────────────────────────────────────────────────┐
│                    metano 核心                           │
│   技能系统 │ 安全系统 │ 模型路由 │ 知识库RAG │ 知识图谱     │
│   本地向量 │ 记忆系统 │ 定时任务 │ 子代理 │ 浏览器         │
│   语音TTS │ 智能家居 │ 代码沙箱 │ 图像生成 │ 看板          │
│   ┌──────────────┐ ┌──────────────┐ ┌───────────────┐    │
│   │ 自我进化引擎   │ │ 经验记忆+路由  │ │ 配置热重载     │    │
│   │ Obs→Reas→Act │ │ 任务签名→bandit│ │ config_watcher│    │
│   │ →Refl→Maint  │ │ →经验注入DO/AVO│ │ mtime轮询     │    │
│   └──────────────┘ └──────────────┘ └───────────────┘    │
│   ┌──────────────┐ ┌──────────────┐ ┌───────────────┐    │
│   │ 知识主动探索   │ │ Honcho 建模   │ │ 只读远程MCP+A2A│    │
│   │ Tavily→摄入   │ │ 观察→信念→推理 │ │ /mcp JWT鉴权   │    │
│   └──────────────┘ └──────────────┘ └───────────────┘    │
└──────────────────────┬──────────────────────────────────┘
                       │
              ┌────────┴────────┐
              │   SQLite 数据库  │
              │  bridge.db      │
              │  evo.db         │
              │  memory.db      │
              │  knowledge.db   │
              │  honcho.db      │
              └─────────────────┘
```

## 🚀 快速开始

> 📖 **完整部署指南见 [`DEPLOYMENT.md`](DEPLOYMENT.md)**（含精确命令、验证清单、故障排查，AI 可直接执行）

```bash
# 1. 克隆仓库
git clone <仓库地址> metano && cd metano

# 2. 一键安装（环境检测 → venv 装依赖 → 前端构建 → 生成配置 → 初始化 DB → 启动 + 健康检查）
./install.sh

#    可选参数：
#    --with-embedding   额外安装本地向量嵌入（sentence-transformers + torch）
#    --with-browser     额外安装 Playwright 浏览器自动化
#    --skip-frontend    跳过前端构建（已构建过 / 无网络时）
#    --skip-start       只安装不启动，之后可运行 bash metano.sh start
```

安装脚本最后会输出**访问地址**（`http://localhost:9120`）与**初始 admin 密码**（admin / 随机密码，同时写入 `$METANO_HOME/initial_admin_password.txt`，权限 600）。首次登录后请立即在 Web 面板修改密码。

数据目录由 `METANO_HOME` 控制（默认 `~/.claude/metano`），可部署到任意目录：`METANO_HOME=/srv/metano ./install.sh`。

**日常运行管理**（`metano.sh`）：

```bash
./metano.sh start                # 一键启动全部服务（Web 面板 + Cron + 消息网关 + CocoIndex）
./metano.sh start web            # 按服务启动：web / cron / gateway / cocoindex
./metano.sh status               # 查看各服务状态
./metano.sh stop / restart       # 停止 / 重启全部服务
./metano.sh setup                # 重新运行 gen_config.py 并启动
./metano.sh maintain-timer       # （重）建每日维护 systemd timer（沙箱外执行 backup/healthcheck/vacuum）

bash healthcheck.sh              # 健康检查（web/gateway/cron/cocoindex），--repair 自动重启 DOWN 的服务
```

> Claude Code 集成（进化引擎依赖）：配置 `hooks.example.json` 到 `~/.claude/settings.local.json`，详见 DEPLOYMENT.md 第 6 节。

> 服务管理：默认走 **systemd 用户服务**（`metano-web` / `metano-cron` / `metano-gateway`，由 `metano.sh` 自动创建 unit；每日维护走 `metano-maintain.timer`）。`ecosystem.config.js` 为**遗留 PM2 配置**，仅作参考——与 systemd 并存会冲突，不建议使用。

## ⚙️ 环境变量

代码通过 `os.environ` 读取以下变量（仓库提供模板 `.env.example`，复制到 `$METANO_HOME/.env` 后填写）：

| 变量 | 用途 |
|------|------|
| `METANO_HOME` | 运行时数据目录（DB/配置/备份/venv 根目录），默认 `~/.claude/metano`；可部署到任意目录 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` | 进化引擎/反思器/行为分析的 LLM |
| `HONCHO_MODEL` | 用户建模/反思模型 |
| `HERMES_JWT_SECRET` | Web 面板 JWT 密钥（不设则 `gen_config.py` 自动生成并写入 gateway_config.yaml） |
| `HERMES_DEFAULT_PASSWORD` | 管理员初始密码（不设则 `gen_config.py` 随机生成） |
| `METANO_EMBED_PYTHON` | 本地向量嵌入所用解释器路径（装有 sentence-transformers+torch，如 cocoindex venv）；未设自动探测 |
| `METANO_EMBED_MODEL` | 本地向量嵌入模型（默认 `Snowflake/snowflake-arctic-embed-xs`） |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / ... | 飞书网关（可选，也可放 gateway_config.yaml） |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | 其他模型提供商（可选） |
| `HA_URL` / `HA_TOKEN` | 智能家居 Home Assistant（可选） |
| `INJECT_TAGS` | SessionStart 注入记忆的场景 tag（逗号分隔，per-tag 上限 4 条） |
| `BACKUP_RETENTION_DAYS` | 数据库备份保留天数（默认 7 天） |
| `METANO_EXPERIENCE_ENABLED` | 开启经验记忆+路由反馈闭环（`1` 开启，默认关） |
| `METANO_COLLAB_SCHEME` | 跨设备协作 A2A 派发传输协议（http/https，默认 https；或 gateway_config.yaml `collab.scheme`） |
| `METANO_COLLAB_VERIFY_SSL` | https 派发是否校验远端 TLS 证书（默认 true；或 `collab.verify_ssl`） |
| `METANO_A2A_TOKEN_<HOST>` | 远端 A2A Bearer token（按主机，host 大写、点转下划线，如 `METANO_A2A_TOKEN_PEER_EXAMPLE_COM`；或 `collab.tokens[host]`） |
| `METANO_A2A_TOKEN` | 远端 A2A Bearer token 全局回退（未按主机配置时使用） |

## 🔌 Claude Code 集成（自我进化引擎的关键）

进化系统通过 Claude Code **钩子**从会话中持续学习。部署者必须配置：

1. 把 `hooks.example.json` 的内容合并进 `~/.claude/settings.local.json`（把 `__PROJECT_ROOT__` 换成项目绝对路径）
2. 记忆注入钩子：SessionStart 调用 `hook_inject_memory.py` 按 `INJECT_TAGS` 注入场景记忆
3. MCP 服务器注册：在 `~/.claude/settings.json` 的 `mcpServers` 中加入：

```json
{
  "mcpServers": {
    "metano": {
      "command": "python3",
      "args": ["-m", "metano.mcp_server"],
      "cwd": "/绝对路径/到/项目"
    }
  }
}
```

配置后每次会话开始/结束/用户输入/工具调用都会触发进化采集（30min 定时收割 + SessionEnd 纠正即时学习 + SessionStart 记忆注入）。

## ⚙️ 配置

`gateway_config.yaml`（位于 `$METANO_HOME/`）管理模型提供商、消息网关、智能家居、语音（TTS）等。首次运行由 `gen_config.py` 自动生成：随机 JWT secret + 初始 admin 密码（bcrypt），各消息网关默认 disabled；可用 `metano.sh setup` 重新运行。仓库另提供**脱敏示例** `gateway_config.example.yaml`，真实密钥请自行填写，且**不要提交到 Git**。

### 🧭 配置引导（首次配置向导）

`./install.sh` 在检测到 LLM key 缺失时会询问是否运行向导；也可随时手动运行：

```bash
python3 gen_config.py --wizard                                  # 交互式配置全部核心功能
python3 gen_config.py --wizard --home /srv/metano               # 指定数据目录
```

向导逐项交互询问，**每一步立即写入** `gateway_config.yaml`（幂等，Ctrl-C 后重跑可从断点继续；已有值会作为默认值预填）。它**不会覆盖已有 auth**（JWT secret / admin 密码），除非加 `--force`。向导覆盖：

| 类别 | 配置项 | 要点 |
|------|--------|------|
| **LLM 通道**（必填） | `base_url` / `api_key` / `model` | 写入 `models.default`；默认网关 `https://opencode.ai/zen/go`、默认模型 `claude-sonnet-4-6`。此 key 是对话 / 记忆 / 进化的共同底座 |
| **飞书** | `app_id` / `app_secret`（可选 `encryption_key` / `verification_token`） | 需在[飞书开放平台](https://open.feishu.cn)创建应用，配置事件订阅与机器人能力 |
| **QQ** | `ws_url` | 默认 `ws://127.0.0.1:3001`；**前置：先运行 NapCat（或其它 OneBot v11 实现）** |
| **微信** | `method`（`wcferry` / `ilink`） | `wcferry` 需在 Windows 端运行微信机器人框架；`ilink` 为 iPad 协议登录 |
| **Telegram** | `bot_token` | 需先通过 [@BotFather](https://t.me/BotFather) 创建机器人 |
| **Discord** | `bot_token`（可选 `guild_id`） | 需在 Discord Developer Portal 创建应用并邀请机器人入服 |
| **本地向量嵌入** | 安装决策 | 选「是」后按提示运行 `./install.sh --with-embedding` 安装 torch 等依赖；向导同时写入 `METANO_EMBED_MODEL` |
| **浏览器自动化** | 安装决策 | 选「是」后按提示运行 `./install.sh --with-browser` 安装 Playwright + chromium |
| **只读远程 MCP** | `METANO_ALLOWED_HOSTS` | 选「是」并填局域网 Host 模式（如 `192.168.1.50:*,nas.local:*`），写入 `$METANO_HOME/.env`；供另一台机器跨设备调用本机只读工具 |

**自查清单**（哪些功能需要什么）：

- **Web 面板 / AI 对话 / 记忆 / 进化** → 只需 LLM 通道的 `api_key`（`models.default.api_key` 或 env `ANTHROPIC_API_KEY` 任选其一）。
- **消息网关** → 对应渠道的凭据 + 该渠道前置（NapCat / BotFather / 飞书开放平台 / Discord 开发者后台 / wcferry 等）；各渠道独立，未配置的渠道不影响其它功能。
- **RAG 知识库语义检索** → 需要本地向量嵌入（`METANO_EMBED_PYTHON` 指向装有 torch 的解释器；未配时回退 CocoIndex 或关键词检索）。
- **网页浏览 / 截图工具** → 需要 Playwright 浏览器（`./install.sh --with-browser`）。
- **局域网远程 MCP / 跨设备协作** → 需要 `METANO_ALLOWED_HOSTS` 含对应主机，且服务监听可被局域网访问；访问 `/mcp` 需先签发 Bearer JWT（`POST /api/mcp/token`）。
- **网页搜索** → 可选 `TAVILY_API_KEY`（未配置自动降级 duckduckgo）。

修改 `gateway_config.yaml` 后 web 进程会自动热重载（见「🔄 配置热重载」），改价格 / 模型 / 凭据无需重启。

**模型提供商配置**（`models.<name>` 段）：
- `price: {input, output, cache_read}` 自定义价格（解析顺序：显式配置 → 内置表 → 默认 3/15）
- `proxy: direct` 强制直连（清系统代理 + 注入 NO_PROXY），`proxy: http://host:port` 为该模型单独走指定代理——解决"某些模型需代理、某些需直连"的混合场景
- `default: true` 设为默认模型

**配置变更自动热重载**：修改 `gateway_config.yaml` 后 web 进程自动检测并生效（见"🔄 配置热重载"节），改价格/模型无需重启。

## 📁 项目结构

```
metano/
├── install.sh               # 一键安装脚本（环境检测 → venv 依赖 → 前端构建 → 生成配置 → DB 初始化 → 启动）
├── gen_config.py            # 生成 gateway_config.yaml（随机 JWT secret + admin 密码 → initial_admin_password.txt；--wizard 交互式配置全部核心功能）
├── .env.example             # 环境变量模板（METANO_HOME / LLM key / embedding 等）
├── requirements-embedding.txt # 可选依赖：本地向量嵌入（sentence-transformers + torch）
├── metano.sh                # 服务启动脚本（start/stop/status/restart/setup + 按服务）
├── healthcheck.sh           # 健康检查（web/gateway/cron/cocoindex，支持 --repair）
├── backup.sh                # 数据库自动备份（5 DB + 配置，7 天保留）
├── maintain_daily.sh        # 每日维护（备份 + 健康检查--repair + DB VACUUM + 清理）
├── hook_inject_memory.py    # SessionStart hook：按 tag 注入记忆
├── sink_claude_prefs.py     # 低频 CLAUDE.md 经验下沉到记忆库（带 tag）
├── backfill_memory_tags.py  # 为存量记忆回填场景 tag
│
├── metano/                  # Python 后端包
│   ├── paths.py             #   集中路径解析（METANO_HOME 支持，DB/日志/数据目录）
│   ├── web_server.py        #   FastAPI Web 面板 + REST API + WebSocket
│   ├── serve.py             #   Web 服务 CLI 入口（uvicorn :9120，含配置热重载启动）
│   ├── config_watcher.py    #   gateway_config.yaml 轮询热重载（mtime+内容签名，失败保旧）
│   ├── mcp_server.py        #   MCP 工具服务器（60+ 工具）
│   ├── mcp_policy.py        #   MCP 工具风险分级（16 只读 / 19 危险）
│   ├── mcp_http.py          #   只读远程 MCP（Streamable HTTP，allowed_hosts 局域网访问）
│   ├── mcp_gateway.py       #   MCP Bearer JWT 鉴权中间件 + /api/mcp/token（跨设备协同）
│   ├── a2a_server.py        #   A2A v1.0 服务（AgentCard / JSON-RPC / tasks）
│   ├── route_events.py      #   经验记忆闭环核心（任务签名 + bandit + reward）
│   ├── experience.py        #   Reflexion 反思经验 + DO:/AVOID: prompt 注入
│   ├── collab.py            #   跨设备协作（A2A 客户端派发 message/send+tasks/get，本地/远程执行）
│   ├── auth.py              #   登录认证（JWT access/refresh + 角色）
│   ├── evolution.py         #   自我进化协调器（Observe→Reason→Act→Reflect→Maintain）
│   ├── harvester.py         #   观察收割器
│   ├── adapter.py           #   信念→行为 适配器 + 提案执行引擎
│   ├── reflector.py         #   自我反思引擎
│   ├── behavior_analyzer.py #   行为模式分析（纠正聚类→规则）
│   ├── skill_improvement.py #   类级技能改进提案（Be-ACTIVE）
│   ├── code_introspector.py #   自扫描代码反模式 → 观察/提案
│   ├── architect.py         #   架构自建模 + 瓶颈检测 + 重构提案
│   ├── knowledge.py         #   RAG 知识库 + 知识图谱 + 本地向量检索
│   ├── knowledge_explorer.py#   知识主动探索（Tavily + LLM + 缺口发现）
│   ├── memory.py            #   记忆系统（tags / FTS5 / 压缩 / 导入导出）
│   ├── model_router.py      #   多模型路由（per-provider proxy: direct / 自定义价格）
│   ├── db.py                #   数据库层（含会话保留策略 purge_old_sessions）
│   ├── cron_daemon.py       #   定时任务守护进程
│   ├── honcho/              #   用户建模引擎（信念/观察/方言推理）
│   ├── skills/              #   技能系统（发现/管理/校验/模板）
│   ├── skills_data/         #   内置技能定义（48+）
│   ├── gateway/             #   多平台消息网关
│   └── voice/               #   语音模块（仅 TTS，edge-tts）
├── web/                     # React 19 前端（含移动端抽屉导航、SSE 流式聊天）
│   └── src/lib/chatStream.ts#   模块级 SSE 流管理器（跨路由流不中断）
├── tests/                   # pytest 测试（181）
├── backups/                 # 数据库自动备份（按日期归档，保留 7 天）
└── personalities/           # 人格模板（12）
```

## 🔗 MCP 工具（部分，64+ 内置 + 外部）

**进化系统**：`evolution_status` · `evolution_run` · `evolution_approve` · `evolution_suggestions` · `evolution_log`

**用户建模**：`honcho_observe` · `honcho_profile` · `honcho_beliefs` · `honcho_dialectic` · `personality_list`

**知识库 / 图谱**：`knowledge_ingest` · `knowledge_search` · `knowledge_list`

**记忆系统**：`memory_add` · `memory_search` · `memory_stats` · `memory_compress` · `memory_timeline`

**会话 / 统计**：`session_search` · `session_list` · `session_get` · `analytics_summary`

**技能 / 定时任务**：`skills_list` · `skill_view` · `cron_list` · `cron_add` · `cron_remove`

**浏览器 / 搜索**：`browser_navigate` · `browser_screenshot` · `web_search` · `x_search`

**语音（TTS）**：`voice_speak` · `voice_list`

**其他**：`agent_spawn` · `code_run` · `image_generate` · `home_control` · `kanban_board` · `security_status`

**只读远程 MCP**（跨设备协同，D 方案）：`/mcp` 端点暴露 16 个只读工具子集（`session_search` · `knowledge_search` · `memory_search` 等），Bearer JWT 鉴权（`aud=metano-mcp`，`POST /api/mcp/token` 签发 1h token），支持局域网跨设备访问（`allowed_hosts` 配置）。另一台机器在 Claude Code 中配置 `metano-local` MCP server 即可调用本机工具。

**A2A**：`/.well-known/agent-card.json` 暴露 AgentCard，`/a2a` 支持 JSON-RPC task 委派（跨设备协作）。

## ⏰ 定时任务（cron 默认 9 个内置进化任务）

| 任务 | 调度 | 职责 |
|------|------|------|
| `harvest` | 每 30 分钟 | 收割未处理会话 → 观察/纠正/工具错误 |
| `introspect` | 每 2 小时 | 代码反模式扫描 → 生成提案 |
| `evaluate` | 每 6 小时 | 评估已应用提案的效果 |
| `maintenance` | 每日 03:03 | 进化维护一次跑完：信念衰减/压缩/合并 → LLM 反思 → 信念适应 |
| `self-modify` | 每日 04:30 | 自举：扫描反模式 → 修复 → 沙箱验证 → 审批 → 应用（**内置默认停用**，`DEFAULT_JOBS=False`；需在 `cron/jobs.json` 显式开启） |
| `knowledge-sink` | 每日 05:30 | 进化经验沉淀知识库 + 成功经验综合为规则 |
| `explore` | 周日 03:00 | 主动探索知识缺口（2 主题/次） |
| `architect` | 周日 05:00 | 架构自审 + 重构提案 |
| `session-retention` | 周日 06:00 | 会话保留清理（180 天/512MB） |

调度分散避开整点批量。上述 9 个为**内置进化任务**（`cron_daemon.DEFAULT_JOBS` 种子）；运行实例的 `cron/jobs.json` 可能额外注册运维类 shell 任务（`healthcheck` / `maintain-daily`），实际任务数可多于 9。

> **运维脚本执行方式（重要）**：`backup.sh` / `healthcheck.sh` / `maintain_daily.sh` 是 **shell 脚本**，若注册为 cron daemon 的 shell 任务，会在 bwrap 沙箱（`--tmpfs $HOME`）内执行而**看不到真实 `METANO_HOME` → 直接失败**。因此它们**不应注册为默认 cron 任务**，而应由 `metano.sh` 创建的 systemd user timer（`metano-maintain.timer`，每日 02:30，`metano.sh start` 或 `metano.sh maintain-timer` 创建）在沙箱外执行，或手工运行。健康检查 `healthcheck.sh` 已改用 `systemctl --user is-active` 判定（兼容 systemd；无 systemd 时回退 pidfile）。

## 🧪 测试

```bash
python3 -m pytest tests/ -q
```

## ⚠️ 数据安全

本仓库为**纯源码导出**，不含任何运行时数据：

- ❌ 不包含任何 `.db` 数据库文件（bridge.db / evo.db / memory.db / knowledge.db / honcho.db 等）
- ❌ 不包含 `.env` / 密钥文件 / 真实配置
- ❌ 不包含会话记录、用户建模数据、进化日志、知识库数据
- ✅ 所有密钥以 `gateway_config.example.yaml` 脱敏模板提供

## 🤝 贡献指南

欢迎贡献！无论是修 bug、加功能、改进文档，还是提 issue，都非常感谢。

### 开发环境

```bash
# 克隆 + 一键安装（含开发依赖）
git clone https://github.com/<your-repo>/metano.git && cd metano
./install.sh --skip-start

# 前端开发模式（热更新，:5173 代理到 :9120）
cd web && npm run dev

# 后端（另开终端）
METANO_HOME=./dev-data python3 -m metano.serve

# 运行测试
python3 -m pytest tests/ -q
```

### 提交规范

- **分支**：从 `main` 切新分支（`feat/xxx` 或 `fix/xxx`），完成后开 PR
- **Commit 信息**：`feat:` / `fix:` / `docs:` / `chore:` / `refactor:` 前缀 + 中文或英文描述
- **测试**：Python 改动必须带 pytest 用例；前端改动跑 `npm run build` 验证编译
- **验证**：改动后实际运行验证（不是只看代码逻辑推断），涉及前端必须重建 dist

### 代码约定

- **后端**：Python 3.11+，模块按职责拆分到 `metano/`；新增能力参考现有模块风格（函数注释 + `logger` 而非 `print`）
- **前端**：React 19 + shadcn/ui + Tailwind；新页面加到 `web/src/pages/` 并在 `App.tsx` 注册路由
- **配置**：新增可配置项优先走 `gateway_config.yaml`（`gen_config.py` 提供向导），密钥永不硬编码——用环境变量
- **隐私**：仓库为纯源码导出，**严禁提交**任何 `.db` / `.env` / 真实密钥 / 私人 ID / 局域网 IP

### 安全

- 发现安全漏洞请**不要**公开提 issue，直接邮件或开私有讨论，修复后再公开披露
- 涉及密钥、鉴权、消息网关的改动请特别注明安全影响

## 📜 License

[MIT](LICENSE) © metano 贡献者
