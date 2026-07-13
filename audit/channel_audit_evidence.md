# Evidence File — 渠道适配+外围模块代码审计

审计日期：2026-07-12

---

## P0-1: SharedBlackboardDB 并发安全

**声明**: SharedBlackboardDB 每次操作新建 SQLite 连接，asyncio.Lock 不能保护跨进程并发写入
**证据源**: `agent_core/shared_blackboard_db.py` 全文
- L47-L59: `put` 方法每次 `sqlite3.connect()` + `conn.close()`
- L80-L96: `get` 方法同上
- L98-L118: `get_with_meta` 方法同上
- 类文档 L7-L9: "SQLite WAL 模式作为背板，多进程/多 worker 可安全共享数据"——但 asyncio.Lock 仅协程内有效
- 对比 `agent_core/shared_blackboard.py`: 使用 `asyncio.Lock` 但文档明确标注"不跨线程/跨进程共享"

## P0-2: QQ 5条消息限制

**声明**: C2C 流式输出无消息条数上限
**证据源**: `qq_bot_adapter.py`
- `_send_streaming_reply` 方法: 群聊用 `split_for_group_passive`（有上限），C2C 用 `_split_text_for_streaming(chunk_size=300)`（无上限）
- L510: `if not is_group:` 判断只在群聊不发打字提示，C2C 无条数保护
- QQ 官方文档: 被动回复 + 主动消息共 5 条/轮

## P0-3: EventBus 绑定泄漏

**声明**: ContextVar set(None) 模式在并发协程间不安全
**证据源**: `core/event_bus.py`
- L57-L60: `bind_user` 用 `_current_user.set(user)`，`unbind_user` 用 `_current_user.set(None)`
- `qq_bot_adapter.py` `_process_c2c_reply`: try/finally 中 bind/unbind，如果 bind 前异常，unbind 会清除其他协程的绑定
- 对比 `agent_core/_shared.py`: `_current_request_ctx` 使用 token 模式（L17-L18: `ContextVar` + `reset(token)`）

## P0-4: StructuredBlackboard 索引内存泄漏

**声明**: tag_index/direction_index 不随数据过期清理
**证据源**: `agent_core/structured_blackboard.py`
- L20-L25: `_tag_index` / `_direction_index` 定义
- L42-L48: `put_structured` 只添加索引，从不清理
- 父类 `SharedBlackboard.cleanup_expired()` 只清理 `_store`，不知晓子类索引

## P0-5: CLI 双重输出

**声明**: CLIUser.deliver 和 status_notify 都会打印工具状态
**证据源**: `cli.py` L189-L195
- `event_bus.bind_user(CLIUser())` → 子代理事件会触发 CLIUser.deliver 打印
- `status_callback=status_notify` → 工具状态也会触发 `_status_translate` 打印
- `agent_core/user_cli.py`: CLIUser.deliver 对 TOOL_STARTED/COMPLETED/FAILED 都有 print

## P1-7: 群聊 EventBus 绑定缺失

**声明**: 群聊路径未 bind QQUser
**证据源**: `qq_bot_adapter.py`
- `_process_c2c_reply`: 有 `event_bus.bind_user(QQUser(...))` / `event_bus.unbind_user()`
- `_process_group_reply` 或 `on_group_at_message_create`: 在可读取的代码段中未找到对应的 bind_user 调用

## P1-4: 子代理工具执行缺少 safe_mode

**声明**: SubAgent._execute_tool 未传 user_id/safe_mode
**证据源**: `agent_dispatcher.py`
- `_execute_tool` 方法: `result = await self._tool_executor.execute(tool_name, args)` — 无 user_id/safe_mode
- 对比 `tool_engine/tool_executor.py`: `execute(tool_name, arguments, user_id="", safe_mode=False)` — 参数存在但默认值宽松

## P2-5: ToolCallHandler task_id 为空

**声明**: _task_id 属性从未设置
**证据源**: `tool_engine/tool_call_handler.py` L145
- `task_id=getattr(self, "_task_id", "")` — 始终返回 ""
- ToolCallHandler.__init__ 无 `_task_id` 属性赋值
