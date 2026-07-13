# nahida-agent 核心调度层代码质量审计报告

**审计范围**: EventBus + CancelToken + Router + User 核心调度链路  
**审计日期**: 2026-07-12  
**Python 版本**: 3.11+ (TimeoutError ⊂ Exception; asyncio.TimeoutError is TimeoutError)

---

## 发现汇总

| 等级 | 数量 | 说明 |
|------|------|------|
| P1   | 4    | 运行时崩溃风险 / 语义不一致 |
| P2   | 7    | 性能损耗 / 代码质量 / 边缘情况 |

---

## P1 — 运行时崩溃风险或语义不一致

### BUG-01: 超时事件类型不一致 — `_dispatch_single_sub_agent` vs `_parallel_run_one`

| 项目 | 内容 |
|------|------|
| 文件 | `agent_core/sub_agent_manager.py` |
| 行号 | L113-121 vs L386-391 |
| 描述 | 同一条件（`asyncio.wait_for` 超时）在两条路径中发射了不同事件类型。`_dispatch_single_sub_agent` 在 `except TimeoutError` 中发射 `SUB_CANCELLED`（L116-121），而 `_parallel_run_one` 在 `except TimeoutError` 中发射 `SUB_FAILED`（L387-391）。下游消费者（UI 通知、日志聚合、BeliefRouter 反馈）会收到语义矛盾的信号：同是超时，有时是"已取消"，有时是"已失败"。此外 data 字典 key 也不一致：前者用 `"reason"`，后者用 `"error"`。 |
| 修复 | 统一为 `SUB_CANCELLED`（超时 = 主动取消的一种），统一 data key 为 `"reason"`。或在 `_parallel_run_one` 中补 `CancelToken` 使语义对齐。 |

### BUG-02: `CancelToken.__init__` 中 `asyncio.create_task` 无运行中事件循环时崩溃

| 项目 | 内容 |
|------|------|
| 文件 | `core/cancel_token.py` |
| 行号 | L47-48 |
| 描述 | `CancelToken.__init__` 在 `timeout > 0` 时直接调用 `asyncio.create_task(self._timeout_watch())`。若实例化发生在 async 函数之外（如模块顶层、同步工厂函数中事件循环尚未启动），`asyncio.create_task` 抛出 `RuntimeError: no running event loop`。文档示例 `CancelToken(timeout=60.0)` 正是这种触发路径。当前 `sub_agent_manager.py` 使用 `timeout=None` 绕过了此问题，但 API 契约本身有陷阱。 |
| 修复 | 将 timer 创建延迟到首次 `check()` 或新增 `async start()` 方法中调用；或在 `__init__` 中用 `try/except RuntimeError` 兜底，改用 `loop.call_later` 延迟到循环可用时注册。 |

### BUG-03: `QQUser.deliver` 以关键字参数调用 `Callable`，类型签名不保证参数名

| 项目 | 内容 |
|------|------|
| 文件 | `agent_core/user_qq.py` |
| 行号 | L39 |
| 描述 | `await self._reply_fn(content=content, msg_seq=self._msg_seq_fn())` 以关键字参数 `content=`/`msg_seq=` 调用 `_reply_fn`。但 `_reply_fn` 的类型声明为 `Callable[[str, int], Awaitable[None]]`，该类型 **不约束参数名**。若实际传入函数签名为 `async def reply(text: str, seq: int)`，运行时抛出 `TypeError: reply() got an unexpected keyword argument 'content'`。`deliver()` 内的 `except Exception` 会吞掉此错误并以 debug 级别日志输出，导致 QQ 端静默丢失通知且极难排查。 |
| 修复 | (a) 改用 `Protocol` 定义 `class ReplyFn(Protocol): async def __call__(self, content: str, msg_seq: int) -> None: ...`，从类型层面约束参数名；(b) 或在调用处改为位置参数 `await self._reply_fn(content, self._msg_seq_fn())`，与 `Callable[[str, int], ...]` 语义一致。 |

### BUG-04: `_dispatch_single_sub_agent` 不可用 agent 早退时无事件通知

| 项目 | 内容 |
|------|------|
| 文件 | `agent_core/sub_agent_manager.py` |
| 行号 | L66-67 |
| 描述 | `if not sub_agent or not sub_agent.available:` 直接返回 `ProcessResult`，未发射任何 `AgentEvent`。对比：正常路径发射 `SUB_STARTED`→`SUB_COMPLETED/FAILED/CANCELLED`，而此处 UI 端（CLI/Web/QQ）完全无感知，用户只看到最终回复，无中间状态提示。当 `sub_agent.available=False` 时，用户可能误以为系统卡死。 |
| 修复 | 在早退前发射 `AgentEvent(type=AgentEventType.SUB_FAILED, agent=target, data={"error": "unavailable"})` ，或在返回前通知 status callback。 |

---

## P2 — 性能损耗 / 代码质量 / 边缘情况

### BUG-05: `delegate_to_agent` 名字检查元组重复

| 项目 | 内容 |
|------|------|
| 文件 | `agent_core/sub_agent_manager.py` |
| 行号 | L538 |
| 描述 | `if name in ("xiaoli", "xiaoli"):` — 元组中两个元素完全相同，第二个 `"xiaoli"` 几乎是复制粘贴残留。参照 `_DEFAULT_MENTION_MAP` 中 `"@可莉": "xiaoli"` 和 `"@小莉": "xiaoli"` 的别名模式，此处可能本意是 `("xiaoli", "可莉")` 或直接简化为 `name == "xiaoli"`。 |
| 修复 | 改为 `if name == "xiaoli":` 或补入正确别名。 |

### BUG-06: `gen_task_id` 的 `input_hint` 参数未使用

| 项目 | 内容 |
|------|------|
| 文件 | `core/event_bus.py` |
| 行号 | L96 |
| 描述 | `def gen_task_id(agent: str, input_hint: str = "") -> str:` — `input_hint` 在函数体内从未被引用。调用方也从未传入此参数。若未来需要基于输入内容生成可辨识的 task_id，需重新引入；否则应移除以消除误导。 |
| 修复 | 移除 `input_hint` 参数，或在 task_id 中拼接 `hashlib.md5(input_hint)[:4]` 使 ID 具备输入可溯性。 |

### BUG-07: `RouterEngine.decide` 每次调用重建正则模式

| 项目 | 内容 |
|------|------|
| 文件 | `core/router_engine.py` |
| 行号 | L171, L212, L231 |
| 描述 | `_build_negative_patterns()`、`_build_keyword_patterns()`、`_build_mention_map()` 在 `decide()` 和 `_match_mentions()` 中每次调用都重建。每个函数内部 `from config import get_agent_display_name` + 构建正则。在高频请求场景下产生不必要的重复计算与临时对象。 |
| 修复 | 在 `RouterEngine.__init__` 中缓存构建结果；或用 `@functools.lru_cache` 装饰（需确保 config 不热更新）。模块级 `MENTION_MAP` 已构建一次但 `decide()` 未复用它。 |

### BUG-08: `_match_mentions` 不去重，可产生重复 agent 调度

| 项目 | 内容 |
|------|------|
| 文件 | `core/router_engine.py` |
| 行号 | L228-234 |
| 描述 | `for mention, agent in _build_mention_map().items(): if mention in user_input: targets.append(agent)` — 若用户输入包含同一 agent 的多个别名（如 `"@小莉 @可莉"`），`targets` 为 `["xiaoli", "xiaoli"]`，导致 `RoutingDecision(mode="parallel", agent_names=["xiaoli", "xiaolia"])` 对同一子代理并行发两次相同请求。 |
| 修复 | 返回 `list(dict.fromkeys(targets))` 保序去重，或用 `set` 去重。 |

### BUG-09: `_build_sub_agent_context` 访问私有属性 `_compressed_summary`

| 项目 | 内容 |
|------|------|
| 文件 | `agent_core/sub_agent_manager.py` |
| 行号 | ~L735 (in `_build_sub_agent_context`) |
| 描述 | `if self.context._compressed_summary:` 直接访问 `Context` 对象的私有属性。若 `Context` 重构重命名此属性（合理的内部演进），此处静默断裂，无任何类型检查或运行时警告。 |
| 修复 | 在 `Context` 类上暴露 `@property compressed_summary`，或通过 `getattr(self.context, "_compressed_summary", "")` 防御性访问。 |

### BUG-10: `CancelToken.is_cancelled` 属性有副作用

| 项目 | 内容 |
|------|------|
| 文件 | `core/cancel_token.py` |
| 行号 | L60-66 |
| 描述 | `is_cancelled` property 在读取时会修改 `_cancelled` 和 `_reason`（L63-65: fallback timeout 检测）。这违反属性只读惯例：调试器、日志框架、条件断点读取 `is_cancelled` 会触发状态变更，导致不可重现的行为。 |
| 修复 | 将 fallback timeout 检测逻辑移到 `check()` 方法中，`is_cancelled` 只读不写；或在 `__init__` 中确保 `_timeout_watch` 是唯一的超时来源，移除 property 中的 fallback。 |

### BUG-11: `belief_router._save_to_db` 的 `run_in_executor` Future 未 await

| 项目 | 内容 |
|------|------|
| 文件 | `belief_router.py` |
| 行号 | L174 (`loop.run_in_executor(None, _do_save)`) |
| 描述 | `run_in_executor` 返回的 `Future` 被丢弃（未 await / 未 add_done_callback）。若进程在 executor 线程完成前退出，最后一次信念更新会丢失。`_do_save` 内部虽有 try/except，但异常仅在线程中日志输出，主循环无感知。高频 `update_belief` 调用还会在 ThreadPoolExecutor 中堆积未限流的任务。 |
| 修复 | 用 `asyncio.create_task(loop.run_in_executor(...))` 包装以便追踪，或收集 Future 到队列中批量等待；至少添加 `future.add_done_callback` 做异常兜底日志。 |

---

## 审计边界说明

- **未审计文件**: `BehavioralSignalStream`、`DirectionRegistry`、`Context`、`Dispatcher` 等非列表内依赖的实现细节
- **已确认非问题**:
  - `_enhanced_router` 在 `belief_router.py` 中初始化为 `None` 并非死代码 — `j_space_bootstrap._wire_hooks()` 在运行时通过 `import belief_router as _br; _br._enhanced_router = _enhanced_router` 注入实例
  - `BehavioralSignalStream.aggregate()` 对空数据返回 `0.0`（非 `None`），不会在 `enhanced_router.py` 中引发 `TypeError`
  - `ContextVar _current_user` 的 per-task 隔离语义正确，子任务继承绑定是预期行为
- **Python 版本假设**: 基于 3.11+，`TimeoutError ⊂ Exception`，`asyncio.TimeoutError is TimeoutError`
