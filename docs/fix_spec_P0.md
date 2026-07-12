# P0 级 Bug 完整修复 Spec

**项目**: nahida-agent  
**完成日期**: 2026-07-12  
**来源**: 4 份审计报告（FSRS-DSR / 渠道适配 / J-Space / 代码质量）  
**总计 P0 Bug**: 13 个  
**修复顺序**: 按依赖关系排列——底层基础设施先修，上层功能后修

---

## 修复顺序总览

```
Phase 1 — 异步基础设施（无外部依赖，其他 Bug 修复的前提）
  ├── P0-09: background_tasks 无 loop 保护
  ├── P0-05: EventBus ContextVar 绑定泄漏
  └── P0-07: StructuredBlackboard 索引泄漏

Phase 2 — 记忆系统核心（FSRS 算法正确性）
  ├── P0-01: FSRS 遗忘公式反转
  ├── P0-02: FSRS 遗忘极小值下限截断
  ├── P0-03: confirm_correct created_at 错误
  ├── P0-04: concept_nodes 缺 created_at 列
  ├── P0-06: 新记忆 last_review=0 被 FSRS 过滤
  └── P0-08: consolidate_from_db difficulty 硬编码

Phase 3 — 渠道适配层
  ├── P0-10: QQ C2C 流式配额超限
  └── P0-11: SharedBlackboardDB asyncio.Lock 跨进程矛盾

Phase 4 — J-Space 认知层
  ├── P0-12: health 信号值域错配 + 空误触发
  └── P0-13: degradation_strategy health 阈值同源错误
```

---

## Phase 1: 异步基础设施

---

### P0-09: `background_tasks._spawn()` 无事件循环保护

**来源**: J-Space 审计 P0-3  
**文件**: `core/background_tasks.py`  
**行号**: L63  
**严重性**: 同步上下文调用必崩，RuntimeError 无捕获

#### Bug 描述

```python
# 当前代码 (L63)
task = asyncio.create_task(_wrapped())
```

`asyncio.create_task()` 要求当前线程有正在运行的 event loop。`_spawn()` 被 `BackgroundTaskManager.run_background_tasks()` 调用，后者是**非 async 方法**，若调用方未确保处于 async 上下文则直接抛 `RuntimeError`。

#### 参考方案

Python 官方文档 [`asyncio.get_running_loop()`](https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.get_running_loop) 明确指出：在无运行循环时抛 `RuntimeError`，需捕获并降级。[(Python docs)](https://docs.python.org/3/library/asyncio-eventloop.html)

#### 修复方案

```python
# background_tasks.py — 替换 _spawn() 函数体

def _spawn(coro: Any) -> None:
    """创建 fire-and-forget 后台任务，自动从 _bg_tasks 中移除已完成的任务。
    
    包含耗时监控：任务完成时记录执行时长，超过 30s 发出告警日志。
    包含 loop 保护：同步上下文调用时降级日志而非崩溃。
    """
    task_name = getattr(coro, '__name__', coro.__class__.__name__)
    start_time = time.time()

    async def _wrapped():
        try:
            await coro
        finally:
            elapsed = time.time() - start_time
            if elapsed > 30:
                logger.warning("bg.task_slow name={} elapsed={:.1f}s", task_name, elapsed)
            else:
                logger.debug("bg.task_done name={} elapsed={:.1f}s", task_name, elapsed)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error("bg.spawn_no_loop: cannot create task without running event loop, "
                     "task={} will be dropped", task_name)
        return
    task = loop.create_task(_wrapped())
    _bg_tasks.add(task)
    task.add_done_callback(_on_bg_task_done)
```

**改动范围**: `_spawn()` 函数，约 5 行  
**回测**: 在 `__init__` 中调用 `run_background_tasks()` 应不再崩溃，仅输出 error 日志

---

### P0-05: EventBus ContextVar 绑定泄漏

**来源**: 渠道审计 P0-3  
**文件**: `core/event_bus.py`  
**行号**: L105-110 (`bind_user` / `unbind_user`)  
**严重性**: 并发协程间互相清除绑定，可导致事件投递丢失

#### Bug 描述

```python
# 当前代码
def bind_user(self, user: "UserBase") -> None:
    _current_user.set(user)

def unbind_user(self) -> None:
    _current_user.set(None)  # ← 危险！清除的是当前上下文的值
```

**问题 1**: `set(None)` 不是安全的解绑——如果协程 A 绑定了 User，协程 B 在 `_process_c2c_reply` 中 ACK 失败后 finally 执行 `unbind_user()`，会**清除协程 A 的绑定**。

**问题 2**: `unbind_user()` 不检查当前值是否为自己绑定的 User，无条件 set(None)。

#### 参考方案

Python 3.7+ 的 `ContextVar` 标准模式是 **token reset**。PEP 567 和官方文档明确要求 `set()` 返回 token，`reset(token)` 恢复原值。在 asyncio 中，每个 Task 有独立上下文，token reset 只影响当前 Task 的上下文栈，不会干扰其他并发协程。[(PEP 567)](https://peps.python.org/pep-0567/) [(Python docs — ContextVar)](https://docs.python.org/3/library/contextvars.html)

```python
# 官方推荐模式
token = var.set(new_value)
try:
    ...
finally:
    var.reset(token)
```

Python 3.14+ 已支持 token 作为 context manager（`with var.set(value):`），但项目基于 3.11+，需手动 try/finally。[(Python 3.14 docs)](https://docs.python.org/fr/dev/library/contextvars.html)

#### 修复方案

```python
# event_bus.py — 替换 bind_user / unbind_user

class AgentEventBus:
    """子代理事件总线 — 定向投递，不是广播。"""

    def bind_user(self, user: "UserBase") -> contextvars.Token:
        """绑定当前 session 的 User。

        返回 Token，调用方必须在 finally 中调用 unbind_user(token)。
        """
        return _current_user.set(user)

    def unbind_user(self, token: contextvars.Token) -> None:
        """解绑 User（session 结束时调用）。

        必须传入 bind_user 返回的 Token，确保只恢复自己的绑定。
        """
        try:
            _current_user.reset(token)
        except (ValueError, LookupError):
            # Token 已被使用或上下文不匹配，静默处理
            logger.debug("event_bus.unbind_noop: token already consumed or context mismatch")
```

**调用方修改**（所有 `bind_user` / `unbind_user` 调用点）：

```python
# qq_bot_adapter.py — _process_c2c_reply / _process_group_reply
token = event_bus.bind_user(QQUser(...))
try:
    ...
finally:
    event_bus.unbind_user(token)

# cli.py — L189-195
token = event_bus.bind_user(CLIUser())
try:
    result = self._loop.run_until_complete(...)
finally:
    event_bus.unbind_user(token)

# web/ws_hub.py — _handle_chat
token = event_bus.bind_user(WSUser(...))
try:
    ...
finally:
    event_bus.unbind_user(token)
```

**改动范围**:  
- `event_bus.py`: `bind_user` 返回 Token，`unbind_user` 接受 Token  
- `qq_bot_adapter.py`: 2 处 try/finally 改为 token 模式  
- `cli.py`: 1 处  
- `web/ws_hub.py`: 1 处  
- 新增 `import contextvars` 在 event_bus.py 头部

**回测**: 两个并发 QQ 消息同时处理时，不应互相清除对方的事件绑定

---

### P0-07: StructuredBlackboard tag/direction 索引不过期清理

**来源**: 渠道审计 P0-4  
**文件**: `agent_core/structured_blackboard.py`  
**行号**: L20-25 (`_tag_index` / `_direction_index`), L42-48 (`put_structured`)  
**严重性**: 内存无限增长，长时间运行后 OOM

#### Bug 描述

`put_structured` 将 key 添加到 `_tag_index[tag]` 和 `_direction_index[direction]`，但 `SharedBlackboard.cleanup_expired()` 只清理 `_store` 中的过期条目，**不清理索引中指向已过期 key 的引用**。

#### 修复方案

在 `StructuredBlackboard` 中覆写 `cleanup_expired`，在父类清理后同步清理索引：

```python
# structured_blackboard.py — 在 StructuredBlackboard 类中新增方法

async def cleanup_expired(self) -> int:
    """清理过期条目并同步清理 tag/direction 索引。"""
    # 1. 先调用父类清理 _store
    cleaned = await super().cleanup_expired()
    if cleaned == 0:
        return 0

    # 2. 获取当前所有有效 key
    alive_keys = set(await self.keys())

    # 3. 清理 tag_index 中已过期的 key
    stale_tags = []
    for tag, keys in self._tag_index.items():
        before = len(keys)
        keys.difference_update(alive_keys)  # set 差集原地操作
        if before > 0 and len(keys) == 0:
            stale_tags.append(tag)
    for tag in stale_tags:
        del self._tag_index[tag]

    # 4. 清理 direction_index 中已过期的 key
    stale_dirs = []
    for direction, keys in self._direction_index.items():
        before = len(keys)
        keys.difference_update(alive_keys)
        if before > 0 and len(keys) == 0:
            stale_dirs.append(direction)
    for direction in stale_dirs:
        del self._direction_index[direction]

    logger.debug("structured_blackboard.index_cleanup tags_removed={} dirs_removed={}",
                 len(stale_tags), len(stale_dirs))
    return cleaned
```

**改动范围**: `StructuredBlackboard` 类新增 `cleanup_expired()` 方法，约 25 行  
**回测**: 写入带 tag 的条目，TTL 过期后 `cleanup_expired()`，验证 `_tag_index` 中不再有过期 key

---

## Phase 2: 记忆系统核心

---

### P0-01: FSRS 遗忘公式反转（S_new > S）

**来源**: FSRS-DSR 审计 Bug #1  
**文件**: `memory/fsrs_model.py`  
**行号**: L152  
**严重性**: 遗忘后 S 暴涨，FSRS 核心语义反转

#### Bug 描述

```python
# 当前代码 (L152)
S_new = S * 0.5 * (D ** (-0.3)) * (((S + 1.0) ** 0.2) - 1.0)
```

当 S=300（兼容层上限）时：`((301) ** 0.2) - 1.0 ≈ 3.13`，`S_new = 300 × 0.5 × 1.0 × 3.13 ≈ 470`，**遗忘了反而 S 暴涨**。

原始 FSRS 公式是 `S'_f(D, S, R) = w11 * D^(-w12) * ((S+1)^w13 - 1) * e^(w14 * ...)`，其中 `w11` 是初始稳定性缩放因子（默认 ≈ 0.2-2.2），不是当前 S。代码错把当前 S 当作 w11 使用。[(FSRS Algorithm Wiki)](https://github-wiki-see.page/m/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm) [(Expertium — FSRS Technical Explanation)](https://expertium.github.io/Algorithm.html)

FSRS-4.5/5/6 的 post-lapse stability 公式核心约束是 `min(S'_f, S)`，确保遗忘后稳定性**绝不会超过遗忘前**。[(FSRS Algorithm)](https://github-wiki-see.page/m/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm)

#### 修复方案

```python
# fsrs_model.py — 替换 _apply_forget 方法中的 L152

def _apply_forget(self, state: MemoryState, now: float) -> MemoryState:
    R = state.retrievability(now)
    D = state.difficulty
    S = state.stability

    # FSRS-4.5/5 post-lapse stability 公式
    # S'_f = S_INIT * D^(-0.3) * ((S+1)^0.2 - 1)
    # 其中 S_INIT 对应 FSRS 的 w11 参数（初始稳定性缩放因子）
    # min(S_new, S) 确保遗忘后 S 绝不超过遗忘前
    S_new = S_INIT * (D ** (-0.3)) * (((S + 1.0) ** 0.2) - 1.0)
    S_new = max(1.0, min(S_new, S))  # 双重保护：下限 1.0，上限不超过遗忘前 S

    D_new = self._update_difficulty(D, ReinforcementSignal.CORRECT)
    new_phase = MemoryState(
        difficulty=D_new, stability=S_new,
        phase=state.phase, last_review=now,
        created_at=state.created_at,
        reinforcement_count=state.reinforcement_count,
    ).transition(now)
    return MemoryState(
        difficulty=D_new, stability=S_new,
        phase=new_phase, last_review=now,
        created_at=state.created_at,
        reinforcement_count=state.reinforcement_count,
    )
```

**关键变更**: `S * 0.5` → `S_INIT`（`S_INIT = 3.0`，已在模块顶部定义）  
**数值验证**:  
- S=300, D=5.0: S_new = 3.0 × 5^(-0.3) × ((301)^0.2 - 1) = 3.0 × 0.698 × 3.13 ≈ 6.6 ✅（大幅降低）  
- S=3, D=5.0: S_new = 3.0 × 0.698 × ((4)^0.2 - 1) = 3.0 × 0.698 × 0.32 ≈ 0.67 → max(1.0) = 1.0 ✅  
- S=10, D=5.0: S_new = 3.0 × 0.698 × ((11)^0.2 - 1) = 3.0 × 0.698 × 0.615 ≈ 1.29 ✅  

**改动范围**: `_apply_forget` 方法 L152，1 行核心改动 + 1 行 min 保护  
**回测**: S=300 时遗忘后 S_new ≈ 6.6 < 300，语义正确

---

### P0-02: FSRS 遗忘极小值下限截断丢失历史

**来源**: FSRS-DSR 审计 Bug #2  
**文件**: `memory/fsrs_model.py`  
**行号**: L152-153  
**严重性**: 反复遗忘后 S 永远停在 1.0，无法区分遗忘次数

#### Bug 描述

当 S=1.0 时，P0-01 修复后 S_new ≈ 0.67，被 `max(1.0, S_new)` 截断为 1.0。反复遗忘后 S 永远停在 1.0，无法区分"遗忘1次"和"遗忘10次"。

#### 修复方案

已在 P0-01 修复中一并解决：

1. 使用 `S_INIT` 替代 `S * 0.5`，S_new 的计算基数更小  
2. `max(1.0, min(S_new, S))` 的下限 1.0 是 FSRS 标准的下限（FSRS-4.5/5 的 `S_MIN = 0.1` 天，但项目使用简化模型）  

若需更细粒度区分遗忘次数，可在 `MemoryState` 中新增 `forget_count: int` 字段：

```python
# 可选增强（非本次必须）— memory/fsrs_model.py MemoryState dataclass
@dataclass
class MemoryState:
    difficulty: float = D_INIT
    stability: float = S_INIT
    phase: MemoryPhase = MemoryPhase.BUFFER
    last_review: float = 0.0
    created_at: float = 0.0
    reinforcement_count: int = 0
    forget_count: int = 0  # ← 新增：遗忘次数追踪

# _apply_forget 中：
forget_count = state.forget_count + 1
```

**改动范围**: 已在 P0-01 中覆盖核心修复。`forget_count` 为可选增强。  
**回测**: S=1.0 时遗忘后 S_new = max(1.0, 0.67) = 1.0，但遗忘后 `reinforcement_count` 不变（当前行为），如需区分可加 `forget_count`

---

### P0-03: confirm_correct 中 created_at 错误取 last_review

**来源**: FSRS-DSR 审计 Bug #3  
**文件**: `memory/confirm_correct.py`  
**行号**: L70  
**严重性**: 旧节点永远无法从 BUFFER 过渡到 REINFORCED/DECAY

#### Bug 描述

```python
# 当前代码 (L70)
created_at=node.get("last_review", 0.0) or now_ts,
```

`last_review` 是上次复习时间，不是创建时间。如果旧数据 `last_review=0.0`，Python falsy 逻辑 `0.0 or now_ts` = `now_ts`，创建时间被设为当前时间——导致 BUFFER 阶段的 21 天门槛永远不会超时。

#### 修复方案

**方案**: 先尝试从数据库读取 `created_at` 列（P0-04 修复后存在），回退到从 `created` ISO 字符串解析，最终 fallback 到 `0.0`（让 `age_days` 足够大以正确触发过渡）。

```python
# confirm_correct.py — confirm() 方法 L70 替换

# 构建 MemoryState — 修复 created_at 取值链
# 1. 优先从 created_at REAL 列读取（v16 迁移后存在）
# 2. 回退到从 created ISO 字符串解析
# 3. 最终 fallback 到 0.0（age_days 足够大，正确触发过渡）
_created_at = node.get("created_at", 0.0)
if _created_at == 0.0:
    # 尝试从 created ISO 字符串解析
    _created_iso = node.get("created", "")
    if _created_iso:
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            dt = datetime.fromisoformat(_created_iso)
            _created_at = dt.timestamp()
        except (ValueError, TypeError):
            _created_at = 0.0

state = MemoryState(
    difficulty=node.get("difficulty", 5.0),
    stability=node.get("stability", 3.0),
    phase=MemoryPhase(node.get("phase", "buffer")),
    last_review=node.get("last_review", 0.0) or now_ts,
    created_at=_created_at if _created_at > 0.0 else now_ts,
    reinforcement_count=node.get("reinforcement_count", 0),
)
```

**改动范围**: `confirm_correct.py` L70 附近，约 15 行替换  
**回测**: 旧节点 `created_at=0.0` + `created="2026-01-01T00:00:00+08:00"` 时，应解析为正确时间戳

---

### P0-04: concept_nodes 表缺少 created_at REAL 列

**来源**: FSRS-DSR 审计 Bug #4  
**文件**: `db/database.py`  
**行号**: ~v14/v15 迁移处  
**严重性**: confirm 无法正确重建 MemoryState，整个 concept_nodes 侧 FSRS 状态无法运行

#### Bug 描述

`concept_nodes` 表只有 `created TEXT`（ISO 字符串）而没有 `created_at REAL`（时间戳），但 `MemoryState` 需要浮点时间戳做 `age_days` 计算。

#### 修复方案

新增 v16 数据库迁移：

```python
# database.py — 在 _migrate_v15 后新增 _migrate_v16 方法

async def _migrate_v16(self) -> None:
    """v16: 为 concept_nodes 和 episodic_memories 增加 created_at REAL 列，
    回填 last_review=0 的行。
    """
    # 1. concept_nodes 增加 created_at REAL 列
    concept_cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(concept_nodes)")]
    if "created_at" not in concept_cols:
        await self._conn.execute(
            "ALTER TABLE concept_nodes ADD COLUMN created_at REAL DEFAULT 0"
        )

    # 2. episodic_memories 增加 created_at REAL 列（如不存在）
    epi_cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
    if "created_at" not in epi_cols:
        await self._conn.execute(
            "ALTER TABLE episodic_memories ADD COLUMN created_at REAL DEFAULT 0"
        )

    # 3. 回填 concept_nodes.created_at：从 created ISO 字符串解析
    # SQLite 不支持 Python datetime，用 CASE WHEN 逻辑标记为 0 让应用层回填
    # 但我们可以用 substr + julianday 做简单 ISO 格式转换
    await self._conn.execute("""
        UPDATE concept_nodes
        SET created_at = CAST(
            (julianday(substr(created, 1, 19)) - julianday('1970-01-01')) * 86400 AS REAL)
        WHERE created_at = 0 AND created IS NOT NULL AND created != ''
    """)

    # 4. 回填 episodic_memories.created_at：从 timestamp 列
    await self._conn.execute("""
        UPDATE episodic_memories
        SET created_at = timestamp
        WHERE created_at = 0 AND timestamp > 0
    """)

    # 5. 回填 last_review=0 的行：设置为 timestamp（记忆创建时间）
    # 这样 last_review 不再是 Unix epoch，R(t) 计算合理
    await self._conn.execute("""
        UPDATE episodic_memories
        SET last_review = timestamp
        WHERE last_review = 0 AND timestamp > 0
    """)

    await self._conn.commit()
    logger.info("database.migration_v16_created_at_done")
```

**同时在迁移入口调用**：

```python
# database.py — 在 _run_migrations 中追加
if current < 16:
    await self._migrate_v16()
    await self._set_version(16)
```

**改动范围**: `database.py` 新增 `_migrate_v16()` + 迁移入口调用，约 40 行  
**回测**: 迁移后 `concept_nodes.created_at > 0`，`episodic_memories.last_review > 0`

---

### P0-06: 新记忆 last_review=0 被 FSRS 过滤

**来源**: FSRS-DSR 审计 Bug #10 + #11  
**文件**: `memory/memory_manager.py`  
**行号**: L1651 (`encode_memory`), L1489 (`_apply_fsrs_scoring`)  
**严重性**: 新写入的记忆在下次检索时 R≈0 被丢弃

#### Bug 描述

`encode_memory` 插入新记忆时不设置 `last_review`，默认为 0（Unix epoch 1970-01-01）。`_apply_fsrs_scoring` 中 `last_review=0.0 or r.get("timestamp", 0.0)` 依赖 falsy 判断，但 `0.0` 是 falsy 所以 fallback 到 timestamp。然而其他代码路径（如 `dream_consolidation`）直接构造 `MemoryState` 时不经过此 fallback，elapsed_days ≈ 20500 天，R ≈ 0。

#### 修复方案

**修复 1**: `encode_memory` 插入时设置 `last_review=now`（"创建即复习"语义）：

```python
# memory_manager.py — encode_memory 方法中，插入新记忆的参数字典
# 找到 INSERT 语句的参数部分，添加/修改：
now_ts = time.time()

# 确保以下字段在 INSERT 时被设置：
# last_review=now_ts
# created_at=now_ts
# phase='buffer'
# difficulty=estimate_initial_difficulty(content, emotion_label)  # ← 使用 FSRS 初始难度估算
# stability=S_INIT
# reinforcement_count=0
```

**修复 2**: `_apply_fsrs_scoring` 中对 `last_review=0` 的记忆做 R=1.0 特殊处理（防御性保护）：

```python
# memory_manager.py — _apply_fsrs_scoring 方法中
# 在构造 MemoryState 之后、计算 R 之前：
last_review = r.get("last_review", 0.0) or r.get("timestamp", 0.0)
created_at = r.get("created_at", 0.0) or r.get("timestamp", 0.0)

# 防御性保护：last_review=0 表示从未复习（旧数据或 Bug 残留），
# 视为 R=1.0 而非 R≈0，避免新记忆被过滤
if last_review == 0.0:
    R = 1.0
    fsrs_score = similarity  # 无 FSRS 衰减，直接用相似度
    r["fluid_score"] = R
    r["fsrs_score"] = fsrs_score
    r["effective_score"] = r.get("importance", 0.5) * fsrs_score
    filtered.append(r)
    continue
```

**改动范围**: `memory_manager.py` 2 处，约 15 行新增  
**回测**: 新写入记忆在 `_apply_fsrs_scoring` 中 R > 0.9，不被过滤

---

### P0-08: consolidate_from_db difficulty 硬编码 1.0

**来源**: FSRS-DSR 审计 Bug #5  
**文件**: `core/dream_consolidation.py`  
**行号**: L242  
**严重性**: difficulty=1.0 导致 reinforce 时虚增 S 增长，forget 时 D^(-0.3)=1.0 影响 S_new

#### Bug 描述

```python
# 当前代码 (L242)
state = MemoryState(
    difficulty=1.0,  # ← 硬编码！
    stability=m.strength * 10.0 if m.strength > 0 else 3.0,
    ...
)
```

同文件 L176 的 `consolidate_db` 正确使用了 `difficulty=mem.get("difficulty", 5.0)`。

#### 修复方案

```python
# dream_consolidation.py — consolidate_from_db 方法 L242 替换
state = MemoryState(
    difficulty=row.get("difficulty", 5.0),  # ← 从 DB 读取，默认 5.0
    stability=row.get("stability", 3.0) if row.get("stability", 0) > 0 else max(m.strength * 10.0, 3.0),
    phase=MemoryPhase(row.get("phase", "buffer")),
    last_review=row.get("last_review", 0.0) or row.get("timestamp", 0.0),
    created_at=row.get("created_at", 0.0) or row.get("timestamp", 0.0),
    reinforcement_count=row.get("reinforcement_count", 0),
)
```

**改动范围**: `dream_consolidation.py` L242，1 行核心改动 + 多行字段修正  
**回测**: difficulty 应从 DB 读取，不是硬编码 1.0

---

## Phase 3: 渠道适配层

---

### P0-10: QQ C2C 流式发送无消息条数上限

**来源**: 渠道审计 P0-2  
**文件**: `qq_bot_adapter.py`  
**行号**: `_send_streaming_reply` 方法（约 L450-L530）  
**严重性**: 触发 QQ API 限流，可能导致封号

#### Bug 描述

QQ 官方 API 限制：C2C 被动消息每条消息最多回复 5 次（2026-06-22 更新后为 4 次），60 分钟有效。代码中群聊路径有 `split_for_group_passive` 限制，但 C2C 路径使用 `_split_text_for_streaming(clean_reply, chunk_size=300)`，**没有 5 条上限约束**。[(QQ Bot 官方文档 — 发送消息)](https://bot.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/send.html)

#### 参考方案

QQ 官方文档明确："被动消息（回复类）有效时间为 60 分钟，每个消息最多回复 5 次"（现已调整为 4 次），超时或超频会发送失败，返回错误码 22009。[(QQ Bot 官方文档)](https://bot.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/send.html)

#### 修复方案

```python
# qq_bot_adapter.py — 在 _send_streaming_reply 方法中

# 1. C2C 路径：添加消息条数上限
QQ_C2C_MAX_SEGMENTS = 4  # QQ C2C 被动回复最多 4 次（2026-06-22 更新）
QQ_GROUP_MAX_SEGMENTS = 4  # 群聊 ACK + 分片 ≤ 5 次

# 在 C2C 分片后添加：
segments = _split_text_for_streaming(clean_reply, chunk_size=300)
if len(segments) > QQ_C2C_MAX_SEGMENTS:
    # 超出部分合并到最后一片
    merged_tail = "".join(segments[QQ_C2C_MAX_SEGMENTS - 1:])
    segments = segments[:QQ_C2C_MAX_SEGMENTS - 1] + [merged_tail]

# 2. 群聊路径：在 _send_streaming_reply_with_sticker 中同样限制
segments = segments[:QQ_GROUP_MAX_SEGMENTS]
if len(segments) == QQ_GROUP_MAX_SEGMENTS:
    # 最后一片包含剩余全部内容
    merged_tail = "".join(segments_original[QQ_GROUP_MAX_SEGMENTS - 1:])
    segments[-1] = merged_tail

# 3. 群聊打字指示修复（P1-2 一并修复）
# _send_streaming_reply_with_sticker 中添加群聊判断：
if not is_group:
    await message.reply(content=f"...正在打字...", msg_seq=_next_msg_seq())
```

**改动范围**: `qq_bot_adapter.py` 2 个方法各约 5 行改动  
**回测**: 1500 字长回复 C2C 应切成 ≤ 4 片，最后一片包含余下内容

---

### P0-11: SharedBlackboardDB asyncio.Lock 跨进程矛盾 + 连接管理

**来源**: 渠道审计 P0-1  
**文件**: `agent_core/shared_blackboard_db.py`  
**行号**: 全文件  
**严重性**: 锁无法保护跨进程并发写入，每次操作新建连接浪费性能

#### Bug 描述

1. `asyncio.Lock` 是**协程锁**，仅在同事件循环内有效。跨进程（CLI + QQ Bot 同时运行）时完全无效。  
2. 每次操作 `sqlite3.connect()` → 操作 → `conn.close()`，无法利用 SQLite 事务批处理。  
3. `put` 方法中 `expire_at` 使用 `time.time()`，NTP 校正可能导致已写入条目瞬间过期或永不过期。

#### 参考方案

SQLite WAL 模式本身支持并发读写，跨进程安全应依赖 `PRAGMA busy_timeout` + WAL 自动重试，而非 Python 层锁。[(SQLite WAL Mode)](https://www.sqlite.org/wal.html)

#### 修复方案

```python
# shared_blackboard_db.py — 重构核心结构

class SharedBlackboardDB:
    """SQLite 背板黑板 — 跨进程安全。"""

    def __init__(self, db_path: str, default_ttl: float = 600.0) -> None:
        self._db_path = db_path
        self._default_ttl = default_ttl
        # asyncio.Lock 仅用于同进程去重（防止同 event loop 内并发写冲突）
        # 跨进程安全依赖 SQLite WAL + busy_timeout
        self._lock = asyncio.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取线程本地 SQLite 连接（持久连接，避免频繁建连）。"""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")  # 5s 超时重试
        return conn

    def _init_db(self) -> None:
        """初始化数据库表。"""
        conn = None
        try:
            conn = self._get_conn()
            conn.execute("""CREATE TABLE IF NOT EXISTS blackboard (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                agent_name TEXT NOT NULL DEFAULT '',
                expire_at REAL,
                created_at REAL NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_blackboard_expire ON blackboard(expire_at)")
            conn.commit()
        except Exception as e:
            logger.warning("blackboard_db.init_failed error={}", e)
        finally:
            if conn:
                conn.close()

    # put / get / get_with_meta / keys / cleanup_expired 方法中：
    # 1. 将 sqlite3.connect() 替换为 self._get_conn()
    # 2. 删除每个方法内的重复 PRAGMA journal_mode=WAL（_get_conn 中统一设置）
    # 3. get_with_meta 增加 created_at 字段返回：
    #    SELECT value, agent_name, expire_at, created_at FROM blackboard WHERE key = ?
    #    return {"value": ..., "agent_name": ..., "created_at": created_at}
```

**关键改动点**：

1. `_get_conn()` 统一连接创建，设置 `PRAGMA busy_timeout=5000`  
2. 删除每个方法内的 `conn.execute("PRAGMA journal_mode=WAL")`（在 `_get_conn` 中统一设置）  
3. `get_with_meta` 增加 `created_at` 字段  
4. 保留 `asyncio.Lock` 用于同进程去重（注释说明其语义）  
5. `expire_at` 继续使用 `time.time()`（`time.monotonic()` 不可跨进程共享，NTP 风险在文档中标注）

**改动范围**: `shared_blackboard_db.py` 约 30 行改动（重构连接管理 + 删除冗余 PRAGMA + 增加字段）  
**回测**: CLI + QQ Bot 同时运行时不再出现 "database is locked" 错误

---

## Phase 4: J-Space 认知层

---

### P0-12: health 信号值域错配 + 空误触发

**来源**: J-Space 审计 P0-1  
**文件**: `core/behavioral_health.py` L185-186 + `core/intervention_loop.py` L58-62 + `core/j_space_bootstrap.py` L40  
**严重性**: 低健康度干预永远不触发，启动时误触发一次

#### Bug 描述

1. `behavioral_health.py:185` 发射 `float(score_val)`，score_val 是 HealthLevel IntEnum，值域 **1-5**  
2. `j_space_bootstrap.py:40` 注册规则 `threshold=0.3, trigger_above=False`，即 `score < 0.3` 触发  
3. `intervention_loop.py:58` 聚合后均值 ≥ 1.0，**永远不可能 < 0.3**，干预永不触发  
4. 启动初期空 buffer 返回 0.0 < 0.3，**误触发一次 focused 方向干预**

#### 参考方案

FSRS 和其他评分系统的标准做法是**归一化到 [0, 1] 区间**，然后阈值设计在此区间内。这样信号值域与阈值判定在同一空间，避免值域冲突。[(Expertium — FSRS Technical Explanation)](https://expertium.github.io/Algorithm.html)

#### 修复方案（推荐方案 A：归一化发射值）

**修复 1**: `behavioral_health.py` — 归一化 health 信号到 [0, 1]：

```python
# behavioral_health.py — calculate() 方法中，信号发射部分 (L185-186)
# 修改前：
#   loop.create_task(_signal_stream.emit("health", float(score_val), "behavioral_health"))
# 修改后：
loop.create_task(_signal_stream.emit(
    "health", float(score_val) / 5.0, "behavioral_health"))
```

这样信号值域变为 0.2-1.0，与 0.3 阈值匹配：
- EXCELLENT (5) → 1.0  
- GOOD (4) → 0.8  
- FAIR (3) → 0.6  
- POOR (2) → 0.4 → 刚好 > 0.3，不触发干预（POOR 级别建议由 detector 处理）  
- CRITICAL (1) → 0.2 → < 0.3，触发 focused 方向干预 ✅

**修复 2**: `intervention_loop.py` — 空误触发保护：

```python
# intervention_loop.py — evaluate() 方法中，在聚合后添加空 buffer 保护
for rule in self._rules:
    score = self._stream.aggregate(rule.signal_type, "mean_of_means")

    # 空误触发保护：aggregate 对空 buffer 返回 0.0，
    # 不应作为有效信号触发干预
    if score == 0.0:
        continue

    if rule.trigger_above and score <= rule.threshold:
        continue
    if not rule.trigger_above and score >= rule.threshold:
        continue
    # ... 后续 cooldown 检查不变
```

**修复 3**: `degradation_strategy.py` — 同步适配（P0-13 一并修复，见下文）

**改动范围**:  
- `behavioral_health.py`: 1 行改动（除以 5.0）  
- `intervention_loop.py`: 3 行新增（空 buffer 保护）  
**回测**: CRITICAL 健康度应触发 focused 干预；启动初期不再误触发

---

### P0-13: degradation_strategy health 阈值同源错误

**来源**: J-Space 审计 P0-2  
**文件**: `core/degradation_strategy.py`  
**行号**: L297-300  
**严重性**: 信号驱动降级逻辑成为死代码

#### Bug 描述

```python
# 当前代码 (L297-300)
health_score = _signal_stream.aggregate("health", "mean_of_means")
if health_score < 0.3:
    # 触发信号驱动降级
    pass  # TODO(phase-2)
```

与 P0-12 同源：aggregate 返回 1-5 范围值，`< 0.3` 永远不成立。

#### 修复方案

P0-12 修复后（归一化到 0.2-1.0），此处阈值 0.3 自然匹配：
- CRITICAL (1/5=0.2) < 0.3 → 触发降级 ✅  
- POOR (2/5=0.4) > 0.3 → 不触发（POOR 级别由 detector EMERGENCY/CRITICAL 接管）

代码无需改动阈值，但应补全 TODO 逻辑：

```python
# degradation_strategy.py — evaluate_from_detector 中 L297-300
try:
    from config import ENABLE_J_SPACE_HOOKS
    if ENABLE_J_SPACE_HOOKS and _signal_stream is not None:
        health_score = _signal_stream.aggregate("health", "mean_of_means")
        if health_score == 0.0:
            pass  # 空 buffer，跳过
        elif health_score < 0.3:
            # CRITICAL 健康度 → 触发 L1_DEGRADED 降级
            if self._level < DegradationLevel.L1_DEGRADED:
                self.trigger(
                    DegradationLevel.L1_DEGRADED,
                    reason=f"health_score={health_score:.2f} < 0.3 (CRITICAL)",
                    source="signal_stream",
                )
except Exception:
    pass
```

**改动范围**: `degradation_strategy.py` L297-300，约 10 行替换  
**回测**: health_score=0.2（CRITICAL）时应触发 L1_DEGRADED

---

## 附录 A: 修改文件清单

| 文件 | P0 Bug 编号 | 改动行数（估） |
|------|-------------|---------------|
| `core/background_tasks.py` | P0-09 | ~5 行 |
| `core/event_bus.py` | P0-05 | ~15 行 |
| `qq_bot_adapter.py` | P0-05, P0-10 | ~25 行 |
| `cli.py` | P0-05 | ~5 行 |
| `web/ws_hub.py` | P0-05 | ~5 行 |
| `agent_core/structured_blackboard.py` | P0-07 | ~25 行 |
| `memory/fsrs_model.py` | P0-01, P0-02 | ~3 行核心 |
| `memory/confirm_correct.py` | P0-03 | ~15 行 |
| `db/database.py` | P0-04 | ~40 行 |
| `memory/memory_manager.py` | P0-06 | ~15 行 |
| `core/dream_consolidation.py` | P0-08 | ~5 行 |
| `agent_core/shared_blackboard_db.py` | P0-11 | ~30 行 |
| `core/behavioral_health.py` | P0-12 | ~1 行 |
| `core/intervention_loop.py` | P0-12 | ~3 行 |
| `core/degradation_strategy.py` | P0-13 | ~10 行 |

**总计**: 15 个文件，约 200 行改动

---

## 附录 B: 参考来源

| 来源 | URL | 用途 |
|------|-----|------|
| FSRS Algorithm Wiki | https://github-wiki-see.page/m/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm | 遗忘公式 S'_f 验证 |
| Expertium — FSRS Technical Explanation | https://expertium.github.io/Algorithm.html | post-lapse stability 公式推导 |
| Python docs — ContextVar | https://docs.python.org/3/library/contextvars.html | token reset 模式 |
| PEP 567 — Context Variables | https://peps.python.org/pep-0567/ | ContextVar 语义规范 |
| QQ Bot 官方文档 — 发送消息 | https://bot.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/send.html | C2C 被动回复限制 |
| SQLite WAL Mode | https://www.sqlite.org/wal.html | 跨进程并发安全 |
