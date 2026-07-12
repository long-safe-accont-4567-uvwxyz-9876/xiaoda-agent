# nahida-agent 变更前后量化质量评分 Spec

| 元数据 | 值 |
|---|---|
| 完成日期 | 2026-07-12 |
| 基线版本 | a5e1d27 |
| 变更版本 | eb41f7c |
| 评分对象 | nahida-agent 核心模块 |
| 评分方法 | 源码静态分析（逻辑审查 + 模式识别 + 边界推演） |

---

## 一、评分维度定义

每个维度 0-10 分，规则如下：

| 维度 | 权重 | 10分标准 | 5分标准 | 0分标准 |
|---|---|---|---|---|
| **代码正确性** | 30% | 零逻辑 bug；边界全面；异常路径全覆盖 | 存在非关键边界遗漏；异常处理不完整但主流程安全 | 有逻辑 bug；关键路径未处理异常 |
| **接口设计** | 25% | 职责单一；最小接口面；无过度设计；向后兼容 | 接口偏大或偶有过度设计；兼容性基本保持 | 接口膨胀；严重过度设计；破坏兼容性 |
| **异步安全** | 20% | 无竞态；无泄漏；ContextVar 安全；锁粒度合理 | 局部竞态风险；小概率泄漏；ContextVar 使用基本正确 | 明确竞态；资源泄漏；ContextVar 误用 |
| **向后兼容** | 15% | 旧调用方零改动；迁移路径清晰；降级优雅 | 少量调用方需改动但无破坏性变更；迁移有文档 | 删除/重命名关键接口；无迁移路径 |
| **文档/注释质量** | 10% | 100% docstring 覆盖；注释准确无冗余；设计决策有记录 | 核心函数有 docstring；部分注释过时或冗余 | 缺失 docstring；注释误导或与代码不一致 |

---

## 二、变更前（a5e1d27 基线）模块评分

### 2.1 core/router_engine.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **8** | 路由决策逻辑完整（@mention→否定→自指→语音→关键词→默认）；`_build_mention_map`/`_build_keyword_patterns` 每次调用重建正则有性能浪费但无 bug；`decide()` 同步方法在异步环境中调用安全 |
| 接口设计 | **8** | `RoutingDecision` dataclass 设计清晰；`decide()` 返回值类型明确；但 `_match_mentions` 静态方法每次遍历全量 map 不够高效 |
| 异步安全 | **9** | 纯同步模块，无竞态风险；`_build_mention_map` 内 `from config import` 有延迟导入但无状态问题 |
| 向后兼容 | **9** | 底部 `MENTION_MAP` 保持旧代码可访问；`decide()` 签名稳定 |
| 文档/注释质量 | **7** | 模块级 docstring 清晰；`RoutingDecision` 有 Attributes 文档；但 `_build_*` 系列函数无 docstring |
| **加权总分** | **8.15** | |

### 2.2 belief_router.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **7** | Thompson Sampling 实现正确（Marsaglia + Tsang）；但 `_gamma_sample(shape<1)` 递归无上界保护（极端 shape≈0 时理论无限递归）；`_load_from_db` 的 `alpha/beta` 下限 0.01 合理 |
| 接口设计 | **7** | `select_agent()` 的 `task_type` 参数未使用（仅传给 enhanced_router）；J-Space Hook 通过模块级全局变量注入，耦合较重 |
| 异步安全 | **6** | `_save_to_db` 通过 `run_in_executor` 非阻塞写入，但 `_beliefs` dict 在主线程读取 + executor 线程写入无锁保护（`beliefs_snapshot` 在主线程快照，实际安全，但模式脆弱）；`_save_to_json` 在主线程同步写文件 |
| 向后兼容 | **9** | `BeliefRouter` API 完全不变；`_enhanced_router` 为增量添加 |
| 文档/注释质量 | **7** | `AgentBelief` 有 docstring；`select_agent` 有 docstring；但 `_gamma_sample`/`_load_from_json` 无文档 |
| **加权总分** | **7.10** | |

### 2.3 agent_core/sub_agent_manager.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **7** | 超时保护（180s + CancelToken + asyncio.wait_for）完善；`parallel_dispatch` 的 `return_exceptions=True` 正确；但 `_parallel_run_one` 中 event_bus emit 与 BeliefRouter update 分散在 try/except 各分支，逻辑重复度高；`_bb_task_key` 使用 md5 但 `usedforsecurity=False` 正确 |
| 接口设计 | **6** | `delegate_to_agent` 方法过长（~100 行），承担 pipe/ensemble/retry_fallback/debate 四种模式 + 单代理委托；`_dispatch_single_sub_agent` 含表情包/TTS/隐私扫描等与调度无关的职责 |
| 异步安全 | **8** | `asyncio.gather` + `return_exceptions=True` 正确；CancelToken 清理在 finally 块中；`_current_request_ctx` ContextVar 使用合理 |
| 向后兼容 | **8** | Mixin 模式不破坏组合；`delegate_to_xiaoli` 签名不变 |
| 文档/注释质量 | **7** | `_SUB_AGENT_EMOTION_RULE` 有详细注释说明注入策略；`parallel_dispatch` 有 docstring；但 `_parallel_run_one` 和 `_finalize_parallel_reply` 缺少 docstring |
| **加权总分** | **7.05** | |

### 2.4 agent_core/message_processor.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **7** | Harness 验收循环逻辑完整（MAX_VERIFICATION_TURNS=8, 墙钟超时50s, 连续失败3次熔断）；fast_path 中 DSML + OpenAI 两种 tool call 格式均处理；但 `_execute_fast_path_tools` 递归调用 LLM 时未传 tools 导致只做一轮工具调用 |
| 接口设计 | **5** | `_process_impl` 方法超过 30 行调用链（init→slash→chat_target→fast_path→task_graph→main_path），层级过深；`_run_verification_loop` 参数 10+ 个；多处 `_` 前缀方法实为 public API |
| 异步安全 | **7** | `_process_impl` 中多处 fire-and-forget（XP、profile_learner）异常吞没合理；但 `_stream_llm_response` 丢弃已积累部分流式内容后降级到同步调用，可能丢失部分回复 |
| 向后兼容 | **8** | ProcessResult 数据结构稳定；`_process_impl` 签名不变 |
| 文档/注释质量 | **6** | 模块级 docstring 完整；但内部方法大多无 docstring；`_is_simple_chat` / `_is_simple_task` 的判定规则未在注释中说明阈值来源 |
| **加权总分** | **6.40** | |

### 2.5 agent_core/shared_blackboard.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **9** | TTL 过期逻辑正确（`time.monotonic()`）；`subscribe` 创建占位条目永不过期避免竞态；`cleanup_expired` 在 `keys()` 和 `get()` 中均有惰性清理 |
| 接口设计 | **9** | 接口精简（put/get/get_with_meta/subscribe/keys/cleanup_expired）；`get_with_meta` 返回 dict 而非独立类型略欠优雅但实用 |
| 异步安全 | **9** | 全方法 `asyncio.Lock` 保护；subscribe 的 `asyncio.Event` 在锁内操作安全；`_Entry.subscribers` 在 put 时 clear 防泄漏 |
| 向后兼容 | **10** | 纯新增模块，无旧调用方 |
| 文档/注释质量 | **9** | 模块级 docstring 详细说明线程安全模型；`put`/`get`/`subscribe` 均有完整 docstring；`_Entry.__slots__` 优化有注释说明 |
| **加权总分** | **9.10** | |

### 2.6 memory/fluid_memory.py（变更前旧版）

> 注：当前文件已是兼容层，变更前旧版为独立实现（含自定义 R(t) 公式）。

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **6** | 旧版自定义衰减公式 `R = e^(-t/S)` 但 stability 仅用线性增长 `S_INIT + access_count * 14`，无 FSRS 的 difficulty 维度；`score()` 中 `similarity * peak_weight * R` 缺少归一化 |
| 接口设计 | **6** | 类常量与实例方法混用（STABILITY_BASE_DAYS 既是类属性又作为默认值）；`score()` 签名含 peak_weight 参数但语义不直观 |
| 异步安全 | **9** | 纯同步无状态计算，无竞态风险 |
| 向后兼容 | **8** | 保留为兼容层委托 FSRSModel；旧调用方零改动 |
| 文档/注释质量 | **5** | 兼容层注释简单；旧版公式推导无记录 |
| **加权总分** | **6.55** | |

### 2.7 memory/memory_manager.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **7** | 六路 RRF 混合检索逻辑正确；冷启动三段路由（cold/warm/hot）设计合理；但 `_has_duplicate` 的 FTS + 归一化匹配存在漏检风险（FTS 召回 top5 可能不含真正重复项）；`_enrich_memory_async` 中 JSON 提取逻辑脆弱（依赖 LLM 输出格式） |
| 接口设计 | **5** | `retrieve_memories_hybrid` 参数 7 个含 use_reranker/use_kg/include_raw 等控制标志，接口膨胀；`__init__` 参数 10+ 个；`MemoryManager` 类职责过重（检索+编码+蒸馏+去重+KG+实体） |
| 异步安全 | **7** | `_memory_count_cache` 无锁保护（单事件循环内安全但脆弱）；`_last_lazy_migrate_ts` 时间戳节流无锁但单协程安全 |
| 向后兼容 | **7** | `retrieve_memories_hybrid` 新增参数有默认值不破坏旧调用；但 scope 参数强制要求 Scope 对象需调用方适配 |
| 文档/注释质量 | **7** | `reciprocal_rank_fusion` 有完整 docstring；`retrieve_memories_hybrid` 有详细 Args 文档；但内部方法如 `_hybrid_fts_search_scoped` 无 docstring |
| **加权总分** | **6.25** | |

### 2.8 memory/confirm_correct.py（变更前）

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **6** | correct() 匹配验证逻辑（共享≥2 token 或覆盖≥50%）正确；但 `history` 字段从 JSON 解析后 `append` 再 `json.dumps` 可能因旧数据格式异常崩溃；`_make_node_id` 用 md5[:12] 碰撞风险虽低但无检测 |
| 接口设计 | **7** | confirm/correct 双方法清晰；但 `confirm` 中边强化循环（`get_edges` → `update_edge` ×2）对大图性能差 |
| 异步安全 | **7** | 全异步方法但无显式锁；依赖 DB 层并发控制 |
| 向后兼容 | **8** | 新增模块无旧调用方 |
| 文档/注释质量 | **7** | 模块级 docstring 说明 confirm/correct 语义；方法内步骤注释清晰 |
| **加权总分** | **6.70** | |

### 2.9 memory/concept_graph.py（变更前）

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **8** | `remember` 的去重检查（existing 存在则跳过）正确；`lazy_migrate` 的 `source_mem_id` 检查防重复迁移；`auto_link` 委托 DB 层 |
| 接口设计 | **8** | 接口精简（remember/lazy_migrate/get_node/get_edges）；`_make_node_id` 与 `ConfirmCorrect` 重复但各自模块隔离 |
| 异步安全 | **8** | 无显式锁，依赖 DB 层 |
| 向后兼容 | **9** | 新增模块 |
| 文档/注释质量 | **8** | 类级 docstring 说明 Hippocampus 层职责；`remember` 和 `lazy_migrate` 有 docstring |
| **加权总分** | **8.05** | |

### 2.10 qq_bot_adapter.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **7** | 消息去重（`_processed_msg_ids` + TTL 清理）正确；HITL 两段式确认流程完整；`_send_streaming_reply` 分片逻辑处理群聊/C2C 差异；但 `_patched_send_heart` monkey-patch `BotWebSocket._send_heart` 全局副作用大；`__main__` 块使用同步 `client.run()` 与异步 `run_qq_bot` 两套启动逻辑 |
| 接口设计 | **5** | `AIQQBot` 类超过 800 行，职责过重（消息处理+流式发送+媒体上传+HITL+终端）；`_process_message_attachments` 提取后仍有大量内联逻辑 |
| 异步安全 | **6** | `_msg_seq_lock` 是 `threading.Lock`（跨线程安全但与 asyncio 混用需注意）；`_env_write_lock` 同理；`_ACTIVE_BOT` 全局变量无锁保护（单进程内理论安全） |
| 向后兼容 | **8** | `run_qq_bot` 签名不变；monkey-patch 向后兼容 botpy 库 |
| 文档/注释质量 | **6** | HITL 流程有注释；但 monkey-patch 部分无设计决策记录；流式分片逻辑注释分散 |
| **加权总分** | **6.15** | |

### 2.11 web/ws_hub.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **7** | WebSocket 协议处理完整（chat/terminal/abort/ping）；`ConnectionManager.MAX_CONNECTIONS=32` 防资源耗尽；`_verify_response` 检测空响应/错误循环/降级；但 `_handle_terminal_start` 的 PTY fork 在子进程中 `os.execvpe` 无 fallback shell |
| 接口设计 | **6** | `process_and_serialize` 函数参数 8+ 个；`_handle_chat` 含 PLAN/EXECUTE/VERIFY 三阶段逻辑但函数名不体现；终端相关逻辑应独立模块 |
| 异步安全 | **7** | `_pty_sessions_lock` 是 `threading.Lock`（PTY fd 回调在主线程但读写在子线程）；`_tasks` dict 的 `pop` 在 `add_done_callback` 中但无锁保护（单事件循环安全） |
| 向后兼容 | **8** | WebSocket 协议不变；EventBus bind/unbind 在 finally 中清理 |
| 文档/注释质量 | **6** | 模块级 docstring 说明 §9 协议；但 PTY 管理函数群无架构说明 |
| **加权总分** | **6.55** | |

### 2.12 cli_client.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **8** | WebSocket 瘦客户端逻辑完整；`login` 带重试保护；`_listener` 处理 greeting/final/error/status 四类事件；但 `_pending` dict 无超时清理（长时间运行可能积累已完成 Future） |
| 接口设计 | **8** | CLI 接口简洁（connect/chat/set_agent/run）；`NahidaCLI` 类职责合理 |
| 异步安全 | **7** | `_listener` 和 `chat` 并发访问 `_pending` dict（单事件循环安全）；`_status_handler` 可能在 chat 超时后仍被触发 |
| 向后兼容 | **9** | 纯客户端，服务端变更不影响 |
| 文档/注释质量 | **7** | 模块级 docstring 说明用法；但 STAGE_TEXT / GREETINGS 等常量无注释说明来源 |
| **加权总分** | **7.75** | |

### 2.13 tool_engine/tool_call_handler.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **7** | `_sanitize_tool_result` 防注入（EXTERNAL 级别标记）正确；`_exec_semaphore` 限制并发（5个）；路径白名单校验写操作工具；但 `_extract_path_from_args` 仅覆盖 write_file 和通用 path 参数，遗漏部分工具 |
| 接口设计 | **6** | `__init__` 参数 10 个；`_handle_tool_calls` 方法过长（含修复+执行+回调+事件发射）；`ToolCallHandler` 职责过重（执行+修复+安全+事件） |
| 异步安全 | **7** | `_exec_semaphore` 防并发过载；但 `_tool_repair.clear_storm_window()` 在消息处理开始时调用，跨请求共享状态 |
| 向后兼容 | **8** | `ToolCallHandler` API 基本稳定 |
| 文档/注释质量 | **6** | `_WRITE_TOOLS` 和 `_sanitize_tool_result` 有注释；但核心方法缺少 docstring |
| **加权总分** | **6.60** | |

### 2.14 db/database.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **8** | v15 迁移幂等性设计；FAT 文件系统检测并降级 journal_mode；busy_timeout 优先设置；复合索引通过 IndexManager 幂等创建 |
| 接口设计 | **7** | `DatabaseManager` 聚合多个子 DB 模块（MemoryDB/NotebookDB/LearningDB 等）合理；但 `__init__` 中 `self._conn` 与子 DB 的生命周期耦合 |
| 异步安全 | **7** | `aiosqlite` 单连接在多协程间共享，依赖 aiosqlite 内部锁；`init()` 幂等性通过关闭旧连接实现但无并发 init 保护 |
| 向后兼容 | **8** | schema version 15 增量迁移；旧数据自动迁移 |
| 文档/注释质量 | **6** | FAT 文件系统处理有注释；但 PRAGMA 策略选择无设计文档 |
| **加权总分** | **7.15** | |

---

## 三、变更后（eb41f7c）新增/重写模块评分

### 3.1 core/event_bus.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **9** | 定向投递设计（非广播）正确；`_current_user` ContextVar 保证协程安全；emit 时 User 为 None 静默忽略合理；`gen_task_id` 唯一性保证（agent + uuid4[:8]） |
| 接口设计 | **9** | 接口极简（bind_user/unbind_user/emit/bound_user）；`AgentEvent` dataclass 字段合理；`AgentEventType` 枚举严格定义；事件不传任意 dict 而通过 `data: dict` 扩展 |
| 异步安全 | **9** | ContextVar 实现协程安全隔离；bind/unbind 在 session 开始/结束时调用，生命周期清晰；emit 内 `user.deliver()` 异常不中断调用方 |
| 向后兼容 | **9** | 纯新增模块；全局单例 `event_bus` 方便旧代码迁移 |
| 文档/注释质量 | **9** | 模块级 docstring 详细说明设计原则（非广播、定向投递、User 按渠道决定投递）；`AgentEvent` 每个字段有注释 |
| **加权总分** | **9.05** | |

### 3.2 core/cancel_token.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **8** | 超时自动取消（`_timeout_watch` asyncio.Task）正确；`is_cancelled` 属性双重检查（timer + monotonic 比对）保证可靠性；`cleanup()` 取消 timer task 防泄漏；但 `_timeout_watch` 中 `self._timeout` 引用在 task 内，若 timeout 很长且 token 被重复使用可能语义不清 |
| 接口设计 | **9** | 接口精简（cancel/check/cleanup/is_cancelled/reason）；`CancellationError` 异常类带 reason 字段便于调试 |
| 异步安全 | **7** | `_cancelled`/`_reason` 非 atomic 写入，在 `_timeout_watch`（asyncio task）和 `cancel()`（可能跨协程调用）间存在理论竞态；但 CPython GIL + 单事件循环下实际安全 |
| 向后兼容 | **9** | 纯新增模块 |
| 文档/注释质量 | **8** | 模块级 docstring 含使用示例；类级 docstring 说明 timeout 参数语义 |
| **加权总分** | **8.20** | |

### 3.3 core/enhanced_router.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **7** | Thompson + direction_bias + signal_adjustment 三路评分逻辑正确；`AGENT_TASK_MAP` 硬编码映射（security→xiaolang, debug→xiaoke 等）缺乏配置化；`direction_hint` 参数在 `select_agent` 中可覆盖 `task_type` 但未在调用链中传递 |
| 接口设计 | **7** | 构造函数依赖 `BehavioralSignalStream` + `DirectionRegistry`，耦合 J-Space 全套组件；`update_belief` 委托给 base_router 但不返回结果 |
| 异步安全 | **9** | 纯同步计算无竞态；依赖的 `_stream.aggregate` 和 `_base.sample_agent` 均为同步方法 |
| 向后兼容 | **8** | 在 `belief_router.py` 中通过 J-Space Hook 条件启用，旧路径完全保留 |
| 文档/注释质量 | **7** | 模块级 docstring 对齐 ACT/RepE 参考；但 `AGENT_TASK_MAP` 无注释说明映射逻辑来源 |
| **加权总分** | **7.55** | |

### 3.4 core/behavioral_direction.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **7** | `DirectionVector` 的 `__mul__`/`__add__` 算术正确；`apply_to_context` 对 prompt/tool/emotion/route 四维度映射合理；`DirectionRegistry` 的 JSON 持久化正确；但 `apply_to_context` 中 `prompt_modifier`/`tool_bias`/`emotion_offset`/`route_bias` 四个 key 无 schema 定义 |
| 接口设计 | **7** | `DirectionVector` dataclass 设计清晰；`DirectionRegistry` 的 register/get/list 简洁；但 `apply_to_context` 返回新 dict（浅拷贝）但未处理嵌套 dict 合并 |
| 异步安全 | **9** | 纯同步无状态；`_save_to_storage` 文件写入无并发保护但单进程单事件循环安全 |
| 向后兼容 | **9** | 纯新增模块 |
| 文档/注释质量 | **8** | 每个方法标注对齐的 RepE/reprobe 参考模块；`DirectionVector` 字段有注释 |
| **加权总分** | **7.75** | |

### 3.5 core/behavioral_signal.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **7** | `BehavioralSignalStream` 的 deque(maxlen) 自动淘汰正确；`aggregate` 三种策略（max_of_means/mean_of_means/max_absolute）实现正确；但 `subscribe` 的 `asyncio.Event` 列表无清理机制（事件触发后 Event 引用仍保留在列表中） |
| 接口设计 | **8** | emit/subscribe/get_history/aggregate 四方法精简；`SignalEntry` dataclass 设计合理 |
| 异步安全 | **7** | `_buffer` 是 `collections.deque` 线程安全；但 `_subscribers` dict 的 `append` 与 `set()` 在不同协程间无锁（单事件循环安全） |
| 向后兼容 | **9** | 纯新增模块 |
| 文档/注释质量 | **8** | 每个方法标注对齐参考；`aggregate` 策略有说明 |
| **加权总分** | **7.70** | |

### 3.6 core/intent_decomposition.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **6** | `_rule_encode` 的关键词匹配评分逻辑（hits * 0.3, 上限 1.0）简单有效；`residual` 计算基于解释力比例合理；但 `INTENT_KEYWORDS` 中英双语关键词混合且权重相同（"根据" 和 "according to" 等价不合理）；`_llm_encode` 抛 `NotImplementedError` 在运行时才发现 |
| 接口设计 | **7** | encode/decode 对齐 SAE 范式清晰；`DecomposedOutput.dominant_intent`/`sparsity` 属性实用；但 `_use_llm` 标志在 `__init__` 中设置后无法切换 |
| 异步安全 | **9** | `_rule_encode` 纯同步无状态；`_llm_encode` 未实现 |
| 向后兼容 | **9** | 纯新增模块 |
| 文档/注释质量 | **8** | SAE 对齐参考详细；`IntentFactor`/`DecomposedOutput` 字段有注释 |
| **加权总分** | **7.20** | |

### 3.7 core/intervention_loop.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **7** | cooldown 检查正确；trigger_above/trigger_below 双向阈值判断完整；`apply_intervention` 委托 `DirectionVector.apply_to_context` 正确；`get_convergence_metrics` 的趋势判断（最近5次 score 递减→converging）简单但有效 |
| 接口设计 | **8** | register_rule/evaluate/apply_intervention/get_convergence_metrics 四方法清晰；`InterventionRule` dataclass 字段合理 |
| 异步安全 | **7** | `_intervention_history` 是 deque 无锁（单事件循环安全）；`rule.last_triggered` 在 evaluate 中写入但无并发保护 |
| 向后兼容 | **9** | 纯新增模块 |
| 文档/注释质量 | **8** | 对齐 reprobe Monitor + Steerer 参考；`InterventionRule` 每字段有注释 |
| **加权总分** | **7.65** | |

### 3.8 core/j_space_bootstrap.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **6** | `init_j_space()` 全局变量初始化顺序正确（stream→registry→loop→blackboard→router）；`_wire_hooks()` 通过 `import` + 属性注入连接各模块；但 `_wire_hooks` 中任一 import 失败仅 warning 但后续 hook 仍可能访问 None；全局变量 `_signal_stream` 等无 `Final` 类型标注 |
| 接口设计 | **6** | 全局单例 + getter 函数模式（`get_signal_stream()` 等）不利于测试；`_wire_hooks` 通过 monkey-patching 模块级变量注入，替代方案应使用显式依赖注入 |
| 异步安全 | **7** | `init_j_space()` 同步初始化，在 Agent 启动时调用一次；全局变量后续只读 |
| 向后兼容 | **8** | `ENABLE_J_SPACE_HOOKS` 开关控制，默认关闭不破坏旧路径 |
| 文档/注释质量 | **7** | `init_j_space()` 有初始化步骤说明；但 `_wire_hooks` 的注入目标和原因无文档 |
| **加权总分** | **6.60** | |

### 3.9 agent_core/user_base.py + user_cli.py + user_web.py + user_qq.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **9** | `UserBase` ABC 定义清晰；`CLIUser` 按 event type 分支打印；`WebUser` 构建完整 payload 并通过 `send_fn` 发送；`QQUser` 仅 SUB_STARTED 发消息节省 5 条限制；`AGENT_DISPLAY`/`STATUS_ICON` 共享映射表 |
| 接口设计 | **9** | `deliver(event)` 单方法接口极简；各渠道 User 构造函数参数合理（WebUser 接受 send_fn，QQUser 接受 reply_fn + msg_seq_fn）；策略模式替代 if-else 渠道判断 |
| 异步安全 | **9** | 各 User 无共享状态；`WebUser._send_fn` 和 `QQUser._reply_fn` 均为注入的异步函数；异常不外泄 |
| 向后兼容 | **8** | 新增模块替代旧的内联渠道判断；旧代码中直接 print/ws.send 可逐步迁移 |
| 文档/注释质量 | **9** | 每个文件有模块级 docstring 说明渠道特性和消息条数限制；`UserBase.deliver` 抽象方法有详细说明 |
| **加权总分** | **8.80** | |

### 3.10 agent_core/structured_blackboard.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **7** | 继承 `SharedBlackboard` 并扩展 tag/direction 索引正确；`query_by_tag`/`query_by_direction` 先查索引再取值；`merge_from` 逐 key 导入保留原值；但 `_tag_index`/`_direction_index` 中的 key 在父类 `cleanup_expired` 时不会同步清理（索引指向已过期 key） |
| 接口设计 | **7** | `put_structured` 参数较多（key/value/agent_name/ttl/tags/direction/quality）但合理；`merge_from` 接口简洁 |
| 异步安全 | **7** | 继承父类 `asyncio.Lock` 保护 put/get；但 `_tag_index`/`_direction_index` 的修改在 `put_structured` 中无显式锁保护（父类锁不覆盖子类新增操作） |
| 向后兼容 | **9** | 继承 `SharedBlackboard`，旧代码用 `put`/`get` 不受影响 |
| 文档/注释质量 | **7** | 对齐 SAE/Steerer/jlens 参考；但 tag/direction 索引与过期清理不同步的问题未在注释中说明 |
| **加权总分** | **7.35** | |

### 3.11 agent_core/shared_blackboard_db.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **8** | SQLite WAL 模式跨进程安全；`put`/`get`/`get_with_meta`/`keys`/`cleanup_expired` 逻辑正确；过期条目在 get 时惰性清理；`_serialize`/`_deserialize` 处理 JSON 编码 |
| 接口设计 | **8** | 接口与 `SharedBlackboard` 完全对齐（put/get/get_with_meta/keys/cleanup_expired），可互换使用 |
| 异步安全 | **7** | `asyncio.Lock` 保护并发访问；DB 操作通过 `run_in_executor` 非阻塞；但每个操作单独获取/释放连接而非连接池，高频场景下性能差 |
| 向后兼容 | **9** | 接口与 `SharedBlackboard` 兼容，可按需切换实现 |
| 文档/注释质量 | **8** | 模块级 docstring 详细说明适用场景（多 worker/跨进程）；与 SharedBlackboard 的区别有说明 |
| **加权总分** | **7.90** | |

### 3.12 memory/fsrs_model.py

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **9** | FSRS-DSR 三变量模型（Difficulty/Stability/Retrievability）实现正确；`R(t) = e^(-t/S)` 公式标准；状态机 `BUFFER→REINFORCED/DECAY→PERMANENT/ARCHIVED` 转换完整；`_apply_recall` 的 growth 计算含 difficulty_factor + retrievability_bonus；`_apply_forget` 的 S_new 下限保护 `max(1.0, S_new)` |
| 接口设计 | **9** | `MemoryState` dataclass 字段完整；`reinforce(state, signal)` 签名清晰；`ReinforcementSignal` 枚举含 growth_factor 属性设计优雅；`estimate_initial_difficulty` 函数独立 |
| 异步安全 | **10** | 纯同步无状态计算；所有方法均为纯函数 |
| 向后兼容 | **8** | 替代旧 `FluidMemory`；兼容层保留旧接口 |
| 文档/注释质量 | **8** | 模块级 docstring 说明核心公式和状态机；`MemoryPhase` 枚举值有注释；但 `_apply_forget` 的公式推导无注释 |
| **加权总分** | **8.85** | |

### 3.13 memory/fluid_memory.py（重写后兼容层）

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **8** | 兼容层委托 `FSRSModel` 正确；`score()` 方法重建 `MemoryState` 并调用 `retrievability` 得到 R 值；但 `score()` 中 `stability = S_INIT + access_count * STABILITY_PER_ACCESS` 仍用旧线性公式而非 FSRS 的 reinforce 更新后的 stability |
| 接口设计 | **8** | 保留旧 API 签名（`score/is_permanent/should_filter/should_archive`）；类常量委托 FSRS 常量 |
| 异步安全 | **9** | 纯同步无状态 |
| 向后兼容 | **9** | 旧调用方零改动 |
| 文档/注释质量 | **7** | 模块级 docstring 说明迁移到 FSRSModel；但 `score()` 中旧的 stability 计算与 FSRS reinforce 结果的差异未说明 |
| **加权总分** | **8.05** | |

### 3.14 memory/confirm_correct.py（修改后）

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **8** | confirm 现在正确使用 FSRS reinforce（STRONG_CONFIRM）；weight 由 R 驱动（`min(1.0, R)`）合理；同步 episodic_memories FSRS 状态；correct 的 supersedes 链逻辑不变 |
| 接口设计 | **7** | 与变更前一致；confirm 中边强化循环性能问题未改善 |
| 异步安全 | **7** | 与变更前一致 |
| 向后兼容 | **8** | API 不变；FSRS 状态列通过 DB 迁移添加 |
| 文档/注释质量 | **8** | FSRS reinforce 步骤有编号注释 |
| **加权总分** | **7.55** | |

### 3.15 memory/concept_graph.py（修改后）

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **8** | 新增 `estimate_initial_difficulty` 计算初始 difficulty；`remember` 写入 difficulty 字段；其余逻辑不变 |
| 接口设计 | **8** | 与变更前一致 |
| 异步安全 | **8** | 与变更前一致 |
| 向后兼容 | **8** | 新增 difficulty 列通过 DB 迁移添加 |
| 文档/注释质量 | **8** | 与变更前一致 |
| **加权总分** | **8.05** | |

### 3.16 db/database.py（v15 迁移后）

| 维度 | 分数 | 依据 |
|---|---|---|
| 代码正确性 | **8** | 与变更前一致；v15 迁移新增 FSRS 相关列（difficulty/stability/phase/last_review/reinforcement_count） |
| 接口设计 | **7** | 与变更前一致 |
| 异步安全 | **7** | 与变更前一致 |
| 向后兼容 | **8** | schema version 15 增量迁移 |
| 文档/注释质量 | **6** | 与变更前一致 |
| **加权总分** | **7.15** | |

---

## 四、总评分对比表

### 4.1 变更前基线模块汇总

| 模块 | 正确性 | 接口设计 | 异步安全 | 向后兼容 | 文档注释 | **加权总分** |
|---|---|---|---|---|---|---|
| core/router_engine.py | 8 | 8 | 9 | 9 | 7 | **8.15** |
| belief_router.py | 7 | 7 | 6 | 9 | 7 | **7.10** |
| agent_core/sub_agent_manager.py | 7 | 6 | 8 | 8 | 7 | **7.05** |
| agent_core/message_processor.py | 7 | 5 | 7 | 8 | 6 | **6.40** |
| agent_core/shared_blackboard.py | 9 | 9 | 9 | 10 | 9 | **9.10** |
| memory/fluid_memory.py(旧) | 6 | 6 | 9 | 8 | 5 | **6.55** |
| memory/memory_manager.py | 7 | 5 | 7 | 7 | 7 | **6.25** |
| memory/confirm_correct.py(旧) | 6 | 7 | 7 | 8 | 7 | **6.70** |
| memory/concept_graph.py(旧) | 8 | 8 | 8 | 9 | 8 | **8.05** |
| qq_bot_adapter.py | 7 | 5 | 6 | 8 | 6 | **6.15** |
| web/ws_hub.py | 7 | 6 | 7 | 8 | 6 | **6.55** |
| cli_client.py | 8 | 8 | 7 | 9 | 7 | **7.75** |
| tool_engine/tool_call_handler.py | 7 | 6 | 7 | 8 | 6 | **6.60** |
| db/database.py | 8 | 7 | 7 | 8 | 6 | **7.15** |
| **基线均值** | **7.21** | **6.64** | **7.36** | **8.43** | **6.71** | **7.04** |

### 4.2 变更后新增/重写模块汇总

| 模块 | 正确性 | 接口设计 | 异步安全 | 向后兼容 | 文档注释 | **加权总分** |
|---|---|---|---|---|---|---|
| core/event_bus.py | 9 | 9 | 9 | 9 | 9 | **9.05** |
| core/cancel_token.py | 8 | 9 | 7 | 9 | 8 | **8.20** |
| core/enhanced_router.py | 7 | 7 | 9 | 8 | 7 | **7.55** |
| core/behavioral_direction.py | 7 | 7 | 9 | 9 | 8 | **7.75** |
| core/behavioral_signal.py | 7 | 8 | 7 | 9 | 8 | **7.70** |
| core/intent_decomposition.py | 6 | 7 | 9 | 9 | 8 | **7.20** |
| core/intervention_loop.py | 7 | 8 | 7 | 9 | 8 | **7.65** |
| core/j_space_bootstrap.py | 6 | 6 | 7 | 8 | 7 | **6.60** |
| agent_core/user_base+cli+web+qq | 9 | 9 | 9 | 8 | 9 | **8.80** |
| agent_core/structured_blackboard.py | 7 | 7 | 7 | 9 | 7 | **7.35** |
| agent_core/shared_blackboard_db.py | 8 | 8 | 7 | 9 | 8 | **7.90** |
| memory/fsrs_model.py | 9 | 9 | 10 | 8 | 8 | **8.85** |
| memory/fluid_memory.py(新) | 8 | 8 | 9 | 9 | 7 | **8.05** |
| memory/confirm_correct.py(新) | 8 | 7 | 7 | 8 | 8 | **7.55** |
| memory/concept_graph.py(新) | 8 | 8 | 8 | 8 | 8 | **8.05** |
| db/database.py(v15) | 8 | 7 | 7 | 8 | 6 | **7.15** |
| **变更后均值** | **7.75** | **7.88** | **8.06** | **8.63** | **7.88** | **7.84** |

### 4.3 维度对比图

| 维度 | 基线均值 | 变更后均值 | 变化 |
|---|---|---|---|
| 代码正确性 | 7.21 | 7.75 | **+0.54** |
| 接口设计 | 6.64 | 7.88 | **+1.24** |
| 异步安全 | 7.36 | 8.06 | **+0.70** |
| 向后兼容 | 8.43 | 8.63 | **+0.20** |
| 文档/注释质量 | 6.71 | 7.88 | **+1.17** |
| **加权总分** | **7.04** | **7.84** | **+0.80** |

---

## 五、关键发现

### 5.1 变更收益

1. **接口设计显著改善（+1.24）**：EventBus + User 策略模式消除了各适配器中的渠道 if-else 分支；FSRSModel 的纯函数设计优于旧 FluidMemory 的有状态计算；CancelToken 的显式取消语义优于裸 `asyncio.wait_for`。
2. **异步安全提升（+0.70）**：EventBus 的 ContextVar 隔离替代了旧代码中隐式的全局状态传递；CancelToken 提供了显式取消 + 超时双重保护；FSRSModel 纯函数无竞态。
3. **文档质量改善（+1.17）**：新增模块普遍有对齐参考（RepE/SAE/reprobe/jlens）的设计文档；EventBus/CancelToken/User 系列的 docstring 覆盖率接近 100%。

### 5.2 变更风险

1. **j_space_bootstrap 全局变量注入模式**：`_wire_hooks()` 通过 monkey-patching 模块级变量注入依赖，任一 hook 失败仅 warning 但后续代码可能访问 None。这是变更后最低分模块（6.60），建议改为显式依赖注入或使用 `__init_subclass__` 等更安全的注册机制。
2. **StructuredBlackboard 索引-过期不同步**：`_tag_index`/`_direction_index` 中的 key 在父类 `cleanup_expired` 清理过期条目时不会同步清理索引，可能导致查询返回已过期条目。
3. **BehavioralSignalStream 订阅者泄漏**：`subscribe()` 返回的 `asyncio.Event` 在触发后仍保留在 `_subscribers` 列表中，无清理机制。

### 5.3 未改善的技术债

1. **message_processor.py 接口膨胀未改善**：`_process_impl` 调用链过深、参数过多的问题在变更中未触及，仍是全项目接口设计最低分（5 分）。
2. **qq_bot_adapter.py 职责过重未改善**：`AIQQBot` 超过 800 行，monkey-patch botpy 的全局副作用仍在。
3. **tool_call_handler.py 职责过重未改善**：执行+修复+安全+事件四重职责未拆分。

---

## 六、改进建议

| 优先级 | 模块 | 建议 | 预期效果 |
|---|---|---|---|
| **P0** | core/j_space_bootstrap.py | 将 `_wire_hooks()` 的 monkey-patching 替换为显式依赖注入：各模块接收 `signal_stream`/`intervention_loop` 作为构造参数，或在 `AgentCore.init()` 中统一注入 | 消除全局变量 None 访问风险；提升可测试性 |
| **P0** | agent_core/structured_blackboard.py | 重写 `cleanup_expired` 为虚方法或 hook 机制，在父类清理过期 key 后同步清理子类 `_tag_index`/`_direction_index` | 修复索引-过期不同步 bug |
| **P1** | core/behavioral_signal.py | `subscribe()` 返回包装对象，在 Event.set() 后自动从 `_subscribers` 列表中移除自身 | 防止订阅者泄漏 |
| **P1** | core/cancel_token.py | 为 `_cancelled`/`_reason` 添加 `asyncio.Lock` 保护或将 `_timeout_watch` 中的写入改为 CAS 模式 | 消除理论竞态 |
| **P1** | agent_core/message_processor.py | 将 `_process_impl` 拆分为独立阶段对象（`ProcessingPipeline`），每个阶段实现 `async def process(ctx) -> ctx` | 降低方法复杂度；提升可测试性 |
| **P2** | core/intent_decomposition.py | 为 `INTENT_KEYWORDS` 添加权重配置（中英文区别对待），移除 `_use_llm` 标志改为策略模式 | 提升意图识别精度；消除运行时 NotImplementedError |
| **P2** | qq_bot_adapter.py | 将 `_send_streaming_reply`/`_send_reply_with_sticker`/`_convert_to_silk` 等提取到 `qq_message_sender.py` 独立模块 | 降低 AIQQBot 行数；单一职责 |
| **P2** | core/enhanced_router.py | 将 `AGENT_TASK_MAP` 移到 config/agents 配置文件 | 提升可配置性 |
| **P3** | agent_core/shared_blackboard_db.py | 引入 `aiosqlite` 连接池替代单连接 `run_in_executor` | 提升高频场景性能 |
| **P3** | memory/fluid_memory.py | 兼容层 `score()` 应使用 DB 中存储的 FSRS reinforce 后的 stability，而非旧线性公式计算 | 消除兼容层与 FSRS 语义不一致 |

---

## 七、评分方法论说明

1. **评分依据**：所有评分基于实际源码静态分析，逐模块审查逻辑正确性、接口边界、异步模式、兼容性和文档覆盖。
2. **基线版本**：a5e1d27 对应的模块状态为"变更前"；eb41f7c 对应新增/重写模块为"变更后"。
3. **未修改模块**：db/database.py 在两版本间仅 schema version 递增（v15 迁移），评分相同。
4. **兼容层特殊处理**：memory/fluid_memory.py 变更前评分为假设旧版独立实现（含自定义衰减公式），变更后评分为当前兼容层。
5. **User 系列合并评分**：user_base.py / user_cli.py / user_web.py / user_qq.py 作为一个整体评分，因它们构成完整的策略模式。
