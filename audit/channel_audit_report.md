# 代码质量审计报告：nahida-agent 渠道适配+外围模块

完成日期：2026-07-12

---

## P0 — 严重Bug（影响正确性/数据安全/生产稳定性）

### P0-1: SharedBlackboardDB 每次操作新建 SQLite 连接，存在并发写入数据丢失风险

**文件**: `agent_core/shared_blackboard_db.py`
**行号**: 全文件（`put`/`get`/`get_with_meta`/`keys`/`cleanup_expired` 方法）
**描述**: 每个 `put`/`get`/`keys` 操作都 `sqlite3.connect()` → 操作 → `conn.close()`。虽然 WAL 模式支持并发读，但 `asyncio.Lock` + `run_in_executor` 意味着所有写操作会串行执行在同一个锁内，而每次新建连接无法利用 SQLite 的事务批处理。更关键的是，`asyncio.Lock` 是协程锁，`run_in_executor` 将实际 DB 操作推到线程池，**锁只保护了协程调度顺序，不能阻止其他线程/进程通过不同 `SharedBlackboardDB` 实例并发写入同一 DB 文件**——这与跨进程共享的设计目标矛盾。

此外，`put` 方法中 `expire_at` 使用 `time.time()` 计算，但 `get` 方法也用 `time.time()` 比较——如果系统时钟回拨（NTP 校正），已写入的条目可能瞬间过期或永不过期。

**修复建议**:
1. 改用持久连接（`self._conn` 在 `__init__` 创建，`close()` 方法关闭），避免频繁建连开销
2. 跨进程安全应依赖 SQLite 自身的 WAL + 适当重试（`PRAGMA busy_timeout`），不要依赖 `asyncio.Lock`（它仅在同事件循环内有效）。移除 `asyncio.Lock` 或仅用作同进程去重
3. 在 `_init_db` 中设置 `PRAGMA busy_timeout=5000`
4. `expire_at` 统一使用单调时钟 `time.monotonic()`（需在持久化时转换，或接受 monotonic 不可跨进程的现实，改回 `time.time()` + 文档标注 NTP 风险）

---

### P0-2: QQ 群聊 5 条消息限制未在 C2C 流式输出中遵守

**文件**: `qq_bot_adapter.py`
**行号**: `_send_streaming_reply` 方法（约 L450-L530）
**描述**: QQ 官方限制每轮被动回复 + 主动消息共 5 条。代码中群聊路径使用 `split_for_group_passive` 切片，注释声称"最多 4 片，ACK+4片=5次"。但 C2C 路径使用 `_split_text_for_streaming(clean_reply, chunk_size=300)`，**没有 5 条上限约束**——长回复可能切成 10+ 片，每片调用 `message.reply`，在 C2C 场景下虽然无 5 条硬限制，但高频发送会触发 QQ API 限流（`msg_seq` 递增过快导致服务端拒绝）。

更重要的是，**流式发送前的"正在打字"提示**（L510: `message.reply(content=f"...正在打字...")`）也会消耗 1 次配额。在群聊中这会导致 ACK + 打字提示 + 分片 = 可能超过 5 条。代码在群聊中确实做了判断 `if not is_group` 才发打字提示，这是正确的，但注释中的"ACK 占 1 次配额"未计入 `_send_reply_with_sticker` 中 ACK 发送和流式分片的配额分配。

**修复建议**:
1. C2C 流式添加上限：`max_segments = min(len(segments), 5)`，超出部分合并为最后一片
2. 在 `_send_streaming_reply_with_sticker` 中，群聊路径严格限制 segments ≤ 4（含 ACK 共 5），在切片后 `segments = segments[:4]`，最后一片包含剩余全部内容
3. 添加 `_qq_msg_budget` 计数器，每次 `message.reply` / `post_c2c_message` 后递减，为 0 时停止发送

---

### P0-3: QQ 适配器 EventBus unbind_user 在异常路径可能泄漏

**文件**: `qq_bot_adapter.py`
**行号**: `_process_c2c_reply` 方法（约 L640）和 `_process_group_reply` 方法
**描述**: `event_bus.bind_user(qq_user)` 后用 `try/finally: event_bus.unbind_user()` 保护。但 `_process_c2c_reply` 在 `bind_user` 之前先发送了 ACK（`message.reply`），如果 ACK 失败抛异常，`bind_user` 从未被调用，但 finally 中的 `unbind_user` 仍然会执行（将之前的绑定清除掉）。这是 **ContextVar 语义问题**：`unbind_user` 调用 `_current_user.set(None)`，如果当前协程从未绑定过，会清除其他并发协程绑定的 User。

**复现路径**: 协程 A 在 `_process_c2c_reply` 中 `bind_user(QQUser)` → 协程 B 进入 `_process_c2c_reply`，ACK 失败异常 → finally 执行 `unbind_user()` → **清除了协程 A 的绑定** → 协程 A 的子代理事件丢失。

**修复建议**:
1. 使用 ContextVar token 模式（类似 `_current_request_ctx`）：`token = _current_user.set(user)` → `finally: _current_user.reset(token)`
2. 或在 `unbind_user` 中检查当前值是否为自己绑定的 User，不是则跳过

---

### P0-4: StructuredBlackboard 的 tag_index/direction_index 不过期清理，导致内存泄漏

**文件**: `agent_core/structured_blackboard.py`
**行号**: L20-L25（`_tag_index` / `_direction_index` 定义），L42-L48（`put_structured`）
**描述**: `put_structured` 将 key 添加到 `_tag_index[tag]` 和 `_direction_index[direction]`，但 `SharedBlackboard.cleanup_expired()` 只清理 `_store` 中的过期条目，**不会清理 `_tag_index` 和 `_direction_index` 中指向已过期 key 的引用**。随着时间推移，这些索引集合会无限增长，且 `query_by_tag` / `query_by_direction` 查到已过期的 key 后调用 `get_with_meta` 返回 None，虽然功能正确但产生大量无效查询。

**修复建议**:
1. 在 `cleanup_expired` 后遍历 `_tag_index` / `_direction_index`，移除已过期的 key
2. 或重写 `cleanup_expired`，在清理 `_store` 时同步清理索引

---

### P0-5: CLI 中 EventBus 绑定在 process 之前，但 status_callback 中无事件推送

**文件**: `cli.py`
**行号**: L189-L195
**描述**:
```python
event_bus.bind_user(CLIUser())
try:
    result = self._loop.run_until_complete(
        self.bot.process(user_input, user_id="cli_owner", source="cli",
                         status_callback=status_notify)
    )
finally:
    event_bus.unbind_user()
```
`CLIUser` 的 `deliver` 方法会打印 TOOL_STARTED/TOOL_COMPLETED 等事件，但 `status_notify` 回调也打印了工具状态（`_status_translate(msg)`）。**双重输出**：TOOL_STARTED 事件通过 EventBus → CLIUser.deliver 打印一次，同时 `status_callback` 也打印一次。而 `status_callback` 的翻译更友好（带 emoji），CLIUser 的打印更原始。

**修复建议**:
1. CLI 场景中让 EventBus 的 TOOL_* 事件不重复打印——在 `status_notify` 中不处理 tool 类型消息，或让 `CLIUser.deliver` 对 TOOL_* 事件静默（因为 `status_notify` 已处理）
2. 或反过来，移除 `status_notify` 中的工具状态翻译，完全依赖 EventBus → CLIUser

---

## P1 — 重要Bug（功能异常/逻辑缺陷，可绕过但不应存在）

### P1-1: SharedBlackboardDB.get_with_meta 缺少 created_at 字段

**文件**: `agent_core/shared_blackboard_db.py`
**行号**: L120-L135（`get_with_meta` 方法）
**描述**: 数据库表有 `created_at` 列，但 `get_with_meta` 只返回 `{"value": ..., "agent_name": ...}`，缺少 `created_at`。`StructuredBlackboard.query_by_tag` 返回的结果也缺少时间信息。

**修复建议**: 在 `get_with_meta` 的 SELECT 中增加 `created_at`，返回 `{"value": ..., "agent_name": ..., "created_at": ...}`

---

### P1-2: _send_streaming_reply_with_sticker 群聊打字指示泄漏

**文件**: `qq_bot_adapter.py`
**行号**: `_send_streaming_reply_with_sticker` 方法
**描述**: 与 `_send_streaming_reply` 不同，`_send_streaming_reply_with_sticker` **在群聊中也会发送打字指示**（无 `if not is_group` 判断）。这会额外消耗 1 次群聊消息配额，可能导致超过 5 条限制。

**修复建议**: 在 `_send_streaming_reply_with_sticker` 中添加群聊判断，群聊不发打字指示（与 `_send_streaming_reply` 保持一致）

---

### P1-3: GreetingScheduler._tick 的 DND 补发可能重复触发

**文件**: `web/greeting_scheduler.py`
**行号**: L105-L115（`_tick` 方法中 deferred 补发逻辑）
**描述**: `has_deferred` 检查和 `pending, self._deferred = self._deferred, []` 清空不在同一个锁保护范围内（虽然有 `_deferred_lock`，但 `has_deferred = bool(self._deferred)` 和后续的 `with self._deferred_lock` 之间存在 TOCTOU 窗口）。更严重的是，`_tick` 每 30 秒执行一次，DND 结束后补发的问候被 `fire()` 执行，但 `fire()` 内部会 `_sent_today_count()` 检查上限——如果在 DND 结束的 tick 中 `_sent_today_count` 已达到上限，补发的问候会被跳过，但 `_deferred` 已被清空，**问候永久丢失**。

**修复建议**:
1. 将 deferred 补发放在 DND 结束后第一个 tick，如果配额已满则保留 deferred 不清空，等待下一个有配额的 tick
2. 或者：补发前先检查配额，配额不足时保留 deferred 不清空

---

### P1-4: SubAgent._execute_tool 检查了 guardrails 但未传递 user_id/safe_mode

**文件**: `agent_dispatcher.py`
**行号**: `_execute_tool` 方法（约 L530-L560）
**描述**: 子代理的工具执行路径 `self._tool_executor.execute(tool_name, args)` 缺少 `user_id` 和 `safe_mode` 参数，默认为空/False。这意味着：
1. 审计日志中 `user_id` 为空
2. 沙箱安全检查中 `safe_mode=False`，非主人的群聊消息通过子代理执行工具时跳过了沙箱限制

**修复建议**: 将 `user_id` 和 `safe_mode` 从 `SubAgent.chat` 传入 `_execute_tool`，并最终传给 `self._tool_executor.execute`

---

### P1-5: StructuredBlackboard.merge_from 不合并索引

**文件**: `agent_core/structured_blackboard.py`
**行号**: L64-L74（`merge_from` 方法）
**描述**: `merge_from` 通过 `other.get(key)` + `self.put(key, val)` 合并数据，但**不合并 `_tag_index` 和 `_direction_index`**。合并后的数据在 `self` 中没有标签/方向索引，`query_by_tag` / `query_by_direction` 查不到合并过来的条目。

**修复建议**: 在 `merge_from` 中，对 `other` 中每个 key 查询其 structured 元信息（需要 `other` 是 `StructuredBlackboard` 实例），将 tags/direction 索引也合并过来

---

### P1-6: message_processor._stream_llm_response 降级时丢弃已积累内容

**文件**: `agent_core/message_processor.py`
**行号**: `_stream_llm_response` 方法（约 L380-L400）
**描述**: 流式调用失败时降级到同步调用：`return await self.router.route(task_type, messages, **kwargs)`。注释说"丢弃已积累的部分流式内容"，但 `full_response` 列表中的内容完全被忽略。如果已经积累了 90% 的回复再降级，用户会看到重新等待和重复内容。

**修复建议**: 降级时将已积累的内容作为系统提示的一部分传给同步调用（如 `messages[-1]["content"] += "\n[已生成部分内容]\n" + "".join(full_response)`），或直接返回已积累的部分内容加截断提示

---

### P1-7: QQ Bot on_group_at_message_create 中未 bind QQUser 到 EventBus

**文件**: `qq_bot_adapter.py`
**行号**: `on_group_at_message_create` / `_process_group_reply` 方法
**描述**: C2C 路径 (`_process_c2c_reply`) 正确地 `bind_user(QQUser(...))` / `unbind_user()`，但群聊路径中**未找到 bind_user 的调用**。这导致群聊中子代理的 SUB_STARTED 等事件不会被投递到 QQ 端（虽然 QQUser 对大部分事件静默，但 SUB_STARTED 会发通知）。

**修复建议**: 在 `_process_group_reply` 中添加与 C2C 相同的 `bind_user` / `unbind_user` 逻辑

---

## P2 — 次要问题（代码质量/可维护性/潜在风险）

### P2-1: EmotionState._save 在主线程同步写文件

**文件**: `emotion/emotion_state.py`
**行号**: L170-L180（`_save` 方法）
**描述**: `_save` 在 `update()` 方法中同步调用，而 `update()` 可能在主事件循环中被调用。`write_text` 是阻塞 I/O，会短暂阻塞事件循环。

**修复建议**: 使用 `asyncio.to_thread(self._persist_path.write_text, ...)` 或将写入推迟到后台线程

---

### P2-2: SharedBlackboardDB 每个操作都执行 PRAGMA journal_mode=WAL

**文件**: `agent_core/shared_blackboard_db.py`
**行号**: 每个方法中的 `conn.execute("PRAGMA journal_mode=WAL")`
**描述**: WAL 模式是持久设置，只需在 `_init_db` 中设置一次。每次操作重复设置是冗余的，虽然不会出错但增加了不必要的开销。

**修复建议**: 仅在 `_init_db` 中设置一次 `PRAGMA journal_mode=WAL`

---

### P2-3: CLIUser/CLIEventBus 绑定缺少 ContextVar token 保护

**文件**: `cli.py` L189-L195, `web/ws_hub.py` L280-L290
**描述**: EventBus 使用全局 `ContextVar` `_current_user` 存储 User 绑定。`bind_user` / `unbind_user` 是 set(None) 模式，不如 ContextVar token 模式安全。虽然目前单用户场景下不会出问题，但如果未来支持多用户 CLI 或 WebSocket 并发处理，会出现 P0-3 同样的绑定泄漏。

**修复建议**: EventBus 的 `bind_user` 返回 token，`unbind_user` 接受 token 并 reset

---

### P2-4: SubAgent._chat_loop 中 _inject_dsml_if_needed 未定义

**文件**: `agent_dispatcher.py`
**行号**: `_chat_loop` 方法（约 L330）
**描述**: `tools = self._inject_dsml_if_needed(working, tools, is_reasoning, tool_names)` 调用了一个方法，但在截断的源码中未看到定义。如果此方法不存在，推理模型的子代理调用会抛 `AttributeError`。从代码结构推断它应该在 SubAgent 类中定义（可能在截断部分），但需要确认。

**修复建议**: 确认 `_inject_dsml_if_needed` 方法存在于 SubAgent 类中；如果缺失则实现它

---

### P2-5: ToolCallHandler._notify_tool_status 中 task_id 始终为空

**文件**: `tool_engine/tool_call_handler.py`
**行号**: L135-L155（`_notify_tool_status` 方法）
**描述**: `AgentEvent` 的 `task_id` 字段使用 `getattr(self, "_task_id", "")` 获取，但 `ToolCallHandler` 类从未设置 `_task_id` 属性。所有 TOOL_STARTED/COMPLETED/FAILED 事件的 `task_id` 始终为空字符串，无法关联到具体任务。

**修复建议**: 在 `handle()` 方法开始时生成 `self._task_id = gen_task_id(...)`，在 `handle()` 结束时清空

---

### P2-6: model_router.refresh_client 中旧客户端关闭使用 fire-and-forget

**文件**: `model_router.py`
**行号**: `refresh_client` 方法
**描述**: 旧客户端关闭使用 `loop.create_task(old.close())`，但未 `await` 该 task，也未保存引用。如果 `close()` 失败（网络错误等），异常会被静默吞掉；更严重的是，如果事件循环在 `close()` 完成前退出，连接会泄漏。

**修复建议**: 收集旧客户端引用，在方法末尾 `await asyncio.gather(*[c.close() for c in old_clients], return_exceptions=True)`

---

### P2-7: GreetingScheduler._deferred_lock 使用 threading.Lock 但在 async 上下文

**文件**: `web/greeting_scheduler.py`
**行号**: L30（`self._deferred_lock = threading.Lock()`）
**描述**: `_deferred_lock` 是 `threading.Lock`，但 `_tick` 是 `async` 方法且在事件循环中运行。`with self._deferred_lock:` 在协程中会阻塞事件循环。虽然临界区非常短（只操作列表），但在高并发场景下可能导致延迟。

**修复建议**: 改用 `asyncio.Lock`，或确认无跨线程访问后直接去掉锁（单事件循环内不需要线程锁）

---

### P2-8: agent_core/user_base.py 中 AGENT_DISPLAY 硬编码显示名

**文件**: `agent_core/user_base.py`
**行号**: L23-L29
**描述**: `AGENT_DISPLAY` 字典硬编码了子代理显示名（小莉/小狼/小涟/小可/小妲），但这些名称可由 `config/agents/*.json` 自定义。代码中其他位置已迁移到 `get_agent_display_name()` 动态获取，此处仍使用硬编码，名称变更时不同步。

**修复建议**: 将 `AGENT_DISPLAY` 改为动态获取（懒初始化），或在模块加载时从 config 读取

---

### P2-9: ws_hub.py 中 _handle_chat 缺少 image_data 错误处理

**文件**: `web/ws_hub.py`
**行号**: L260-L280（`_handle_chat` 方法中图片处理）
**描述**: 从文本中提取 `[Image: URL]` 并加载本地文件时，如果 `encode_image_to_base64` 返回空或异常，image_data 列表会包含无效条目（`{"mimeType": "", "data": ""}`），传给 `core.process` 后可能导致 LLM 收到空图片。

**修复建议**: 在 `encode_image_to_base64` 调用后检查返回值，跳过无效图片

---

### P2-10: config.py 中 ENV_PATH 的 .env 迁移逻辑在 Docker 中可能误触发

**文件**: `config.py`
**行号**: `get_env_path()` 函数
**描述**: frozen 模式下，如果旧安装目录有 `.env` 但用户目录没有，会 `shutil.copy2` 迁移。在 Docker 场景中，volume 挂载点可能初始为空，而镜像内 `.env` 存在，每次容器重启都会执行迁移。虽然 `copy2` 不会覆盖已有文件（因为目标 `.env` 不存在），但会打印 `[config] .env migrated from ... to ...` 日志噪音。

**修复建议**: 迁移后创建标记文件（如 `.env.migrated`），避免重复迁移

---

## 审计总结

| 优先级 | 数量 | 关键发现 |
|--------|------|----------|
| P0 | 5 | 黑板DB并发安全、QQ消息配额超限、EventBus绑定泄漏、结构化黑板内存泄漏、CLI双重输出 |
| P1 | 7 | DB字段缺失、群聊打字指示泄漏、DND补发丢失、子代理沙箱绕过、索引未合并、流式降级丢内容、群聊EventBus缺失 |
| P2 | 10 | 同步I/O阻塞、冗余PRAGMA、ContextVar安全、方法缺失、task_id为空、客户端关闭泄漏、锁类型不匹配、硬编码名称、图片错误处理、Docker迁移噪音 |

**最高风险项**: P0-3（EventBus 绑定泄漏，可导致并发请求间状态污染）、P0-2（QQ 消息配额超限，可导致消息发送失败/被封禁）
