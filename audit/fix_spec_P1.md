# nahida-agent P1 级 Bug 修复规格文档

**项目**: nahida-agent — 本地部署单用户 Agent，Python 异步  
**完成日期**: 2026-07-12  
**文档版本**: v1.0  
**Bug 总数**: 27 个 P1 级  
**模块分组**: 核心调度层(4) / 记忆系统层(9) / J-Space 认知层(7) / 渠道适配层(7)

---

## 目录

- [一、核心调度层（4 个 P1）](#一核心调度层4-个-p1)
- [二、记忆系统层（9 个 P1）](#二记忆系统层9-个-p1)
- [三、J-Space 认知层（7 个 P1）](#三j-space-认知层7-个-p1)
- [四、渠道适配层（7 个 P1）](#四渠道适配层7-个-p1)
- [附录：跨模块模式索引](#附录跨模块模式索引)

---

## 一、核心调度层（4 个 P1）

> 来源：`audit/code_quality_audit_report.md`

---

### BUG-01: 超时事件类型不一致

| 项目 | 内容 |
|------|------|
| **文件** | `agent_core/sub_agent_manager.py` |
| **行号** | L113–121 vs L386–391 |
| **严重度** | P1 — 下游消费者收到语义矛盾信号 |

**问题描述**：同一条件（`asyncio.wait_for` 超时）在两条路径中发射了不同事件类型：`_dispatch_single_sub_agent` 发射 `SUB_CANCELLED`，`_parallel_run_one` 发射 `SUB_FAILED`。此外 data 字典 key 不一致：前者用 `"reason"`，后者用 `"error"`。

**根因分析**：两处代码独立编写，缺少统一超时处理约定。超时语义上属于"主动取消"（`SUB_CANCELLED`），而非"执行失败"（`SUB_FAILED`）。

**修复方案**：

```python
# ── 文件：agent_core/sub_agent_manager.py ──

# 改动 1：L386–391 _parallel_run_one 中的 except TimeoutError 块
# 当前代码（错误）：
#   await self._emit(AgentEventType.SUB_FAILED, agent=target,
#                    data={"error": f"timeout ({timeout}s)"})

# 修改为：
except TimeoutError:
    await self._emit(AgentEventType.SUB_CANCELLED, agent=target,
                     data={"reason": f"timeout ({timeout}s})"})
```

**验证方法**：在 `_dispatch_single_sub_agent` 和 `_parallel_run_one` 超时路径中，断言事件类型为 `SUB_CANCELLED` 且 data key 为 `"reason"`。

**参考来源**：[asyncio.wait_for 超时语义](https://docs.python.org/3/library/asyncio-task.html#asyncio.wait_for)

---

### BUG-02: CancelToken.__init__ 中 asyncio.create_task 无运行中事件循环时崩溃

| 项目 | 内容 |
|------|------|
| **文件** | `core/cancel_token.py` |
| **行号** | L47–48 |
| **严重度** | P1 — 实例化于 async 函数外时 RuntimeError |

**问题描述**：`CancelToken.__init__` 在 `timeout > 0` 时直接调用 `asyncio.create_task(self._timeout_watch())`。若实例化发生在 async 函数之外（模块顶层、同步工厂函数），抛出 `RuntimeError: no running event loop`。

**根因分析**：`asyncio.create_task()` 要求当前线程有正在运行的 event loop，`__init__` 可在任意上下文中被调用。

**修复方案**：

```python
# ── 文件：core/cancel_token.py ──

# 改动：L47–48，将直接 create_task 改为延迟注册模式

# 当前代码（错误）：
#   if timeout > 0:
#       asyncio.create_task(self._timeout_watch())

# 修改为：
def __init__(self, timeout: float | None = None, reason: str = ""):
    self._cancelled = False
    self._reason = reason
    self._timeout = timeout
    self._timeout_task: asyncio.Task | None = None

    if timeout is not None and timeout > 0:
        try:
            loop = asyncio.get_running_loop()
            self._timeout_task = loop.create_task(self._timeout_watch())
        except RuntimeError:
            # 无运行中事件循环，延迟到首次 check() 或 start() 时注册
            self._timeout_task = None

async def ensure_started(self) -> None:
    """确保超时守卫已启动。在 async 上下文中调用一次即可。"""
    if self._timeout and self._timeout_task is None and not self._cancelled:
        self._timeout_task = asyncio.create_task(self._timeout_watch())
```

**验证方法**：在同步上下文中实例化 `CancelToken(timeout=5.0)` 不崩溃；随后在 async 上下文中调用 `await token.ensure_started()` 后超时正常触发。

**参考来源**：[Fixdevs — asyncio RuntimeError no running event loop](https://fixdevs.com/blog/python-asyncio-runtime-error-no-running-event-loop/)

---

### BUG-03: QQUser.deliver 以关键字参数调用 Callable，类型签名不保证参数名

| 项目 | 内容 |
|------|------|
| **文件** | `agent_core/user_qq.py` |
| **行号** | L39 |
| **严重度** | P1 — 实际传入函数参数名不匹配时 TypeError 被静默吞掉 |

**问题描述**：`await self._reply_fn(content=content, msg_seq=self._msg_seq_fn())` 以关键字参数调用 `_reply_fn`，但 `_reply_fn` 类型声明为 `Callable[[str, int], Awaitable[None]]`，该类型不约束参数名。若实际函数签名为 `async def reply(text: str, seq: int)`，运行时抛出 `TypeError`，被 `except Exception` 吞掉导致 QQ 端静默丢通知。

**根因分析**：`Callable` 类型仅约束参数数量和类型，不约束参数名。使用关键字参数调用时依赖参数名匹配，但类型系统无法保障这一点。

**修复方案**（双管齐下）：

```python
# ── 文件：agent_core/user_qq.py ──

# 改动 1：L39 调用处改为位置参数（最小改动）
# 当前代码（错误）：
#   await self._reply_fn(content=content, msg_seq=self._msg_seq_fn())

# 修改为：
await self._reply_fn(content, self._msg_seq_fn())

# 改动 2：类型声明从 Callable 改为 Protocol（长期类型安全）
# 当前代码（不足）：
#   _reply_fn: Callable[[str, int], Awaitable[None]]

# 修改为：
from typing import Protocol

class ReplyFn(Protocol):
    async def __call__(self, content: str, msg_seq: int) -> None: ...

class QQUser:
    def __init__(self, reply_fn: ReplyFn, ...):
        self._reply_fn = reply_fn
```

**验证方法**：mypy 检查 `ReplyFn` 实现是否匹配参数名；单元测试传入 `async def reply(text, seq)` 时验证位置参数调用不报错。

**参考来源**：[typing.python.org — Protocol callables](https://typing.python.org/en/latest/spec/callables.html)

---

### BUG-04: _dispatch_single_sub_agent 不可用 agent 早退时无事件通知

| 项目 | 内容 |
|------|------|
| **文件** | `agent_core/sub_agent_manager.py` |
| **行号** | L66–67 |
| **严重度** | P1 — UI 端无感知，用户误以为系统卡死 |

**问题描述**：`if not sub_agent or not sub_agent.available:` 直接返回 `ProcessResult`，未发射任何 `AgentEvent`。正常路径发射 `SUB_STARTED→SUB_COMPLETED/FAILED/CANCELLED`，此处 UI 端完全无感知。

**根因分析**：早退路径遗漏了事件通知，缺少与正常路径对齐的事件协议。

**修复方案**：

```python
# ── 文件：agent_core/sub_agent_manager.py ──

# 改动：L66–67，在早退前发射 SUB_FAILED 事件
# 当前代码（缺失事件）：
#   if not sub_agent or not sub_agent.available:
#       return ProcessResult(...)

# 修改为：
if not sub_agent or not sub_agent.available:
    await self._emit(
        AgentEventType.SUB_FAILED,
        agent=target,
        data={"error": f"agent unavailable: {target}"}
    )
    return ProcessResult(...)
```

**验证方法**：设置子代理 `available=False`，断言 EventBus 收到 `SUB_FAILED` 事件。

**参考来源**：项目内 `AgentEventType` 枚举定义与已有事件发射模式

---

## 二、记忆系统层（9 个 P1）

> 来源：`audit_fsrs_dsr_report.md`

---

### Bug #6: FluidMemory.score() 缺失 difficulty 字段

| 项目 | 内容 |
|------|------|
| **文件** | `memory/fluid_memory.py` |
| **行号** | L30–55 |
| **严重度** | P1 — 兼容层评分偏差 |

**问题描述**：`score()` 方法从 `access_count` 和 `created_at` 重建 MemoryState，但 `difficulty` 始终使用默认值 5.0。旧 FluidMemory 没有 difficulty 概念，兼容层按 5.0 计算合理——但如果调用方已将真实 difficulty 存入 DB 并期望读取，该兼容层会丢弃它。此外线性 S 公式与 FSRS 的指数衰减 R(t) 在高 access_count 时不匹配。

**根因分析**：兼容层未考虑 DB 已存储 FSRS 状态的场景，默认 5.0 是折中值但非精确值。

**修复方案**：

```python
# ── 文件：memory/fluid_memory.py ──

# 改动：score() 方法签名增加可选 MemoryState 参数，优先使用已有 FSRS 状态
# 当前代码（不足）：
#   def score(self, similarity: float, peak_weight: float = 1.0) -> float:

# 修改为：
def score(self, similarity: float, peak_weight: float = 1.0,
          fsrs_state: MemoryState | None = None) -> float:
    if fsrs_state is not None:
        # 优先使用已有 FSRS 状态
        D = fsrs_state.difficulty
        S = fsrs_state.stability
        R = fsrs_state.retrievability(time.time())
        return similarity * peak_weight * R

    # fallback：兼容层线性估算（旧逻辑保留）
    access_count = self.access_count
    stability = min(S_INIT + access_count * STABILITY_PER_ACCESS, 300.0)
    difficulty = 5.0  # 兼容默认值
    ...
```

**验证方法**：传入已有 `MemoryState(stability=30, difficulty=7)` 时，`score()` 使用 FSRS R(t) 而非线性估算；无 fsrs_state 时退化为旧逻辑。

**参考来源**：[FSRS-4.5 Algorithm — open-spaced-repetition wiki](https://github-wiki-see.page/m/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm)

---

### Bug #7: FluidMemory.is_permanent() 与 FSRS transition() 判定不一致

| 项目 | 内容 |
|------|------|
| **文件** | `memory/fluid_memory.py` |
| **行号** | L57 |
| **严重度** | P1 — 两套判定逻辑导致行为分歧 |

**问题描述**：`is_permanent()` 仅看 `access_count >= 5`，而 FSRS 的 PERMANENT 过渡条件是 `stability >= S_PERMANENT(30.0) AND reinforcement_count > 0 AND age > BUFFER_DAYS(21)`。两套判定可产生矛盾。

**根因分析**：`is_permanent()` 是旧 FluidMemory 的简化判定，未与 FSRS 四杆框架对齐。

**修复方案**：

```python
# ── 文件：memory/fluid_memory.py ──

# 改动：废弃 is_permanent()，委托给 FSRSModel.transition()
# 当前代码（错误）：
#   def is_permanent(self) -> bool:
#       return self.access_count >= 5

# 修改为：
def is_permanent(self, fsrs_model: "FSRSModel | None" = None) -> bool:
    """判断记忆是否已达永久状态。优先使用 FSRS transition 判定。"""
    if fsrs_model is not None:
        state = self._build_memory_state()
        new_phase = fsrs_model.transition(state, time.time())
        return new_phase == MemoryPhase.PERMANENT
    # fallback：旧逻辑（向后兼容）
    return self.access_count >= 5
```

**验证方法**：`access_count=3, stability=35` 时 `is_permanent(fsrs_model)` 返回 `True`（FSRS 判定），不带参数返回 `False`（旧逻辑）。

**参考来源**：[deepwiki FSRS — forgetting curve mathematics](https://deepwiki.com/open-spaced-repetition/fsrs-optimizer/7.2-forgetting-curve-mathematics)

---

### Bug #8: _apply_recall/_apply_forget 双重构造 MemoryState

| 项目 | 内容 |
|------|------|
| **文件** | `memory/fsrs_model.py` |
| **行号** | L135–146, L155–166 |
| **严重度** | P1 — reinforcement_count 双重 +1 巧合正确但脆弱；两次构造浪费 |

**问题描述**：两个方法都先构造临时 MemoryState 调用 `.transition(now)` 获取新 phase，然后重新构造最终 MemoryState。临时对象的 `reinforcement_count` 在 `_apply_recall` 中被 +1 了两次（L139 和 L145），当前结果恰好正确但脆弱。

**根因分析**：缺少提取 phase 计算的辅助方法，导致每个状态转换方法都要完整构造 MemoryState 才能调用 `transition()`。

**修复方案**：

```python
# ── 文件：memory/fsrs_model.py ──

# 改动：提取 _compute_phase 辅助方法，消除双重构造

def _compute_phase(self, D: float, S: float, state: MemoryState,
                   now: float) -> MemoryPhase:
    """根据新的 D/S 和当前状态计算目标 phase，无需构造完整 MemoryState。"""
    age_days = (now - state.created_at) / 86400.0
    if S >= S_PERMANENT and state.reinforcement_count > 0 and age_days > BUFFER_DAYS:
        return MemoryPhase.PERMANENT
    elif state.reinforcement_count > 0 and age_days > BUFFER_DAYS:
        return MemoryPhase.REINFORCED
    elif age_days > BUFFER_DAYS:
        return MemoryPhase.DECAY
    else:
        return MemoryPhase.BUFFER

# _apply_recall 改为（L135–146）：
def _apply_recall(self, state: MemoryState, signal: RecallSignal,
                  now: float) -> MemoryState:
    D_new = self._update_difficulty(state.difficulty, signal)
    S_new = self._apply_stability_growth(state, signal, now)
    new_phase = self._compute_phase(D_new, S_new, state, now)
    rc = state.reinforcement_count + 1
    return MemoryState(
        difficulty=D_new, stability=S_new, phase=new_phase,
        last_review=now, created_at=state.created_at,
        reinforcement_count=rc
    )

# _apply_forget 改为（L155–166）：
def _apply_forget(self, state: MemoryState, now: float) -> MemoryState:
    D_new = self._update_difficulty(state.difficulty, RecallSignal.FORGET)
    S_new = S_INIT * (D_new ** (-0.3)) * (((state.stability + 1.0) ** 0.2) - 1.0)  # Bug #1 修复
    S_new = max(1.0, S_new)
    new_phase = self._compute_phase(D_new, S_new, state, now)
    return MemoryState(
        difficulty=D_new, stability=S_new, phase=new_phase,
        last_review=now, created_at=state.created_at,
        reinforcement_count=state.reinforcement_count  # 遗忘不增加
    )
```

**验证方法**：单元测试 `_apply_recall` 后 `reinforcement_count` 精确 +1（非 +2）；`_apply_forget` 后 `reinforcement_count` 不变。

**参考来源**：[FSRS-4.5 stability_after_failure 公式](https://github-wiki-see.page/m/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm)

---

### Bug #9: _apply_fsrs_scoring 每次新建 FSRSModel

| 项目 | 内容 |
|------|------|
| **文件** | `memory/memory_manager.py` |
| **行号** | L1480 |
| **严重度** | P1 — 热路径性能浪费 |

**问题描述**：每次检索评分都 `FSRSModel()` 实例化。FSRSModel 是无状态纯计算类，重复创建无意义。一次检索可能对 50+ 条记忆调用此方法。

**根因分析**：未在类级别缓存 FSRSModel 实例。

**修复方案**：

```python
# ── 文件：memory/memory_manager.py ──

# 改动 1：__init__ 中创建复用实例
class MemoryManager:
    def __init__(self, ...):
        ...
        self._fsrs = FSRSModel()  # 新增：复用实例

# 改动 2：L1480，_apply_fsrs_scoring 中不再新建
# 当前代码（浪费）：
#   fsrs = FSRSModel()
#   result = fsrs.score(...)

# 修改为：
    result = self._fsrs.score(...)
```

**验证方法**：基准测试 50 次评分，验证不再创建 50 个 FSRSModel 实例。

**参考来源**：无状态纯计算类的实例复用是 Python 基本优化模式

---

### Bug #10: encode_memory 不初始化新记忆的 FSRS 状态

| 项目 | 内容 |
|------|------|
| **文件** | `memory/memory_manager.py` |
| **行号** | L1651–1760 |
| **严重度** | P1 — 新写入记忆 `last_review=0`，FSRS 评分 R≈0 被过滤 |

**问题描述**：`encode_memory` 插入新记忆时不设置 difficulty/stability/phase/last_review/reinforcement_count，完全依赖数据库 DEFAULT 值（difficulty=5.0, stability=3.0, phase='buffer', last_review=0, reinforcement_count=0）。`last_review=0`（Unix epoch）导致 `age_days ≈ 20500`，transition() 立即判定 DECAY，R ≈ 0，新记忆被 should_filter 过滤。

**根因分析**：`encode_memory` 编写时 FSRS 列尚不存在，后续增加列时未同步更新插入逻辑。

**修复方案**：

```python
# ── 文件：memory/memory_manager.py ──

# 改动：L1651–1760 encode_memory 方法，插入后初始化 FSRS 状态
# 在 INSERT INTO episodic_memories 之后添加：

import time as _time

async def encode_memory(self, content: str, ...):
    now = _time.time()
    # ... 现有插入逻辑 ...

    # 新增：初始化 FSRS 状态
    initial_difficulty = self._fsrs.estimate_initial_difficulty(content)
    await db.execute(
        """UPDATE episodic_memories
           SET difficulty = ?, stability = ?, phase = 'buffer',
               last_review = ?, reinforcement_count = 0
           WHERE id = ?""",
        (initial_difficulty, S_INIT, now, mem_id)
    )
```

**验证方法**：插入新记忆后立即查询 `last_review`，断言为当前时间戳而非 0；`_apply_fsrs_scoring` 对新记忆 R > 0.5。

**参考来源**：[py-fsrs 库 — 初始化 S/D](https://github-wiki-see.page/m/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm)

---

### Bug #11: _apply_fsrs_scoring last_review=0 fallback 语义错误

| 项目 | 内容 |
|------|------|
| **文件** | `memory/memory_manager.py` |
| **行号** | L1489 |
| **严重度** | P1 — 新记忆用 timestamp 替代 last_review 让 R(t) 偏高 |

**问题描述**：`last_review=r.get("last_review", 0.0) or r.get("timestamp", 0.0)` 当 last_review=0 时 fallback 到 timestamp（记忆创建时间），语义上不正确——elapsed_days=3 而非"从未复习"。

**根因分析**：Bug #10 的补偿逻辑——因为新记忆 last_review=0 会导致 R≈0，所以用 timestamp 兜底，但引入了语义错误。

**修复方案**：

```python
# ── 文件：memory/memory_manager.py ──

# 改动：L1489，结合 Bug #10 修复后 last_review 不再为 0
# 当前代码（语义错误）：
#   last_review=r.get("last_review", 0.0) or r.get("timestamp", 0.0)

# 修改为（Bug #10 修复后 last_review 已正确初始化）：
    last_review = r.get("last_review", 0.0)
    if last_review == 0.0:
        # 数据库迁移前的旧数据：使用 timestamp 作为最接近的近似
        last_review = r.get("timestamp", 0.0)
        logger.debug(f"FSRS: memory {r.get('id')} has last_review=0, "
                     f"falling back to timestamp={last_review}")
```

**验证方法**：Bug #10 修复后新记忆不再进入 fallback 路径；旧记忆 fallback 时记录 debug 日志。

**参考来源**：此修复是 Bug #10 的配套改动，根因在 Bug #10

---

### Bug #12: consolidate_db/consolidate_from_db 逻辑分裂

| 项目 | 内容 |
|------|------|
| **文件** | `core/dream_consolidation.py` |
| **行号** | L107–140, L155–290 |
| **严重度** | P1 — Dream 整合后不回写 FSRS 状态到数据库 |

**问题描述**：`consolidate_db` 只做归档（低 R → archive），不更新 S/D。`consolidate_from_db` 做完整四杆框架但只修改内存中的 Memory 对象，不回写 FSRS 状态到数据库。两方法都不更新 DB 中的 stability/difficulty/phase。

**根因分析**：`consolidate_from_db` 设计时未考虑 FSRS 状态持久化，仅操作内存数据结构。

**修复方案**：

```python
# ── 文件：core/dream_consolidation.py ──

# 改动：consolidate_from_db 末尾添加批量 FSRS 状态回写
# 在 L290（方法末尾）添加：

    # 批量回写 FSRS 状态到数据库
    updates = []
    for mid, m in memories.items():
        if hasattr(m, '_fsrs_state') and m._fsrs_state:
            updates.append((
                m._fsrs_state.difficulty,
                m._fsrs_state.stability,
                m._fsrs_state.phase.value,
                m._fsrs_state.last_review,
                mid
            ))
    if updates:
        await db.executemany(
            """UPDATE episodic_memories
               SET difficulty=?, stability=?, phase=?, last_review=?
               WHERE id=?""",
            updates
        )
        await db.commit()
```

**验证方法**：运行 `consolidate_from_db` 后查询 DB，断言 stability/difficulty/phase 已更新。

**参考来源**：[pythonhowtoprogram — aiosqlite batch commit](https://pythonhowtoprogram.com/how-to-use-python-aiosqlite-for-async-sqlite/)

---

### Bug #13: auto_link 每条边单独 commit O(N²)

| 项目 | 内容 |
|------|------|
| **文件** | `db/db_concept.py` |
| **行号** | L55, 79, 106, 131, 169 |
| **严重度** | P1 — 100 个节点 → 200 次 commit，写放大 |

**问题描述**：ConceptDB 的所有写操作在方法末尾 `await self._conn.commit()`。`auto_link` 对每个共享 ≥3 keys 的节点调用 `create_edge` 两次，每次 commit。N 个节点最坏 2N 次 commit。

**根因分析**：每个方法自行 commit，缺少事务边界控制参数。

**修复方案**：

```python
# ── 文件：db/db_concept.py ──

# 改动：所有写入方法增加 auto_commit 参数

async def insert_node(self, ..., auto_commit: bool = True):
    # ... 现有逻辑 ...
    if auto_commit:
        await self._conn.commit()

async def create_edge(self, ..., auto_commit: bool = True):
    # ... 现有逻辑 ...
    if auto_commit:
        await self._conn.commit()

async def update_edge(self, ..., auto_commit: bool = True):
    # ... 现有逻辑 ...
    if auto_commit:
        await self._conn.commit()

async def auto_link(self, ...):
    # 外层统一 commit，内部调用传 auto_commit=False
    for node_a, node_b in pairs:
        await self.create_edge(node_a, node_b, auto_commit=False)
        await self.create_edge(node_b, node_a, auto_commit=False)
    await self._conn.commit()  # 一次提交

async def confirm(self, ...):
    await self.update_edge(edge_id_1, ..., auto_commit=False)
    await self.update_edge(edge_id_2, ..., auto_commit=False)
    await self._conn.commit()  # 一次提交
```

**验证方法**：`auto_link` 10 个节点，断言 `commit()` 仅调用 1 次而非 20 次。

**参考来源**：[pythonhowtoprogram — aiosqlite batch commit pattern](https://pythonhowtoprogram.com/how-to-use-python-aiosqlite-for-async-sqlite/)

---

### Bug #14: get_alive_nodes() 无分页 OOM

| 项目 | 内容 |
|------|------|
| **文件** | `db/db_concept.py` |
| **行号** | L81–87 |
| **严重度** | P1 — 数万节点时单次调用消耗大量内存 |

**问题描述**：`get_alive_nodes()` 返回所有 `valid_to IS NULL` 的节点到内存字典。`auto_link` 调用它做全表扫描。concept_nodes 增长到数万条时 OOM 风险。

**根因分析**：初始设计未考虑大规模数据，单用户场景下通常不会超过几千条，但 auto_link 的 O(N²) 组合使 N 增长后果严重。

**修复方案**：

```python
# ── 文件：db/db_concept.py ──

# 改动：增加分页参数和流式查询模式

async def get_alive_nodes(self, limit: int = 500, offset: int = 0
                          ) -> dict[str, dict]:
    """分页获取存活节点。auto_link 应多次调用或使用 iterate_alive_nodes。"""
    cursor = await self._conn.execute(
        "SELECT id, content, keys, weight, peak_weight, confidence, "
        "access_count, difficulty, stability, phase, last_review "
        "FROM concept_nodes WHERE valid_to IS NULL "
        "LIMIT ? OFFSET ?",
        (limit, offset)
    )
    rows = await cursor.fetchall()
    return {row[0]: dict(row) for row in rows}

async def iterate_alive_nodes(self, batch_size: int = 500
                              ) -> AsyncIterator[dict[str, dict]]:
    """流式迭代存活节点，每次返回 batch_size 条。"""
    offset = 0
    while True:
        batch = await self.get_alive_nodes(limit=batch_size, offset=offset)
        if not batch:
            break
        yield batch
        offset += batch_size

# auto_link 改为流式处理：
async def auto_link(self, min_shared: int = 3):
    async for batch in self.iterate_alive_nodes():
        # 对当前 batch 内的节点做匹配（或跨 batch 缓存 key 集合）
        ...
```

**验证方法**：10000 个节点时 `get_alive_nodes(limit=500)` 返回 ≤500 条；`iterate_alive_nodes` 遍历完毕总条数 = 10000。

**参考来源**：SQLite LIMIT/OFFSET 分页标准模式

---

## 三、J-Space 认知层（7 个 P1）

> 来源：`audit_j_space_report.md`

---

### P1-1: fire-and-forget Task 引用丢失，可能被 GC 中途回收

| 项目 | 内容 |
|------|------|
| **文件** | `core/behavioral_health.py:185` / `core/agent_introspection.py:149` |
| **严重度** | P1 — Task 中途静默消失 |

**问题描述**：两处均使用 `loop.create_task(_signal_stream.emit(...))` 创建后台任务但未保存引用。Python 文档明确警告：event loop 仅持有 task 的弱引用，未引用的 task 可能被 GC 回收导致中途静默消失。

**根因分析**：fire-and-forget 模式缺少引用持有，Python 3.12+ 的 GC 更激进。

**修复方案**：

```python
# ── 文件：core/behavioral_health.py 和 core/agent_introspection.py ──

# 在模块顶部添加：
_pending_tasks: set[asyncio.Task] = set()

def _fire_and_forget(coro: Coroutine) -> asyncio.Task:
    """安全创建后台 Task，持有引用防止 GC 回收。"""
    task = asyncio.create_task(coro)
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return task

# 改动：behavioral_health.py L185
# 当前代码（不安全）：
#   loop.create_task(_signal_stream.emit("health", float(score_val)))

# 修改为：
    _fire_and_forget(_signal_stream.emit("health", float(score_val) / 5.0))
    # 注意：同时修复了 P0-1 的值域归一化问题

# 改动：agent_introspection.py L149
# 当前代码（不安全）：
#   loop.create_task(_signal_stream.emit("introspection", ...))

# 修改为：
    _fire_and_forget(_signal_stream.emit("introspection", ...))
```

**验证方法**：在 GC 触发后断言 `_pending_tasks` 中的 task 仍在执行；`len(_pending_tasks)` 在 done callback 后正确递减。

**参考来源**：[mkennedy.codes — fire-and-forget with asyncio](https://mkennedy.codes/posts/fire-and-forget-or-never-with-python-s-asyncio/) [(pythontutorials.net)](https://www.pythontutorials.net/blog/python-asyncio-create-task-really-need-to-keep-a-reference/)

---

### P1-2: subscribe() 无 unsubscribe 内存泄漏

| 项目 | 内容 |
|------|------|
| **文件** | `core/behavioral_signal.py` |
| **行号** | L49–55 |
| **严重度** | P1 — 长期运行 Event 对象永不释放，emit 遍历不断增长列表 |

**问题描述**：`subscribe()` 向 `self._subscribers[signal_type]` 追加 `asyncio.Event`，但无 `unsubscribe()` 方法。订阅者销毁后 Event 对象仍留在列表中。

**根因分析**：设计时未考虑订阅生命周期管理。

**修复方案**：

```python
# ── 文件：core/behavioral_signal.py ──

# 改动 1：将 _subscribers 从 list[Event] 改为 WeakSet
import weakref

class BehavioralSignalStream:
    def __init__(self):
        self._subscribers: dict[str, weakref.WeakSet[asyncio.Event]] = {}

    def subscribe(self, signal_type: str) -> asyncio.Event:
        if signal_type not in self._subscribers:
            self._subscribers[signal_type] = weakref.WeakSet()
        ev = asyncio.Event()
        self._subscribers[signal_type].add(ev)
        return ev

    # WeakSet 在订阅者 Event 被销毁后自动移除，无需 unsubscribe
    # 如需显式取消，可添加：
    def unsubscribe(self, signal_type: str, event: asyncio.Event) -> None:
        if signal_type in self._subscribers:
            self._subscribers[signal_type].discard(event)

    async def emit(self, signal_type: str, value: float) -> None:
        if signal_type in self._subscribers:
            for ev in list(self._subscribers[signal_type]):  # list() 防迭代中 WeakSet 变化
                ev.set()
```

**验证方法**：创建 Event 后删除所有引用，触发 GC，断言 `WeakSet` 中该 Event 已自动移除。

**参考来源**：[Python weakref.WeakSet 文档](https://docs.python.org/ja/3.14/library/weakref.html)

---

### P1-3: start_monitoring() 无停止接口

| 项目 | 内容 |
|------|------|
| **文件** | `core/behavioral_health.py` |
| **行号** | L247–265 |
| **严重度** | P1 — 无法从 Scorer 内部停止监控 Task |

**问题描述**：`start_monitoring()` 创建永不退出的 `_loop()` 协程并返回 Task，但类自身不持有引用、不提供 `stop_monitoring()` 方法。

**根因分析**：设计时假设监控一旦启动永不停止，未考虑动态启停需求。

**修复方案**：

```python
# ── 文件：core/behavioral_health.py ──

# 改动：持有 Task 引用，增加 stop_monitoring 方法
class BehavioralHealthScorer:
    def __init__(self):
        ...
        self._monitor_task: asyncio.Task | None = None

    def start_monitoring(self, interval: float = 60.0) -> asyncio.Task:
        if self._monitor_task and not self._monitor_task.done():
            return self._monitor_task
        self._monitor_task = asyncio.create_task(self._loop(interval))
        return self._monitor_task

    def stop_monitoring(self) -> None:
        """停止健康监控循环。"""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = None
```

**验证方法**：`start_monitoring()` 后 `stop_monitoring()`，断言 Task 被 cancel 且 `_monitor_task is None`。

**参考来源**：asyncio.Task 生命周期管理标准模式

---

### P1-4: agent_introspection 访问 BehavioralHealthScorer 私有方法

| 项目 | 内容 |
|------|------|
| **文件** | `core/agent_introspection.py` |
| **行号** | L229–231 |
| **严重度** | P1 — 封装破裂，Scorer 重构后此处静默断裂 |

**问题描述**：`scorer._collect_runtime_metrics()` 访问私有方法。若 Scorer 重构内部指标采集逻辑，此调用将断裂且无编译期告警。

**根因分析**：缺少公共接口封装内部两步操作（采集指标 → 计算评分）。

**修复方案**：

```python
# ── 文件：core/behavioral_health.py ──

# 改动：BehavioralHealthScorer 增加公共方法
class BehavioralHealthScorer:
    ...
    def calculate_from_runtime(self) -> HealthScore:
        """从运行时指标自动计算健康评分（公共接口）。"""
        return self.calculate(self._collect_runtime_metrics())

# ── 文件：core/agent_introspection.py ──

# 改动：L229–231 改用公共方法
# 当前代码（私有访问）：
#   metrics = scorer._collect_runtime_metrics()
#   score = scorer.calculate(metrics)

# 修改为：
    score = scorer.calculate_from_runtime()
```

**验证方法**：grep 确认 `agent_introspection.py` 中不再有 `_collect_runtime_metrics` 调用。

**参考来源**：Python 封装原则——公共接口 vs 私有实现

---

### P1-5: seed_baseline() 篡改私有属性

| 项目 | 内容 |
|------|------|
| **文件** | `core/degradation_detector.py` |
| **行号** | L148–152 |
| **严重度** | P1 — BehaviorBaseline 重构后此处写入错误值 |

**问题描述**：`baseline._ewma_mean = ...` 直接修改 `BehaviorBaseline` 的私有属性。若 `BehaviorBaseline` 改用 Welford 在线算法，此处静默写入错误值。

**根因分析**：缺少公共 seed 接口，外部模块被迫访问内部状态。

**修复方案**：

```python
# ── 文件：behavioral_baseline.py（BehaviorBaseline 类所在文件） ──

# 改动：BehaviorBaseline 增加 seed 公共方法
class BehaviorBaseline:
    ...
    def seed(self, mean: float, std: float, n: int) -> None:
        """用历史统计量初始化基线（公共接口）。"""
        self._ewma_mean = float(mean)
        self._ewma_var = float(std) ** 2
        self._n = max(n, 1)

# ── 文件：core/degradation_detector.py ──

# 改动：L148–152 改用公共方法
# 当前代码（私有访问）：
#   baseline._ewma_mean = float(mean)
#   baseline._ewma_var = float(std) ** 2
#   baseline._n = max(self._min_baseline_samples, 10)

# 修改为：
    baseline.seed(mean, std, max(self._min_baseline_samples, 10))
```

**验证方法**：grep 确认 `degradation_detector.py` 中不再有 `_ewma_mean` / `_ewma_var` / `_n` 直接访问。

**参考来源**：Python 封装原则——seed/initialize 公共接口模式

---

### P1-6: _wire_hooks() from-import 不可见

| 项目 | 内容 |
|------|------|
| **文件** | `core/j_space_bootstrap.py` |
| **行号** | L30–54 |
| **严重度** | P1 — 任何新增 from-import 都会引入隐蔽 Bug |

**问题描述**：`_wire_hooks()` 通过 `module._signal_stream = _signal_stream` 注入全局变量。若下游模块使用 `from core.agent_introspection import _signal_stream`，该绑定在 import 时已固定为 None，后续修改模块属性不会更新。

**根因分析**：Python 模块属性赋值不更新已绑定的 from-import 局部名称。

**修复方案**：

```python
# ── 文件：core/j_space_bootstrap.py ──

# 改动：使用 getter 函数模式替代直接模块属性注入

# 在下游模块（如 agent_introspection.py）中改为 holder 模式：
# agent_introspection.py:
_signal_stream_holder: list = [None]

def _get_signal_stream():
    return _signal_stream_holder[0]

# j_space_bootstrap.py _wire_hooks() 中：
# 当前代码（脆弱）：
#   _ai._signal_stream = _signal_stream

# 修改为：
    _ai._signal_stream_holder[0] = _signal_stream

# 或在 _wire_hooks() 顶部添加文档注释：
"""
WARNING: _wire_hooks() 通过 module.attr = value 注入全局变量。
下游模块必须使用 module.attr 访问，禁止 from-import。
违反此约定将导致注入值不可见。
"""
```

**验证方法**：在 `agent_introspection.py` 中添加 `from core.agent_introspection import _get_signal_stream`，断言 `_get_signal_stream()` 在 `_wire_hooks()` 后返回正确实例。

**参考来源**：Python module attribute assignment vs from-import 语义

---

### P1-7: clear_bg_tasks() 不取消孤立 Task

| 项目 | 内容 |
|------|------|
| **文件** | `core/background_tasks.py` |
| **行号** | L247–249 |
| **严重度** | P1 — 孤立 Task 异常不被记录，无法 shutdown 统一 cancel |

**问题描述**：`_bg_tasks.clear()` 清空集合后，已创建的 `asyncio.Task` 仍在 event loop 中运行，失去跟踪。异常不会被 `_on_bg_task_done` 回调记录。

**根因分析**：`clear()` 仅清空跟踪集合，未取消正在执行的任务。

**修复方案**：

```python
# ── 文件：core/background_tasks.py ──

# 改动：L247–249，clear 前先 cancel 所有未完成 Task
# 当前代码（不安全）：
#   @staticmethod
#   def clear_bg_tasks() -> None:
#       _bg_tasks.clear()

# 修改为：
    @staticmethod
    def clear_bg_tasks() -> None:
        for task in list(_bg_tasks):
            if not task.done():
                task.cancel()
        _bg_tasks.clear()
```

**验证方法**：`clear_bg_tasks()` 后断言 `_bg_tasks` 为空且所有原 Task 状态为 `cancelled` 或 `done`。

**参考来源**：asyncio.Task.cancel() 标准模式

---

## 四、渠道适配层（7 个 P1）

> 来源：`audit/channel_audit_report.md`

---

### P1-1: SharedBlackboardDB.get_with_meta 缺少 created_at 字段

| 项目 | 内容 |
|------|------|
| **文件** | `agent_core/shared_blackboard_db.py` |
| **行号** | L120–135 |
| **严重度** | P1 — 消费方无法获取记忆创建时间 |

**问题描述**：数据库表有 `created_at` 列，但 `get_with_meta` 只返回 `{"value": ..., "agent_name": ...}`，缺少 `created_at`。

**根因分析**：`get_with_meta` 编写时 `created_at` 列尚未添加或未被需要，后续未同步。

**修复方案**：

```python
# ── 文件：agent_core/shared_blackboard_db.py ──

# 改动：L120–135 get_with_meta 方法
# 当前代码（缺失字段）：
#   return {"value": row[1], "agent_name": row[2]}

# 修改为：
    async def get_with_meta(self, key: str) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT key, value, agent_name, created_at "
            "FROM blackboard WHERE key = ? AND (expire_at IS NULL OR expire_at > ?)",
            (key, time.time())
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "value": row[1],
            "agent_name": row[2],
            "created_at": row[3]
        }
```

**验证方法**：`get_with_meta("test")` 返回包含 `"created_at"` 键的字典。

**参考来源**：SQLite SELECT 列扩展标准模式

---

### P1-2: 群聊打字指示泄漏

| 项目 | 内容 |
|------|------|
| **文件** | `qq_bot_adapter.py` |
| **行号** | `_send_streaming_reply_with_sticker` 方法 |
| **严重度** | P1 — 额外消耗 1 次群聊消息配额，可能导致超 5 条限制 |

**问题描述**：与 `_send_streaming_reply` 不同，`_send_streaming_reply_with_sticker` 在群聊中也会发送打字指示（无 `if not is_group` 判断），额外消耗 1 次群聊消息配额。

**根因分析**：`_send_streaming_reply_with_sticker` 复制自 `_send_streaming_reply` 但遗漏了群聊判断。

**修复方案**：

```python
# ── 文件：qq_bot_adapter.py ──

# 改动：_send_streaming_reply_with_sticker 方法中添加群聊判断
# 在发送打字指示的代码前添加条件：

# 当前代码（缺少判断）：
#   await message.reply(content=f"...正在打字...")

# 修改为：
    is_group = hasattr(message, 'group_openid') and message.group_openid
    if not is_group:
        await message.reply(content=f"...正在打字...")
```

**验证方法**：群聊中调用 `_send_streaming_reply_with_sticker` 不发送打字指示；C2C 中正常发送。

**参考来源**：同文件 `_send_streaming_reply` 中的 `if not is_group` 模式

---

### P1-3: DND 补发可能重复触发 / 问候永久丢失

| 项目 | 内容 |
|------|------|
| **文件** | `web/greeting_scheduler.py` |
| **行号** | L105–115 |
| **严重度** | P1 — deferred 问候在配额满时被清空后永久丢失 |

**问题描述**：`has_deferred` 检查和 `pending, self._deferred = self._deferred, []` 清空存在 TOCTOU 窗口。更严重的是，DND 结束后补发时如果 `_sent_today_count()` 已达上限，补发被跳过但 `_deferred` 已清空，问候永久丢失。

**根因分析**：补发逻辑在执行前就清空了 deferred 队列，未考虑执行失败的场景。

**修复方案**：

```python
# ── 文件：web/greeting_scheduler.py ──

# 改动：L105–115，补发前先检查配额，失败时保留 deferred 不清空
# 当前代码（丢失问候）：
#   has_deferred = bool(self._deferred)
#   if has_deferred:
#       with self._deferred_lock:
#           pending, self._deferred = self._deferred, []
#       for greeting in pending:
#           await self.fire(greeting)  # 可能因配额满跳过

# 修改为：
    if self._deferred:
        with self._deferred_lock:
            pending = list(self._deferred)
        # 先尝试补发，成功的才从 deferred 中移除
        still_deferred = []
        for greeting in pending:
            sent = await self.fire(greeting)
            if not sent:
                still_deferred.append(greeting)
        with self._deferred_lock:
            self._deferred = still_deferred + self._deferred
```

同时 `fire()` 方法需返回 `bool` 表示是否成功发送：

```python
async def fire(self, greeting) -> bool:
    """执行问候发送。返回 True 表示成功，False 表示配额满或失败。"""
    if self._sent_today_count() >= self._daily_limit:
        return False
    # ... 现有发送逻辑 ...
    return True
```

**验证方法**：配额满时 `fire()` 返回 False，deferred 问候保留到下一个 tick 重试。

**参考来源**：TOCTOU 修复的标准模式——先执行后确认，失败时回滚

---

### P1-4: _execute_tool 未传递 user_id/safe_mode

| 项目 | 内容 |
|------|------|
| **文件** | `agent_dispatcher.py` |
| **行号** | L530–560 |
| **严重度** | P1 — 子代理沙箱安全检查被绕过，审计日志 user_id 为空 |

**问题描述**：子代理的工具执行路径 `self._tool_executor.execute(tool_name, args)` 缺少 `user_id` 和 `safe_mode` 参数，默认为空/False。非主人的群聊消息通过子代理执行工具时跳过了沙箱限制。

**根因分析**：`_execute_tool` 未从调用链上游传递安全上下文。

**修复方案**：

```python
# ── 文件：agent_dispatcher.py ──

# 改动 1：SubAgent.chat 方法签名增加安全上下文参数
async def chat(self, messages: list, ..., user_id: str = "",
               safe_mode: bool = False) -> str:
    self._user_id = user_id
    self._safe_mode = safe_mode
    ...

# 改动 2：_execute_tool 方法传递参数
# 当前代码（缺失参数）：
#   result = await self._tool_executor.execute(tool_name, args)

# 修改为：
    result = await self._tool_executor.execute(
        tool_name, args,
        user_id=self._user_id,
        safe_mode=self._safe_mode
    )

# 改动 3：sub_agent_manager.py 调用处传递参数
# _dispatch_single_sub_agent / _parallel_run_one 中：
    result = await sub_agent.chat(
        ..., user_id=self._current_user_id, safe_mode=self._current_safe_mode
    )
```

**验证方法**：群聊非主人消息通过子代理执行工具时，`_tool_executor.execute` 收到 `safe_mode=True`。

**参考来源**：最小权限原则——安全上下文必须沿调用链显式传递

---

### P1-5: merge_from 不合并索引

| 项目 | 内容 |
|------|------|
| **文件** | `agent_core/structured_blackboard.py` |
| **行号** | L64–74 |
| **严重度** | P1 — 合并后的数据在 query_by_tag / query_by_direction 中查不到 |

**问题描述**：`merge_from` 通过 `other.get(key) + self.put(key, val)` 合并数据，但不合并 `_tag_index` 和 `_direction_index`。

**根因分析**：`merge_from` 设计时仅考虑数据合并，未考虑结构化元数据索引。

**修复方案**：

```python
# ── 文件：agent_core/structured_blackboard.py ──

# 改动：L64–74 merge_from 方法，增加索引合并
# 当前代码（缺失索引合并）：
#   def merge_from(self, other: "StructuredBlackboard") -> None:
#       for key in other.keys():
#           val = other.get(key)
#           if val is not None:
#               self.put(key, val)

# 修改为：
    def merge_from(self, other: "StructuredBlackboard") -> None:
        for key in other.keys():
            val = other.get(key)
            if val is not None:
                self.put(key, val)
        # 合并标签索引
        if isinstance(other, StructuredBlackboard):
            for tag, keys in other._tag_index.items():
                if tag not in self._tag_index:
                    self._tag_index[tag] = set()
                self._tag_index[tag].update(keys)
            # 合并方向索引
            for direction, keys in other._direction_index.items():
                if direction not in self._direction_index:
                    self._direction_index[direction] = set()
                self._direction_index[direction].update(keys)
```

**验证方法**：`source.put_structured("k1", "v1", tags=["t1"], direction="d1")` → `target.merge_from(source)` → `target.query_by_tag("t1")` 返回 `["k1"]`。

**参考来源**：结构化索引合并的标准模式

---

### P1-6: 流式降级丢弃已积累内容

| 项目 | 内容 |
|------|------|
| **文件** | `agent_core/message_processor.py` |
| **行号** | L380–400 |
| **严重度** | P1 — 积累 90% 回复后降级，用户看到重新等待和重复内容 |

**问题描述**：流式调用失败时降级到同步调用，`full_response` 列表中的已积累内容被完全忽略。

**根因分析**：降级逻辑未考虑部分结果的有效性，直接重走完整同步路径。

**修复方案**：

```python
# ── 文件：agent_core/message_processor.py ──

# 改动：L380–400 流式降级时保留已积累内容
# 当前代码（丢弃内容）：
#   except Exception:
#       logger.warning("streaming failed, falling back to sync")
#       return await self.router.route(task_type, messages, **kwargs)

# 修改为：
    except Exception as e:
        accumulated = "".join(full_response)
        if accumulated:
            # 已有部分内容，直接返回 + 截断提示
            logger.warning(f"streaming failed after {len(accumulated)} chars, "
                          f"returning partial result")
            return accumulated + "\n\n[⚠️ 内容生成中断，以上为已生成的部分]"
        else:
            # 无积累内容，完整降级
            logger.warning("streaming failed with no content, falling back to sync")
            return await self.router.route(task_type, messages, **kwargs)
```

**验证方法**：模拟流式在积累 100 字后异常，断言返回内容包含已积累的 100 字 + 截断提示。

**参考来源**：部分结果保留是优雅降级的基本原则

---

### P1-7: 群聊未 bind QQUser 到 EventBus

| 项目 | 内容 |
|------|------|
| **文件** | `qq_bot_adapter.py` |
| **行号** | `_process_group_reply` 方法 |
| **严重度** | P1 — 群聊中子代理事件（SUB_STARTED 等）不投递到 QQ 端 |

**问题描述**：C2C 路径正确地 `bind_user(QQUser(...))` / `unbind_user()`，但群聊路径中未找到 `bind_user` 调用。导致群聊中子代理事件不被投递。

**根因分析**：群聊处理路径编写时遗漏了 EventBus 绑定步骤。

**修复方案**：

```python
# ── 文件：qq_bot_adapter.py ──

# 改动：_process_group_reply 方法中添加 EventBus 绑定/解绑
# 参照 _process_c2c_reply 的模式：

async def _process_group_reply(self, message, ...):
    qq_user = QQUser(
        reply_fn=lambda content, msg_seq: message.reply(content=content,
                                                         msg_type=0,
                                                         msg_seq=msg_seq),
        msg_seq_fn=lambda: ...,
        group_openid=message.group_openid,
        ...
    )
    token = event_bus.bind_user(qq_user)  # 新增：使用 token 模式
    try:
        result = await self.bot.process(
            user_input=..., user_id=..., source="qq_group",
            status_callback=...
        )
    finally:
        event_bus.unbind_user(token)  # 新增：使用 token 安全解绑
```

**注意**：此修复需配合 EventBus 的 `bind_user` / `unbind_user` 接口改造为 ContextVar Token 模式（见 channel_audit_report.md P0-3），以避免并发绑定泄漏。

**验证方法**：群聊中触发子代理，断言 QQUser 收到 `SUB_STARTED` 事件通知。

**参考来源**：[runebook.dev — ContextVar.reset token 模式](https://runebook.dev/en/docs/python/library/contextvars/contextvars.ContextVar.reset)

---

## 附录：跨模块模式索引

本节汇总多个 Bug 共用的修复模式，便于统一实施和交叉验证。

### 模式 A：Task 引用持有（_pending_tasks set）

**适用 Bug**：BUG-02, P1-1(J-Space), P1-7(J-Space)

```python
_pending_tasks: set[asyncio.Task] = set()

def _fire_and_forget(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return task
```

**参考来源**：[mkennedy.codes](https://mkennedy.codes/posts/fire-and-forget-or-never-with-python-s-asyncio/)

### 模式 B：ContextVar Token 安全绑定

**适用 Bug**：P1-7(渠道), P0-3(渠道，非 P1 但强关联)

```python
# bind_user 返回 token，unbind_user 接受 token
def bind_user(self, user) -> contextvars.Token:
    return _current_user.set(user)

def unbind_user(self, token: contextvars.Token) -> None:
    _current_user.reset(token)
```

**参考来源**：[runebook.dev — ContextVar.reset](https://runebook.dev/en/docs/python/library/contextvars/contextvars.ContextVar.reset)

### 模式 C：Protocol 替代 Callable 约束参数名

**适用 Bug**：BUG-03

```python
class ReplyFn(Protocol):
    async def __call__(self, content: str, msg_seq: int) -> None: ...
```

**参考来源**：[typing.python.org — Protocol callables](https://typing.python.org/en/latest/spec/callables.html)

### 模式 D：SQLite auto_commit 参数控制事务边界

**适用 Bug**：Bug #13

```python
async def insert_node(self, ..., auto_commit: bool = True):
    # ... SQL execute ...
    if auto_commit:
        await self._conn.commit()
```

**参考来源**：[pythonhowtoprogram — aiosqlite batch commit](https://pythonhowtoprogram.com/how-to-use-python-aiosqlite-for-async-sqlite/)

### 模式 E：WeakSet 自动清理订阅者

**适用 Bug**：P1-2(J-Space)

```python
self._subscribers: dict[str, weakref.WeakSet[asyncio.Event]] = {}
```

**参考来源**：[Python weakref.WeakSet 文档](https://docs.python.org/ja/3.14/library/weakref.html)

### 模式 F：SQLite UPSERT（INSERT ... ON CONFLICT DO UPDATE）

**适用 Bug**：Bug #25(FSRS-DSR P2，非 P1 但强关联 Bug #13)

```sql
INSERT INTO concept_nodes(id, content, keys, weight)
VALUES(?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    content = excluded.content,
    keys = excluded.keys,
    weight = excluded.weight
-- 未出现在 SET 中的列（difficulty, stability, phase）保持不变
```

**参考来源**：[coddy.tech — SQLite UPSERT](https://coddy.tech/docs/ja/sqlite/upsert)

### 模式 G：封装公共接口替代私有属性访问

**适用 Bug**：P1-4(J-Space), P1-5(J-Space)

```python
# 在被访问类上暴露公共方法
class BehavioralHealthScorer:
    def calculate_from_runtime(self) -> HealthScore: ...

class BehaviorBaseline:
    def seed(self, mean: float, std: float, n: int) -> None: ...
```

---

## 修复优先级排序（建议实施顺序）

| 优先级 | Bug ID | 模块 | 简述 | 依赖关系 |
|--------|--------|------|------|----------|
| 1 | Bug #10 | 记忆 | 新记忆 last_review=0 被 FSRS 过滤 | Bug #11 依赖此修复 |
| 2 | Bug #11 | 记忆 | last_review=0 fallback 语义错误 | 依赖 Bug #10 |
| 3 | Bug #8 | 记忆 | _apply_forget S 暴涨（含 P0 Bug #1 修复） | FSRS 核心公式 |
| 4 | BUG-02 | 核心 | CancelToken create_task 无事件循环 | 模式 A |
| 5 | P1-1 | J-Space | fire-and-forget Task GC | 模式 A |
| 6 | P1-7 | J-Space | clear_bg_tasks 孤立 Task | 模式 A |
| 7 | P1-2 | J-Space | subscribe 无 unsubscribe | 模式 E |
| 8 | BUG-03 | 核心 | QQUser 关键字参数 Callable | 模式 C |
| 9 | P1-7 | 渠道 | 群聊未 bind QQUser | 模式 B |
| 10 | Bug #13 | 记忆 | auto_link O(N²) commit | 模式 D |
| 11 | Bug #9 | 记忆 | FSRSModel 重复实例化 | 独立 |
| 12 | Bug #12 | 记忆 | Dream 不回写 FSRS 状态 | 独立 |
| 13 | P1-6 | 渠道 | 流式降级丢内容 | 独立 |
| 14 | P1-4 | 渠道 | _execute_tool 未传安全上下文 | 独立 |
| 15 | BUG-01 | 核心 | 超时事件类型不一致 | 独立 |
| 16 | BUG-04 | 核心 | 不可用 agent 无事件通知 | 独立 |
| 17 | Bug #6 | 记忆 | FluidMemory.score() 缺 difficulty | Bug #7 前置 |
| 18 | Bug #7 | 记忆 | is_permanent 判定不一致 | 依赖 Bug #6 |
| 19 | Bug #14 | 记忆 | get_alive_nodes OOM | 独立 |
| 20 | P1-3 | J-Space | start_monitoring 无停止 | 独立 |
| 21 | P1-4 | J-Space | 访问私有方法 | 模式 G |
| 22 | P1-5 | J-Space | seed_baseline 篡改私有属性 | 模式 G |
| 23 | P1-6 | J-Space | _wire_hooks from-import 不可见 | 独立 |
| 24 | P1-1 | 渠道 | get_with_meta 缺 created_at | 独立 |
| 25 | P1-2 | 渠道 | 群聊打字指示泄漏 | 独立 |
| 26 | P1-3 | 渠道 | DND 补发丢失问候 | 独立 |
| 27 | P1-5 | 渠道 | merge_from 不合并索引 | 独立 |

---

*文档结束。每个修复方案均包含：精确文件定位、当前错误代码、修复后代码、验证方法、参考来源。*
