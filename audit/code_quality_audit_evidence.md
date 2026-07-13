# 审计证据文件 — nahida-agent 核心调度层

**生成日期**: 2026-07-12

---

## E-01: 超时事件类型不一致

**声明**: `_dispatch_single_sub_agent` 超时发 `SUB_CANCELLED`，`_parallel_run_one` 超时发 `SUB_FAILED`

**来源**: `agent_core/sub_agent_manager.py`

路径1 — L113-121:
```python
except TimeoutError:
    token.cancel("timeout")
    await event_bus.emit(AgentEvent(
        type=AgentEventType.SUB_CANCELLED,
        agent=target,
        task_id=task_id,
        data={"reason": "timeout"},
    ))
```

路径2 — L386-391:
```python
except TimeoutError:
    await event_bus.emit(AgentEvent(
        type=AgentEventType.SUB_FAILED,
        agent=t,
        task_id=task_id,
        data={"error": "timeout"},
    ))
```

差异: 事件类型 `SUB_CANCELLED` vs `SUB_FAILED`；data key `"reason"` vs `"error"`

---

## E-02: CancelToken.__init__ 中 asyncio.create_task 崩溃风险

**声明**: `asyncio.create_task` 在无运行中事件循环时抛出 `RuntimeError`

**来源**: `core/cancel_token.py` L47-48

```python
def __init__(self, timeout: Optional[float] = 60.0) -> None:
    ...
    if timeout is not None and timeout > 0:
        self._timer_task = asyncio.create_task(self._timeout_watch())
```

验证 — Python 3.11+ 行为:
```python
>>> import asyncio
>>> asyncio.create_task(asyncio.sleep(1))
RuntimeError: no running event loop
```

当前调用方使用 `timeout=None`（L83 of sub_agent_manager.py），但文档示例 `CancelToken(timeout=60.0)` 和测试 `tests/test_cancel_token.py` 使用 timeout>0。

---

## E-03: QQUser 关键字参数调用风险

**声明**: `Callable[[str, int], Awaitable[None]]` 不约束参数名，关键字调用可能 TypeError

**来源**: `agent_core/user_qq.py` L26, L39

类型声明:
```python
reply_fn: Callable[[str, int], Awaitable[None]],
```

调用:
```python
await self._reply_fn(content=content, msg_seq=self._msg_seq_fn())
```

若实际函数签名为 `async def reply(text: str, seq: int)`:
```python
>>> reply(content="msg", msg_seq=1)
TypeError: reply() got an unexpected keyword argument 'content'
```

`deliver()` 的 `except Exception` 会以 debug 级别吞掉此错误，QQ端静默丢失通知。

---

## E-04: 不可用 agent 早退无事件通知

**声明**: agent 不可用时直接返回，未发射 AgentEvent

**来源**: `agent_core/sub_agent_manager.py` L66-67

```python
sub_agent = self.dispatcher.get_agent(target)
if not sub_agent or not sub_agent.available:
    return ProcessResult(reply=f"{sub_agent.config.display_name if sub_agent else target}现在有点累了...等会儿再来吧！💤")
```

对比正常路径 L71-76 必先发射 `SUB_STARTED`：
```python
await event_bus.emit(AgentEvent(
    type=AgentEventType.SUB_STARTED,
    ...
))
```

---

## E-05: delegate_to_agent 名字检查元组重复

**声明**: `("xiaoli", "xiaoli")` 重复元素

**来源**: `agent_core/sub_agent_manager.py` L538

```python
if name in ("xiaoli", "xiaoli"):
    return await self.delegate_to_xiaoli(task)
```

参照 `_DEFAULT_MENTION_MAP` 中 `"@可莉": "xiaoli"` 别名模式，此处应为 `("xiaoli", "可莉")` 或简化为 `name == "xiaoli"`。

---

## E-06: gen_task_id 未使用 input_hint 参数

**声明**: `input_hint` 参数在函数体中未被引用

**来源**: `core/event_bus.py` L96

```python
def gen_task_id(agent: str, input_hint: str = "") -> str:
    return f"{agent}_{uuid.uuid4().hex[:8]}"
```

---

## E-07: RouterEngine.decide 每次调用重建正则

**声明**: `_build_negative_patterns()`, `_build_keyword_patterns()`, `_build_mention_map()` 在每次 decide() 调用时重建

**来源**: `core/router_engine.py` L171, L212, L231

```python
# L171
for pat in _build_negative_patterns():
# L212
for pattern, target in _build_keyword_patterns():
# L231
for mention, agent in _build_mention_map().items():
```

每个函数内部含 `from config import get_agent_display_name` + 正则构建。

---

## E-08: _match_mentions 不去重

**声明**: 同一 agent 的多个别名可导致 targets 列表重复

**来源**: `core/router_engine.py` L228-234

```python
@staticmethod
def _match_mentions(user_input: str) -> list[str]:
    targets = []
    for mention, agent in _build_mention_map().items():
        if mention in user_input:
            targets.append(agent)
    return targets
```

`_DEFAULT_MENTION_MAP` 中 `"@小莉": "xiaoli"` 和 `"@可莉": "xiaoli"` 同时存在。用户输入 `"@小莉 @可莉"` → targets = `["xiaoli", "xiaoli"]` → `RoutingDecision(mode="parallel")` → 对同一 agent 并行调度两次。

---

## E-09: _build_sub_agent_context 访问私有属性

**来源**: `agent_core/sub_agent_manager.py` (in `_build_sub_agent_context`)

```python
if self.context._compressed_summary:
    parts.append(f"[早期对话摘要]\n{self.context._compressed_summary[:300]}")
```

跨模块直接访问 `._compressed_summary`，无类型检查保障。

---

## E-10: CancelToken.is_cancelled 属性有副作用

**来源**: `core/cancel_token.py` L60-66

```python
@property
def is_cancelled(self) -> bool:
    if self._timeout is not None and self._timeout > 0:
        if not self._cancelled and time.monotonic() - self._created_at > self._timeout:
            self._cancelled = True          # <-- 副作用: 修改状态
            self._reason = f"timeout({self._timeout}s)"  # <-- 副作用
    return self._cancelled
```

---

## E-11: belief_router._save_to_db Future 未 await

**来源**: `belief_router.py` L174

```python
loop.run_in_executor(None, _do_save)  # Future 被丢弃
```

`run_in_executor` 返回 `concurrent.futures.Future`，未 await 或 add_done_callback。进程退出时最后一次写入可丢失。
