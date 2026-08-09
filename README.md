# metano

> AI 网关桥接层 · 自我进化引擎 · 多平台消息接入 · RAG 知识库 · 技能系统

为 Claude Code 提供多维度扩展能力的桥接层：把个人 AI 助手从"单会话对话"升级为**能记住你、能自我改进、能跨平台触达**的常驻系统。

---

## ✨ 核心特性

| 模块 | 说明 |
|------|------|
| 🧬 **自我进化引擎** | `Observe → Reason → Act → Reflect → Maintain` 五阶段闭环，系统从会话中持续学习你的偏好并内化为行为规则 |
| 💾 **用户建模 (Honcho)** | 信念生命周期：DRAFT → ESTABLISHED → CORE，置信度驱动，观察→信念方言推理 |
| 🧠 **记忆系统** | 自动收割会话 → 提取观察 → 生成信念 → 注入 CLAUDE.md（原子回滚） |
| 🔧 **技能系统** | 48+ 内置技能（源自 Hermes Agent 精选），支持技能发现、校验、自定义 |
| 📚 **RAG 知识库** | 文档导入、向量检索、语义搜索 |
| 🔌 **MCP 服务器** | 40+ 工具注册给 Claude Code（stdin/stdout 协议） |
| 💬 **多平台网关** | Discord / Telegram / QQ / 微信 / 飞书 / Web Chat 统一路由 |
| 🖥️ **Web 控制面板** | React 19 + FastAPI，17 个页面，进化审批、数据分析、实时日志 |
| 🧪 **Be-ACTIVE 即时学习** | 检测到用户纠正后立即生成规则/技能提案，不等定时任务 |

## 🧬 自我进化系统

这是本项目区别于一般"记忆工具"的核心：

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

**安全机制**：
- CLAUDE.md 注入用 `<!-- LEARNED-PREFS-START/END -->` 标记隔离，可原子回滚
- 设置/技能变更永远走审批门，不自动应用
- 内置/pinned 技能受保护，自主进程不可修改
- 每日进化成本超阈值自动熔断（区分引擎成本与对话成本）

## 🏗️ 架构

```
┌─────────────────────────────────────────────────────────┐
│                    消息平台网关                           │
│  Discord │ Telegram │ QQ │ WeChat │ Feishu │ Web Chat   │
└──────────────────────┬──────────────────────────────────┘
                       │ MessageRouter
                       ▼
┌─────────────────────────────────────────────────────────┐
│                    metano 核心                  │
│   技能系统 │ 安全系统 │ 模型路由 │ 知识库RAG │ 定时任务     │
│   子代理 │ 浏览器 │ 语音TTS/STT │ 智能家居 │ 看板         │
│  ┌─────────────────────────────────────────┐            │
│  │        自我进化引擎                       │            │
│  │  Observe → Reason → Act → Reflect       │            │
│  │  收割器 → 方言推理 → 适配器 → 反思器      │            │
│  └─────────────────────────────────────────┘            │
│  ┌─────────────────────────────────────────┐            │
│  │        Honcho 用户建模                    │            │
│  │  观察 → 信念 → 方言推理 → 信念生命周期     │            │
│  └─────────────────────────────────────────┘            │
└──────────────────────┬──────────────────────────────────┘
                       │
              ┌────────┴────────┐
              │   SQLite 数据库  │
              └─────────────────┘
```

## 🚀 快速开始

> 📖 **完整部署指南见 [`DEPLOYMENT.md`](DEPLOYMENT.md)**（含精确命令、验证清单、故障排查，AI 可直接执行）

```bash
# 1. Python 依赖
pip install -r requirements.txt

# 2. 环境变量（LLM key 等，详见「环境变量」节）
cp .env.example .env && vim .env

# 3. 配置（可选，网关/认证等）
cp gateway_config.example.yaml gateway_config.yaml

# 4. 前端（Web 面板 UI，必需：dist 不在仓库，API 可用但面板需此步）
cd web && npm install && npm run build && cd ..

# 5. 启动服务
python3 -m metano.serve              # Web 面板 + API（:9120，首启自动建 admin）
python3 -m metano.honcho.serve       # Honcho 用户建模（:9121）
python3 -m metano.cron_daemon start  # 定时任务（首启自动播种进化调度）

# 6. Claude Code 集成（进化引擎依赖，详见 DEPLOYMENT.md 第 6 节）
#    配置 hooks.example.json 到 ~/.claude/settings.local.json
```

> 生产部署：`ecosystem.config.js` 为 PM2 进程管理配置（metano-web/honcho/gateway/cron 四服务）。

> 💡 首次启动会自动创建 admin 账号：设了 `HERMES_DEFAULT_PASSWORD` 用它，否则生成随机密码（见启动日志）。部署后请立即改密码。

## ⚙️ 环境变量

代码通过 `os.environ` 读取以下变量（详见 `.env.example`）：

| 变量 | 用途 |
|------|------|
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` | 进化引擎/反思器/行为分析的 LLM |
| `HONCHO_MODEL` | 用户建模/反思模型 |
| `HERMES_JWT_SECRET` | Web 面板 JWT 密钥（不设则自动生成） |
| `HERMES_DEFAULT_PASSWORD` | 管理员初始密码（不设则随机生成） |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / ... | 飞书网关（可选，也可放 gateway_config.yaml） |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | 其他模型提供商（可选） |
| `HA_URL` / `HA_TOKEN` | 智能家居 Home Assistant（可选） |

## 🔌 Claude Code 集成（自我进化引擎的关键）

进化系统通过 Claude Code **钩子**从会话中持续学习。部署者必须配置：

1. 把 `hooks.example.json` 的内容合并进 `~/.claude/settings.local.json`（把 `__PROJECT_ROOT__` 换成项目绝对路径）
2. MCP 服务器注册：在 `~/.claude/settings.json` 的 `mcpServers` 中加入：

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

配置后每次会话开始/结束/用户输入/工具调用都会触发进化采集（30min 定时收割 + SessionEnd 纠正即时学习）。

## ⚙️ 配置

`gateway_config.yaml` 管理模型提供商、消息网关、智能家居、语音等。仓库只提供**脱敏示例** `gateway_config.example.yaml`，真实密钥请自行填写，且**不要提交到 Git**。

## 📁 项目结构

```
├── metano/                    # Python 后端包
│   ├── evolution.py           # 自我进化协调器（Observe→Reason→Act→Reflect→Maintain）
│   ├── harvester.py           # 观察收割器
│   ├── adapter.py             # 信念→行为 适配器 + 提案执行引擎
│   ├── reflector.py           # 自我反思引擎
│   ├── behavior_analyzer.py   # 行为模式分析（纠正聚类→规则）
│   ├── skill_improvement.py   # 类级技能改进提案（Be-ACTIVE）
│   ├── code_introspector.py   # 自扫描代码反模式 → 观察/提案
│   ├── architect.py           # 架构自建模 + 瓶颈检测 + 重构提案
│   ├── knowledge_explorer.py  # 知识缺口探索
│   ├── model_router.py        # 多模型路由
│   ├── honcho/                # 用户建模引擎（信念/观察/方言推理）
│   ├── skills/                # 技能系统（发现/管理/校验/模板）
│   ├── skills_data/           # 内置技能定义（48+）
│   ├── gateway/               # 多平台消息网关
│   ├── voice/                 # 语音 TTS/STT
│   ├── web_server.py          # FastAPI Web 面板
│   └── mcp_server.py          # MCP 工具服务器（40+ 工具）
├── web/                       # React 19 前端
├── tests/                     # pytest 测试（155+）
└── personalities/             # 人格模板（12）
```

## 🔗 MCP 工具（部分）

`evolution_status` · `evolution_run` · `evolution_approve` · `evolution_suggestions` · `honcho_observe` · `honcho_profile` · `knowledge_ingest` · `knowledge_search` · `skills_list` · `skill_view` · `cron_list` · `cron_add` · `web_browse` · `session_search` · `analytics_summary` · `agent_spawn` · `voice_speak` ...

## 🧪 测试

```bash
python3 -m pytest tests/ -q
```

## ⚠️ 数据安全

本仓库为**纯源码导出**，不含任何运行时数据：

- ❌ 不包含任何 `.db` 数据库文件（bridge.db / evo.db / honcho.db 等）
- ❌ 不包含 `.env` / 密钥文件 / 真实配置
- ❌ 不包含会话记录、用户建模数据、进化日志、知识库数据
- ✅ 所有密钥以 `gateway_config.example.yaml` 脱敏模板提供

## 📜 License

MIT
