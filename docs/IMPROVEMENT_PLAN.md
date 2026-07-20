# 纳西妲 QQ Bot AI Agent — 全面审查与改进计划

> 审查日期：2026-06-11　|　修订：v2（2026-06-11，吸收外部评审意见）
> 审查方式：4 路并行深度代码审查（架构/多智能体、记忆/人格/情感/媒体、QQ/命令/CLI、工具/安全/设备）
> 每条结论均基于实际代码定位（file:line），多个独立审查路线交叉证实的问题已标注 ★
> 本文档为**待审查的改进计划**，经确认后按阶段实施
>
> **v2 修订要点**（针对评审反馈）：
> 1. 工时改为"编码净时 × 1.5 联调回归系数"双口径（原估算系编码净时，未含跨模块联调）
> 2. 情感枚举从 14 种收敛为 9 种核心 + TTS 风格层映射，并增加准确率验证关卡（**实现更新**：实际落地时枚举扩展为 16 种，详见 `emotion/emotion_enum.py` 与第 6 节）
> 3. 新增第 12 节《监控与可观测性方案》：每项修复绑定验收指标
> 4. 3.1#4 DSML 拆分补充接口边界定义（ToolCallExtractor 协议）
> 5. 新增第 13 节《回滚与发布策略》：环境变量开关 + 独立 commit + 数据备份
> 6. 新增 5.5 节：架构进一步演进项（后台任务持久化等）与明确不做项

---

## 目录

1. [总体结论](#1-总体结论)
2. [P0 — 关键缺陷（功能不可用/安全失守）](#2-p0--关键缺陷)
3. [P1 — 高优缺陷（稳定性/一致性）](#3-p1--高优缺陷)
4. [P2 — 中优缺陷与体验优化](#4-p2--中优缺陷与体验优化)
5. [架构重构方案（最重要）](#5-架构重构方案)
6. [情感系统统一方案](#6-情感系统统一方案)
7. [安全加固方案](#7-安全加固方案)
8. [CLI 体验优化方案](#8-cli-体验优化方案)
9. [能力增强建议（设备调控/编程能力）](#9-能力增强建议)
10. [测试补强计划](#10-测试补强计划)
11. [分阶段实施路线图](#11-分阶段实施路线图)
12. [监控与可观测性方案](#12-监控与可观测性方案)
13. [回滚与发布策略](#13-回滚与发布策略)

---

## 1. 总体结论

项目整体架构思路清晰（分层 + 子代理 + 三层提示），但存在 **3 类系统性问题**：

| 类别 | 核心表现 |
|------|---------|
| **功能性断裂** | 记忆工具(remember/recall/forget)完全不可用；视频生成必然超时；/sys 命令永远报错 |
| **安全失守** | 默认 BYPASS 权限 + 沙箱配置全空 + python_executor 可绕过，三层防线同时形同虚设 |
| **一致性缺失** | 情绪体系三套标准（4/8/13 种）互不兼容；多处并发竞态；向量与记忆数据不同步 |

统计：**P0 × 9，P1 × 23，P2 × 20+**。

**工时口径说明**：文中各项标注的是**编码净时**。涉及跨模块联动的修复（典型如 2.1 记忆工具，要同时动 memory_tool / agent_core / vector_store / db_memory）需按 **×1.5 系数**计入联调与回归测试。核心修复（P0+P1）编码净时约 40-50h，**计划口径约 60-75h**；含架构重构与体验项的全量计划口径约 **100h**。每个 Phase 末尾的端到端回归（约 2h/次）已单列，不摊入单项。

---

## 2. P0 — 关键缺陷

### 2.1 ★ 记忆工具完全不可用 — `tools/memory_tool.py:16`

**问题**：`MemoryManager(config)` 传参错误（构造函数实际需要 `(db, memory, vector_store, router, knowledge_graph, security_filter)`）；且调用的 `memory.remember()` / `memory.retrieve()` / `memory.forget()` 三个方法在 MemoryManager 中**全部不存在**（实际方法是 `retrieve_memories(query, k=5)` / `encode_memory(context)`）。LLM 调用 remember/recall/forget 时全部抛异常，这正是"纳西妲编造记忆"的根因之一。

**修复方案**：
- 重构 memory_tool.py 的懒初始化：不要自建 MemoryManager，改为由 `agent_core.init()` 注入已初始化的全局实例（新增 `memory_tool.bind(memory_manager)`）
- `remember` → 调用 `memory.memory.insert_episodic_memory(summary, importance, emotion_label)` + `vector_store.upsert()`
- `recall` → 调用 `memory.retrieve_memories(query, k=top_k)` 并格式化输出
- `forget` → 先 `retrieve_memories(query, k=1)` 定位 ID，再走统一删除路径（含向量同步删除，见 3.4）
- **工时：4h**

### 2.2 ★ 工具超时冲突：全局 60s vs 视频生成 120s — `tool_executor.py:30` + `tools/agnes_tools.py:139`

**问题**：ToolExecutor 全局超时 60s，agnes 视频生成轮询 24 次 × 5s = 120s，视频生成**必然被截断**。

**修复方案**：
- ToolExecutor 增加按工具超时表：`{"agnes_video_generate": 240, "document_reader": 120, "default": 60}`
- agnes_tools 轮询改为自适应间隔（前 3 次 5s，之后 10s），总等待 180s，超时后返回任务 ID 供后续查询
- **工时：2h**

### 2.3 ★ 默认权限模式 BYPASS — `permission_manager.py:55`

**问题**：未设置 `AGENT_PERMISSION_MODE` 时默认 BYPASS，安全检查（威胁检测、敏感工具确认、路径沙箱）全部跳过。

**修复方案**：
- 默认值改为 `PermissionMode.DEFAULT`
- 启动时若未显式配置环境变量，打印 CRITICAL 警告并落到 DEFAULT（不直接退出，避免服务起不来）
- 启动日志明确输出当前权限模式
- 同步修改 `.env.example` 和部署文档
- **工时：1h**

### 2.4 ★ python_executor 沙箱可绕过 — `tools/code_tools_v2.py:62-135`

**问题**：`_audit_code()` 纯正则文本审查，可被字符串拼接（`'__imp'+'ort__'`）、字节转义（`'\x5f\x5f...'`）、`__class__.__bases__[0].__subclasses__()` 链等多种方式绕过。

**修复方案**（分两步，适配 Orange Pi 1.5G 内存约束）：
1. **短期**：正则审查替换为 **AST 解析审查**（遍历 Import/ImportFrom/Call/Attribute 节点，拦截禁用模块与 dunder 属性访问）+ `resource.setrlimit` 限制子进程内存 50MB / CPU 10s
2. **中期**：执行迁移到独立子进程（`asyncio.create_subprocess_exec` + 受限环境变量 + timeout kill），避免污染主进程；Docker 在 1.5G 设备上成本过高，不推荐
- **工时：4h（短期）**

### 2.5 沙箱配置默认全空 — `sandbox_config.py:33-44`

**问题**：`DEFAULT_SANDBOX` 空白名单、`block_private_ips=False`、不限端口、不限文件路径 → SSRF（含 `169.254.169.254`）与任意文件访问完全敞开。

**修复方案**：
- `block_private_ips=True`，denied_domains 加入 localhost/私有网段/链路本地地址
- `allowed_ports=[80, 443]`
- `allowed_base_dirs=[项目目录, KIOXIA 数据目录, /tmp]`
- `sensitive_paths=["/etc/passwd", "/etc/shadow", "~/.ssh", ".env", "credentials"]`（注意 `.env` 含全部 API Key，必须保护）
- **工时：2h**

### 2.6 ★ QQ 消息去重缓存 FIFO 淘汰导致双发 — `qq_bot_adapter.py:191-201`

**问题**：`_processed_msg_ids` 固定 100 条 LRU；高频消息场景下旧 msg_id 被挤出缓存后，QQ 服务端重推同一消息会被二次处理，产生重复回复。

**修复方案**：改为 (msg_id → 时间戳) 字典，保留最近 1 小时，每次清理过期项。**工时：1.5h**

### 2.7 msg_seq 计数器竞态 — `qq_bot_adapter.py:177-182`

**问题**：全局 `_msg_seq_counter` 无锁递增，并发发送可能产生相同 msg_seq，QQ API 会拒绝或去重丢弃消息。

**修复方案**：`itertools.count()`（CPython 下原子）或加 `threading.Lock`。**工时：0.5h**

### 2.8 emotion 标签泄露到用户可见文本 — `qq_bot_adapter.py:582` + `agent_core.py:1343-1355`

**问题**：`strip_emotion_tag()` 仅处理规范位置的标签；LLM 输出 `[emotion:joy]` 在句中/句尾变体时，标签原样发给用户，严重破坏角色体验。

**修复方案**：在 `agent_core` 回复后处理统一加一层 `re.sub(r'\[emotion:[^\]]*\]', '', text)` 强制剥离（情绪值在剥离前先提取），所有出口（QQ/CLI/ACP）共用。配合 6.3 节的标签自动补全机制。**工时：2h**

### 2.9 委托机制无递归深度限制 — `agent_dispatcher.py:180-186` + `agent_core.py`

**问题**：`[XIAOLI_PENDING]`/`[XIAODA_PENDING]` 字符串前缀约定无类型校验；`RequestContext.delegate_depth` 已定义但**从未检查**，子代理与主 Agent 可能互相委托形成无限循环（在 1.5G 内存设备上会 OOM 触发 systemd 重启）。

**修复方案**：
- 立即：在委托回调入口检查 `delegate_depth`，超过 2 层直接返回兜底回复
- 中期：定义 `DelegationRequest/DelegationResult` dataclass 替代字符串前缀约定（详见 5.3 节）
- **工时：1h（立即）+ 4h（中期）**

---

## 3. P1 — 高优缺陷

### 3.1 多智能体与路由

| # | 问题 | 位置 | 修复方案 | 工时 |
|---|------|------|---------|------|
| 1 | 降级链丢失/不校验 tools 参数，降级到 agnes 后工具能力静默丢失 | `model_router.py:230-234` | 增加 `_filter_tools_for_model()`，按目标模型过滤工具并记录日志 | 1.5h |
| 2 | `_last_reasoning_content` 实例变量在并发请求间互相覆盖 | `model_router.py:81` | 改用 `ContextVar` 管理请求级推理内容 | 1.5h |
| 3 | TaskGraph 无循环检测、无节点级超时、15 步与 150s 总超时不匹配 | `task_orchestrator.py:145-177` | 加 deadline 检查 + 同节点访问 >2 次判环中止 + 节点级 `wait_for` | 3h |
| 4 | DSML 与标准 tool_calls 处理混在同一循环，推理模型工具调用可能漏检 | `agent_dispatcher.py:225-346` | **不拆两条循环**（重复主流程），改为提取策略接口：`ToolCallExtractor.extract(response) -> list[ToolCall] \| None`，两个实现 `StandardExtractor`（读 `message.tool_calls`）与 `DsmlExtractor`（解析文本 DSML 标记），按模型路由选择；`_chat_loop` 保持单一主循环，只依赖统一的 `ToolCall(name, arguments, call_id)` 结构。边界约定：Extractor 只做"识别+解析"，不执行、不修复（修复仍归 tool_repair）、不感知历史。xiaoli_agent 复用同一接口（顺带消除 5.5 提到的重复） | 4h |
| 5 | SubAgent 初始化不验证 API Key，失效凭证到运行时才暴露 | `agent_dispatcher.py:85-111` | init 时发一次 10 token 的探活请求，失败则降级标记并告警 | 1h |
| 6 | 三个路由入口（@mention / 关键词 / TaskGraph）逻辑分散重复 | `agent_core.py:378-444` | 统一为 `RoutingDecision` 决策类（随 AgentCore 拆分一并做，见第 5 节） | — |
| 7 | credential_pool 同步锁 + 游标递增无保护，凭证轮换不公平 | `credential_pool.py:252` | 实例级 `asyncio.Lock` 保护 `get_credential` | 1.5h |
| 8 | ErrorClassifier 不解析 HTTP 状态码，未知错误占比高、降级不精准 | `error_classifier.py:108-200` | 增加 `_match_http_status()`（401/403→AUTH，429→RATE_LIMIT，5xx→SERVER）+ 异常链递归识别 | 2h |

### 3.2 并发安全（跨模块）

| # | 共享状态 | 位置 | 修复 |
|---|---------|------|------|
| 1 | `_user_chat_target` 普通 dict | `agent_core.py:161` | asyncio.Lock 或改为单写者模式 |
| 2 | `_conversation_count` 无原子递增（curator 触发可能跳过） | `agent_core.py:809-811` | asyncio.Lock |
| 3 | tool_guardrails `_call_history` 读写竞态 | `tool_guardrails.py:29-42` | asyncio.Lock |
| 4 | 工具并行执行无并发上限（LLM 一次返回 50 个调用可 OOM） | `tool_call_handler.py:130-131` | `asyncio.Semaphore(5)` | 

合计工时：约 4h

### 3.3 工具链路

| # | 问题 | 位置 | 修复 | 工时 |
|---|------|------|------|------|
| 1 | 风暴检测对比原始 JSON 字符串，参数顺序不同即漏检 | `tool_repair.py:98-100` | 参数 JSON 规范化（parse → sort_keys → dumps）后再比对 | 1h |
| 2 | file_receiver / web_browse SSRF 检查在连接建立后 | `file_receiver.py`、`web_browse_tools.py` | 请求前 DNS 解析 + IP 校验（私有网段拒绝） | 2h |
| 3 | 工具摘要失败时原文直接拼接，超长工具输出可撑爆上下文 | `tool_call_handler.py:174-210` | fallback 路径加 `smart_truncate(combined, 2000)` | 0.5h |

### 3.4 记忆与数据一致性

| # | 问题 | 位置 | 修复 | 工时 |
|---|------|------|------|------|
| 1 | ★ 删除记忆不删向量 → 幽灵向量 | `db_memory.py:61-64` | 在 MemoryManager 封装统一删除（先删向量再删记忆），DB 层不跨模块引用 | 2h |
| 2 | ★ VectorStore.upsert 先删后插非原子 | `vector_store.py:156-185` | 包入 `BEGIN TRANSACTION...COMMIT/ROLLBACK` | 1.5h |
| 3 | 知识图谱提取 prompt 字段名(from/relation/to)与 DB 字段(from_entity/...)规范不一 | `knowledge_graph.py:9-12` + `db_knowledge.py:149-151` | 统一 prompt 输出为 from_entity/relation_type/to_entity，merge_relation 保留多键名容错 | 1.5h |
| 4 | portrait CONSOLIDATE_PROMPT 用 .replace() 注入内容，内容含占位符串时模板被破坏 | `portrait_manager.py:90-96` | 占位符改为低碰撞形式（如 `<<OLD_SECTION>>`）再 replace | 1h |

### 3.5 QQ 链路与推送

| # | 问题 | 位置 | 修复 | 工时 |
|---|------|------|------|------|
| 1 | 分段发送部分失败仍继续发后续段 → 碎片回复 | `qq_bot_adapter.py:578-643` | 失败即中止 + 补发"内容过长部分发送失败"提示 | 1.5h |
| 2 | 媒体上传返回值类型不统一，异常格式返回空串导致静默失败 | `qq_bot_adapter.py:426-516` | 统一 file_info 解析，无值时 raise 而非返回空串；两个几乎重复的 upload 函数合并 | 1.5h |
| 3 | nudge 问候 LLM 调用无超时，可挂死推送循环 | `nudge_engine.py:148-170` | `asyncio.wait_for(..., timeout=15)` | 0.5h |
| 4 | DND 依赖系统本地时间，未显式时区 | `nudge_engine.py:93-97` | 支持 `NUDGE_TIMEZONE` 环境变量，默认 Asia/Shanghai | 1h |
| 5 | 任务提醒发出后立即标记完成，提醒失败也"已完成" | `nudge_engine.py:172-204` | 改为"已提醒"状态，发送成功才标记 | 1.5h |
| 6 | 画像整合失败也重置计时器，失败后要再等 30 分钟 | `nudge_engine.py:232-248` | 仅成功时重置；失败用较短退避（5 分钟） | 0.5h |
| 7 | 问候冷却逻辑在用户长时间离开后产生不合理延迟 | `nudge_engine.py:99-119` | 用户活跃时重置主动消息冷却 | 1h |

### 3.6 TTS 与媒体

| # | 问题 | 位置 | 修复 | 工时 |
|---|------|------|------|------|
| 1 | TTS 合成缓存仅内存，重启全失，重复合成浪费 API 配额 | `tts_engine.py:75-132` | 缓存索引持久化到 JSON（文件已在磁盘，只需索引）；启动时校验文件存在性 | 2h |
| 2 | 语音参考文件全部缺失时静默失败，用户无感知 | `tts_engine.py:147-153` | 全缺失时 ERROR 日志 + 标记 `_available=False`，synthesize 返回明确原因 | 1h |
| 3 | 视频 URL 提取仅查 2 个路径，API 格式微变即失败；下载失败直接报错 | `agnes_tools.py:155-179` | 候选路径列表扩展到 6 个；下载失败时降级返回 URL 给用户 | 1h |
| 4 | 生图/视频无速率限制，可被刷爆 API 配额 | `agnes_tools.py` | 滑动窗口限频：图片 10 次/h，视频 3 次/h | 1h |

### 3.7 上下文系统

| # | 问题 | 位置 | 修复 | 工时 |
|---|------|------|------|------|
| 1 | 压缩按消息数比例而非 token 数，压缩后仍可能超限；无迭代上限 | `agent_context.py:15-128` | 改为 token 目标驱动 + 最多 5 轮压缩 + 最终强制裁剪兜底 | 2.5h |
| 2 | ContextCompressor 返回结构不稳定（是否含摘要 system 消息不确定），调用方过滤逻辑脆弱 | `context_compressor.py:169-200` | 返回 `CompressionResult(messages, summary, tokens_saved)` 结构化契约 | 2h |

---

## 4. P2 — 中优缺陷与体验优化

### 4.1 斜杠命令（逐个检查结果）

| 命令 | 问题 | 位置 | 修复 |
|------|------|------|------|
| `/sys` | 查询服务名 `xiaoda-bot`（实际 `qq-agent`），状态永远 🔴 | `slash_commands.py:362` | 改服务名 |
| `/sys` | `ROUTE_TABLE.get("chat", {}).get("model")` 但表值是字符串，模型永远显示 unknown | `slash_commands.py:370-372` | 改为 `ROUTE_TABLE.get("chat", "unknown")` |
| `/cam` | 无 is_owner 权限检查，任何用户可调摄像头 | `slash_commands.py:375-392` | 加 owner 检查 |
| `/model` | 仅支持 mimo/mimo-pro，无 flash/mini | `slash_commands.py:144-161` | 补充选项 |
| `/hw` | 传感器读取失败仅显示"无法读取"，无原因 | `slash_commands.py:288-343` | 日志记录具体原因 |
| 缺失 | 建议新增：`/memory`（记忆统计）、`/emotion`（当前情绪）、`/knowledge`（知识量）、`/debug`（内部状态，仅 owner） | — | 新增 4 个命令 |

### 4.2 其它 P2

| # | 问题 | 位置 | 修复 |
|---|------|------|------|
| 1 | `wb.close()` 后访问 `wb.sheetnames` | `tools/document_tools.py:147-149` | 统计移到 close 前 |
| 2 | 硬件缓存单一时间戳，target 间互相失效 | `tools/hardware_tools.py:11-12,451-456` | 改 per-target 缓存 dict |
| 3 | 音频转换 finally 中误删输入文件 + pcm_path 可能未定义 | `qq_bot_adapter.py:715-757` | 变量预初始化为 None，仅删中间产物 |
| 4 | 分段消息无衔接提示，阅读断裂 | `text_utils.py:210-250` | 中间段加轻量衔接词（注意保持纳西妲语气） |
| 5 | IDENTITY.md 标注可莉模型为 deepseek-v4-flash（项目已无 DeepSeek） | `config/workspace/IDENTITY.md:102` | 更正为 mimo 路由 |
| 6 | MEMORY.md 中 workspace 路径过时 | `MEMORY.md:6-9` | 更正路径 + 加"最后更新"时间戳 |
| 7 | sticker EMOTION_PATTERN `\w+` 不匹配中文标签变体 | `sticker_manager.py:70` | 收紧为 `[a-z_]+` + 别名映射（随情绪统一一并做） |
| 8 | tools/__init__.py 直接访问 `tool_registry._tools` 私有变量 | `tools/__init__.py:25` | 暴露公共访问函数 |
| 9 | session_store.summary_to_session_info 死代码 | `session_store.py:150` | 删除 |
| 10 | 表情包选择与 emotion 标签可能矛盾（双系统裁决顺序问题） | `agent_core.py:927-941` | 随情绪统一方案解决（第 6 节） |

---

## 5. 架构重构方案（最重要）

### 5.1 AgentCore God Class 拆分

**现状**：`agent_core.py` 1431 行，`__init__` 初始化 30+ 子系统，`_process_impl` 超 300 行。任何修改都需理解全局，单测几乎不可能。

**目标结构**（渐进式，不是一次性重写）：

```
agent_core.py (门面, ~150行)
├── core/bootstrap.py        — AgentCoreBootstrapper：分组初始化
│                              (_init_infrastructure / _init_cognitive / _init_interaction)
├── core/router_engine.py    — RouterEngine：统一三个路由入口
│                              返回 RoutingDecision(agent_names, mode, reasoning)
├── core/chat_processor.py   — ChatProcessor：单轮对话主流程
├── core/tool_orchestrator.py — 工具调用处理（含 hooks 触发）
└── core/background_tasks.py  — 后台任务队列（记忆编码/画像/学习/本能）
```

**迁移步骤**（每步可独立验证、可回滚）：
1. 先抽 `background_tasks.py`（依赖最少，风险最低）
2. 再抽 `bootstrap.py`（纯移动代码，行为不变）
3. 再抽 `router_engine.py`（合并三个路由入口，是行为变更，需重点回归测试）
4. 最后抽 `chat_processor.py` / `tool_orchestrator.py`
5. 每步之后跑 tests/ + QQ 端到端冒烟（发消息/工具调用/子代理/表情包/语音）

**工时：12-16h（分 4 次落地）**

### 5.2 统一路由决策

```python
@dataclass
class RoutingDecision:
    agent_names: list[str]               # ["xiaolang"] / ["xilian","niko"] / ["xiaoda"]
    mode: Literal["single", "parallel", "task_graph"]
    reasoning: str = ""                  # 路由理由，写入日志便于调试
```

决策顺序：显式 @mention → 关键词意图 → 复杂度评估（是否走 TaskGraph）→ 默认 xiaoda。
已实现但未接入的 `belief_router.py`（Thompson Sampling）可作为第二层关键词路由的替代，在 RouterEngine 内以开关接入，灰度验证后替换硬编码关键词。

### 5.3 委托机制类型安全化

用 `DelegationRequest(type, question, delegator, depth)` + `DelegationResult(success, reply, error)` dataclass 替代 `[XIAOLI_PENDING]`/`[XIAODA_PENDING]` 字符串前缀；`ToolResult.data` 携带 DelegationRequest，`_handle_tool_result` 用 isinstance 判断；深度上限 2 层。

### 5.4 单例与并发规范统一

全项目统一约定：
- 全局单例初始化 → 模块级 `asyncio.Lock` 双检
- 请求级状态 → `ContextVar`（reasoning_content、trace 等）
- 共享可变集合 → 实例级 `asyncio.Lock`
- 需要修改的点：credential_pool、model_router、agent_core 计数器、tool_guardrails、context_compressor 单例

### 5.5 进一步架构演进（重构完成后评估）

在 5.1-5.4 落地、指标体系（第 12 节）运行 2 周后，按数据决定是否推进：

| # | 演进项 | 动机 | 前置条件 |
|---|--------|------|---------|
| 1 | **后台任务队列持久化** | 当前记忆编码/画像整合是 fire-and-forget 协程，进程重启（systemd Restart=always 很常见）即丢任务，记忆漏编码无感知 | background_tasks.py 抽出后，加 SQLite 任务表（pending/done/failed）+ 启动时恢复，复用现有 DB，不引入消息队列 |
| 2 | **credential_pool 状态持久化** | 凭证 EXHAUSTED/DEAD 状态重启即丢，重启后重新撞限流 | 复用 cleanup_config 同款 SQLite 小表，启动加载 + 状态变更时写回 |
| 3 | **统一出口管道（ReplyPipeline）** | emotion 剥离/标签补全/AI味去除/分段/表情包选择目前散在 agent_core 与各 adapter，2.8 的修复只是补丁 | RouterEngine 落地后，把回复后处理收敛为有序 pipeline，QQ/CLI/ACP 三出口共用 |
| 4 | **BeliefRouter 转正** | Thompson Sampling 替代硬编码关键词路由 | 12.2 节"路由命中率"指标运行 2 周，灰度对比关键词路由的子代理选择准确率，胜出再切默认 |
| 5 | **嵌入批量并发** | VectorStore.batch_upsert 串行嵌入，冷启动重建索引慢 | 简单 `asyncio.gather` + Semaphore(3) 即可，待记忆量 >5000 条再做 |

**明确不做**（避免过度设计，受 OPi5 1.5G 内存约束）：
- 不引入 Redis/消息队列/微服务拆分——单进程 + SQLite 足够当前规模
- 不做 Docker 沙箱——2.4 的子进程 + resource 限制方案内存成本更低
- AgentCore 拆分止步于 5.1 的 5 个模块，不追求更细粒度

---

## 6. 情感系统统一方案

> **实现更新（2026-07）**：本节原计划"9 种核心情绪 + TTS 风格层映射"两层结构。实际落地时，结合表情包分类粒度需求，核心枚举已扩展为 **16 种**（HAPPY/EXCITED/LOVE/SHY/SAD/ANGRY/SURPRISED/CONFUSED/THINKING/PLAYFUL/MOVED/NEUTRAL/ANXIOUS/FEAR/CURIOUS/POUT，详见 `emotion/emotion_enum.py`）。下文"9 种"相关描述保留作为历史决策记录。

**现状**（三套标准互不兼容，已交叉证实）：
- `emotion_simple.py`：4 类关键词 → 输出中文（喜悦/悲伤/焦虑/愤怒/平静）
- `sticker_manager.py`：8 类英文（happy/sad/shy/angry/curious/greeting/thinking/fear），关键词集与 emotion_simple 大量不重叠（"可惜/遗憾"只在 sticker，"孤独/抑郁"只在 emotion_simple）
- `tts_engine.py`：13 类英文（多出 excited/surprised/caring/playful/lonely）
- `SOUL.md`：规定 10 类标签
- 后果：同一文本在不同模块判定不同情绪；TTS 收到 sticker 的情绪值可能查不到风格；表情与语音情绪可能矛盾

**方案**（新建 `emotion_enum.py`，编码净时约 6h，计划口径 9h）：

设计原则：**核心枚举宁少勿多**——检测准确率撑不起的枚举值只会让三个消费端（表情/语音/标签）各自猜测。采用"9 种核心情绪 + TTS 风格层映射"两层结构，而非把 TTS 的 13 种全部上提为核心枚举：

1. **单一枚举源**：`Emotion(str, Enum)` 定义 **9 个核心值**（happy / sad / angry / anxious / shy / curious / thinking / fear / neutral——即 sticker 8 类 + neutral，与现有关键词覆盖能力匹配），加 `EMOTION_ALIASES` 中文/变体映射表（喜悦→happy、孤独/抑郁→sad、可惜/遗憾→sad 等）
2. **TTS 风格层独立映射**：TTS 专属的 excited/surprised/caring/playful/lonely **不进核心枚举**，作为 `TTS_STYLE_MAP: dict[Emotion, str]` 的细分输出（如 happy+高强度→excited），LLM 标签出现这些值时通过别名表归并到核心枚举。待第 12 节的情绪检测准确率指标达标（核心 9 类 ≥80%）后，再评估是否上提为核心值
3. **emotion_simple.py** 重构为基于枚举的检测器，关键词集取两套体系的并集
4. **sticker_manager** 只做"枚举值 → 表情包"映射，不再自带第二套关键词检测；9 类核心值与 7 套表情包之间补一张降级映射表
5. **tts_engine** `get_emotion_style()` 走 `TTS_STYLE_MAP`，查不到时落 neutral
6. **标签可靠性兜底**（解决 SOUL.md 纯依赖 LLM 的问题）：在 agent_core 回复后处理加 `_ensure_emotion_tag()`——标签存在且合法→提取；非法→经别名表修正；缺失→用统一检测器从文本推断并补全。保证表情包/语音永不因 LLM 漏标签而失效
7. SOUL.md 标签白名单与核心枚举同步（10 类收敛为 9 类），并将正则收紧为 `\[emotion:([a-z_]+)\]`

**验证关卡**：上线前用最近 200 条真实对话日志做离线标注对比，核心 9 类检测准确率 <70% 时先收紧关键词再上线；上线后通过 12.2 节的情绪分布埋点观察 neutral 兜底占比（应 <30%）。

---

## 7. 安全加固方案

按防线层级（除 P0 三项外的补充）：

| 层 | 措施 | 位置 |
|----|------|------|
| 权限 | BYPASS 模式下对高置信度(≥0.95)注入攻击仍记 CRITICAL 日志 | `permission_manager.py:107-134` |
| 权限 | `is_owner()` 的 `startswith("cli")` 放宽判定收紧（仅精确 "cli" 本地会话） | `agent_core.py` |
| 沙箱 | 见 2.5 默认配置加固 | `sandbox_config.py` |
| 代码执行 | AST 审查 + 子进程隔离 + resource 限制，见 2.4 | `code_tools_v2.py` |
| 网络 | SSRF 前置检查（DNS→IP 校验在请求前），见 3.3#2 | `file_receiver.py` 等 |
| 模块开放 | python_executor 安全开放 json/re/datetime/collections/itertools（在子进程隔离落地后） | `code_tools_v2.py` |
| 部署 | `.env` 权限 chmod 600；部署清单加"权限模式必须显式设置"检查项 | 文档 |

---

## 8. CLI 体验优化方案

| # | 项 | 方案 |
|---|----|------|
| 1 | 打字机速度可配 | `NAHIDA_TYPEWRITER_SPEED=fast/normal/slow/off` 环境变量（cli.py:131-146） |
| 2 | 非 TTY 输出 | 保持按行输出，加段落空行，便于管道阅读（cli.py:131-147） |
| 3 | NO_COLOR 支持 | 检测 `NO_COLOR` 环境变量剥离 ANSI 码（cli.py:27-43） |
| 4 | 状态翻译 | `_status_translate` 改为有序优先级列表，避免 dict 遍历顺序歧义（cli.py:149-176） |
| 5 | 多样化问候 | 按时间段（清晨/深夜）和会话次数轮换欢迎语（cli.py:46-66） |
| 6 | 输入增强 | 引入 `readline`（标准库）：历史记录、方向键编辑、Ctrl+R 搜索 |
| 7 | 富显示（可选） | 若允许新增依赖，用 `rich` 实现 markdown 渲染/spinner/面板；不允许则维持 ANSI 手绘 |
| 8 | 会话内命令 | CLI 直接支持 /sys /hw /memory 等斜杠命令（与 QQ 共用 SlashCommandHandler） |

合计工时：约 6h（不含 rich 改造）

---

## 9. 能力增强建议

### 9.1 设备调控（hardware_tools / system_tools）

| 优先级 | 增强项 | 说明 |
|--------|--------|------|
| P1 | 温度/功耗监控工具 | 读 `/sys/class/thermal`，OPi5 高负载发热明显，可联动 nudge 主动告警 |
| P1 | 存储空间监控 | KIOXIA 挂载点使用率（该盘满了服务直接故障），纳入 /hw 与 nudge 巡检 |
| P1 | GPIO PWM 支持 | action="pwm"，frequency + duty_cycle 参数 |
| P2 | I2C 块传输 + 总线频率查询 | SMBus block read/write |
| P2 | GPIO 批量操作 / 边沿中断检测 | 一次调用多 pin |
| P2 | USB 设备管理 | 列举/安全卸载 |

### 9.2 编程能力（python_executor / dev_assist / 银狼）

| 优先级 | 增强项 | 说明 |
|--------|--------|------|
| P1 | python_executor 开放安全标准库 | json/re/datetime/collections/itertools/urllib.parse（依赖子进程隔离先落地） |
| P2 | dev_assist 增加 lint/test 操作 | 调 ruff/pytest（项目 venv 内） |
| P2 | 银狼接入代码审查工具 | 独立 code_review 工具：读文件 + 结构化审查输出 |
| P2 | 可莉增加轻量游戏/互动工具 | 强化角色差异化 |

---

## 10. 测试补强计划

现有：test_hooks / test_credential_pool / test_prompt_caching / deep_integration_test。

**优先补充**（与修复同步写，修一个测一个）：

| 测试文件 | 覆盖 |
|---------|------|
| `tests/test_memory_tool.py` | remember/recall/forget 全链路（修 2.1 时必写） |
| `tests/test_security_bypass.py` | AST 审查对字符串拼接/字节转义/subclasses 链的拦截；路径遍历 `../..` |
| `tests/test_emotion_unified.py` | 枚举映射、别名回退、标签补全 `_ensure_emotion_tag` |
| `tests/test_concurrency.py` | msg_seq 并发唯一性、guardrails 并发、chat_target 竞态 |
| `tests/test_tool_timeout.py` | 按工具超时表、并发 Semaphore 上限 |
| `tests/test_vector_consistency.py` | 删除同步、upsert 事务回滚 |
| `tests/test_model_router.py` | 降级链 tools 过滤、HTTP 状态码分类 |

---

## 11. 分阶段实施路线图

### Phase 1 — 止血（P0 全部，编码净时 ~18h / 计划口径 ~27h）
> 原则：**每项独立提交、修完即跑一轮冒烟测试**（重启服务 → QQ 发消息 → 触发对应功能 → journalctl 无新增 ERROR），全部通过再进下一项；每项的回滚方式见第 13 节

1. memory_tool 重构（2.1）+ test_memory_tool ✅记忆功能恢复
2. 工具超时表 + agnes 轮询（2.2）✅视频生成可用
3. 权限默认值 + 沙箱默认配置（2.3 + 2.5）✅安全基线
4. AST 审查 + resource 限制（2.4）+ test_security_bypass
5. QQ 双发/msg_seq/emotion 标签泄露（2.6-2.8）✅交互体验止血
6. 委托深度限制（2.9 立即项）

**Phase 1 前置（0 号任务）**：先落地 12.1 节的关键指标埋点（~3h）——没有基线数据，修复效果无法量化。

### Phase 2 — 稳定性（P1 核心，编码净时 ~20h / 计划口径 ~30h）
1. 并发安全四件套（3.2）+ test_concurrency
2. 记忆/向量一致性（3.4）+ test_vector_consistency
3. 模型路由三项：tools 过滤、ContextVar、错误分类（3.1 #1/#2/#8）
4. QQ 链路与 nudge 七项（3.5）
5. TTS/生图四项（3.6）
6. 情感系统统一（第 6 节）+ test_emotion_unified

### Phase 3 — 架构重构（编码净时 ~16h / 计划口径 ~24h，分 4 次）
1. 抽 background_tasks → 2. 抽 bootstrap → 3. RouterEngine 统一路由（含 belief_router 灰度接入）→ 4. 抽 chat_processor/tool_orchestrator
2. 委托机制 dataclass 化（5.3）
3. DSML/标准工具调用路径拆分（3.1 #4）
4. 上下文压缩 token 驱动改造（3.7）

### Phase 4 — 体验与能力（编码净时 ~14h / 计划口径 ~19h）
1. 斜杠命令修复 + 新增 4 命令（4.1）
2. CLI 八项优化（第 8 节）
3. 设备监控工具（温度/存储）+ GPIO PWM（9.1）
4. P2 杂项清理（4.2）
5. 文档同步：CLAUDE.md bug 清单更新、IDENTITY/MEMORY.md 更正

**总计：编码净时约 68h，计划口径约 100h（含联调回归系数与 Phase 末端到端回归）。建议按 Phase 顺序执行，每 Phase 结束做一次 QQ 端到端回归（消息/工具/子代理/表情/语音/生图/斜杠命令），并对照第 12 节指标确认无回归。**

---

## 12. 监控与可观测性方案

> 解决"修完怎么知道修好了"的问题。复用现有 `metrics.py`（计数器+计时器）+ `db_analytics.py`（agent_events 表），**不引入新依赖**。Phase 1 之前先落地（~3h），让每项修复都有前后对比数据。

### 12.1 关键指标埋点

| 指标 | 埋点位置 | 验证的修复项 | 健康水位 |
|------|---------|-------------|---------|
| `memory.recall.hit_rate`（recall 返回非空占比）| `tools/memory_tool.py` | 2.1 记忆工具 | 修复前 0%（全异常）→ >60% |
| `memory.recall.latency_ms` | 同上 | 2.1 | p95 < 2s |
| `tool.timeout.count`（按工具名分桶）| `tool_executor.py` | 2.2 超时表 | agnes_video 超时 → ~0 |
| `tool.error.count`（按工具名+错误类分桶）| `tool_executor.py` | 全部工具修复 | 持续下降 |
| `delegate.depth.histogram` | `agent_dispatcher.py` 委托入口 | 2.9 递归限制 | 无 depth>2 样本 |
| `emotion.tag.source`（llm_valid / alias_fixed / inferred / fallback_neutral）| `_ensure_emotion_tag()` | 2.8 + 第 6 节 | fallback_neutral < 30% |
| `emotion.tag.leaked`（出口仍含 `[emotion:` 的回复数）| 各出口发送前 | 2.8 | = 0 |
| `qq.msg.duplicate_dropped` / `qq.msg.send_fail` | `qq_bot_adapter.py` | 2.6 / 3.5 | 双发投诉 = 0 |
| `router.decision`（按 agent+mode 分桶）| RouterEngine | 5.2 / BeliefRouter 灰度 | 用于 5.5#4 对比 |
| `model.fallback.count`（按降级目标分桶）| `model_router.py` | 3.1#1 | agnes 降级时 tools 丢失日志 = 0 |
| `security.block.count`（按威胁类型分桶）| `permission_manager.py` / hooks | 2.3 / 2.4 / 2.5 | 有拦截记录（当前 BYPASS 下恒为 0 即异常）|
| `vector.orphan.count`（向量数 − 记忆数差值）| nudge 巡检每日一次 | 3.4#1 | = 0 |

### 12.2 查看方式

- **即时**：新增 `/debug` 斜杠命令（已列入 4.1，仅 owner）输出 metrics 快照
- **趋势**：指标每小时快照写入 `agent_events` 表（复用现有清理策略防膨胀），用 sqlite3 命令行做周对比
- **告警**：复用 nudge 巡检——`vector.orphan.count > 0`、`tool.timeout` 激增、KIOXIA 使用率 >90% 时主动私聊 owner

### 12.3 每项修复的验收闭环

```
修复前：记录基线（journalctl 错误统计 + metrics 快照）
修复后：冒烟测试 → 观察 24h → 对比指标 → 达标则关闭，未达标回滚（第 13 节）
```

---

## 13. 回滚与发布策略

> 生产环境是运行中的 QQ Bot（systemd Restart=always），任何修复出问题都要能在 **5 分钟内**回退。

### 13.1 通用机制

1. **每项修复 = 一个独立 commit**，commit message 带计划编号（如 `fix(P0-2.3): default permission mode`）。回滚即 `git revert <hash> && sudo systemctl restart qq-agent`
2. **行为开关优先**：凡是改变运行时行为（而非纯 bug 修复）的项，加环境变量开关，回退只需改 `.env` + 重启，无需动代码
3. **发布窗口**：避开用户活跃时段（晚 19-23 点），优先在白天发布并观察
4. **数据先备份**：涉及 DB 结构或批量数据变更前，`cp agent.db agent.db.bak-$(date +%m%d)`（KIOXIA 空间允许，保留最近 3 份）

### 13.2 高风险项的专项回滚方案

| 修复项 | 风险 | 开关/回滚方式 |
|--------|------|--------------|
| 2.3 权限默认 DEFAULT | 误拦正常请求，Bot"变笨" | `AGENT_PERMISSION_MODE=BYPASS` 写入 .env 即回到旧行为；观察 `security.block.count` 中误杀样本 |
| 2.5 沙箱默认加固 | 误伤合法域名/路径，web_browse/file 工具失效 | 新增 `SANDBOX_PROFILE=strict/legacy`，legacy=旧的全空配置；上线 24h 内监控 `tool.error.count` 中 sandbox_denied 占比 |
| 2.4 AST 审查 | 误判合法代码，python_executor 可用性下降 | `PYEXEC_AUDIT_MODE=ast/regex`，且 AST 拦截时记录完整代码样本供排查 |
| 2.1 记忆工具重构 | 写入路径变化污染记忆库 | 上线前备份 agent.db；新写入的记忆带 `source=tool_v2` 标记，异常时可定向清除 |
| 6 情感系统统一 | 表情/语音情绪错乱 | `EMOTION_UNIFIED=on/off`，off 走旧三套逻辑（保留旧代码一个 Phase，确认稳定后删除）|
| 5.1 AgentCore 拆分 | 任意环节行为漂移 | 每步纯移动代码 + 单独 commit；RouterEngine（唯一行为变更步）加 `ROUTER_ENGINE=new/legacy` 双路开关 |
| 5.5#4 BeliefRouter | 路由质量下降 | 灰度期仅 owner 会话启用，`router.decision` 指标对比后再放量 |

### 13.3 开关清理纪律

行为开关是过渡手段：每个开关在对应修复稳定运行 **2 周**后删除旧路径与开关本身，避免双路径长期并存变成新的技术债（在 Phase 收尾清单中逐项核销）。

---

## 附：与 CLAUDE.md 已知 Bug 清单的对照

- CLAUDE.md 4.2 中的 14 个未修复 bug：**全部经本次审查证实仍存在**，已纳入上文对应章节
- 本次新发现且 CLAUDE.md 未记录的问题：约 **30 项**（QQ 双发、msg_seq 竞态、emotion 泄露、风暴检测漏检、nudge 五项、TTS 缓存、知识图谱 prompt 规范、压缩 token 逻辑、TaskGraph 判环等）
- 建议本文档评审通过后，将 CLAUDE.md 第 4 节替换为指向本文档的链接，避免双份清单失同步
