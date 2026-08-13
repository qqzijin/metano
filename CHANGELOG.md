# 更新记录 (Changelog)

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [v3.3.1] - 2026-08-13

第三轮全量审计（8 路独立复核，172 条结论）P0–P2 修复 + 遗留项，全量测试 346 通过。

### 🛡️ 安全加固

- **子进程环境清洗 + 进程组隔离**：router/model_router 走 `scrub_subprocess_env()` 白名单，丢弃 JWT/SSH/飞书等运营密钥；超时 `killpg` 杀整棵进程树，孙进程不残留
- **CORS /api OPTIONS 预检放行**：preflight 走 CORS 层带 access-control 头
- **WebSocket ticket 竞态 + 复放**：时间戳惰性清理替代整体 clear，锁保证同 jti 仅放行一次
- **删除用户后存量 token 失效**：`get_token_version` 不存在用户返回 -1，A2A/MCP 显式拒绝
- **hook 注入 fail-closed**：`cd`/路径遍历拒绝 + `<untrusted_data>` 包裹
- **collab DNS-rebinding TOCTOU + 明文**：连接 pin 已验证 IP + Host 头，scheme 默认 https，per-host token
- **MCP cron / memory_add 补鉴权门**
- **A2A/MCP 分域密钥显式化**（独立随机 secret，不再从 jwt_secret 派生）
- **bundled 技能 SHA-256 哈希白名单**：篡改拒绝加载
- **消息写路径脱敏**：`redact_sensitive` 覆盖 app_secret / sk- / JWT / Bearer / GitHub PAT，存量明文回填清理
- **危险 shell 拦截**：两步"下载+执行"安装变体

### 🐛 功能修复

- **harvest 双触发回归**：interval 任务按墙钟槽位对齐（整点/半点）
- **jobs.json 统一原子写**：tempfile + fsync + os.replace + flock
- **成本计价统一 + 回填虚高**：synthetic 归零、luna 按配置价、真实烧穿成本保留
- **tool_error 落库**：行为学习数据源不再空转
- **llm 审计 session_id 透传**
- **孤儿数据清理**：observations 去重、proposals 悬停闭环、重复知识文档
- **cron 日志盲区**：print → logger，journal 真实时间戳
- **时区统一**：内部 UTC aware，展示处 astimezone
- **healthcheck 迁移 systemd timer**（bwrap 失效修复）；maintain-daily 双调度修复
- **web 访问日志 ISO 8601 格式**

### 📚 文档

- collab 环境变量（`METANO_COLLAB_SCHEME` / `VERIFY_SSL` / `A2A_TOKEN_<HOST>`）与配置示例补充

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
