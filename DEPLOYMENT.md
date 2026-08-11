# metano 部署指南

> 本文档面向**人工或 AI 代理**，每一步都有精确命令和验证命令。按顺序执行即可完成部署。
> 所有步骤在 Linux / macOS / WSL2 下适用。

---

## 0. 环境要求

| 依赖 | 版本 | 验证命令 |
|------|------|---------|
| Python | ≥ 3.10 | `python3 --version` |
| Node.js | ≥ 18（仅前端构建需要） | `node --version` |
| git | 任意 | `git --version` |
| 操作系统 | Linux / macOS / WSL2 | — |

## 1. 克隆与依赖安装

```bash
# 克隆（把 <your-repo> 换成你的仓库地址）
git clone https://github.com/<your-repo>/metano.git
cd metano

# 一键安装（推荐）：环境检测 → venv 依赖 → 前端构建 → 生成配置 → 初始化 DB → 启动 + 健康检查
./install.sh

# 手动安装（可选）：见 README「快速开始」
```

**验证**：
```bash
python3 -c "import fastapi, mcp, jwt, bcrypt, yaml; print('核心依赖 OK')"
```

## 2. 环境变量

复制 `.env.example` 并编辑（或直接 export）：

```bash
cp .env.example .env
```

**必填**：
- `ANTHROPIC_API_KEY` — 进化引擎/反思器的 LLM key（缺了引擎不学习，但系统不崩）

**强烈建议**：
- `HERMES_JWT_SECRET` — Web 面板 JWT 密钥（不设则自动生成）
- `HERMES_DEFAULT_PASSWORD` — 管理员初始密码（不设则**随机生成**，见第 5 步）

**可选**：飞书网关、其他模型、智能家居等（见 `.env.example` 注释）

> 代码读取方式：`os.environ`，即 `.env` 需先 source，或用 `set -a; source .env; set +a`。

## 3. 服务配置

```bash
cp gateway_config.example.yaml gateway_config.yaml
```

按需填写网关（feishu 等）与 auth。若配置无 admin 用户，首次启动会自动创建（见第 5 步）。

## 4. 启动服务

| 服务 | 端口/方式 | 命令 |
|------|----------|------|
| Web 面板 + API | 9120 | `python3 -m metano.serve` |
| Honcho 用户建模 | 9121 | `python3 -m metano.honcho.serve` |
| 定时任务守护（进化引擎核心） | 后台 | `python3 -m metano.cron_daemon start` |
| 消息网关 | 后台 | `python3 -m metano.gateway.launcher` |

> 生产可用 PM2：`pm2 start ecosystem.config.js`（含 metano-web/honcho/gateway/cron 四服务）。

**验证**：
```bash
# Web 面板
curl -s http://127.0.0.1:9120/api/status | python3 -m json.tool | head -20
# 应返回 {"sessions":..., "messages":..., "skills":...} 等状态
```

## 5. 首次启动：管理员账号

首次运行 `metano.serve` 时会自动创建 admin 账号：

1. 若设了 `HERMES_DEFAULT_PASSWORD` → 用它作为密码
2. **未设置** → 生成**随机 16 位密码**，打印在启动日志中（脱敏显示前 2 位），格式：
   ```
   No HERMES_DEFAULT_PASSWORD set; generated random admin password — check logs or set env var
   Default admin created with password: ab**************
   ```
3. 登录：`POST /api/auth/login`（body: `{"username":"admin","password":"..."}`）

> ⚠️ 部署后**立即改密码**：登录后调用 `POST /api/auth/change-password`。

## 6. Claude Code 集成（自我进化引擎的关键）

进化系统通过 Claude Code 钩子从会话中学习。必须配置：

### 6.1 钩子

把 `hooks.example.json` 内容合并进 `~/.claude/settings.local.json` 的 `"hooks"` 字段，
把 `__PROJECT_ROOT__` 替换为项目绝对路径：

```bash
# 示例（替换后）：
python3 -c "
import json, pathlib
p = pathlib.Path.home() / '.claude' / 'settings.local.json'
data = json.loads(p.read_text()) if p.exists() else {}
data['hooks'] = json.load(open('hooks.example.json'))['hooks']
p.write_text(json.dumps(data, ensure_ascii=False, indent=2))
print('hooks 已写入', p)
"
```

### 6.2 MCP 服务器

在 `~/.claude/settings.json` 的 `mcpServers` 加入：

```json
{
  "mcpServers": {
    "metano": {
      "command": "python3",
      "args": ["-m", "metano.mcp_server"],
      "cwd": "/绝对路径/到/metano"
    }
  }
}
```

### 6.3 验证

新开 Claude Code 会话，应看到：
```
[Memory] 可用记忆索引: ...
[Evolution] 行为规则提醒: ...
```
（首次为空属正常，积累几次会话后出现）

## 7. 定时任务（进化引擎调度）

cron 守护进程**首次启动自动播种** 8 个默认进化任务（无需手动配置）：

| 任务 | 调度 | 作用 |
|------|------|------|
| harvest | 每 30 分钟 | 收割会话→观察→信念 |
| introspect | 每 2 小时 | 扫描自身代码反模式 |
| adapt | 每天 03:00 | 信念→行为规则/CLAUDE.md 注入 |
| maintain | 每天 04:00 | 信念衰减/合并/成本熔断 |
| reflect | 每天 04:00 | 自我反思（一致性/效果） |
| evaluate | 每 6 小时 | 评估已应用提案效果 |
| architect | 每周日 05:00 | 架构自建模/瓶颈检测 |
| explore | 每周日 03:00 | 知识缺口探索 |

任务定义存于 `~/.claude/metano/cron/jobs.json`（首启自动生成，可手动增删改）。

## 8. 完整验证清单

```bash
# 1. 服务进程
ps aux | grep -E 'metano.serve|metano.honcho|cron_daemon' | grep -v grep

# 2. Web API
curl -s http://127.0.0.1:9120/api/status | python3 -m json.tool

# 3. 数据库自动创建（~/.claude/metano/ 下）
ls ~/.claude/metano/*.db ~/.claude/metano/honcho_data/honcho.db

# 4. 定时任务已播种
python3 -c "import sys; sys.path.insert(0,'.'); from metano.cron_daemon import load_jobs; print([j['name'] for j in load_jobs()])"

# 5. 登录
curl -s -X POST http://127.0.0.1:9120/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"你的密码"}' | head -c 200
```

## 9. 故障排查

| 症状 | 排查 |
|------|------|
| `/api/status` 无响应 | 确认 `metano.serve` 在跑、端口 9120 未被占用（`ss -tlnp \| grep 9120`） |
| 进化不产生规则 | ① `ANTHROPIC_API_KEY` 未设或无效 ② 钩子未配置（第 6 节）③ cron 未启动 |
| 钩子报错 `module not found` | `__PROJECT_ROOT__` 没替换成绝对路径，或依赖没装 |
| 登录 401 | admin 未创建（首次启动日志）或密码错；重置：设 `HERMES_DEFAULT_PASSWORD` 后删掉配置里 users 段重启 |
| 网关收不到消息 | 对应平台 `enabled: false`，或在 gateway_config.yaml 里未填凭据 |
| MCP 工具不出现 | `metano.mcp_server` 导入报错（`python3 -m metano.mcp_server` 直跑看错误），或 cwd 路径不对 |

## 10. 数据目录说明

运行时数据全部在 `~/.claude/metano/`（不进仓库）：
- `bridge.db` — 会话/消息
- `evo.db` — 行为规则/提案/审计
- `honcho_data/honcho.db` — 用户建模（信念/观察）
- `knowledge/` `kanban/` `evolution/` `cron/` — 知识库/看板/进化日志/定时任务
