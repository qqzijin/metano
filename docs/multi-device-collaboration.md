# 多设备 Claude 协同方案

> 状态:📋 存档待实施(2026-08-11)
> 背景:本机(开发机)+ 远程 NAS(群晖 aarch64)各部署了 Claude Code 与 metano 网关,需要协同执行任务。

## 现有基础设施

| 设备 | 地址 | Claude Code | metano | SSH |
|------|------|-------------|--------|-----|
| 本机 | localhost | ✅(默认) | ✅(9120) | — |
| 远程 NAS | nas.local | ✅(opencode 中转) | ✅(9120) | ✅ `user@nas.local` |

- 远程 Claude 走 opencode 中转(`ANTHROPIC_BASE_URL=https://opencode.ai/zen/go`),模型可配
- metano 有 a2a-hub MCP(多智能体协调器,未接 A2A v1.0)
- 两端 metano 各自独立网关(模型配置独立)

## 协同方案对比

### 方案 A:SSH + Headless(零依赖,立即可用)

本机脚本通过 SSH 远程执行 `claude -p`:

```bash
ssh user@nas.local 'cd <metano-home> && claude -p "任务描述" --output-format text'
```

- ✅ 现在就能用,适合一次性/批量任务分发
- ❌ 同步阻塞、无流式、无权限交互、不适合并发长任务与双向协同
- 演进点:本机 metano 加"远程任务"工具(Bash 里执行 ssh,结果回传)

### 方案 B:Claude Agent SDK + HTTP Agent Server(官方跨设备架构)

远程跑一个 **Agent Server 进程**,本机用 SDK client 连接:

- 远程 agent server 暴露 HTTP API:`POST /agents/{id}/messages`(SSE 流式)、`GET /agents/{id}/status`、`POST /agents/{id}/interrupt`、权限请求/响应
- 本机用 `claude-agent-sdk`(Python/TS)的 client 派任务、收流式结果
- 支持:多会话、流式、权限回调、并发、水平扩展

关键设计(来自 Agent SDK 生态调研):
- **传输层**:Unix Domain Socket(1 Agent = 1 Process = 1 UnixServer)或 **HTTP**(远程/分布式标准)
- **会话持久化**:`~/.claude/projects/<cwd>/<session-id>.jsonl`,支持 `continue`/`resume`/`fork`
- **Hooks**:`PreToolUse`/`PostToolUse`/`SubagentStart`/`Notification` 等,可做事件驱动协同
- **安全**:HTTP 加 bearer token / mTLS;headless 场景可给会话隔离 sandbox,只暴露自定义 MCP 工具

### 方案 C:A2A 协议(协议化,长期)

- 把本机/远程 Claude 各封装为 A2A agent(AgentCard + JSON-RPC),通过 metano 的 a2a-hub 互通
- 任何 A2A 客户端可跨设备调度;多设备互发现、协议化、与外部 agent 互操作
- 前置:metano a2a-hub 对接 A2A v1.0(差 P0+P1 JWS 签名卡,方案已出,~180 行)

### 方案 D:metano 网关集成(远程变成本机 agent)

- 本机 metano 加"远程任务"能力(SSH/HTTP 调远程 Claude),远程作为本机可调用 agent/工具
- 与方案 B 结合最顺:远程 Agent Server → 本机 metano 封装成 tool

## 推荐路径

1. **方案 A 验证概念**(15 分钟):SSH 远程 `claude -p` 跑通"本机派任务→远程执行→回传"
2. **方案 B 完整协同**:远程部署 Agent SDK HTTP server(systemd 常驻),本机 SDK client 或集成进 metano
3. **方案 C/D 演进**:A2A 协议化统一;或 metano 网关集成

## 关键参考

- Claude Agent SDK:https://github.com/anthropics/claude-agent-sdk-python
- Multiagent orchestration(Managed Agents):https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration
- Claude Code agent modes(subagents/background agents/agent teams/dynamic workflows):Agent SDK v0.3.149+ 支持 dynamic workflows
- 跨进程架构参考:UnixAgentServer 提案(1 Agent = 1 Process = 1 UnixServer)、HTTP transport 提案(远程 agent 执行 + SSE)

## 决策记录

- 2026-08-11:调研完成,用户决定"先存档不实施",后续需要时再定方案
