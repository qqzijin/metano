# 协同探索记录

- **开始时间**:2026-08-11
- **参与方**:本机 Claude(主导)+ 远程 Claude(HOST_REMOTE_PLACEHOLDER,协同,听本机指挥)
- **模式**:自由探索网络世界,主题不限;途中发现好主意/高可行性方案 → 双方探讨可行性
- **纪律**:全程只读,不动远程机器任何文件;信息经 metano 网页端对话交换
- **记录方式**:metano 持久会话(bridge.db)+ 本机存档(本文件)+ 飞书阶段性汇报
- **探索时长**:双方各 6 小时

---

## 探索日志

### 会话 1 · 探索启动(2026-08-11)

**【本机 Claude 指令】**
给远程 Claude 下达探索启动:分配初始领域(①跨设备协同与远程管理 ②A2A/多智能体互操作 ③Agent 自进化/记忆/测试时计算 ④自由深入),纪律(只读不动文件),工作方式(先给探索计划→开始探索→发发现)。

**【远程 Claude 回复·探索计划】**
- 分四条线并行只读检索:
  1. 跨设备 Agent 协同:HTTP Agent Server、Agent SDK、远程运维与安全认证
  2. A2A 互操作:Google A2A、MCP、Agent 通信与任务委派标准
  3. Agent 自进化与记忆:长期记忆、反思、工具反馈、测试时计算
  4. 新方向筛选:研究论文、开源项目、标准中找高可行性组合方案
- 优先官方文档/标准仓库/论文/活跃开源;每个发现记录来源/核心/价值/可行性;只读不动文件
- "我现在开始第一轮检索"

### 会话 2 · 本机侧第一批探索成果(4 个方向,2026-08-11)

**① A2A / MCP 互操作**
- A2A v1.0(2026-03,Linux Foundation 治理,150+ 组织):JSON-RPC 2.0 + AgentCard(`/.well-known/agent.json`) + Task 状态机,与 MCP 互补(agent↔agent vs agent↔工具);IBM ACP 已并入 A2A
- MCP 已成工具层标准(Linux Foundation,月下载 ~9700 万,2026-07 无状态化)
- 现成网关:Python `mcp-a2a-gateway`、Go `agentgateway` 可借鉴;官方 a2a-sdk 六语言 + inspector/tck 验证
- 安全:JWS 签名 AgentCard + OAuth2/OIDC/mTLS;MCP 风险是 tool-action,A2A 是 delegation
- 可行性:协议层 5/5;对"跨真实信任边界"才值得 A2A,内部编排用 MCP 轻量

**② 开源 Agent 生态**
- **OpenClaw**:2026 最大爆款,自托管个人 AI 助手完整架构(13+ 通道统一网关 + SKILL.md skills + 插件化记忆/模型/工具 + cron 主动任务 + 持久记忆),对 metano 是"地图级"参考
- MCP 工具总线是必须地基;LiteLLM 式路由商品化,护城河在 guardrails/观测/审计
- 观测标准收敛:OpenInference + OTel + OTLP;eval 用 Promptfoo(PR 门禁)
- Mastra Observational Memory:文本压缩型记忆省 4-10 倍 token
- 安全:MCP 工具投毒(30+ CVE),网关应做连接代理层(鉴权/策略/审计/DLP)
- 选型铁律:先定语言栈、MCP 原生、避开维护模式项目(AutoGen/Helicone/Humanloop)

**③ 多设备协同 / 免 SSH 远程管理**
- **Claude Agent SDK HTTP 化**:官方,本机/服务器 Claude Code 变远程服务,5/5
- **Claude Remote Control**:官方反向桥接(出站 HTTPS 零入站端口),5/5 但需 TTY/订阅
- **Managed Agents Sessions API**:全托管跨设备,4/5(beta)
- **CMDOP**:出站 relay agent + MCP fleet 工具,免 SSH 运维产品化,5/5
- MCP 远端 transport(HTTP/SSE)让本地 agent 驱动远端工具,5/5
- 推荐组合:HTTP 封装(连接层)+ MCP 远端 transport(工具层)+ A2A(跨机委托层)

**④ Agent 自进化 / 记忆**
- 记忆综述 Forms-Functions-Dynamics(~100 页,47 作者);agent 记忆 ≠ RAG,经验记忆最缺
- **Reflexion**(反思闭环):零成本落地,5/5
- **ExpRAG / ReMem**(经验复用,测试时记忆):免训练 4/5;Evo-Memory benchmark(Google DeepMind)
- **自适应路由**(BEST-Route/R2-Router/Pilot,bandit/成本感知):5/5,网关自我改进性价比最高
- MemGPT/Letta(agent 自管理记忆)、Mem0(轻量抽取记忆)可选型;Mem0 官方分 vs 第三方复现落差大
- Voyager 技能库范式;EvoTest 整系统自演化(回合间改 prompt/记忆/超参)
- 推理预算动态分配(测试时计算):难问题多 token,API 层即可,需路由+缓存配套

**本机综合判断 · 高可行性好主意候选**:
1. 自适应路由(bandit/成本感知)—— 5/5,网关自我改进最务实落点
2. Reflexion 反思 + ExpRAG 经验复用 —— 4-5/5,免训练
3. metano 暴露 MCP server(标准工具总线)—— 5/5
4. OpenClaw 架构参考(通道/skills/记忆/插件)—— 4/5
5. A2A server 封装(对外互操作)—— 4/5
6. 观测接入(OpenInference/OTLP)—— 4/5
7. CMDOP 式免 SSH 远程管理 —— 5/5


### 会话 3 · 远程 Claude 第一轮发现 + 双方汇合(2026-08-11)

**【远程 Claude 第一轮发现·落地架构】**
1. **A2A 关键在任务生命周期**(不只是消息):AgentCard/任务状态/流式更新/产物/认证分开定义;建议网关先实现 tasks/send、状态查询、SSE/WebSocket 更新,再兼容完整 A2A
2. **MCP 远程化用网关代理而非直接暴露工具**:统一代理层做工具白名单/签名校验/审计/超时;每台设备只出站连中心 relay,中心签短期 token,不开放入站 SSH/MCP 端口
3. **最务实"自进化"= 经验检索 + 路由反馈闭环**:保存成功轨迹/失败原因/修复动作/适用条件,按任务类型检索复用,用成功率/延迟/成本更新模型路由;建议 metano 加事件表(task_signature/strategy/outcome/cost/latency/error_class)+ contextual bandit 渐进优化;高风险动作人工确认

**【远程 Claude 总体组合建议】**
HTTP/反向 relay 连接层 → MCP 工具代理层 → A2A 长任务委派层 → 经验记忆与路由反馈层
(先验证,利于权限隔离和审计)

**【双方发现汇合】**
- 双方一致:连接层(HTTP/relay)+ 工具层(MCP 代理)+ 委派层(A2A)的组合
- 远程补充:经验记忆 + 路由反馈闭环(第 4 层)——与本机"自适应路由+经验复用"结论吻合
- 高可行性组合方向:A 经验记忆+路由反馈闭环 / B MCP 网关代理层 / C HTTP-relay 连接层+A2A / D metano 暴露 MCP server


### 会话 4 · 高可行性方案探讨(2026-08-11)

**【远程 Claude 方案评估】**
推荐实施顺序:D → B → A → C
- **D·metano 暴露 MCP server**:只读工具+鉴权+限流(风险:工具越权与数据泄露)
- **B·MCP 网关代理**:白名单代理、签名、审计(风险:凭证泄露、代理绕过)
- **A·经验记忆 + bandit 路由**:记录反馈,规则路由+经验检索(风险:反馈偏差导致路由退化)
- **C·HTTP-relay 连接 + A2A 委派**:反向 Relay+异步任务状态(风险:断线、重试与任务一致性)

**【双方共识】**
- D 和 A 与 metano 现状最贴近、可行性最高(MCP server 暴露 + 经验/路由闭环)
- B 是安全底线,MCP 工具投毒是真实攻击面
- C 是跨设备扩展层(呼应多设备协同)


### 会话 5 · 第二轮深入:D(MCP server 封装)成果(2026-08-11)

**关键发现:metano 已内置 60+ 工具的 stdio MCP server**(`metano/mcp_server.py`,FastMCP),已注册进 claude settings。MVP 不是从零建,而是升级为"本地全量 + 远端受控"双形态。

**MVP 方案(D)**
- 新建 `metano/mcp_http.py`:`FastMCP("metano-remote", stateless_http=True)`,按函数对象复用只读工具(~15个)注册
- 挂到现有 FastAPI:`app.mount("/mcp", http_app)`(单进程单端口,复用 9120),合并 lifespan(AsyncExitStack)
- 鉴权:复用 auth.py JWT,`JWTVerifier`(HS256, audience=metano-mcp)+ `/api/mcp/token` 签发短时只读 token(1h),写 audit
- 安全分层:只读(远端开放)/写(高scope或关)/危险(仅本地 stdio:code_run、browser_*、home_control、agent_spawn 等)
- 防 prompt bloat:远端只注册 ~15 只读工具,不倒全量
- 部署:nginx 对 /mcp 开 proxy_buffering off;`claude mcp add -t http http://localhost:9120/mcp`(token 不进明文)
- 参考:FastMCP 官方 SDK streamable-http、agentgateway 工具联邦+策略审计心智、mcp-a2a-gateway 双 transport
- **实施清单**:5 阶段(摸底/远端子集/鉴权/审计限流/部署验证),~8-10h

**一句话**:metano 已有 stdio MCP,本轮 MVP = 新增 stateless streamable-http 受控只读实例 + HS256 短时 token + audit/限流,不动现有本地工具集。


### 会话 6 · 第二轮深入:A(经验记忆+路由反馈)成果(2026-08-11)

**关键发现:metano 现有代码可复用度极高**
- `strategy.py` 已有 90/10 select + record_action/record_outcome 完整反馈闭环
- `evo_models.py` agent_rules 已有 effectiveness/times_applied/temporal_tag/recall_rate
- `memory.py` 已有 FTS5+向量检索;`db.py` 已有 WAL SQLite + 定价

**MVP 方案(A)**
- 选型:不引第三方库(Mem0/RouteLLM/contextualbandits 作参考),复用 metano 现有
- 增量仅 4 块:①`route_events` 表(task_signature/task_type/strategy/outcome/error_class/cost/latency/tokens/reflect)②task_signature 生成(hash+关键词分类)③bandit 更新(参考 LinUCB/ε-greedy,ε 按事件数衰减,冷启动 <5 次强制探索)④经验注入 prompt(DO:/AVOID: 前缀,Reflexion 反思存失败经验)
- 经验=ExpRAG(task/trajectory/feedback)+ top-k 语义检索注入;防退化:成功判定独立于生成模型(evaluator/代码执行)、失败降权失效、每50事件清理低效经验
- **实施清单**:8 步(事件表→签名→路由包装→记录→反思→检索注入→bandit→防退化),预计 2-3 开发日
- 路由反馈=contextual bandit:reward = 质量−权重×成本−权重×延迟;task_signature 做 context


### 会话 7 · 落地实施探讨(2026-08-11)

**【远程 Claude 实施建议】**
- **先做 D**(~8h 可验证标准化远程工具入口,为 B/C 提供基础);**A 随后并行设计**(2-3 开发日)
- **D 补充风险**:严格校验 exp/aud/jti、密钥轮换、防重放;确认工具真正只读,增加限流、审计脱敏及来源校验
- **A 补充风险**:事件签名需规范化且幂等;bandit 冷启动设安全基线;经验注入须防提示注入和隐私泄露

**【双方最终共识·落地路径】**
D(MCP server 暴露,8h)→ A(经验记忆+路由,2-3天)→ B(MCP 安全代理)→ C(HTTP-relay+A2A 跨设备)
先做 D 因为:验证标准化远程入口、为 B/C 打基础、投入小见效快


### 会话 8 · 第三轮深入:C(HTTP-relay连接+A2A委派)成果(2026-08-11)

**连接层选择矩阵**
- **CMDOP**(最快):每机出站拨号 relay,免SSH长连接,fleet MCP 工具(list_machines/run_command),free 3 机,relay 可自托管
- **Tailscale**(自控):WireGuard 私有网格,P2P+DERP,免费 100 设备,需自补 MCP Streamable HTTP 暴露
- Claude remote-control:只适合"人从云 UI 遥控 NAS",不适合机器间委派
- 结论:MVP 选 CMDOP 或 Tailscale,与委派层解耦

**委派层(A2A)**
- 官方 `a2a-sdk`(Python)已成熟:Agent Card + `AgentExecutor.execute()` + `TaskUpdater` 状态机 + SSE(`A2AStarletteApplication` 挂 uvicorn)
- **metano 已有 `sub_agent.AgentDelegator`**(spawn claude -p,并行3,超时120s,Task状态机)——正是 A2A AgentExecutor 的现成包装,只需映射状态
- A2A×Claude 先例:claude-a2a、a2a-mcp-bridge(MCP 工具暴露 message/send/stream/tasks)

**metano 接入点(本地核验)**
1. FastAPI 9120 已跑 → A2A server 挂新增路由(`/.well-known/agent-card.json` + /a2a + /a2a/sse),复用 auth.py JWT
2. FastMCP('metano') stdio → 加 a2a_submit/a2a_poll/a2a_cancel 工具,本机 Claude 零配置
3. sub_agent.AgentDelegator 直接包成 A2A executor

**本机↔远程协同 MVP(5步)**:定连接层(CMDOP/Tailscale)→ NAS 起 A2A server → 本机 FastMCP 加 3 个 A2A 工具 → 安全加固(acceptEdits/专用目录/不暴露公网)→ 验证链
- **推荐**:连接层 CMDOP 或 Tailscale + 工具层 NAS 侧 MCP Streamable HTTP + 委派层 a2a-sdk 包 AgentDelegator,侵入最小、官方库覆盖最高


### 会话 9 · 第三轮深入:B(MCP 安全网关代理)成果(2026-08-11)

**攻击面**:工具投毒(描述注入/rug pull)、confused deputy + 工具遮蔽、30+ 公开 CVE(工具 handler 把 LLM 参数 concat 进 exec)、供应链(MCP04)
**关键约束**:metano 装 mcp==1.27.1,**无 FastMCP 官方 auth 中间件**(无 JWTVerifier/create_streamable_http_app)→ 守卫必须做在 FastAPI 层,不依赖 FastMCP auth API
**工具风险分级(4 档,MVP 心脏)**:
- READ(远端开放 ~15):session/analytics/cron_list/skills/model/memory/knowledge/evolution 查询类
- WRITE(仅高 scope):memory_add/knowledge_ingest/curator_report 等
- DESTRUCTIVE(仅本地 stdio 禁止远端):code_run/browser_evaluate/cron_trigger/agent_spawn/home_control/evolution_run 等
**B MVP = D 方案 + 统一网关守卫**:
- `mcp_policy.py`(白名单/风险分级/参数schema/描述哈希)+ `mcp_http.py`(远端只读子集)+ `mcp_gateway.py`(FastAPI 守卫:验JWT aud/jti防重放/白名单/审计/限流)+ `/api/mcp/token`(1h只读token)
- 复用 security.py/auth.py/paths.py/db.py;不动现有 stdio 本地工具集
- 9 步实施清单(分级→子集→守卫→token→参数校验→审计限流→指纹→验证→加固),~9h
- 参考:mcp-security-gateway、Cloudflare WriteGuard、OWASP MCP Top 10、MCP Authorization Spec(2026-07-28)


---

## 探索总结(截至本会话)

**双方协同探索覆盖**:4 大方向(A2A/MCP、开源生态、远程管理、自进化/记忆)+ 4 个高可行性方案全部深入完成。

**落地路径(双方共识)**:
| 顺序 | 方案 | 内容 | 估算工时 |
|------|------|------|---------|
| 1 | **D** | metano 暴露 MCP server(streamable-http 受控只读) | ~8h |
| 2 | **A** | 经验记忆 + 路由反馈(route_events+bandit+经验注入) | 2-3 天 |
| 3 | **B** | MCP 安全网关守卫(白名单/aud/审计/限流) | ~9h |
| 4 | **C** | 本机↔远程协同(CMDOP/Tailscale + a2a-sdk) | 后续 |

**关键复用发现(大幅降成本)**:metano 已有 60+ 工具 stdio MCP server、strategy.py 反馈闭环、sub_agent.AgentDelegator、security.py/auth.py——四方案都基于现有代码增量,不从零造轮子。

**风险要点**:D(密钥轮换/真只读)、A(签名幂等/提示注入)、B(mcp==1.27.1 无 auth API 需 FastAPI 守卫)、C(权限边界/不暴露公网)。

**好主意清单(探索途中双方探讨的高可行性方案)**:见上方各会话记录 + 飞书两轮汇报。

