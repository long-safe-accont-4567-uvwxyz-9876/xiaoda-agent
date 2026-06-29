# 纳西妲 Agent Web UI 完整设计文档（WEBUI_DESIGN.md）

> 版本：v1.0（2026-06-11）
> 性质：**实施级设计文档**。本文档面向执行实现的工程师/模型，所有接口、文件路径、字段名均为最终约定，实现时不得擅自改名。
> 核心铁律：**UI 上出现的每一个控件，必须对应一个真实存在的后端调用，并产生可观察的实际效果。禁止任何"展示用假数据"或"点击无效"的占位功能。** 每个功能的「UI 控件 → API → 后端模块 → 可观察效果」四元组在 §15 验收清单中逐条列出，验收时逐条核对。

---

## 0. 目录

1. 项目概述与目标
2. 现状盘点：后端已有能力 → UI 功能映射
3. 总体架构
4. API 网关（FastAPI）完整接口设计
5. 后端新增/改造模块清单
6. 前端工程设计（技术栈、目录、构建）
7. 视觉与交互设计（背景板、草元素特效、3D 动效）
8. 页面与侧边栏逐项规格
9. WebSocket 实时协议
10. 鉴权与安全
11. 定时任务与主动问候系统
12. TTS 模块与视频生成模块
13. 健康测试中心（LLM / TTS / 视频自检）
14. 数据库变更
15. 验收清单（防"徒有其表"专用）
16. 实施阶段计划（Milestones）
17. 附录：环境变量、配置文件示例

---

## 1. 项目概述与目标

为现有的小妲 AI Agent（`/home/orangepi/ai-agent/`，Python 3.11，运行于 Orange Pi ARM64）构建一套**生产级 Web UI + HTTP/WebSocket API 网关**，替代现有的极简 Streamlit 页面（`web/app.py`，实现后废弃）。

### 1.1 必须实现的用户需求（原文需求逐条编号，后文以 R1~R14 引用）

| 编号 | 需求 |
|------|------|
| R1 | 以指定图片为背景板（`webui_background.jpg`，1280×800 纳西妲主题图） |
| R2 | 界面有灵动的草元素特效与原神须弥特色 |
| R3 | 交互具有动态 3D 转换效果 |
| R4 | 可扩展模型 / 自定义模型（增删改 provider 与模型条目） |
| R5 | 可增加子 Agent，UI 中自由切换对话对象 |
| R6 | 可扩展 Skills（工具）与 MCP 服务；每个 Agent 都能及时获得对应 Skill/MCP 的调用权限 |
| R7 | 用户可对每个 Agent 单独开关任意 Skill / MCP（权限矩阵），改动即时生效 |
| R8 | 侧边栏选项详细具体，能调控 Agent 每一项配置，且对项目产生切实影响 |
| R9 | 可查看内部状态：情绪情感、对用户的认知（画像）、当天发生的事件 |
| R10 | 定时任务：自定义问候时间点、随机时间问候、免打扰时段 |
| R11 | TTS 模块、视频生成模块 |
| R12 | 测试功能：测试当前 LLM / TTS / 视频生成模型是否可用 |
| R13 | 模型接入同时支持 **OpenAI 兼容格式** 与 **Anthropic 兼容格式**（用户原文"Asrapic"即 Anthropic） |
| R14 | 支持斜杠命令；美术质量在线 |

### 1.2 设计者补充的需求（R15~R24，提升整体体验，同样必须实现）

| 编号 | 补充需求 | 理由 |
|------|---------|------|
| R15 | **流式输出**（SSE/WS token 级流式 + 工具调用过程可视化） | Agent 单轮可能执行 15 次工具迭代，无流式则用户面对 30s 白屏 |
| R16 | **会话管理**：多会话、历史回看、会话内搜索、导出 Markdown | 后端已有 conversation_logs 表，只缺 UI |
| R17 | **记忆管理器**：浏览/搜索/置顶/删除情景记忆、知识图谱可视化 | 后端已有三层记忆系统（db_memory / vector_store / knowledge_graph） |
| R18 | **仪表盘**：API 成本、token 用量、工具调用统计、系统资源（Orange Pi 内存/CPU/温度） | 后端已有 metrics.py 与 api_usage 表 |
| R19 | **审计日志页**：工具执行记录、危险操作记录 | 后端已有 audit_logs 表，安全必需 |
| R20 | **移动端响应式**：手机访问时 3D/粒子降级，布局单栏化 | 用户会用手机访问家中 Orange Pi |
| R21 | **降级开关**：低性能设备自动关闭粒子/3D（prefers-reduced-motion + FPS 探测） | Orange Pi 本机浏览器/老手机性能有限 |
| R22 | **配置热更新**：所有 UI 改的配置写入 `config/webui_overrides.json5`，不重启进程生效 | 否则"侧边栏调控"会变成"改了要重启"的假功能 |
| R23 | **凭证管理 UI**：API Key 增删（仅写入，回显打码），凭证池状态查看 | 后端已有 credential_pool.py 多 Key 轮换 |
| R24 | **表情包/贴纸联动**：回复中的 emotion 驱动前端立绘表情与贴纸展示 | 后端 ProcessResult 已返回 emotion + sticker_path，不用白不用 |

### 1.3 非目标（明确不做）

- 不做多租户/注册系统（单用户"爸爸"+ 密码/Token 即可）
- 不重写 AgentCore 核心逻辑（网关只是**调用方**）
- 不在 Orange Pi 上跑 Node 服务（前端构建产物为纯静态文件，由 FastAPI 托管）
- 不引入 Redis/Postgres 等新基础设施（沿用 SQLite + JSON5 配置）

---

## 2. 现状盘点：后端已有能力 → UI 功能映射

实现前必读。**下表"后端现状"列出的模块全部真实存在**，UI 功能必须接到这些模块上，而不是另起炉灶。

| UI 功能 | 后端现状（文件 / 类 / 方法） | 缺口（需新增） |
|---------|------------------------------|----------------|
| 对话 | `agent_core.py` `AgentCore.process()` → `ProcessResult(reply, emotion, sticker_path, audio_path, image_paths, video_path, tool_results)` | HTTP/WS 封装、流式回调 |
| 子 Agent 切换 | `agent_dispatcher.py` `SubAgentConfig`（keli可莉/yinlang银狼/xilian昔涟/nike尼可）、`/agent` 命令 | Agent 的 CRUD 持久化（现为代码内定义） |
| 模型配置 | `model_router.py` `ROUTE_TABLE` + `FALLBACK_ROUTE`、`config.py`、`.env`、`credential_pool.py` | 运行时改路由表、**Anthropic provider**（现仅 AsyncOpenAI） |
| Skills（工具） | `tool_registry.py` `register_tool()` / `to_openai_tools()`、`tool_executor.py`、`permission_manager.py`（DEFAULT/DEV/STRICT/BYPASS）、`agent_dispatcher.py` `excluded_tools` + `DELEGATE_BLOCKED_TOOLS` | per-agent 开关矩阵的持久化与热生效 |
| MCP | `mcp_client.py` `MCPClient`（stdio）/ `MCPManager`，自动注册到 tool_registry | MCP 服务器的 UI 级 CRUD、启停、per-agent 授权 |
| 斜杠命令 | `slash_commands.py` `SlashCommandHandler.handle()`，17 条命令（/cost /status /model /agent /memory /emotion /knowledge /voice /note /learn …），`OWNER_ONLY_COMMANDS` 权限 | 命令列表查询 API（供前端自动补全） |
| 情绪 | `emotion_simple.py` `detect_emotion()`、`emotion_enum.py` 9 类情绪 | 历史情绪曲线查询 API（数据在 conversation_logs.emotion_label） |
| 用户画像（认知） | `portrait_manager.py` `consolidate_portrait()`、`user_portrait` 表（content/version/change_log） | 读取/手动触发整合的 API |
| 当天事件 | `episodic_memories` 表、`agent_events` 表、`notebooks` 表（INSIGHT/TASK） | 按日聚合查询 API |
| 定时问候/免打扰 | `nudge_engine.py` `NudgeEngine`（greeting_threshold、greeting_max_per_day、dnd_start/dnd_end、`_is_dnd()`、`poke()`、`_tick()` 60s 主循环） | **固定时间点问候、随机时间问候**（现仅"闲置触发"）、配置热更新、Web 通道推送 |
| TTS | `tts_engine.py` `TTSEngine.synthesize(text, voice, style)`，音色 nahida/keli，13 种情绪风格 | HTTP 音频文件服务、试听 API |
| 视频/图片生成 | `tools/agnes_tools.py` `agnes_image_generate` / `agnes_video_generate` | 独立生成页 + 任务队列（视频生成耗时长，需异步任务） |
| 健康测试 | `healthcheck.sh`、`smart_error_handler.py`、`metrics.py`、`error_classifier.py` | LLM/TTS/视频 在线探活 API |
| 会话 | `session_store.py` `SessionInfo` + `fold_session_summary()`、`conversation_logs` 表 | 会话列表/历史分页 API |
| 成本 | `api_usage` 表、`/cost` 命令 | 聚合统计 API |
| 审计 | `audit_logs` 表、`hooks.py` `HookEngine` | 查询 API |

---

## 3. 总体架构

```
┌──────────────────────────── 浏览器 ────────────────────────────┐
│  Vue 3 SPA（静态文件，Vite 构建产物）                            │
│  · Pinia 状态管理  · WebSocket 客户端  · 草元素粒子层(canvas)    │
│  · 3D 卡片交互(CSS transform3d)  · 纳西妲立绘情绪层              │
└───────────────┬───────────────────────────┬────────────────────┘
        REST (HTTPS/JSON)            WebSocket /ws
┌───────────────┴───────────────────────────┴────────────────────┐
│  web/server.py — FastAPI 网关（新增，本文档核心交付物之一）        │
│  · routers/: chat, agents, models, tools, mcp, schedule,        │
│    memory, insight, media, health, sessions, system, auth       │
│  · ws_hub.py: 连接管理 + 事件广播（问候推送、任务进度）           │
│  · config_service.py: webui_overrides.json5 热更新               │
│  · 静态托管: web/dist/ (SPA) + /media/* (音频/图片/视频产物)      │
└───────────────┬─────────────────────────────────────────────────┘
                │ 进程内直接调用（同一 asyncio loop，无 RPC）
┌───────────────┴─────────────────────────────────────────────────┐
│  现有 AgentCore 体系（不重写，只挂接）                            │
│  AgentCore.process() ── AgentDispatcher ── ModelRouter           │
│  ToolRegistry/Executor ── MCPManager ── NudgeEngine              │
│  MemoryManager ── PortraitManager ── TTSEngine ── AgnesTools     │
│  DatabaseManager(SQLite: data/agent.db)                          │
└──────────────────────────────────────────────────────────────────┘
```

**关键决策：**

1. **进程内集成，不是独立服务。** FastAPI 与 AgentCore 跑在同一个 asyncio 事件循环里（与 `qq_bot_adapter.py` 共存）。网关在 `lifespan` 中复用已初始化的 `AgentCore` 单例。这样 Web 改的配置（如关掉某 Agent 的某工具）对 QQ Bot 通道**同时生效**——这正是 R7"及时获取权限"的实现基础。
2. **启动方式**：`scripts/start.sh` 增加分支。`python agent.py --web` 或环境变量 `WEB_UI_ENABLED=true` 时，在主循环中 `uvicorn.Server(config).serve()` 作为 task 启动，端口取 `agent.json5` 的 `gateway.port`（现为 8080）。
3. **前端构建在开发机完成**，产物 `web/dist/` 提交到仓库（Orange Pi 不装 Node）。
4. **所有 UI 可改配置走 `ConfigService`**（§5.1），落盘 `config/webui_overrides.json5`，并立即应用到内存对象，重启后亦能恢复。

---

## 4. API 网关完整接口设计

新增包：`web/server.py`（入口）+ `web/routers/*.py` + `web/schemas.py`（Pydantic 模型）。所有接口前缀 `/api/v1`，返回统一信封：

```json
{ "ok": true, "data": { ... } }
{ "ok": false, "error": { "code": "TOOL_NOT_FOUND", "message": "..." } }
```

鉴权：除 `/auth/login` 与静态文件外，所有接口要求 `Authorization: Bearer <token>`（§10）。

### 4.1 auth（web/routers/auth.py）

| 方法 | 路径 | 说明 | 对接 |
|------|------|------|------|
| POST | `/auth/login` | body `{password}` → `{token, expires_at}` | 校验 `WEBUI_PASSWORD`，签发 HMAC token |
| POST | `/auth/logout` | 失效 token | 内存 token 表 |

### 4.2 chat & sessions（web/routers/chat.py, sessions.py）

| 方法 | 路径 | 说明 | 对接后端 |
|------|------|------|---------|
| POST | `/chat` | 非流式兜底。body `{session_id, agent, text}` → `ProcessResult` 序列化 | `AgentCore.process(user_input, user_id="webui", source="web")` |
| WS | `/ws` | 主通道，协议见 §9（流式回复、工具过程、问候推送） | 同上 + `status_callback` 回调挂接 |
| GET | `/sessions` | 会话列表（分页） | `session_store` + `conversation_logs` 按 session_id 聚合 |
| POST | `/sessions` | 新建会话 → `{session_id}` | `session_store` |
| GET | `/sessions/{id}/messages?before=&limit=50` | 历史消息分页 | `conversation_logs` 表（含 emotion_label、时间戳） |
| DELETE | `/sessions/{id}` | 删除会话及其日志 | DELETE conversation_logs WHERE session_id |
| GET | `/sessions/{id}/export` | 导出 Markdown 文件 | 服务端拼接 |
| GET | `/commands` | 斜杠命令清单 `{name, description, owner_only}[]`，供前端 `/` 自动补全 | 从 `SlashCommandHandler` 的 handlers 表反射（需在该类上补一个 `list_commands()` 方法，§5.6） |

**说明**：聊天输入若以 `/` 开头，前端原样发送，后端 `AgentCore.process()` 内部已会路由到 `SlashCommandHandler.handle()` —— 斜杠命令无须单独接口（R14 零成本达成）。

### 4.3 agents（web/routers/agents.py）—— R5/R7/R8 核心

| 方法 | 路径 | 说明 | 对接后端 |
|------|------|------|---------|
| GET | `/agents` | 所有 Agent 完整配置列表（含主体"纳西妲"+ 4 子代理 + 用户自建） | `AgentRegistry.list()`（§5.2） |
| POST | `/agents` | 新建子 Agent，body 即 `SubAgentConfig` 全字段（name/display_name/provider/model/base_url/api_key_env/personality_text/voice_ref/excluded_tools/mcp_servers/capabilities/route_description/max_turns/effort/permission_mode/memory_scope） | `AgentRegistry.create()` → 写 `config/agents/<name>.json5` + 人格写 `config/agents/<name>_personality.md` → 热注册到 `AgentDispatcher` |
| PUT | `/agents/{name}` | 修改任意字段，**即时生效**（下一条消息即用新配置） | `AgentRegistry.update()` |
| DELETE | `/agents/{name}` | 删除用户自建 Agent（内置 4 个 + 主体只可禁用不可删） | `AgentRegistry.delete()` |
| POST | `/agents/{name}/enable` `/disable` | 启停 | 同上 |
| GET | `/agents/{name}/permissions` | 该 Agent 的 Skill/MCP 权限矩阵：`{tools: {web_search: true, shell_command: false, ...}, mcp_servers: {...}}` | `AgentRegistry.get_permissions()`：tool_registry 全集 − excluded_tools − DELEGATE_BLOCKED_TOOLS |
| PUT | `/agents/{name}/permissions` | 批量改权限矩阵（R7 的核心写接口） | 更新 `excluded_tools`/`mcp_servers` → 持久化 → **当场重建该 SubAgent 的工具白名单**，无须重启 |
| GET | `/agents/{name}/personality` / PUT | 读/写人格 Markdown 全文（UI 内置 Markdown 编辑器） | 读写对应 `*_personality.md`，PUT 后调用 `SubAgent.init()` 重载 |
| POST | `/agents/{name}/test` | 对该 Agent 发一条固定测试语句，返回耗时/回复摘要 | `AgentDispatcher` 单代理委托 |

**当前会话 Agent 切换**（R5）：会话级状态。WS 消息 `{type:"set_agent", agent:"yinlang"}`，网关记录到该 WS 会话上下文；后续 `chat` 消息带 `agent` 字段直达 `AgentCore._dispatch_single_sub_agent()`；`agent:"nahida"` 表示回到主体不委托。前端顶栏 Agent 头像即此开关。

### 4.4 models（web/routers/models.py）—— R4/R13 核心

| 方法 | 路径 | 说明 | 对接后端 |
|------|------|------|---------|
| GET | `/models/providers` | provider 列表：内置 mimo/mimo-pro/agnes + 用户自建。字段 `{id, label, format: "openai"\|"anthropic", base_url, api_key_env, key_masked, enabled}` | `ConfigService` + `credential_pool` |
| POST | `/models/providers` | 新增自定义 provider，**format 字段二选一：`openai`（走 AsyncOpenAI + base_url）或 `anthropic`（走 AsyncAnthropic + base_url，§5.3 新增）** | 写 overrides → `ModelRouter` 注册新 client |
| PUT/DELETE | `/models/providers/{id}` | 改/删 | 同上 |
| POST | `/models/providers/{id}/key` | 写入 API Key（明文只进 `credentials/` 目录文件与进程环境，响应与 GET 永远打码 `sk-***abc`） | `credential_pool` |
| GET | `/models/routes` | 路由表（chat/chat_pro/chat_flash/chat_mini/emotion_analysis/memory_encoding/tool_result_wrap…）每项 `{model, provider, max_tokens, thinking, timeout}` + `FALLBACK_ROUTE` 降级链 | `ModelRouter.ROUTE_TABLE` 运行时副本 |
| PUT | `/models/routes/{task}` | 改某条路由（如把 chat 换成自定义模型），即时生效 | `ModelRouter.update_route()`（§5.3 新增方法） |
| GET | `/models/credentials/status` | 凭证池状态（每个 key: OK/EXHAUSTED/DEAD、最近错误） | `CredentialPool` 状态机 |
| GET | `/models/usage?days=7` | token/成本统计（按天、按模型分组） | `api_usage` 表聚合 |

### 4.5 tools / Skills（web/routers/tools.py）—— R6 核心

| 方法 | 路径 | 说明 | 对接后端 |
|------|------|------|---------|
| GET | `/tools` | 全部已注册工具：`{name, description, category, permission(RO/RW/E), max_frequency, requires_confirmation, source: "builtin"\|"mcp:<server>", enabled_global}` | `tool_registry`（补 `list_tools_meta()`，§5.6） |
| PUT | `/tools/{name}` | 全局开关 / 改 max_frequency / 改 requires_confirmation | `ConfigService` + registry 热更新 |
| POST | `/tools/{name}/invoke` | **调试执行**：body 传 args，返回 ToolResult（owner 权限 + 二次确认头 `X-Confirm: yes`） | `ToolExecutor.execute()`（走完整 guardrails/审计） |
| GET | `/tools/{name}/stats` | 该工具调用次数/成功率/平均耗时 | `metrics` 快照 + audit_logs |
| GET | `/permission-mode` / PUT | 全局权限模式 DEFAULT/DEV/STRICT（**UI 禁止设 BYPASS**） | `permission_manager` |

### 4.6 mcp（web/routers/mcp.py）—— R6 核心

| 方法 | 路径 | 说明 | 对接后端 |
|------|------|------|---------|
| GET | `/mcp/servers` | 列表：`{name, command, args, env_keys, status: running/stopped/error, tool_names[], last_error}` | `MCPManager` |
| POST | `/mcp/servers` | 新增 MCP server（command + args + env），保存即尝试启动并发现工具 | 写 overrides → `MCPManager` 动态 `MCPClient.start()` → 工具自动入 registry |
| PUT/DELETE | `/mcp/servers/{name}` | 改/删（删前先 stop） | 同上 |
| POST | `/mcp/servers/{name}/start` `/stop` `/restart` | 生命周期控制 | `MCPClient.start()/stop()` |
| GET | `/mcp/servers/{name}/tools` | 该 server 发现的工具列表 | `MCPClient.tool_names` |

MCP 工具进入 registry 后自动出现在 §4.5 工具列表（source=`mcp:<server>`）与 §4.3 各 Agent 权限矩阵中 —— **这就是 R6"每个 agent 及时获取 MCP 调用权限"的闭环**：新增 server → 工具注册 → 权限矩阵默认按 Agent 的 `mcp_servers` 白名单决定开/关 → 用户可逐项覆盖。

### 4.7 insight：情绪/认知/事件（web/routers/insight.py）—— R9 核心

| 方法 | 路径 | 说明 | 对接后端 |
|------|------|------|---------|
| GET | `/insight/emotion/current` | 最近一条回复的情绪 `{primary, valence, intensity}` | 网关缓存最近 `ProcessResult.emotion` + `emotion_simple.detect_emotion` |
| GET | `/insight/emotion/history?days=7` | 情绪时间序列（折线图数据） | `conversation_logs.emotion_label` 按小时聚合 |
| GET | `/insight/portrait` | 用户画像全文 + version + change_log + 来源记忆 ID | `user_portrait` 表 |
| POST | `/insight/portrait/consolidate` | 手动触发画像整合（异步任务，进度走 WS） | `PortraitManager.consolidate_portrait()` |
| GET | `/insight/today` | 当天事件聚合：episodic_memories（当日）+ agent_events（当日）+ notebooks 新增 + 学习晋升 + 工具调用计数 | 各表按 `timestamp >= 今日0点` 查询 |
| GET | `/insight/memories?q=&page=&importance_min=` | 记忆浏览/语义搜索 | `MemoryManager.retrieve_memories()`（有 q 时走向量搜索）/ `db_memory.search_memories_by_importance` |
| DELETE | `/insight/memories/{id}` | 删除记忆（连带向量） | `db_memory.delete_memory_with_vector()` |
| GET | `/insight/knowledge/graph?entity=&depth=1` | 知识图谱子图 `{nodes[], edges[]}`（前端力导向图） | `db_knowledge.get_knowledge_relations()` |
| GET | `/insight/notebook` / POST / PUT / DELETE | 笔记 CRUD + 置顶 | `notebook_manager` / `db_notebook` |
| GET | `/insight/learnings` | 学习记录（novice/apprentice/master 等级） | `db_learning` |
| GET | `/insight/instincts` | 本能列表 | `instinct_manager` |

### 4.8 schedule（web/routers/schedule.py）—— R10 核心，详见 §11

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/schedule/config` | NudgeEngine 当前配置（含新字段） |
| PUT | `/schedule/config` | 改 greeting_threshold / greeting_max_per_day / dnd 时段 / enabled，热生效 |
| GET | `/schedule/greetings` | 问候计划列表（固定/随机） |
| POST | `/schedule/greetings` | 新增：`{type:"fixed", time:"08:30", days:[1..7], prompt_hint, channels:["web","qq"]}` 或 `{type:"random", window_start:"09:00", window_end:"22:00", count_per_day:2, ...}` |
| PUT/DELETE | `/schedule/greetings/{id}` | 改/删/启停 |
| GET | `/schedule/dnd` / PUT | 免打扰时段列表（支持多段，如 `[{"start":"23:00","end":"08:00"},{"start":"13:00","end":"14:00"}]`） |
| POST | `/schedule/test-greeting` | 立即生成并推送一条问候（验证链路） |
| GET | `/schedule/history?days=7` | 已发送问候记录 |

### 4.9 media：TTS / 图片 / 视频（web/routers/media.py）—— R11，详见 §12

| 方法 | 路径 | 说明 | 对接后端 |
|------|------|------|---------|
| POST | `/media/tts` | `{text, voice:"nahida"\|"keli", style}` → `{audio_url, duration}` | `TTSEngine.synthesize()`，产物拷入 `web/media/tts/`，由 `/media/*` 静态托管 |
| GET | `/media/tts/voices` | 音色列表 + 13 种情绪风格枚举 | `tts_engine` 的 VOICE/EMOTION_STYLE_MAP 常量反射 |
| PUT | `/media/tts/config` | 全局 TTS 开关、默认音色、自动朗读回复开关 | ConfigService |
| POST | `/media/image` | 文生图，异步任务 → `{task_id}` | `agnes_image_generate` 工具，经 MediaTaskQueue（§5.5） |
| POST | `/media/video` | 文生视频，异步任务 → `{task_id}` | `agnes_video_generate` |
| GET | `/media/tasks` / `/media/tasks/{id}` | 任务列表/状态（queued/running/done/failed + 进度 + 产物 URL），进度同时走 WS 推送 | MediaTaskQueue |
| GET | `/media/gallery?type=image\|video\|audio` | 历史产物画廊（分页缩略图） | 扫描 `web/media/` + media_tasks 表 |

### 4.10 health：测试中心（web/routers/health.py）—— R12，详见 §13

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/health/test/llm` | body `{route:"chat"}` 或 `{provider_id}`：发送固定探针 prompt，返回 `{ok, latency_ms, model, reply_excerpt, error}` |
| POST | `/health/test/tts` | 合成固定短句，返回 `{ok, latency_ms, audio_url, error}`（前端可直接播放验证） |
| POST | `/health/test/video` | 提交最小视频任务（或 provider 的 dry-run），返回 task_id 跟踪 |
| POST | `/health/test/mcp/{server}` | 对 MCP server 调 `tools/list` 探活 |
| POST | `/health/test/all` | 串行跑全部探针，WS 推送逐项进度 |
| GET | `/health/system` | Orange Pi 系统信息：CPU/内存/磁盘/温度（读 `/sys/class/thermal`）/uptime/进程内存 |
| GET | `/health/report` | 最近一次全量自检报告（持久化于 health_reports 表） |

### 4.11 system（web/routers/system.py）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/system/status` | 等价 `/status` 命令：uptime、QQ 连接状态、活跃会话数、版本 |
| GET | `/system/audit?type=&page=` | 审计日志分页（audit_logs 表） |
| GET | `/system/metrics` | `metrics.get_snapshot()` 全量指标 |
| GET | `/system/config` | 当前合并后配置（agent.json5 + overrides，密钥打码） |
| POST | `/system/restart` | 重启 agent 服务（systemd `qq-agent`，需二次确认） |
| GET | `/system/logs?lines=200&level=` | 尾部日志（loguru 文件） |

---

## 5. 后端新增/改造模块清单

> 全部为新文件或对现有文件的**增量**修改。改造原则：现有调用方（QQ Bot/CLI/ACP）行为不变。

### 5.1 `web/config_service.py` — ConfigService（新增，R8/R22 基石）

- 职责：管理 `config/webui_overrides.json5`。结构按域分节：`agents / models / tools / mcp / schedule / tts / ui`。
- API：`get(path, default)` / `set(path, value)` / `watch(path, callback)`。
- `set()` 流程：写内存 → `atomic_write.py` 原子落盘 → 触发该 path 注册的 callbacks（各模块借此热生效）。
- 启动时：`agent.json5`（基线） + `webui_overrides.json5`（覆盖）深合并后供全局读取。
- **密钥永不入此文件**，密钥只进 `credentials/` 目录（沿用现有约定）。

### 5.2 `web/agent_registry.py` — AgentRegistry（新增，R5/R7 基石）

- 启动时：把 `agent_dispatcher.py` 内置 4 个 `SubAgentConfig` 收编为"内置 Agent"，再加载 `config/agents/*.json5` 用户自建 Agent，统一注册进 `AgentDispatcher`。
- `create/update/delete/enable/disable`：操作 json5 文件 + 调用 dispatcher 的（需新增的）`register_sub_agent()/unregister_sub_agent()` 完成**运行时热插拔**。
- `get_permissions(name)` / `set_permissions(name, matrix)`：
  - 读：`set(tool_registry 全部工具) - excluded_tools - DELEGATE_BLOCKED_TOOLS` 得到有效集；MCP 维度按 `mcp_servers` 白名单。
  - 写：更新 config → 落盘 → 调 `SubAgent` 实例重建工具列表（在 `SubAgent` 上新增 `rebuild_tools()`）。**写完成即生效，下一次该 Agent 被委托时使用新权限**（R6/R7 的"及时"语义）。
  - `DELEGATE_BLOCKED_TOOLS` 中的工具在矩阵中显示为"系统锁定"，UI 不可开启（防递归委托等安全问题）。

### 5.3 `model_router.py` 改造（R4/R13）

1. 新增 **Anthropic 客户端支持**：`requirements.txt` 加 `anthropic>=0.40`。`ModelRouter` 的 client 工厂按 provider 的 `format` 字段实例化 `AsyncOpenAI` 或 `AsyncAnthropic`（支持自定义 `base_url`，即"Anthropic 兼容格式"）。`_route_impl` 内做消息格式适配：system 提出为顶层参数、tools schema 转换（OpenAI function ↔ Anthropic tool_use）、`max_tokens` 必填、流式事件归一化为内部统一 delta 事件。
2. 新增方法：`list_routes()` / `update_route(task, cfg)` / `register_provider(p)` / `remove_provider(id)`，全部带参数校验，更新后立即被后续 `route()` 使用。
3. 路由表初始化改为：代码默认值 ← overrides 覆盖（经 ConfigService）。
4. 流式：`route()` 增加可选 `on_delta: Callable[[str], Awaitable]` 回调参数（默认 None，老调用方零影响），网关用它实现 token 流式。

### 5.4 `nudge_engine.py` 改造（R10，详见 §11）

新增 GreetingScheduler 能力（固定时间点 / 随机窗口）、多段 DND、Web 通道推送、配置热更新。

### 5.5 `web/media_tasks.py` — MediaTaskQueue（新增，R11）

- SQLite 表 `media_tasks`（§14），asyncio 单 worker 串行执行（Orange Pi 资源有限，禁止并发跑视频生成）。
- 状态机 queued → running → done/failed；每次状态/进度变化广播 WS 事件 `media_task_update`。
- 产物落 `web/media/{tts,image,video}/`，按 `保留最近 N=200 个` 自动清理（cleanup_config 表加一行）。

### 5.6 小型增量（各现有文件）

| 文件 | 增量 |
|------|------|
| `slash_commands.py` | 加 `list_commands() -> list[dict]`（name/description/owner_only），从 handlers 表与 docstring 生成 |
| `tool_registry.py` | 加 `list_tools_meta()`（含 source 字段标记 MCP 来源）、`set_tool_enabled(name, bool)` 全局开关（执行器入口检查） |
| `agent_core.py` | `process()` 增加可选 `stream_callback`（转发 model delta 与工具事件给 WS）；`source="web"` 时把 `ProcessResult` 的媒体路径转为 `/media/` URL |
| `agent_dispatcher.py` | `register_sub_agent()/unregister_sub_agent()`、`SubAgent.rebuild_tools()` |
| `tts_engine.py` | `list_voices()` 反射常量；`get_status()` 已有，直接复用 |
| `database.py` | 新表建表语句（§14） |
| `scripts/start.sh` / `agent.py` | `--web` 启动分支，uvicorn 挂入主 loop |
| `hooks.py` | `fire_post_tool_use` 增加 WS 广播 hook（工具过程可视化） |

---

## 6. 前端工程设计

### 6.1 技术栈（为低算力实现者明确锁定，不得另选）

| 项 | 选择 | 理由 |
|----|------|------|
| 框架 | **Vue 3 + `<script setup>` + TypeScript** | 单人维护、模板直观，适合低阶模型批量生成组件 |
| 构建 | Vite 5 | 产物纯静态，丢进 `web/dist/` |
| 状态 | Pinia | 每个侧边栏域一个 store，与 §4 路由一一对应 |
| 路由 | vue-router（hash 模式，避免服务端 fallback 配置） |
| UI 基础 | **Naive UI**（按需引入）+ 自定义主题 token | 提供 Form/Table/Modal 等重组件，省工时；主题可深度定制成须弥风 |
| 图表 | ECharts（情绪曲线、成本、力导向知识图谱） |
| 粒子 | 自写 canvas 粒子层（≈200 行，§7.2），**不引入 three.js**（保 R21 性能） |
| Markdown | `markdown-it` + `highlight.js`（聊天气泡渲染） |
| 通信 | 原生 fetch 封装 `api.ts` + 原生 WebSocket 封装 `ws.ts`（自动重连、心跳） |

### 6.2 目录结构

```
web/
├── server.py  routers/  schemas.py  ws_hub.py
├── config_service.py  agent_registry.py  media_tasks.py
├── media/            # 运行期产物（gitignore）
├── dist/             # 构建产物（提交仓库）
└── frontend/         # 源码
    ├── index.html  vite.config.ts  package.json
    └── src/
        ├── main.ts  App.vue
        ├── api/        api.ts  ws.ts  types.ts      # types.ts 与 schemas.py 字段一一对应
        ├── stores/     auth.ts chat.ts agents.ts models.ts tools.ts
        │               mcp.ts insight.ts schedule.ts media.ts health.ts system.ts ui.ts
        ├── views/      ChatView.vue InsightView.vue MediaView.vue
        │               ScheduleView.vue HealthView.vue DashboardView.vue SettingsView.vue
        ├── components/
        │   ├── layout/   SideBar.vue TopBar.vue GlassPanel.vue
        │   ├── chat/     MessageList.vue MessageBubble.vue ChatInput.vue
        │   │             SlashPalette.vue ToolCallCard.vue AgentSwitcher.vue
        │   │             EmotionAvatar.vue AudioPlayer.vue
        │   ├── agents/   AgentList.vue AgentEditor.vue PermissionMatrix.vue PersonalityEditor.vue
        │   ├── models/   ProviderList.vue ProviderForm.vue RouteTable.vue CredentialStatus.vue
        │   ├── tools/    ToolList.vue ToolDebugDialog.vue
        │   ├── mcp/      McpServerList.vue McpServerForm.vue
        │   ├── insight/  EmotionChart.vue PortraitCard.vue TodayTimeline.vue
        │   │             MemoryBrowser.vue KnowledgeGraph.vue NotebookPanel.vue
        │   ├── schedule/ GreetingList.vue GreetingForm.vue DndEditor.vue
        │   ├── media/    TtsPanel.vue VideoGenPanel.vue ImageGenPanel.vue Gallery.vue TaskQueue.vue
        │   ├── health/   TestCenter.vue TestResultCard.vue SystemMonitor.vue
        │   └── fx/       GrassParticles.vue Tilt3D.vue LeafTransition.vue GlowOrb.vue
        └── styles/     theme.css  sumeru-tokens.css  animations.css
```

### 6.3 背景图资产处理（回应你之前关于 `<img src>` 路径的问题）

**不要用 `file:///home/orangepi/Desktop/...` 绝对路径** —— 浏览器出于安全策略禁止 http 页面引用本地 `file://` 资源，部署后也找不到该路径。正确做法（实现的第一步）：

```bash
mkdir -p web/frontend/public/assets
cp /home/orangepi/Desktop/webui_background.jpg web/frontend/public/assets/webui_background.jpg
```

构建后随 `dist/assets/` 一起由 FastAPI 托管，CSS 中以相对路径引用：

```css
/* styles/theme.css */
.app-bg {
  background-image:
    linear-gradient(rgba(8, 24, 16, .35), rgba(8, 24, 16, .55)),  /* 暗化叠层保证文字对比度 */
    url("/assets/webui_background.jpg");
  background-size: cover;
  background-position: center;
  background-attachment: fixed;
}
```

原图 1280×800 偏小，需生成 webp 版本并对 >1440px 屏幕启用 `image-rendering: auto` + 轻微 `backdrop-blur` 容器弱化放大失真。另存一份 320px 高斯模糊缩略图作为加载占位（LQIP）。

---

## 7. 视觉与交互设计（R1/R2/R3/R14）

### 7.1 设计语言「须弥·世界树」

- **色板**（`sumeru-tokens.css` CSS 变量）：
  - 主绿 `--dendro: #7fd650`、深林 `--forest: #1d3b2a`、智慧金 `--wisdom: #e8d5a3`、月白 `--moon: #f2f7ee`、警示用枫红 `--alert: #d96a5f`。
  - 暗色为基调（背景图偏暗绿），所有面板用**玻璃拟态**：`background: rgba(20,40,28,.45); backdrop-filter: blur(14px); border: 1px solid rgba(127,214,80,.18); border-radius: 16px;`（封装为 `GlassPanel.vue`，全站唯一面板容器）。
- **字体**：标题 `"Noto Serif SC"`（神性/典籍感），正文 `"Noto Sans SC"`，代码 `"JetBrains Mono"`。本地 woff2 打包，不走 CDN（Orange Pi 可能离线）。
- **图标**：Naive UI 自带 + 少量自绘 SVG（草元素四叶印、世界树枝、蒲公英种子）。

### 7.2 草元素特效（`fx/GrassParticles.vue`）

全屏 `position:fixed; pointer-events:none; z-index:1` 的 `<canvas>`，自写粒子系统：

- **常驻层**：30~60 个"草叶光萤"粒子（2~5px 圆点 + 6~12px 叶形贝塞尔路径精灵，`--dendro` 色 60% 透明 + 外发光），以柏林噪声风场缓慢漂浮，呼吸式明暗（sin 周期 3~6s 随机）。
- **交互层**：
  - 鼠标移动：指针 80px 范围内粒子受轻微斥力，划过留下 0.6s 衰减的淡绿轨迹。
  - 发送消息：从输入框位置爆出 8~12 片叶子粒子飞向消息气泡（`LeafTransition.vue` 同款贝塞尔飞行）。
  - 收到回复：纳西妲头像处绽放一圈"草环"波纹（CSS `@keyframes` 扩散圆 + canvas 撒 6 颗种子粒子）。
  - 问候推送到达：屏幕右上飘落 3 秒蒲公英种子雨。
- **性能守则（R21，必须实现）**：`requestAnimationFrame` 节流至 30fps；页面 `visibilitychange` 隐藏时暂停；启动时跑 2s FPS 探测，均值 <24fps 则自动把粒子数降到 12 个并关闭轨迹；`prefers-reduced-motion: reduce` 或设置页"轻量模式"开关打开时整层不挂载。粒子状态存 `ui` store，设置页可调密度（关/低/中/高）。

### 7.3 动态 3D 转换（`fx/Tilt3D.vue`，R3）

不用 WebGL，用 **CSS `transform-style: preserve-3d` + `perspective`**，封装为指令式组件包裹器：

- `Tilt3D` 包裹任意卡片：监听 `pointermove`，按指针相对中心位置计算 `rotateX(±6deg) rotateY(±8deg) translateZ(8px)`，配 `transition: transform .15s ease-out`；离开时回弹（带 1 次轻微过冲的 `cubic-bezier(.34,1.56,.64,1)`）。内部子元素可声明 `data-depth="20"` 获得视差 `translateZ`。
- **应用位置**（统一克制，仅以下五处，避免廉价感）：
  1. 登录卡片（首屏第一印象）；
  2. Agent 切换器的角色卡（头像 + 名字 + 元素色边，hover 翻起）；
  3. Dashboard 的统计卡；
  4. 画廊媒体卡；
  5. 测试中心的结果卡（测试通过时 3D 翻转出绿色对勾面 —— `rotateY(180deg)` 双面卡）。
- **页面切换**：vue-router 过渡用"叶片翻页"——旧视图 `rotateY(-12deg) + opacity 0 + translateX(-40px)`，新视图反向进入，350ms。移动端与轻量模式降级为纯淡入淡出。

### 7.4 纳西妲情绪立绘（`chat/EmotionAvatar.vue`，R24）

聊天区右上角固定一个 96px 圆形头像容器，根据最近回复的 `emotion`（9 类：喜悦/悲伤/愤怒/焦虑/害羞/好奇/思考/恐惧/中性）切换：

- 资产：`public/assets/emotions/{joy,sad,angry,anxious,shy,curious,thinking,fear,neutral}.png`（若立绘资产暂缺，先用同一头像 + 不同颜色光环 + emoji 角标占位，**但切换逻辑必须真实接 emotion 字段**，资产后补不算假功能）。
- 切换动画：旧图缩小淡出、新图带草环波纹弹入（200ms）。
- 头像 hover 显示 tooltip：`当前情绪：喜悦 (valence 0.8)`，点击跳转 Insight 页情绪曲线。
- 后端若返回 `sticker_path`，气泡尾部追加贴纸图。

---

## 8. 页面与侧边栏逐项规格（R8 核心）

### 8.1 布局骨架

```
┌──┬─────────────────────────────────────────────┐
│  │ TopBar: 会话标题 | AgentSwitcher | 情绪头像 | 连接状态● │
│侧 ├─────────────────────────────────────────────┤
│边 │                                             │
│栏 │              主视图（router-view）            │
│64 │                                             │
│px ├─────────────────────────────────────────────┤
│   │ ChatView 时: ChatInput（含 / 命令面板）        │
└──┴─────────────────────────────────────────────┘
```

侧边栏（`SideBar.vue`）：64px 图标栏，hover/点击展开为 280px 抽屉（展开动画带 3D 轻微开门效果 `rotateY(4deg)→0`）。条目自上而下：

| 图标 | 条目 | 路由 | 对应 store |
|------|------|------|-----------|
| 💬 | 对话（会话列表抽屉） | `/chat` | chat |
| 🧚 | Agent 管理 | `/settings/agents` | agents |
| 🧠 | 模型与凭证 | `/settings/models` | models |
| 🛠 | Skills 工具 | `/settings/tools` | tools |
| 🔌 | MCP 服务 | `/settings/mcp` | mcp |
| 🌱 | 内在世界（情绪/画像/记忆/事件） | `/insight` | insight |
| ⏰ | 定时与问候 | `/schedule` | schedule |
| 🎙 | 媒体工坊（TTS/图/视频） | `/media` | media |
| 🩺 | 测试中心 | `/health` | health |
| 📊 | 仪表盘 | `/dashboard` | system |
| ⚙ | 系统设置 | `/settings/system` | ui/system |

**通用交互规范（每个设置项必须遵守）**：改动 → 立即 PUT → 成功后绿色草叶 toast「已生效 ✓」+ 受影响对象旁的状态点闪烁一次；失败 → 红 toast 带后端 error.message 并回滚 UI 值。**所有开关/滑杆/输入均为受控组件，初始值来自 GET，绝不硬编码。**

### 8.2 ChatView（对话页）

- **MessageList**：虚拟滚动；气泡区分 user/assistant/system；assistant 气泡支持 Markdown + 代码高亮 + 一键复制；流式时尾部光标呼吸闪烁；附带 emotion 角标、TTS 播放按钮（点击即调 `/media/tts` 朗读该条，缓存 audio_url）、`audio_path`/`image_paths`/`video_path` 内嵌播放器或图片预览。
- **ToolCallCard**：流式过程中出现的工具调用折叠卡——`🛠 web_search("纳西妲") · 1.2s · ✓`，点击展开入参/出参 JSON（被截断时提示）。数据来自 WS `tool_event`。
- **ChatInput**：多行自适应；`Enter` 发送 / `Shift+Enter` 换行；输入 `/` 弹出 **SlashPalette**（数据来自 GET `/commands`，模糊过滤，↑↓ 选择，owner_only 命令标 👑 且非 owner 置灰）；支持图片粘贴上传（POST 临时文件 → 消息携带，后端走 vision 流程，**Phase 4 实现，之前禁用该入口而非假装可用**）。
- **AgentSwitcher**（顶栏）：横排角色小卡（纳西妲/可莉/银狼/昔涟/尼可/自建…），点击切换当前会话受话 Agent（WS `set_agent`），选中卡片 3D 抬起 + 元素色描边；切换后系统气泡提示「现在由 银狼 接管对话」。每张卡 hover 显示该 Agent 的 model 与可用工具数（来自 agents store）。
- **会话抽屉**：会话列表（标题=首条消息截断，可重命名）、新建、删除（确认）、导出 Markdown、关键字过滤。

### 8.3 Agent 管理页

- **AgentList**：卡片网格（Tilt3D），每卡显示 display_name、模型、provider、启用开关、工具数/MCP 数徽章、内置/自建标签。点击进入编辑。
- **AgentEditor** 表单字段（与 §4.3 POST body 完全一致，逐项渲染）：display_name、provider（下拉，选项来自 models store）、model（文本+下拉建议）、base_url、api_key_env（下拉自 credentials 列表）、route_description（自然语言路由描述，textarea）、capabilities（tag 输入）、voice_ref（下拉自 TTS voices）、max_turns、effort（low/medium/high segmented）、permission_mode、memory_scope（shared/isolated）、background 开关。每个字段带 ❓tooltip 用一句话解释其后端含义（文案见 schemas.py 的 field description，前后端共用）。
- **PermissionMatrix**（R7 核心组件）：表格，行=全部工具（按 category 分组：memory/web/file/code/document/hardware/system/vision/media/mcp:*），列=该 Agent；单元格为开关。特性：
  - 系统锁定项（DELEGATE_BLOCKED_TOOLS）显示 🔒 disabled + tooltip 说明原因；
  - 行内显示工具 permission 等级色点（RO 绿 / RW 黄 / E 红）；
  - 顶部「按分组全开/全关」「复制另一 Agent 的配置」；
  - 改动批量暂存，底部「应用」一次 PUT；应用后 toast 显示「银狼 现在拥有 23 个工具」。
  - MCP 区段：按 server 整组开关 + server 内逐工具微调。
- **PersonalityEditor**：左 Markdown 编辑 / 右实时预览，保存即 PUT personality，顶部「测试此人格」按钮调 `/agents/{name}/test` 并弹出回复预览。

### 8.4 模型与凭证页

- **ProviderList**：内置（mimo/agnes，可改 base_url 不可删）+ 自建。每条显示 format 徽章（`OpenAI 兼容` 蓝 / `Anthropic 兼容` 橙）、base_url、key 打码、enabled 开关、「测试」按钮（直连 §13 LLM 探针）。
- **ProviderForm**：id、label、format（**单选：openai / anthropic**，R13）、base_url、API Key（password 输入，仅提交不回显）、默认 model 名。保存即注册，列表实时刷新。
- **RouteTable**：表格行=任务路由（chat/chat_pro/chat_flash/chat_mini/chat_agnes/emotion_analysis/memory_encoding/tool_result_wrap），列=model（可改）、provider（可改）、max_tokens（数字）、thinking 开关、timeout。行尾「测试此路由」。底部展示降级链可视化：`chat_pro → chat_flash → chat_mini → chat_agnes`（箭头流程图，节点亮灯=健康，数据来自最近探针结果）。
- **CredentialStatus**：凭证池表——key 序号、打码值、状态灯（OK 绿/EXHAUSTED 黄/DEAD 红）、最近错误、最近使用时间；「新增 Key」走 §4.4 key 接口。
- **用量卡**：ECharts 柱状图，按天 × 模型的 token 与估算成本（GET `/models/usage`）。

### 8.5 Skills 工具页 / MCP 页

- **ToolList**：搜索框 + category 筛选 + source 筛选（builtin/mcp）。每行：名称、描述、权限色点、来源、全局开关、max_frequency（行内可编辑数字）、requires_confirmation 开关、调用统计 sparkline、「调试」按钮。
- **ToolDebugDialog**：按工具 schema 自动生成参数表单（string→input、number→数字框、enum→下拉、object→JSON 编辑器），执行后展示 ToolResult（success/data/error）与耗时；执行前红色警示条「将真实执行，操作会被审计」。
- **McpServerList**：每条 server 卡片——名称、command + args（代码字体）、状态灯（running/stopped/error + last_error）、发现的工具 chips、操作（启/停/重启/编辑/删）。新增表单（McpServerForm）：name、command、args（逐行）、env（key-value 对，value password 处理）；「保存并启动」后实时显示握手→tools/list 的进度日志。

### 8.6 内在世界 InsightView（R9 核心）

四个 tab：

1. **情绪**：当前情绪大卡（立绘 + primary + valence/intensity 仪表盘）+ 7 天情绪折线/河流图（ECharts，9 类情绪堆叠）+ 今日情绪分布饼图。
2. **认知（用户画像）**：画像全文卡（Markdown 渲染）、version 与更新时间、change_log 时间线（每版变了什么）、「🔄 立即整合画像」按钮（POST consolidate，按钮转圈直至 WS 通知完成并刷新）。
3. **今日事件**：垂直时间线（TodayTimeline），混排今日 episodic_memories（🌱）、agent_events（⚙）、新笔记（📝）、学习晋升（🎓）、已发送问候（💌），每项带时刻与摘要；顶部统计条「今天对话 N 轮 · 调用工具 M 次 · 新增记忆 K 条」。
4. **记忆与知识**：
   - MemoryBrowser：搜索框（走向量语义搜索）+ importance 滑杆过滤 + 分页列表（摘要/重要度星级/时间/情绪标签），行操作=查看详情/删除（确认弹窗：「连带删除向量索引，不可恢复」）。
   - KnowledgeGraph：ECharts graph 力导向图，输入实体名聚焦，depth 1/2 切换，节点按 kind 着色，点边显示 relation_type；
   - NotebookPanel：笔记列表（note/task/insight 三类 tab）、置顶、增删改。
   - 学习/本能两个折叠区：learnings 表（等级徽章 novice/apprentice/master）、instincts 列表。

### 8.7 定时与问候页（R10，逻辑见 §11）

- **总开关卡**：NudgeEngine enabled、greeting_max_per_day（滑杆 0-10）、闲置触发阈值 greeting_threshold（分钟）。
- **GreetingList**：计划卡片列表。固定型显示 `⏰ 每天 08:30 · 周一~周五 · 早安问候 · 通道: Web+QQ`；随机型显示 `🎲 09:00~22:00 窗口内随机 2 次`。每卡：启停开关、编辑、删除、「立即试发」。
- **GreetingForm**：type 单选（固定时间/随机窗口）→ 动态字段（time picker / 窗口起止 + 每日次数）+ 星期多选 + prompt_hint（可选，给 LLM 的问候主题提示，如"提醒喝水"）+ channels 多选（web/qq）。
- **DndEditor**：24h 环形时段选择器（拖拽起止点），支持多段；列表显示 `🌙 23:00–08:00`；说明文案：「免打扰期间所有主动问候静默并顺延」。
- **历史**：最近 7 天已发送问候表（时间/内容/通道/触发原因 fixed|random|idle）。

### 8.8 媒体工坊（R11，见 §12）/ 测试中心（R12，见 §13）/ 仪表盘 / 系统设置

- **TtsPanel**：文本框、voice 下拉（含每音色的描述）、style 下拉（13 种情绪风格，中文标签）、合成按钮 → 波形播放器 + 下载；右侧「自动朗读回复」全局开关与默认音色设置（PUT tts/config，**该开关直接影响 ChatView 收到回复后是否自动调 TTS**）。
- **ImageGenPanel / VideoGenPanel**：prompt 文本框 + 提交 → TaskQueue 组件实时进度（WS）→ 完成后产物卡入 Gallery。视频面板顶部提示预计耗时与「队列中 N 个任务」。
- **Gallery**：瀑布流，type 筛选，点击放大预览，删除。
- **TestCenter**：见 §13.3。
- **DashboardView**：四枚 3D 统计卡（今日消息数/今日成本/工具调用数/记忆总量）+ 成本折线 + 工具调用 Top10 横条图 + SystemMonitor（CPU/内存/磁盘/**SoC 温度**仪表，5s 轮询 `/health/system`）+ 最近审计日志 10 条。
- **系统设置**：UI 主题项（粒子密度、轻量模式、3D 开关、自动朗读）、权限模式（DEFAULT/DEV/STRICT 单选 + 说明）、日志查看器（level 过滤 + 尾随刷新）、审计日志页签、「重启服务」（双重确认 modal，输入 RESTART 才可点）。

---

## 9. WebSocket 实时协议（`/ws`）

连接：`GET /ws?token=<bearer>`。心跳：客户端每 25s `{"type":"ping"}`，服务端回 `pong`；60s 无 ping 断开。断线前端指数退避重连（1s/2s/4s/…max 30s），重连后自动重放 `set_agent` 与订阅状态。顶栏连接状态点：绿=连接、黄=重连中、红=失败。

### 9.1 客户端 → 服务端

```jsonc
{ "type": "chat", "session_id": "s_xx", "agent": "nahida", "text": "...", "msg_id": "c_uuid" }
{ "type": "set_agent", "agent": "yinlang" }
{ "type": "abort", "msg_id": "c_uuid" }          // 中断当前生成（取消 process task）
{ "type": "ping" }
```

### 9.2 服务端 → 客户端

```jsonc
{ "type": "delta",      "msg_id": "c_uuid", "text": "今天" }                    // token 流
{ "type": "tool_event", "msg_id": "c_uuid", "phase": "start|end",
  "tool": "web_search", "args_preview": "...", "ok": true, "elapsed_ms": 1240 }
{ "type": "status",     "msg_id": "c_uuid", "stage": "thinking|tool|replying" } // 输入区上方状态条
{ "type": "final",      "msg_id": "c_uuid", "reply": "...", "emotion": "喜悦",
  "sticker_url": null, "audio_url": null, "image_urls": [], "video_url": null,
  "agent": "nahida", "usage": {"tokens": 812, "cost": 0.0021}, "elapsed_ms": 5320 }
{ "type": "error",      "msg_id": "c_uuid", "code": "...", "message": "..." }
{ "type": "greeting",   "text": "爸爸早安～", "audio_url": null, "reason": "fixed|random|idle" } // §11 推送
{ "type": "media_task_update", "task_id": "t_xx", "status": "running", "progress": 0.4, "result_url": null }
{ "type": "config_changed", "domain": "agents|models|tools|mcp|schedule" }      // 多标签页同步刷新
{ "type": "health_progress", "item": "llm:chat", "ok": true, "detail": "..." }  // 测试中心逐项进度
```

`ws_hub.py`：维护 `dict[conn_id, WebSocket]`；`broadcast(event)` 与 `send_to(conn_id)`；NudgeEngine、MediaTaskQueue、ConfigService 通过依赖注入拿到 hub 引用。

---

## 10. 鉴权与安全

1. **登录**：沿用 `WEBUI_PASSWORD` 环境变量。POST `/auth/login` 校验后签发 `token = base64(expiry) + "." + HMAC_SHA256(SECRET, expiry+nonce)`，有效期 7 天，存浏览器 localStorage。`SECRET` 启动时若无 `WEBUI_SECRET` 环境变量则随机生成并写 `credentials/webui_secret`。
2. **未设密码**：登录页显著红色警告（沿用现 Streamlit 行为），且**仅允许局域网 IP**（检查 RFC1918 网段），公网来源 403。
3. **登录防爆破**：同 IP 5 次失败锁 10 分钟。
4. **owner 语义**：Web 登录用户即 owner（单用户系统），`user_id="webui"` 加入 `OWNER_IDS` 等效集，斜杠 owner 命令可用。
5. **危险操作双闸**：`requires_confirmation` 的工具调试执行、`/system/restart`、记忆删除、Agent 删除 → 前端二次确认 + 请求头 `X-Confirm: yes`，后端缺头直接 400。
6. **审计**：所有 PUT/POST/DELETE 写 `audit_logs`（event_type=`webui.<router>.<action>`，detail=变更 diff）。
7. **媒体目录隔离**：`/media/*` 静态服务用 `os.path.realpath` 校验解析路径必须位于 `web/media/` 内，杜绝路径穿越。
8. **权限模式**：UI 不暴露 BYPASS；后端若检测当前为 BYPASS，Dashboard 顶部常驻红色横幅警告。
9. **CORS**：同源部署，禁用跨域；WS 校验 Origin。
10. **密钥纪律**：任何响应/日志中的 key 一律 `sk-***末4位`；前端不存 key。

---

## 11. 定时任务与主动问候系统（R10 实现细节）

对 `nudge_engine.py` 的增量改造（保留现有 idle 问候、学习晋升、数据清理、提醒、画像整合等 `_tick()` 职责）：

### 11.1 数据模型

新表 `greeting_schedules`（§14）。`NudgeEngine` 启动时载入，ConfigService watch `schedule.*` 实现热更新（增删改计划不需重启）。

### 11.2 调度算法（在现有 60s `_tick()` 内扩展）

- **fixed**：每 tick 检查每个启用计划：`今天属于 days && 当前时间 ∈ [time, time+60s) && 今日未发`（查 `greeting_log` 防重）→ 触发。
- **random**：每天 00:05（或引擎启动时若当天未抽签）对每个随机计划在 `[window_start, window_end]` 内均匀抽取 `count_per_day` 个时刻，**抽签时即剔除与 DND 重叠的时刻**（落入则在窗口内重抽，最多 20 次），结果存 `greeting_schedules.next_fire_times`（JSON）；tick 时同 fixed 逻辑比对。
- **idle**（现有逻辑保留）：`idle_seconds > greeting_threshold` 且未超 `greeting_max_per_day`。
- **DND 多段化**：`_is_dnd()` 改为遍历 `schedule.dnd_periods` 列表（每段支持跨午夜），任一命中即静默。被 DND 拦下的 fixed/random 问候**顺延至 DND 结束后 5 分钟内补发**（每天每计划至多补发 1 次），idle 型直接跳过。
- **全局额度**：所有类型共享 `greeting_max_per_day` 上限。

### 11.3 问候生成与投递

触发 → `ModelRouter.route("chat_flash", ...)` 用 prompt_hint + 当前时段 + 最近记忆摘要生成一句纳西妲口吻问候 → 按 channels 投递：`web` → ws_hub.broadcast `greeting` 事件（前端蒲公英特效 + 插入聊天流 + 若开自动朗读则带 audio_url）；`qq` → 复用现有 QQ 推送路径。每次投递写 `greeting_log` 表（time/content/channel/reason）。

---

## 12. TTS 与视频生成模块（R11 实现细节）

### 12.1 TTS

- 合成入口统一走 `TTSEngine.synthesize(text, voice, style)`（现有，MiMo voiceclone 后端）。网关侧：限制 text ≤ 500 字；产物 wav 移动到 `web/media/tts/{sha1(text+voice+style)}.wav` 实现**内容寻址缓存**（同文重复请求直接命中）；返回 `/media/tts/xx.wav`。
- 「自动朗读回复」开关（ui 配置）：开启时 `final` 事件后前端自动请求 TTS 并播放；voice 自动跟随当前 Agent 的 `voice_ref`（可莉说话用可莉嗓音——这是 per-agent voice_ref 字段的真实用途）。
- style 自动映射：`final.emotion`（9 类）→ `EMOTION_STYLE_MAP`（13 风格）做一张固定映射表放 `tts_engine.py`，手动合成时下拉可覆盖。

### 12.2 图片 / 视频生成

- 经 `MediaTaskQueue`（§5.5）调用 `agnes_image_generate` / `agnes_video_generate` 工具实现（不绕过 tool_executor，保持审计与频率限制一致）。
- 视频任务超时 10 分钟，失败原因写 task.error 并 WS 推送；队列页可取消 queued 状态任务。
- 聊天内联动：对话中模型自行调用生成工具时，`ProcessResult.image_paths/video_path` 同样转 `/media/` URL 内嵌气泡展示——**聊天与工坊两条路径产物统一进 Gallery**。

---

## 13. 健康测试中心（R12 实现细节）

### 13.1 探针定义（`web/routers/health.py` + `web/probes.py` 新增）

| 探针 | 做什么 | 判定 |
|------|--------|------|
| `llm:<route>` | `ModelRouter.route(route, [{"role":"user","content":"请只回复：草元素已就绪"}], max_tokens=20)`，**绕过降级链**（直连目标，否则测不出真实故障） | 2xx 且有非空回复 → ok；记录 latency、实际 model、回复摘录 |
| `provider:<id>` | 用该 provider 的默认 model 同上（自定义 provider 验活） | 同上 |
| `tts` | `synthesize("纳西妲在哦～", "nahida", "neutral")` | 返回音频文件且 >1KB；附 audio_url 供人工试听 |
| `video` | 提交最低参数视频任务（若 provider 支持 dry-run 参数则 dry-run，否则真实小任务并标注消耗） | 任务进入 running 即判通道 ok，最终产物另行通知 |
| `mcp:<server>` | `tools/list` 往返 | 有响应即 ok |
| `db` | `SELECT 1` + 各核心表 count | ok |
| `vector` | VectorStore 试搜索一次 | ok |

### 13.2 报告持久化

每次 `/health/test/all` 结果整体写 `health_reports` 表（§14），Dashboard 顶部显示「上次全检：2026-06-11 22:40 · 9/10 通过」点击进详情。

### 13.3 TestCenter UI

探针卡片网格（LLM 各路由 / 各 Provider / TTS / 视频 / MCP 各 server / DB / 向量库），每卡：状态灯（灰=未测、绿=通过、红=失败 + 错误摘要）、latency、单测按钮；顶部「🩺 一键全检」→ 各卡依 WS `health_progress` 逐个点亮（3D 翻转出结果面，§7.3）；TTS 卡通过后出现播放按钮人工复核音质。失败卡展开显示 error_classifier 的分类与 smart_error_handler 生成的修复建议。

---

## 14. 数据库变更（`database.py` 增量）

```sql
-- 问候计划（§11）
CREATE TABLE IF NOT EXISTS greeting_schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL CHECK(type IN ('fixed','random')),
  time TEXT,                -- fixed: "08:30"
  window_start TEXT, window_end TEXT, count_per_day INTEGER, -- random
  days TEXT NOT NULL DEFAULT '[1,2,3,4,5,6,7]',  -- JSON 星期数组
  prompt_hint TEXT DEFAULT '',
  channels TEXT NOT NULL DEFAULT '["web"]',
  enabled INTEGER NOT NULL DEFAULT 1,
  next_fire_times TEXT DEFAULT '[]',             -- random 当日抽签结果
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS greeting_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  schedule_id INTEGER, fired_at REAL NOT NULL,
  content TEXT, channel TEXT, reason TEXT  -- fixed|random|idle|manual_test
);

-- 媒体任务（§5.5）
CREATE TABLE IF NOT EXISTS media_tasks (
  id TEXT PRIMARY KEY,           -- t_uuid
  kind TEXT NOT NULL,            -- tts|image|video
  prompt TEXT, params TEXT,      -- JSON
  status TEXT NOT NULL DEFAULT 'queued',
  progress REAL DEFAULT 0, result_path TEXT, error TEXT,
  created_at REAL NOT NULL, finished_at REAL
);

-- 自检报告（§13.2）
CREATE TABLE IF NOT EXISTS health_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_at REAL NOT NULL, passed INTEGER, total INTEGER,
  detail TEXT NOT NULL           -- JSON: 每探针结果
);
```

另：`cleanup_config` 表追加 `media_tasks`（保留 30 天）、`greeting_log`（90 天）两行。Agent/模型/工具/MCP/DND 配置**不进数据库**，统一走 json5 文件（人类可直接编辑、可 git 管理）。

---

## 15. 验收清单（防"徒有其表"，最高优先级）

> 验收方法：逐行执行"操作"列，必须观察到"后端可验证效果"列（看日志/查库/看另一通道行为），二者缺一即视为该项**未完成**。实现者每完成一项在 PR 描述中粘贴证据（日志行或 SQL 查询结果）。

| # | 操作（UI） | 后端可验证效果 |
|---|-----------|----------------|
| 1 | 聊天发「你好」 | WS 收到 delta 流与 final；`conversation_logs` 新增一行含 emotion_label |
| 2 | 顶栏切到银狼再提问编程问题 | 日志出现 `dispatch → yinlang`，回复用银狼人格；`agent_events` 有记录 |
| 3 | 新建子 Agent「胡桃」（自定义 provider） | `config/agents/hutao.json5` 生成；**不重启**直接 @胡桃 对话成功 |
| 4 | 权限矩阵关闭可莉的 `shell_command` | `config/agents/keli.json5` excluded_tools 含该项；让可莉执行 shell 被拒，回复明确说无此权限；**QQ 通道同样被拒** |
| 5 | 重新打开该权限 | 立即可用，无重启 |
| 6 | 新增 Anthropic 兼容 provider 并把 chat_pro 路由指向它 | `/health/test/llm` chat_pro 探针返回该 provider 的 model 名；对话深度问题真实走新模型（日志可证） |
| 7 | 新增一个 MCP server（如 filesystem server） | 状态变 running；其工具出现在工具页（source=mcp:xx）与所有 Agent 权限矩阵；对话中模型能调用它 |
| 8 | 停止该 MCP server | 工具从可用集消失，对话中调用返回明确错误而非挂死 |
| 9 | 输入 `/` 弹出命令面板，执行 `/status` | 返回与 CLI 一致的真实运行数据 |
| 10 | Insight 页看情绪 | 与最近一条回复 final.emotion 一致；曲线数据与 conversation_logs 聚合一致 |
| 11 | 点「立即整合画像」 | `user_portrait` 表 version+1，change_log 有新条目，UI 刷新出新画像 |
| 12 | 今日事件时间线 | 条目与当日各表 SQL 查询逐条对得上 |
| 13 | 新建固定问候 2 分钟后的时间点（channels=web） | 到点 WS 收到 greeting 事件，前端飘蒲公英；`greeting_log` 落库 |
| 14 | 设置当前时刻为 DND 段，再「立即试发」 | 不投递；日志显示 DND 拦截；移除 DND 后补发逻辑可观察 |
| 15 | 创建随机问候（窗口含未来时段） | `greeting_schedules.next_fire_times` 出现抽签时刻且避开 DND |
| 16 | TTS 面板合成「爸爸早安」选 happy 风格 | 返回可播放 wav；重复同参请求秒回（缓存命中，日志可证） |
| 17 | 提交视频生成任务 | media_tasks 状态机流转，WS 进度推送，完成后 Gallery 出现可播放视频 |
| 18 | 测试中心一键全检 | 每卡逐个点亮；拔掉某 provider 的 key 再测，该卡变红且错误信息真实 |
| 19 | 工具调试页执行 `get_weather` | 返回真实天气 ToolResult；`audit_logs` 新增 webui 来源记录 |
| 20 | 改全局权限模式为 STRICT | 写类工具（write_file）在对话中被拒 |
| 21 | 仪表盘成本数字 | 与 `SELECT SUM(cost) FROM api_usage WHERE ...` 一致 |
| 22 | 删除一条记忆 | episodic_memories 与向量索引同删；语义搜索不再召回 |
| 23 | 断网 10s 恢复 | WS 自动重连，状态点黄→绿，会话与所选 Agent 不丢 |
| 24 | 开两个浏览器标签页改 Agent 配置 | 另一标签收到 config_changed 并刷新显示 |
| 25 | 关闭"自动朗读"开关 | 新回复不再触发 TTS 请求（network 面板可证） |

---

## 16. 实施阶段计划

> 每阶段必须可独立运行、可演示、过本阶段验收项后才进下一阶段。预估为单人/单模型连续工作量。

- **Phase 0 — 骨架（1~2 天）**：FastAPI 网关 + lifespan 接入 AgentCore + auth + 静态托管；Vue 工程脚手架 + 主题 token + 背景板 + GlassPanel + 登录页（含 Tilt3D）。验收：登录后空壳布局可见，`/system/status` 真实数据。
- **Phase 1 — 对话核心（2~3 天）**：WS 协议、流式 `stream_callback` 改造、ChatView 全套（气泡/工具卡/SlashPalette/会话管理/EmotionAvatar）。验收项：1、2、9、10、23。
- **Phase 2 — 配置中枢（3~4 天）**：ConfigService、AgentRegistry、Agent 页 + 权限矩阵、模型页 + **Anthropic provider**、工具页、MCP 页。验收项：3~8、19、20、24。
- **Phase 3 — 内在世界与定时（2~3 天）**：Insight 全部 tab、NudgeEngine 改造 + Schedule 页。验收项：11~15、22。
- **Phase 4 — 媒体与测试（2~3 天）**：MediaTaskQueue、TTS/图/视频面板、Gallery、TestCenter、Dashboard。验收项：16~18、21、25。
- **Phase 5 — 打磨（1~2 天）**：GrassParticles 全部交互特效、页面转场、移动端响应式、FPS 降级、空态/错误态文案（纳西妲口吻，如「这里还没有长出叶子哦～」）、全清单回归。

---

## 17. 附录

### 17.1 新增环境变量（`.env.example` 追加）

```bash
WEB_UI_ENABLED=true
WEBUI_PASSWORD=change_me          # 必设
WEBUI_SECRET=                     # 留空则自动生成
WEBUI_ALLOW_PUBLIC=false          # 无密码时禁公网
ANTHROPIC_API_KEY=                # 自建 Anthropic 兼容 provider 用（示例）
```

### 17.2 `config/webui_overrides.json5` 示例

```json5
{
  schedule: {
    enabled: true,
    greeting_max_per_day: 3,
    dnd_periods: [{ start: "23:00", end: "08:00" }],
  },
  tts: { auto_speak: false, default_voice: "nahida" },
  ui: { particles: "medium", tilt3d: true },
  tools: { wolfram_query: { enabled: false } },
}
```

### 17.3 `config/agents/hutao.json5`（用户自建 Agent 示例）

```json5
{
  name: "hutao",
  display_name: "胡桃",
  provider: "my-anthropic",        // 指向自建 provider id
  model: "claude-sonnet-4-6",
  personality_file: "config/agents/hutao_personality.md",
  voice_ref: null,
  excluded_tools: ["shell_command", "service_manage"],
  mcp_servers: ["filesystem"],
  capabilities: ["闲聊", "诗词"],
  route_description: "古风闲聊、诗词创作时召唤胡桃",
  max_turns: 8, effort: "medium",
  permission_mode: "default", memory_scope: "shared",
  enabled: true,
}
```

---

*文档完。实现过程中如发现本文档与代码事实冲突（行号漂移、方法签名变化），以代码为准并回写更新本文档对应小节——文档与系统一样，是会生长的世界树。*
