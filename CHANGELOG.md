# 更新记录 (Changelog)

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [v3.3.0] - 2026-08-13

自 v3.2.0 以来的 23 个提交：两轮安全/功能审计修复（48 + 97 项发现全量验证）、前端依赖升级、开箱即用改进。

### 🛡️ 安全加固

- **网关消息权限 + 平台闭环**：跨渠道安全修复（C-01/C-02/H-08），宿主级工具执行已堵死
- **Web 后端权限/IDOR/SSRF**：认证分域、令牌安全（H-01/H-02/H-03/H-06/H-09）
- **执行沙箱 + 自修改 + Cron 单源**（H-04/H-05/F-01）
- **MCP / A2A 授权**收窄与加固
- **登录秒登出修复**：Secure Cookie 在 HTTP 转发下被浏览器丢弃 → 按请求 scheme / `X-Forwarded-Proto` 决定 Secure 标记（HTTPS 下仍强制）
- **CLAUDE.md 注入内容策略** + config 类型安全（H4/H9）
- 全库 `busy_timeout` + 三入口 `umask` + auth 文件 `chmod`
- 多用户隔离 / 危险命令检测（补二轮审计遗漏）

### ✨ 新特性

- **开箱即用**：`metano.sh` 自动创建 systemd user unit + cron PATH 去硬编码
- **移除硬编码本机路径**，环境变量可覆盖
- `router.py` claude 路径支持 `CLAUDE_BIN` 环境变量（与其他 3 处一致）

### 🐛 功能修复

- **前端依赖升级**：vite 5.4 → 6.4.3，npm audit 17 → 0 漏洞
- **vite /ws 代理**（L-03 WS 前端可连）
- **功能闭环审计修复**：Cron 单源 / provider / 进化 / 记忆 / 索引器 / 前端（F 系列）
- **飞书 message_read_v1 未注册处理器** — 消除持续 ERROR 噪音
- **UserPromptSubmit / SessionEnd 钩子字段名修复**（F-5 根因 + 同类遗漏）
- **数据正确性 / 成本 / 学习链路**（B 系列）
- **F-3 sink 过滤**：correction 类观察不再直入知识库
- 监控 / 备份 / 部署脚本 / 供应链（二轮审计项）

### 📚 文档

- 示例配置补充 `experience.enabled`（经验闭环开关，M8）
- `.gitignore` 忽略 CocoIndex 索引目录

## [v3.2.0] - 2026-08-12

首个带标签发布。自进化 AI 网关：多平台消息接入（飞书/QQ/微信/Telegram/Discord）、RAG 知识库 + 知识图谱、记忆技能库、经验记忆 + 路由反馈自进化闭环、systemd 用户服务管理。

[CHANGELOG 自 v3.3.0 起维护]
