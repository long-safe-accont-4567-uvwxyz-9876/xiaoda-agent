# nahida-agent P2 Bug 完整修复规格书

**项目**: nahida-agent — 本地部署单用户Agent，Python异步  
**版本基准**: 2026-07-12 审计快照  
**编制日期**: 2026-07-12  
**覆盖范围**: 4份审计报告中的全部 33 个 P2 级 Bug/建议  

---

## 目录

1. [模块一：核心调度层（7个P2）](#模块一核心调度层7个p2)
2. [模块二：记忆系统层（11个P2）](#模块二记忆系统层11个p2)
3. [模块三：J-Space认知层（5个P2）](#模块三j-space认知层5个p2)
4. [模块四：渠道适配层（10个P2）](#模块四渠道适配层10个p2)

---

## 模块一：核心调度层（7个P2）

> 审计来源：`code_quality_audit_report.md`

---

### P2-CORE-01: `delegate_to_agent` 名字检查元组重复

| 项目 | 内容 |
|------|------|
| 文件 | `agent_core/sub_agent_manager.py` |
| 行号 | L538 |
| 描述 | `if name in ("xiaoli", "xiaoli"):` 元组中两个元素完全相同，第二个是复制粘贴残留。 |

**修复方案**

```python
# --- 修改前 (L538) ---
if name in ("xiaoli", "xiaoli"):

# --- 修改后 ---
if name == "xiaoli":
```

**依据**: 元组去重是基本代码质量要求。参照 `_DEFAULT_MENTION_MAP` 中的别名模式（`"@可莉": "xiaoli"`, `"@小莉": "xiaoli"`），如需支持别名应改为 `if name in ("xiaoli", "可莉"):`，但当前调用链中 name 始终为 agent 内部名（如 `"xiaoli"`），直接 `==` 比较即可。  
**参考**: Python `in` 运算符对元组线性扫描，单值 `==` 更高效且语义清晰。

---

### P2-CORE-02: `gen_task_id` 的 `input_hint` 参数未使用

| 项目 | 内容 |
|------|------|
| 文件 | `core/event_bus.py` |
| 行号 | L96 |
| 描述 | `def gen_task_id(agent: str, input_hint: str = "") -> str:` — `input_hint` 在函数体内从未被引用，调用方也从未传入。 |

**修复方案**（推荐方案 B：使 ID 具备输入可溯性）

```python
# --- 修改前 (L96) ---
def gen_task_id(agent: str, input_hint: str = "") -> str:
    return f"{agent}-{uuid.uuid4().hex[:8]}"

# --- 修改后 ---
import hashlib

def gen_task_id(agent: str, input_hint: str = "") -> str:
    """生成 task_id。若提供 input_hint，拼接其前4位MD5使ID具备输入可溯性。"""
    if input_hint:
        hint_hash = hashlib.md5(input_hint.encode()).hexdigest()[:4]
        return f"{agent}-{hint_hash}-{uuid.uuid4().hex[:6]}"
    return f"{agent}-{uuid.uuid4().hex[:8]}"
```

**依据**: 保留 `input_hint` 参数并赋予实际意义，比删除更优——日志中出现 `xiaoli-a3f2-9c4e1b` 时可直接定位到特定输入，排查效率远高于纯随机ID。  
**参考**: [Python hashlib 文档](https://docs.python.org/3/library/hashlib.html)

---

### P2-CORE-03: `RouterEngine.decide` 每次调用重建正则模式

| 项目 | 内容 |
|------|------|
| 文件 | `core/router_engine.py` |
| 行号 | L171, L212, L231 |
| 描述 | `_build_negative_patterns()`、`_build_keyword_patterns()`、`_build_mention_map()` 在 `decide()` 和 `_match_mentions()` 中每次调用都重建，产生不必要的重复计算。 |

**修复方案**

```python
# --- 修改：在 RouterEngine.__init__ 中缓存 ---
class RouterEngine:
    def __init__(self):
        # 缓存构建结果，避免每次 decide() 重复构建
        self._negative_patterns: re.Pattern | None = None
        self._keyword_patterns: dict[str, re.Pattern] | None = None
        self._mention_map: dict[str, str] | None = None

    def _ensure_patterns_cached(self) -> None:
        """延迟构建并缓存正则模式。config 不热更新，缓存安全。"""
        if self._negative_patterns is None:
            self._negative_patterns = _build_negative_patterns()
        if self._keyword_patterns is None:
            self._keyword_patterns = _build_keyword_patterns()
        if self._mention_map is None:
            self._mention_map = _build_mention_map()

    def decide(self, user_input: str, ...) -> RoutingDecision:
        self._ensure_patterns_cached()
        # 后续使用 self._negative_patterns / self._keyword_patterns / self._mention_map
        ...
```

**依据**: 正则编译开销在热路径上累积显著。`@functools.lru_cache` 也可行但不适合含 `from config import` 的函数（config 对象不可 hash）。实例级缓存是更安全的选择——若未来支持 config 热更新，只需在更新时 `self._negative_patterns = None` 清除缓存。  
**参考**: [Python functools.lru_cache 文档](https://docs.python.org/3/library/functools.html#functools.lru_cache)；[Python re.compile 缓存机制](https://docs.python.org/3/library/re.html#re.compile)（Python 内部已缓存最近512个编译结果，但自定义构建逻辑不在其内）

---

### P2-CORE-04: `_match_mentions` 不去重，可产生重复 agent 调度

| 项目 | 内容 |
|------|------|
| 文件 | `core/router_engine.py` |
| 行号 | L228-234 |
| 描述 | 若用户输入包含同一 agent 的多个别名（如 `"@小莉 @可莉"`），targets 为 `["xiaoli", "xiaoli"]`，导致对同一子代理并行发两次相同请求。 |

**修复方案**

```python
# --- 修改前 (L228-234) ---
targets = []
for mention, agent in _build_mention_map().items():
    if mention in user_input:
        targets.append(agent)
return targets

# --- 修改后 ---
targets = []
for mention, agent in self._ensure_mention_map().items():  # 配合 P2-CORE-03 缓存
    if mention in user_input:
        targets.append(agent)
return list(dict.fromkeys(targets))  # 保序去重
```

**依据**: `dict.fromkeys()` 在 Python 3.7+ 保证插入序，是保序去重的惯用写法。`set` 去重会丢失顺序，而调度顺序可能影响优先级。  
**参考**: [DataCamp: Remove Duplicates From A List](https://www.datacamp.com/tutorial/python-how-to-remove-the-duplicates-from-a-list) — `list(dict.fromkeys(seq))` 是 Python 3.7+ 保序去重的标准模式

---

### P2-CORE-05: `_build_sub_agent_context` 访问私有属性 `_compressed_summary`

| 项目 | 内容 |
|------|------|
| 文件 | `agent_core/sub_agent_manager.py` |
| 行号 | ~L735 |
| 描述 | `if self.context._compressed_summary:` 直接访问 `Context` 对象的私有属性，若 Context 重构重命名此属性将静默断裂。 |

**修复方案**（推荐方案 A：暴露公共 property）

```python
# --- 在 Context 类中添加 (context.py) ---
class Context:
    @property
    def compressed_summary(self) -> str:
        """返回压缩后的上下文摘要（公共接口，替代直接访问 _compressed_summary）。"""
        return getattr(self, "_compressed_summary", "")

# --- 修改调用处 (sub_agent_manager.py ~L735) ---
# 修改前:
if self.context._compressed_summary:
# 修改后:
if self.context.compressed_summary:
```

**依据**: `getattr(self, "_compressed_summary", "")` 作为 property 实现既提供公共接口，又保持内部属性名的灵活性。Python 私有属性（`_` 前缀）是约定而非强制，但跨类访问仍是封装破裂信号。  
**参考**: [Python getattr 防御性访问模式](https://www.pyinns.com/python/built-in-function/getattr-python-2026-dynamic-attribute-access-modern-patterns-safety) — `getattr(obj, name, default)` 防止 AttributeError 的标准模式

---

### P2-CORE-06: `CancelToken.is_cancelled` 属性有副作用

| 项目 | 内容 |
|------|------|
| 文件 | `core/cancel_token.py` |
| 行号 | L60-66 |
| 描述 | `is_cancelled` property 在读取时会修改 `_cancelled` 和 `_reason`（fallback timeout 检测），违反属性只读惯例，调试器/日志框架读取会触发状态变更。 |

**修复方案**

```python
# --- 修改前 (L60-66) ---
@property
def is_cancelled(self) -> bool:
    if self._cancelled:
        return True
    # fallback: check if timeout elapsed
    if self._timeout and time.monotonic() > self._deadline:
        self._cancelled = True
        self._reason = "timeout"
        return True
    return False

# --- 修改后 ---
def check(self) -> bool:
    """主动检查是否已取消。含 fallback timeout 检测，有副作用——仅应在调度点调用。"""
    if self._cancelled:
        return True
    if self._timeout and time.monotonic() > self._deadline:
        self._cancelled = True
        self._reason = "timeout"
        return True
    return False

@property
def is_cancelled(self) -> bool:
    """纯读取，无副作用。调试器/日志可安全访问。"""
    return self._cancelled
```

然后将所有调用 `token.is_cancelled` 且需要 timeout 检测的位置改为 `token.check()`。

**依据**: Python 社区共识——property getter 不应有副作用。[Effective Python](https://blog.csdn.net/wolfpirelee/article/details/148337658) 明确指出"不要在 getter 中修改其他属性"。将副作用逻辑移到显式方法 `check()` 符合最小惊讶原则。  
**参考**: [Python Alchemist: Stop Writing Getters and Setters](https://www.pythonalchemist.com/blog/property-decorator-magic) — "A getter should not modify state. It should only return a value. If reading a value changes something, use a method with a verb name like `get_and_increment()` to make the side effect obvious."

---

### P2-CORE-07: `belief_router._save_to_db` 的 `run_in_executor` Future 未 await

| 项目 | 内容 |
|------|------|
| 文件 | `belief_router.py` |
| 行号 | L174 |
| 描述 | `loop.run_in_executor(None, _do_save)` 返回的 Future 被丢弃，进程退出前最后一次信念更新会丢失。 |

**修复方案**

```python
# --- 修改前 (L174) ---
loop.run_in_executor(None, _do_save)

# --- 修改后 ---
import asyncio

_background_saves: set[asyncio.Task] = set()

def _save_to_db(self):
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, _do_save)
    task = asyncio.ensure_future(future)  # 包装为 Task 以便追踪

    def _on_save_done(t: asyncio.Task):
        _background_saves.discard(t)
        if t.cancelled():
            return
        if exc := t.exception():
            logger.error(f"belief_save_failed: {exc}")

    task.add_done_callback(_on_save_done)
    _background_saves.add(task)
```

**依据**: Python 官方文档明确警告——event loop 仅持有 task 的弱引用，未引用的 task 可能被 GC 回收。使用 `set` + `add_done_callback` 是 fire-and-forget 模式的标准做法。  
**参考**: [Python asyncio 官方文档](https://docs.python.org/zh-cn/3.14/library/asyncio-task.html#asyncio.create_task) — "保存一个指向此函数结果的引用，以避免任务在执行过程中消失。事件循环将只保留对任务的弱引用。"

---

## 模块二：记忆系统层（11个P2）

> 审计来源：`audit_fsrs_dsr_report.md`

---

### P2-MEM-01: `MemoryPhase` 枚举值与数据库 `phase` 列字符串可能不匹配

| 项目 | 内容 |
|------|------|
| 文件 | `memory/fsrs_model.py` + `memory/confirm_correct.py` |
| 行号 | fsrs_model.py:8-14, confirm_correct.py:65 |
| 描述 | 如果数据库中存了非法字符串（如 `"decay"` 而非 `"decayed"`），`MemoryPhase(r.get("phase", "buffer"))` 会抛 `ValueError` 导致整条记忆被跳过。 |

**修复方案**

```python
# --- 修改 _apply_fsrs_scoring 中 (memory_manager.py ~L1488) ---
# 修改前:
phase = MemoryPhase(r.get("phase", "buffer"))

# 修改后:
try:
    phase = MemoryPhase(r.get("phase", "buffer"))
except ValueError:
    logger.warning(f"fsrs_invalid_phase: id={r.get('id')}, phase={r.get('phase')!r}, fallback=buffer")
    phase = MemoryPhase.BUFFER
```

同时建议在 `MemoryPhase` 类上增加类方法：

```python
class MemoryPhase(str, Enum):
    BUFFER = "buffer"
    REINFORCED = "reinforced"
    DECAY = "decayed"
    PERMANENT = "permanent"

    @classmethod
    def safe(cls, value: str) -> "MemoryPhase":
        """从字符串安全构造枚举，非法值 fallback 到 BUFFER。"""
        try:
            return cls(value)
        except ValueError:
            return cls.BUFFER
```

**依据**: Python Enum 从字符串构造时，非法值直接抛 `ValueError`。生产环境中数据库可能存在历史脏数据，需要防御性处理。  
**参考**: [Python Enum 安全回退模式](https://docs.pingcode.com/insights/gml804fkefc8dhqwkod19cmj) — "最佳实践是包裹转换逻辑并提供安全回退：以 try/except 捕获 ValueError 或 KeyError，返回一个保底枚举成员"

---

### P2-MEM-02: `_apply_recall` 中 `growth` 可为负数（WEAK_HIT + 高 difficulty）

| 项目 | 内容 |
|------|------|
| 文件 | `memory/fsrs_model.py` |
| 行号 | L130-133 |
| 描述 | 当 D 因浮点误差超过 10 时，`difficulty_factor < 0`，`growth < 0`，`S_new < S`，形成恶性循环。 |

**修复方案**

```python
# --- 修改 _apply_recall 中 (fsrs_model.py ~L132) ---
# 修改前:
difficulty_factor = (10.0 - D) / 9.0
growth = growth_factor * difficulty_factor * retrievability_bonus

# 修改后:
difficulty_factor = max(0.0, (10.0 - D) / 9.0)  # 防御性保护
growth = growth_factor * difficulty_factor * retrievability_bonus
```

**依据**: FSRS 标准 公式中 `(11-D)` 确保 D≤10 时为正，但浮点精度可能使 D 略超 10（如 `10.0000000001`）。`max(0.0, ...)` 是零成本防御，不影响正常路径。  
**参考**: [FSRS 算法技术说明](https://brainwo.github.io/expertium.github.io/fsrs/2024/08/04/Algorithm.html) — `SInc = 1 + (11 - D) · S^{-w9} · f(R)`，D 的合法范围为 [1,10]

---

### P2-MEM-03: FluidMemory.score() 中 `peak_weight` 参数被传入但与 FSRS weight 双重降权

| 项目 | 内容 |
|------|------|
| 文件 | `memory/fluid_memory.py` |
| 行号 | L26 |
| 描述 | `score()` 中 `similarity * peak_weight * R`，但 FSRS 状态中 `weight = min(1.0, R)`，两边同时应用 peak_weight 和 R 会双重降权。 |

**修复方案**

```python
# --- 修改 fluid_memory.py score() ---
# 修改前:
final_score = similarity * peak_weight * R

# 修改后:
# 当 FSRS 状态可用时，R 已内含衰减权重，不再额外乘 peak_weight
if state is not None:  # FSRS 路径
    final_score = similarity * R
else:  # 旧路径（无 FSRS 状态）
    final_score = similarity * peak_weight * R

# 同时添加弃用警告
import warnings
if peak_weight != 1.0 and state is not None:
    warnings.warn(
        "peak_weight is ignored when FSRS state is available; "
        "FSRS weight already incorporates decay via R(t). "
        "peak_weight will be removed in a future version.",
        DeprecationWarning,
        stacklevel=2,
    )
```

**依据**: FSRS 的 `R(t)` 已完整表达记忆衰减，`peak_weight` 是旧 FluidMemory 的遗留概念。混用导致评分偏差。弃用警告给调用方迁移窗口。  
**参考**: [FSRS Retrievability 公式](https://open-spaced-repetition.github.io/py-fsrs/fsrs.html) — `R = (1 + FACTOR * elapsed / stability) ** DECAY`

---

### P2-MEM-04: v15 迁移 `last_review DEFAULT 0` 与 FSRS 算法语义冲突

| 项目 | 内容 |
|------|------|
| 文件 | `db/database.py` |
| 行号 | L999-1001 |
| 描述 | `last_review REAL DEFAULT 0` 意味着旧数据 last_review=0（Unix epoch），直接用 `state.retrievability(now)` 时 elapsed_days≈20000，R≈0，记忆会被立即归档。 |

**修复方案**（v16 迁移）

```python
# --- 在 database.py 迁移链中增加 v16 ---
async def _migrate_v16(self) -> None:
    """回填 last_review=0 的行为 timestamp 值，避免 FSRS 评分 R≈0。"""
    # episodic_memories 表
    await self._conn.execute("""
        UPDATE episodic_memories
        SET last_review = timestamp
        WHERE last_review = 0 AND timestamp > 0
    """)
    # concept_nodes 表（如有 last_review 列）
    try:
        await self._conn.execute("""
            UPDATE concept_nodes
            SET last_review = created_at
            WHERE last_review = 0 AND created_at > 0
        """)
    except Exception:
        pass  # concept_nodes 可能无 last_review 列
    await self._conn.commit()
    logger.info("v16 migration: backfilled last_review=0 rows with timestamp")
```

**依据**: SQLite WAL 模式下 `UPDATE ... WHERE` 是高效操作，只影响 last_review=0 的行。回填为 timestamp 使 `elapsed_days` 从创建时间算起，语义虽非精确（创建≠复习），但远优于 Unix epoch。  
**参考**: [SQLite WAL 模式持久性](https://www.sqlite.org/wal.html) — WAL 模式是持久设置，回填操作在 WAL 下性能良好

---

### P2-MEM-05: DreamConsolidator `m.strength = R` 覆盖了原始 strength（正反馈衰减）

| 项目 | 内容 |
|------|------|
| 文件 | `core/dream_consolidation.py` |
| 行号 | L112-113 |
| 描述 | `m.strength = R` 直接将 strength 覆盖为当前 Retrievability，导致正反馈衰减——记忆一旦开始遗忘就会加速被遗忘。 |

**修复方案**

```python
# --- 修改前 (L112-113) ---
m.strength = R

# --- 修改后 ---
m.strength = max(m.strength * 0.95, R)
# 0.95 每次衰减 5%，R 作为下限保证衰减不会低于实际可检索性
# 当 R > strength*0.95 时（例如刚被强化），取 R
```

**依据**: Ebbinghaus 遗忘曲线的核心特征是**减速衰减**——遗忘速度随时间递减。直接覆盖 `strength = R` 等价于 `strength_new = f(t)`，而 `f(t)` 是加速衰减函数，与理论矛盾。`max(strength * 0.95, R)` 模拟了减速衰减：每次最多降 5%，但不会低于实际 R 值。  
**参考**: [FSRS Power-Law 遗忘曲线](https://loadwords.com/learn/docs/spaced-repetition/fsrs-algorithm/) — `R(t,S) = (1 + t/(9S))^{-1}`，衰减速率随时间递减

---

### P2-MEM-06: DreamConsolidator 是有状态单例但 `consolidate_from_db` 不更新内部 `_memories`

| 项目 | 内容 |
|------|------|
| 文件 | `core/dream_consolidation.py` |
| 行号 | L155-290 |
| 描述 | `consolidate_from_db` 操作局部 `memories` 字典，不更新 `self._memories`，导致 `stats()` 统计不准确。 |

**修复方案**

```python
# --- 在 consolidate_from_db 方法末尾添加 ---
async def consolidate_from_db(self, ...):
    memories = {}
    # ... 原有逻辑 ...

    # 同步更新内部状态
    self._memories.update(memories)
    # 移除已被归档的记忆
    archived_ids = {mid for mid, m in memories.items() if m.phase == "archived"}
    for aid in archived_ids:
        self._memories.pop(aid, None)
```

**依据**: 单例模式要求内部状态一致。`stats()` 被 Web UI 和监控调用，如果 `_memories` 不反映 DB 实际状态，统计无意义。

---

### P2-MEM-07: `_migrate_v15` 中 UPDATE 在 ALTER ADD COLUMN 后幂等性脆弱

| 项目 | 内容 |
|------|------|
| 文件 | `db/database.py` |
| 行号 | L1032-1037 |
| 描述 | v15 迁移逐列检查是否 ALTER，但 UPDATE 语句无保护，二次执行可能误更新新创建的 buffer 记忆。 |

**修复方案**

```python
# --- 修改前 (v15 迁移逻辑) ---
async def _migrate_v15(self) -> None:
    if "phase" not in epi_cols:
        await self._conn.execute("ALTER TABLE ...")
    await self._conn.execute("UPDATE ... SET phase='permanent' WHERE access_count >= 5 AND phase = 'buffer'")
    ...

# --- 修改后 ---
async def _migrate_v15(self) -> None:
    if self._version >= 15:
        return  # 整体跳过已完成的迁移
    # ... 原有 ALTER/UPDATE 逻辑 ...
    self._version = 15
    await self._conn.commit()
```

更根本的修复是在迁移入口增加整体版本检查：

```python
async def _run_migrations(self):
    current = await self._get_user_version()
    for version, migrator in self._MIGRATIONS.items():
        if current < version:
            await migrator(self)
            await self._set_user_version(version)
```

**依据**: SQLite 支持 `PRAGMA user_version` 做迁移版本标记，比逐列检查更可靠。  
**参考**: [SQLite PRAGMA user_version](https://www.sqlite.org/pragma.html#pragma_user_version)

---

### P2-MEM-08: `confirm_correct.correct()` 新节点 `insert_node` 缺少 `source_mem_id` 和 FSRS 列

| 项目 | 内容 |
|------|------|
| 文件 | `memory/confirm_correct.py` |
| 行号 | L119-132 |
| 描述 | 创建新节点时未传入 `source_mem_id`、`difficulty`、`stability`、`phase`、`last_review`、`reinforcement_count`，导致纠正后记忆从零开始 FSRS 评分。 |

**修复方案**

```python
# --- 修改 confirm_correct.py correct() 中创建新节点部分 ---
# 修改前:
await self.db.insert_node(
    node_id=new_id, concept=new_concept, weight=..., peak_weight=..., confidence=..., access_count=1, keys=merged_keys
)

# --- 修改后 ---
now_ts = time.time()
await self.db.insert_node(
    node_id=new_id,
    concept=new_concept,
    weight=old_node.get("weight", 0.5),       # 继承旧节点权重
    peak_weight=old_node.get("peak_weight", 1.0),
    confidence=old_node.get("confidence", 0.5),
    access_count=1,
    keys=merged_keys,
    # FSRS 状态：继承旧节点的 stability/difficulty，重置复习计数
    source_mem_id=old_node.get("source_mem_id") or old_node.get("id"),
    difficulty=old_node.get("difficulty", 5.0),
    stability=old_node.get("stability", 3.0),
    phase="reinforced",                        # 纠正后立即进入强化阶段
    last_review=now_ts,
    reinforcement_count=0,
)
```

**依据**: 纠正是记忆的"强化"行为，新节点应继承旧节点的 FSRS 学习历史（stability/difficulty），仅重置 reinforcement_count 和 last_review。

---

### P2-MEM-09: `estimate_initial_difficulty` 边界值测试不足

| 项目 | 内容 |
|------|------|
| 文件 | `memory/fsrs_model.py` |
| 行号 | L80-92 |
| 描述 | D_INIT + 调整项可能达到 9.5，虽在 [1,10] 内但边界紧密。 |

**修复方案**（增加单元测试）

```python
# --- 新增测试文件 tests/test_fsrs_model.py ---
import pytest
from memory.fsrs_model import FSRSModel

class TestEstimateInitialDifficulty:
    def setup_method(self):
        self.model = FSRSModel()

    def test_short_neutral_text(self):
        """短文本 + 无情感 + 无关键词 → D ≈ 5.0"""
        d = self.model.estimate_initial_difficulty("你好")
        assert 4.0 <= d <= 6.0

    def test_long_emotional_abstract(self):
        """长文本 + 情感 + 抽象关键词 → D 接近上限 9.5"""
        d = self.model.estimate_initial_difficulty(
            "我深刻理解了存在主义哲学中关于荒诞的概念，" * 20  # >200 字符
        )
        assert d <= 10.0
        assert d >= 8.0  # 应在高区间

    def test_fact_keywords(self):
        """事实关键词 → D 降低"""
        d_fact = self.model.estimate_initial_difficulty("日期是2024年1月1日")
        d_normal = self.model.estimate_initial_difficulty("你好世界")
        assert d_fact < d_normal

    def test_boundary_never_exceeds_10(self):
        """极端组合不超出 [1,10]"""
        d = self.model.estimate_initial_difficulty("抽象" * 200 + "！")
        assert 1.0 <= d <= 10.0
```

**依据**: 边界值测试是算法正确性的基础保障。当前代码虽然逻辑正确，但缺乏自动化回归保护。

---

### P2-MEM-10: CognitiveMemory 与 FSRS-DSR 完全断联，两套衰减逻辑并存

| 项目 | 内容 |
|------|------|
| 文件 | `memory/cognitive_memory.py` |
| 行号 | 全文件 |
| 描述 | CognitiveMemory 有自己的 salience/decay_factor/access_count 衰减体系，与 FSRS 的 D/S/R 体系完全独立，两套状态机互不感知。 |

**修复方案**（推荐：标记废弃 + 迁移指南）

```python
# --- 在 cognitive_memory.py 文件头部添加 ---
"""
⚠️ DEPRECATED: 本模块使用独立的衰减逻辑，与 FSRS-DSR 不兼容。
新代码应使用 memory.fsrs_model.FSRSModel 和 memory.confirm_correct 模块。

迁移路径:
  - CognitiveMemory.salience  → FSRSModel.retrievability()
  - CognitiveMemory.decay_factor → MemoryState.stability
  - CognitiveMemory.consolidate() → DreamConsolidator.consolidate_from_db()

预计移除版本: v0.5.0
"""
import warnings

warnings.warn(
    "CognitiveMemory is deprecated. Use FSRSModel + DreamConsolidator instead.",
    DeprecationWarning,
    stacklevel=2,
)
```

**依据**: 两套衰减逻辑并存是架构债务，短期无法统一（影响面太大），但标记废弃可阻止新代码继续依赖。  
**参考**: Python 标准库 `warnings.warn(DeprecationWarning)` 是官方推荐的废弃标记方式。

---

### P2-MEM-11: `db_concept.py` 中 `insert_node` 使用 `INSERT OR REPLACE` 会静默覆盖已有节点

| 项目 | 内容 |
|------|------|
| 文件 | `db/db_concept.py` |
| 行号 | L44-56 |
| 描述 | `INSERT OR REPLACE` 在主键冲突时删除旧行再插入新行，旧行的 FSRS 状态被新默认值覆盖。 |

**修复方案**

```python
# --- 修改前 (db_concept.py ~L44-56) ---
INSERT OR REPLACE INTO concept_nodes (id, concept, ...) VALUES (?, ?, ...)

# --- 修改后: 使用 UPSERT 精确更新指定列 ---
INSERT INTO concept_nodes (id, concept, weight, peak_weight, confidence,
                           access_count, keys, source_mem_id, difficulty,
                           stability, phase, last_review, reinforcement_count)
VALUES (:id, :concept, :weight, :peak_weight, :confidence,
        :access_count, :keys, :source_mem_id, :difficulty,
        :stability, :phase, :last_review, :reinforcement_count)
ON CONFLICT(id) DO UPDATE SET
    concept = excluded.concept,
    weight = excluded.weight,
    peak_weight = excluded.peak_weight,
    confidence = excluded.confidence,
    access_count = excluded.access_count,
    keys = excluded.keys,
    source_mem_id = CASE
        WHEN excluded.source_mem_id IS NOT NULL THEN excluded.source_mem_id
        ELSE concept_nodes.source_mem_id
    END
    -- difficulty/stability/phase/last_review 不在 UPDATE 中，
    -- 确保已有 FSRS 状态不被默认值覆盖
```

**依据**: SQLite `INSERT OR REPLACE` = `DELETE + INSERT`，会触发 DELETE 触发器、重置自增主键、覆盖未 SET 的列为默认值。`ON CONFLICT ... DO UPDATE` (UPSERT) 是 SQLite 3.24+ 推荐替代，精确控制更新哪些列。  
**参考**: [SQLite UPSERT 官方文档](https://www2.sqlite.org/lang_UPSERT.html)；[SQLite ON CONFLICT 子句](https://www.sqlite.org/lang_conflict.html) — "REPLACE 先删除再插入，UPSERT 直接更新指定列"

---

## 模块三：J-Space认知层（5个P2）

> 审计来源：`audit_j_space_report.md`

---

### P2-JSPACE-01: DirectionVector.apply_to_context() 静默丢弃未知维度

| 项目 | 内容 |
|------|------|
| 文件 | `core/behavioral_direction.py` |
| 行号 | L42-50 |
| 描述 | 只处理 `prompt/tool/emotion/route` 四个维度，其他 key 被静默跳过，无告警。 |

**修复方案**

```python
# --- 修改 apply_to_context() ---
def apply_to_context(self, context: dict) -> None:
    known_dims = {"prompt", "tool", "emotion", "route"}
    applied = set()
    for dim, value in self.dimensions.items():
        if dim in known_dims:
            context[f"direction_{dim}"] = value
            applied.add(dim)
        else:
            logger.debug(f"direction_unapplied_dim: dim={dim}, value={value}")

    unapplied = set(self.dimensions.keys()) - known_dims
    if unapplied:
        context.setdefault("unapplied_dims", []).extend(unapplied)
```

**依据**: 静默丢弃是调试黑洞。`logger.debug` 不影响生产性能，但在开发模式下可发现配置错误。`unapplied_dims` 写入 context 供上层诊断。

---

### P2-JSPACE-02: DecomposedOutput.sparsity 硬编码引用 IntentDecomposer 类变量

| 项目 | 内容 |
|------|------|
| 文件 | `core/intent_decomposition.py` |
| 行号 | L38 |
| 描述 | `total = len(IntentDecomposer.INTENT_DIMENSIONS)` — 子类化后仍使用父类的维度数。 |

**修复方案**

```python
# --- 修改 DecomposedOutput ---
@dataclass
class DecomposedOutput:
    values: dict[str, float]
    total_dimensions: int = 0  # 新增字段

    @property
    def sparsity(self) -> float:
        total = self.total_dimensions or len(IntentDecomposer.INTENT_DIMENSIONS)
        return len(self.values) / max(total, 1)

# --- 修改 IntentDecomposer._rule_encode() ---
def _rule_encode(self, ...) -> DecomposedOutput:
    return DecomposedOutput(
        values=result,
        total_dimensions=len(self.INTENT_DIMENSIONS),  # 动态写入
    )
```

**依据**: 数据类携带元数据是面向对象的标准做法。`total_dimensions` 字段使 `DecomposedOutput` 自包含，不依赖外部类引用。

---

### P2-JSPACE-03: `_create_default_directions()` 被重复调用

| 项目 | 内容 |
|------|------|
| 文件 | `core/j_space_bootstrap.py` |
| 行号 | L72-74 |
| 描述 | `_create_default_directions()` 在注册和日志两处各调用一次，每次创建新 DirectionVector 对象。 |

**修复方案**

```python
# --- 修改前 ---
for direction in _create_default_directions():
    _direction_registry.register(direction)
logger.info(f"... count={len(_create_default_directions())}")

# --- 修改后 ---
default_dirs = _create_default_directions()
for direction in default_dirs:
    _direction_registry.register(direction)
logger.info(f"... count={len(default_dirs)}")
```

**依据**: DRY 原则。缓存局部变量是零成本优化，消除重复对象分配。

---

### P2-JSPACE-04: DirectionVector.__add__() 硬编码 magnitude=1.0，丢失物理语义

| 项目 | 内容 |
|------|------|
| 文件 | `core/behavioral_direction.py` |
| 行号 | L30 |
| 描述 | 方向叠加时 magnitude 被重置为 1.0，丢失强度信息。 |

**修复方案**

```python
# --- 修改前 ---
def __add__(self, other: "DirectionVector") -> "DirectionVector":
    merged = {k: self.dimensions.get(k, 0) + other.dimensions.get(k, 0)
              for k in set(self.dimensions) | set(other.dimensions)}
    return DirectionVector(name=f"{self.name}+{other.name}",
                           dimensions=merged, magnitude=1.0)

# --- 修改后 ---
import math

def __add__(self, other: "DirectionVector") -> "DirectionVector":
    merged = {k: self.dimensions.get(k, 0) + other.dimensions.get(k, 0)
              for k in set(self.dimensions) | set(other.dimensions)}
    magnitude = math.sqrt(sum(v ** 2 for v in merged.values()))
    return DirectionVector(name=f"{self.name}+{other.name}",
                           dimensions=merged,
                           magnitude=max(magnitude, 0.001))  # 避免 0
```

**依据**: 向量合成的范数 `||a+b|| = √(Σv²)` 是标准物理语义。magnitude=0 时下游可能除零，设最小值 0.001 防御。

---

### P2-JSPACE-05: `_should_run()` 在 DB 异常时返回 False，周期任务可能永久静默

| 项目 | 内容 |
|------|------|
| 文件 | `core/background_tasks.py` |
| 行号 | L200-205 |
| 描述 | DB 持续异常时所有周期任务永远返回 False，仅有 warning 级日志，难以察觉。 |

**修复方案**

```python
# --- 修改前 ---
except (OSError, RuntimeError):
    logger.warning(...)
    return False

# --- 修改后 ---
class _PeriodicTaskState:
    def __init__(self):
        self._consecutive_failures: dict[str, int] = {}

    def record_failure(self, task_name: str) -> None:
        count = self._consecutive_failures.get(task_name, 0) + 1
        self._consecutive_failures[task_name] = count
        if count >= 5:
            logger.error(f"periodic_task_possibly_dead: task={task_name}, "
                         f"consecutive_failures={count}")
        elif count >= 3:
            logger.warning(f"periodic_task_degraded: task={task_name}, "
                           f"consecutive_failures={count}")
        else:
            logger.warning(f"periodic_task_db_error: task={task_name}")

    def record_success(self, task_name: str) -> None:
        self._consecutive_failures.pop(task_name, None)

# 在 _should_run 中使用
try:
    # ... DB check ...
    self._task_state.record_success(task_name)
    return True
except (OSError, RuntimeError) as e:
    self._task_state.record_failure(task_name)
    return False
```

**依据**: 连续失败计数是运维可观测性的基础。5次以上提升为 `error` 级别可触发告警系统。  
**参考**: [生产级 asyncio 模式](https://timderzhavets.com/blog/taming-asyncio-production-patterns-that-prevent-silent/) — "silent failures are the most insidious asyncio bug"

---

## 模块四：渠道适配层（10个P2）

> 审计来源：`audit/channel_audit_report.md`

---

### P2-CHAN-01: EmotionState._save 在主线程同步写文件

| 项目 | 内容 |
|------|------|
| 文件 | `emotion/emotion_state.py` |
| 行号 | L170-180 |
| 描述 | `_save` 在 `update()` 中同步调用 `write_text`，短暂阻塞事件循环。 |

**修复方案**

```python
# --- 修改前 ---
def _save(self) -> None:
    self._persist_path.write_text(json.dumps(self._data, ensure_ascii=False))

# --- 修改后 ---
async def _save(self) -> None:
    """异步持久化，避免阻塞事件循环。"""
    data = json.dumps(self._data, ensure_ascii=False)
    await asyncio.to_thread(self._persist_path.write_text, data, encoding="utf-8")

# --- 修改 update() 调用处 ---
async def update(self, ...):
    # ... 原有逻辑 ...
    await self._save()
```

**依据**: `asyncio.to_thread()` (Python 3.9+) 是将阻塞 I/O 卸载到线程池的推荐方式，不阻塞事件循环。  
**参考**: [Python asyncio.to_thread 文档](https://docs.python.org/3/library/asyncio-task.html#asyncio.to_thread)；[Fix Python asyncio Blocking the Event Loop](https://www.fixdevs.com/blog/python-async-sync-mix/) — "For blocking I/O, use `asyncio.to_thread()` or switch to an async library"

---

### P2-CHAN-02: SharedBlackboardDB 每个操作都执行 PRAGMA journal_mode=WAL

| 项目 | 内容 |
|------|------|
| 文件 | `agent_core/shared_blackboard_db.py` |
| 行号 | 每个方法中 |
| 描述 | WAL 模式是持久设置，只需在 `_init_db` 中设置一次。每次操作重复设置是冗余开销。 |

**修复方案**

```python
# --- 修改前：每个方法中都有 ---
conn.execute("PRAGMA journal_mode=WAL")

# --- 修改后：仅在 _init_db 中设置一次 ---
def _init_db(self, conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""CREATE TABLE IF NOT EXISTS ...""")
    # ...

# --- 删除所有其他方法中的 PRAGMA journal_mode=WAL 行 ---
```

**依据**: SQLite WAL 模式一旦设置即持久化到数据库文件，后续连接自动继承。反复设置是无意义的额外 SQL 执行。  
**参考**: [SQLite WAL 官方文档](https://www.sqlite.org/wal.html) — "WAL mode is persistent — you only need to set it once per database file"；[How to Improve SQLite INSERT Performance](https://www.sqlprostudio.com/blog/50-how-to-improve-sqlite-insert-performance)

---

### P2-CHAN-03: CLIUser/CLIEventBus 绑定缺少 ContextVar token 保护

| 项目 | 内容 |
|------|------|
| 文件 | `cli.py` L189-195, `web/ws_hub.py` L280-290 |
| 描述 | EventBus 使用 `bind_user` / `unbind_user` 是 set(None) 模式，不如 ContextVar token 模式安全。 |

**修复方案**

```python
# --- 修改 EventBus 绑定接口 ---
class EventBus:
    _current_user: ContextVar[User | None] = ContextVar("_current_user", default=None)

    @classmethod
    def bind_user(cls, user: User) -> contextvars.Token:
        """绑定当前协程的用户，返回 token 用于安全解绑。"""
        return cls._current_user.set(user)

    @classmethod
    def unbind_user(cls, token: contextvars.Token) -> None:
        """使用 token 安全解绑，不会影响其他协程的绑定。"""
        cls._current_user.reset(token)

# --- 修改调用处 (cli.py) ---
event_bus = EventBus()
token = event_bus.bind_user(CLIUser())
try:
    result = self._loop.run_until_complete(
        self.bot.process(user_input, user_id="cli_owner", source="cli",
                         status_callback=status_notify)
    )
finally:
    event_bus.unbind_user(token)  # 使用 token 解绑

# --- 修改调用处 (ws_hub.py) 同理 ---
```

**依据**: `ContextVar.set()` 返回的 `Token` 对象配合 `reset(token)` 是 Python 官方推荐的安全解绑模式——`reset` 只撤销该 token 对应的 set 操作，不会误清其他协程的绑定。  
**参考**: [PEP 567 – Context Variables](https://peps.python.org/pep-0567/) — "ContextVar.reset(token) restores the variable to the value it had before the set() operation that created the token"；[Python ContextVar token as context manager (3.14+)](https://docs.python.org/ja/3.15/library/contextvars.html)

---

### P2-CHAN-04: SubAgent._chat_loop 中 `_inject_dsml_if_needed` 未定义

| 项目 | 内容 |
|------|------|
| 文件 | `agent_dispatcher.py` |
| 行号 | `_chat_loop` 方法（约 L330） |
| 描述 | 调用了 `_inject_dsml_if_needed` 方法，但在可见源码中未看到定义。 |

**修复方案**

```python
# --- 步骤1: 确认方法是否存在 ---
# 运行: python -c "from agent_dispatcher import SubAgent; print(hasattr(SubAgent, '_inject_dsml_if_needed'))"
# 如果返回 True → 方法在截断部分定义，无需修复，但应添加单元测试
# 如果返回 False → 需要实现该方法

# --- 步骤2: 如果方法缺失，实现如下 ---
def _inject_dsml_if_needed(self, working_messages: list, tools: list,
                           is_reasoning: bool, tool_names: list[str]) -> list:
    """为推理模型注入 DSML (Domain-Specific Markup Language) 上下文。"""
    if not is_reasoning:
        return tools
    # 推理模型可能需要额外的工具描述标记
    enhanced_tools = []
    for tool in tools:
        if isinstance(tool, dict):
            tool_copy = tool.copy()
            tool_copy.setdefault("description", "")
            tool_copy["description"] += "\n[DSML: structured_output_required]"
            enhanced_tools.append(tool_copy)
        else:
            enhanced_tools.append(tool)
    return enhanced_tools
```

**依据**: 方法缺失会导致 `AttributeError`，属于运行时崩溃风险。需先确认是否存在再决定实现策略。

---

### P2-CHAN-05: ToolCallHandler._notify_tool_status 中 task_id 始终为空

| 项目 | 内容 |
|------|------|
| 文件 | `tool_engine/tool_call_handler.py` |
| 行号 | L135-155 |
| 描述 | `AgentEvent` 的 `task_id` 用 `getattr(self, "_task_id", "")` 获取，但类从未设置此属性。 |

**修复方案**

```python
# --- 修改 ToolCallHandler ---
class ToolCallHandler:
    def __init__(self):
        self._task_id: str = ""

    async def handle(self, tool_name: str, args: dict, ...) -> ...:
        # 生成 task_id 并绑定到当前处理流程
        self._task_id = gen_task_id(agent="tool", input_hint=tool_name)
        try:
            # ... 原有逻辑 ...
            pass
        finally:
            self._task_id = ""  # 清理

    def _notify_tool_status(self, event_type, ...):
        event = AgentEvent(
            type=event_type,
            task_id=self._task_id,  # 现在有值了
            ...
        )
```

**依据**: `gen_task_id` 已在 `core/event_bus.py` 中定义，直接复用。绑定到 `handle()` 生命周期确保同一工具调用的事件可关联。

---

### P2-CHAN-06: model_router.refresh_client 中旧客户端关闭使用 fire-and-forget

| 项目 | 内容 |
|------|------|
| 文件 | `model_router.py` |
| 行号 | `refresh_client` 方法 |
| 描述 | `loop.create_task(old.close())` 未 await 也未保存引用，连接可能泄漏。 |

**修复方案**

```python
# --- 修改前 ---
loop.create_task(old.close())

# --- 修改后 ---
_old_clients_to_close: list = []

# 在 refresh_client 中
_old_clients_to_close.append(old)

# 在方法末尾或定期清理
async def _cleanup_old_clients():
    if _old_clients_to_close:
        clients = _old_clients_to_close[:]
        _old_clients_to_close.clear()
        await asyncio.gather(*[c.close() for c in clients], return_exceptions=True)
```

或在 `refresh_client` 中直接 await（如果上下文允许）：

```python
# 如果 refresh_client 是 async 方法
await asyncio.gather(*[old.close() for old in old_clients], return_exceptions=True)
```

**依据**: `asyncio.gather(return_exceptions=True)` 并发等待所有 close 完成，异常不会传播但会被记录。  
**参考**: [Python asyncio 官方文档](https://docs.python.org/3/library/asyncio-task.html) — fire-and-forget task 需保存强引用防止 GC

---

### P2-CHAN-07: GreetingScheduler._deferred_lock 使用 threading.Lock 但在 async 上下文

| 项目 | 内容 |
|------|------|
| 文件 | `web/greeting_scheduler.py` |
| 行号 | L30 |
| 描述 | `_deferred_lock` 是 `threading.Lock`，但 `_tick` 是 async 方法，`with self._deferred_lock:` 会阻塞事件循环。 |

**修复方案**

```python
# --- 修改前 ---
self._deferred_lock = threading.Lock()

# --- 修改后 ---
self._deferred_lock = asyncio.Lock()

# --- 修改 _tick 中的使用 ---
# 修改前:
with self._deferred_lock:
    pending, self._deferred = self._deferred, []

# 修改后:
async with self._deferred_lock:
    pending, self._deferred = self._deferred, []
```

**依据**: `threading.Lock` 的 `with` 语句是同步阻塞操作，在 async 函数中使用会阻塞整个事件循环。`asyncio.Lock` 通过 `await` 协作式调度，不阻塞。  
**参考**: [asyncio.Lock vs threading.Lock](https://docs.bswen.com/blog/2026-04-20-asyncio-lock-shared-resources/) — "threading.Lock blocks the entire thread, including event loop. asyncio.Lock yields to the event loop — other tasks can run while waiting."

---

### P2-CHAN-08: `user_base.py` 中 AGENT_DISPLAY 硬编码显示名

| 项目 | 内容 |
|------|------|
| 文件 | `agent_core/user_base.py` |
| 行号 | L23-29 |
| 描述 | `AGENT_DISPLAY` 字典硬编码了子代理显示名，但名称可由 config 自定义，此处不同步。 |

**修复方案**

```python
# --- 修改前 ---
AGENT_DISPLAY = {
    "xiaoli": "小莉",
    "xiaolang": "小狼",
    "xiaolian": "小涟",
    "xiaoke": "小可",
    "xiaoda": "小妲",
}

# --- 修改后: 懒初始化从 config 读取 ---
from config import get_agent_display_name

_AGENT_DISPLAY: dict[str, str] | None = None

def get_agent_display_map() -> dict[str, str]:
    """动态获取子代理显示名映射，从 config 读取而非硬编码。"""
    global _AGENT_DISPLAY
    if _AGENT_DISPLAY is None:
        # 从 config 中已知 agent 名列表构建
        from config.agents import list_agent_names
        _AGENT_DISPLAY = {
            name: get_agent_display_name(name)
            for name in list_agent_names()
        }
    return _AGENT_DISPLAY

# 使用处改为:
# AGENT_DISPLAY[name] → get_agent_display_map()[name]
```

**依据**: 配置外部化是项目的基本工程规范。硬编码与配置文件的显示名不一致时，用户看到矛盾的输出。

---

### P2-CHAN-09: ws_hub.py 中 `_handle_chat` 缺少 image_data 错误处理

| 项目 | 内容 |
|------|------|
| 文件 | `web/ws_hub.py` |
| 行号 | L260-280 |
| 描述 | `encode_image_to_base64` 返回空或异常时，image_data 列表包含无效条目，传给 LLM 后可能导致错误。 |

**修复方案**

```python
# --- 修改 _handle_chat 中图片处理部分 ---
# 修改前:
image_data.append({
    "mimeType": mime_type,
    "data": encoded
})

# --- 修改后 ---
encoded = encode_image_to_base64(image_url)
if not encoded or not encoded.strip():
    logger.warning(f"ws_hub_image_skip: url={image_url}, reason=empty_base64")
    continue  # 跳过无效图片

# 额外验证 base64 数据长度（空图片通常 < 100 字符）
if len(encoded) < 100:
    logger.warning(f"ws_hub_image_suspicious: url={image_url}, "
                   f"data_length={len(encoded)}")
    continue

image_data.append({
    "mimeType": mime_type,
    "data": encoded
})
```

**依据**: 空 base64 数据传给 LLM 会触发 API 错误。防御性检查是调用外部 API 的基本要求。  
**参考**: [Base64 编码验证](https://www.pythontutorials.net/blog/check-if-a-string-is-encoded-in-base64-using-python/) — "A valid image will have non-zero bytes. Print `len(binary_data)` — if it's 0, the Base64 string was invalid."

---

### P2-CHAN-10: config.py 中 `.env` 迁移逻辑在 Docker 中可能误触发

| 项目 | 内容 |
|------|------|
| 文件 | `config.py` |
| 行号 | `get_env_path()` 函数 |
| 描述 | Docker 中 volume 挂载点初始为空，镜像内 `.env` 存在，每次容器重启都会执行迁移并打印日志噪音。 |

**修复方案**

```python
# --- 修改 get_env_path() ---
def get_env_path() -> Path:
    user_env = USER_DATA_DIR / ".env"
    legacy_env = LEGACY_DIR / ".env"
    migration_marker = USER_DATA_DIR / ".env.migrated"

    if user_env.exists():
        return user_env

    # 检查迁移标记，避免重复迁移
    if migration_marker.exists():
        return user_env  # 已迁移过，即使文件不存在也不再迁移

    if legacy_env.exists() and not user_env.exists():
        shutil.copy2(legacy_env, user_env)
        # 创建迁移标记文件
        migration_marker.touch()
        logger.info(f"[config] .env migrated from {legacy_env} to {user_env}")

    return user_env
```

**依据**: 幂等迁移需要标记文件。`shutil.copy2` 不覆盖已有文件，但日志噪音是运维负担。标记文件 `.env.migrated` 使迁移只执行一次。  
**参考**: [shutil.copy2 文档](https://docs.python.org/zh-tw/3/library/shutil.html) — copy2 保留元数据但不提供幂等标记

---

## 附录A：修复优先级建议

虽然全部为 P2 级别，但按**修复性价比**排序推荐：

| 优先级 | Bug ID | 理由 |
|--------|--------|------|
| 🔴 高 | P2-CORE-07 | Future 未 await = 数据丢失风险，5分钟修完 |
| 🔴 高 | P2-MEM-01 | 枚举 ValueError = 整条记忆评分被跳过，1行 try/except |
| 🔴 高 | P2-CORE-06 | property 有副作用 = 调试器触发状态变更，需拆分方法 |
| 🔴 高 | P2-CHAN-05 | task_id 为空 = 事件无法关联，影响全链路追踪 |
| 🔴 高 | P2-CHAN-07 | threading.Lock 阻塞事件循环，改 asyncio.Lock 即可 |
| 🟡 中 | P2-CORE-04 | 重复调度 = 资源浪费，1行去重 |
| 🟡 中 | P2-MEM-04 | last_review=0 = 旧数据 R≈0，迁移修复 |
| 🟡 中 | P2-MEM-11 | INSERT OR REPLACE = 覆盖 FSRS 状态，改 UPSERT |
| 🟡 中 | P2-CHAN-03 | ContextVar token = 并发安全基础 |
| 🟡 中 | P2-CHAN-01 | 同步写文件阻塞事件循环 |
| 🟢 低 | P2-CORE-01 | 元组重复，纯代码质量 |
| 🟢 低 | P2-CORE-02 | 未使用参数，可快速修 |
| 🟢 低 | P2-CORE-03 | 正则缓存，性能优化 |
| 🟢 低 | P2-CORE-05 | 私有属性访问，封装改善 |
| 🟢 低 | P2-MEM-02 | growth 负数防御，加 max 即可 |
| 🟢 低 | P2-MEM-03 | peak_weight 弃用 |
| 🟢 低 | P2-MEM-05 | 正反馈衰减，改衰减公式 |
| 🟢 低 | P2-MEM-06 | _memories 不更新 |
| 🟢 低 | P2-MEM-07 | 迁移幂等性 |
| 🟢 低 | P2-MEM-08 | insert_node 缺 FSRS 列 |
| 🟢 低 | P2-MEM-09 | 单元测试 |
| 🟢 低 | P2-MEM-10 | CognitiveMemory 废弃标记 |
| 🟢 低 | P2-JSPACE-01~05 | J-Space 层 P2 均为低频触发 |
| 🟢 低 | P2-CHAN-02 | 冗余 PRAGMA |
| 🟢 低 | P2-CHAN-04 | 方法确认 |
| 🟢 低 | P2-CHAN-06 | fire-and-forget 客户端关闭 |
| 🟢 低 | P2-CHAN-08 | 硬编码名称 |
| 🟢 低 | P2-CHAN-09 | 图片错误处理 |
| 🟢 低 | P2-CHAN-10 | Docker 迁移噪音 |

---

## 附录B：跨模块依赖关系

```
P2-CORE-03 (正则缓存) ← P2-CORE-04 (mention 去重)  [同一文件 router_engine.py]
P2-CORE-06 (property 副作用) ← P2-CORE-07 (Future 未 await)  [异步安全主题]
P2-MEM-04 (last_review=0) ← P2-MEM-01 (枚举 ValueError)  [FSRS 评分链]
P2-MEM-11 (INSERT OR REPLACE) ← P2-MEM-08 (缺 FSRS 列)  [同一文件 db_concept.py]
P2-CHAN-03 (ContextVar token) ← P2-CORE-06 (CancelToken property)  [并发安全主题]
```

建议按依赖链分组修复，避免修复一个 Bug 引入另一个。

---

*文档结束 — 全部 33 个 P2 Bug 修复规格完整覆盖*
