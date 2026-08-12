---
trust: bundled
name: native-mcp
description: "metano MCP: built-in tool surface, remote read-only /mcp, external servers via ~/.mcp.json."
version: 1.1.0
author: ""
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [MCP, Tools, Integrations, metano]
    related_skills: []
---


# metano 的 MCP 能力

metano 自身就是一台 FastMCP 服务器（`metano/mcp_server.py`，60+ 工具），同时也可作为
MCP *客户端* 连接外部 MCP 服务器（stdio / HTTP）并把这些工具暴露给上层。本技能说明这两面。

> 原 Hermes 版的 `~/.hermes/config.yaml` + `mcp_servers` 配置在 metano 中**不存在**。
> 外部 MCP 服务器请用标准的 `~/.mcp.json` 配置（见下）。

## 何时使用

- 想了解 metano 自己暴露了哪些 MCP 工具
- 想把外部 MCP 服务器（filesystem、github、数据库等）接到 metano 会话
- 想从远程（另一台机器 / 远程 Claude）只读查询 metano 的数据

## metano 内置 MCP 工具面（本地 stdio / 进程内）

`metano/mcp_server.py` 用 FastMCP 暴露 60+ 工具，按用途分组：

| 组 | 工具 |
|---|---|
| 会话/搜索 | `session_search` `session_list` `session_get` |
| 统计 | `analytics_summary` `analytics_daily` |
| 技能 | `skills_list` `skill_view` `skill_manage` `skill_bundle` |
| 浏览器 | `browser_navigate` `browser_screenshot` `browser_click` `browser_fill` `browser_get_content` |
| 搜索 | `web_search` `web_search_tavily` `x_search` |
| 代码/子代理 | `code_run` `agent_spawn` `agent_status` `agent_result` |
| 记忆/知识 | `memory_search` `memory_timeline` `knowledge_search` `knowledge_list` |
| 图片/语音 | `image_generate` `image_describe` `voice_speak` `voice_list` |
| 其他 | `cron_*` `personality_*` `kanban_*` `home_*` `honcho_*` `evolution_*` `model_list` `security_*` |

在进程内直接用（例如网关 router / 测试）：`from metano.mcp_server import mcp; await mcp.call_tool("code_run", {...})`。

## 远程只读 MCP 端点（Streamable HTTP）

metano 把 `mcp_policy.READ_TOOLS` 白名单（只读工具）挂载在 Web 服务的 `/mcp` 路径，
供远程 MCP 主机（如另一台机器的 Claude）查询数据。鉴权用 Bearer JWT（`aud=metano-mcp`）。

```bash
# 1. 签发 1h 只读 token（POST /api/mcp/token，参数 username）
TOKEN=$(curl -s -X POST http://127.0.0.1:PORT/api/mcp/token \
  -H "Content-Type: application/json" -d '{"username":"alice"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 2. 在远程 MCP 客户端里配置该端点
#    url: http://<metano-host>:<port>/mcp
#    headers: { "Authorization": "Bearer $TOKEN" }
```

白名单见 `metano/mcp_policy.py::READ_TOOLS`；`code_run`、`agent_spawn`、`browser_*`、
`skill_manage` 等危险/写工具**永远不会**暴露到远程端点。允许的 Host 由
`METANO_ALLOWED_HOSTS` 环境变量控制（逗号分隔，默认仅 localhost）。

## 外部 MCP 服务器（作为客户端）

连接外部 MCP 服务器（stdio 或 HTTP）用标准的 `~/.mcp.json`（Claude Code / 桌面端通用格式）：

```json
{
  "mcpServers": {
    "tavily": {
      "command": "tavily-mcp",
      "env": { "TAVILY_API_KEY": "tvly-..." }
    },
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/documents"]
    },
    "remote_api": {
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer sk-..." }
    }
  }
}
```

- **stdio**：`command` + `args`（可选 `env`、`timeout`）。
- **HTTP / StreamableHTTP**：`url` + 可选 `headers`。
- 每个 server 必须有 `command`（stdio）或 `url`（HTTP），二选一。
- 修改 `~/.mcp.json` 后需要重启上层客户端（Claude Code / 网关）才会重新发现工具。

## Tavily 搜索密钥

`web_search_tavily` 的 `TAVILY_API_KEY` 通过以下任一方式提供（`metano/mcp_bridge.py`）：

1. 环境变量 `TAVILY_API_KEY`
2. `~/.mcp.json` 里 `mcpServers.tavily.env.TAVILY_API_KEY`

未配置时 `web_search_tavily` 返回 `{"error": "TAVILY_API_KEY not configured"}`。

## 安全

- 远程 `/mcp` 端点是**只读白名单**（JWT 校验 + `mcp_policy` 分层），写/破坏性工具不暴露。
- 外部 stdio MCP 子进程的 env 被过滤：只传安全基线变量（PATH/HOME/USER/LANG/TERM 等），
  密钥须显式在 `env` 里声明（见 `metano/mcp_bridge.py`）。
- 工具调用出错时，错误消息里的凭据模式（`ghp_`、`sk-`、`token=` 等）会被打码。

## Troubleshooting

| 现象 | 处理 |
|---|---|
| `web_search_tavily` 返回 "not configured" | 设 `TAVILY_API_KEY` 或补 `~/.mcp.json` 的 tavily env |
| 远程 `/mcp` 401 | 重新 `POST /api/mcp/token` 拿新 token |
| 外部服务器连不上 | 检查 `command`/`url` 是否正确、npx/uvx 是否在 PATH、timeout 是否过短 |
| 工具没出现 | 确认 server 在 `mcpServers`（不是 `mcp`）、重启上层客户端 |

## 注意事项

- metano 内置工具和外部 MCP 工具可以同时使用，互不冲突。
- 内置工具面在进程内常驻；外部服务器连接生命周期由上层客户端管理。
- 增加/删除外部服务器需要重启客户端（无热加载）。
