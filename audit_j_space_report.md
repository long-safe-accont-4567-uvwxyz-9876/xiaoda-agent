# J-Space 认知架构层代码质量审计报告

**审计日期**: 2026-07-12  
**审计范围**: `core/` 下 11 个文件（behavioral_direction / behavioral_signal / behavioral_health / intent_decomposition / intervention_loop / j_space_bootstrap / agent_introspection / degradation_detector / degradation_strategy / background_tasks / bootstrap）  
**审计维度**: 运行时错误 / 异步问题 / 接口断裂 / 逻辑正确性 / EventBus-Blackboard 安全  

---

## Bug 总览

| 级别 | 数量 | 说明 |
|------|------|------|
| P0   | 3    | 必定导致功能失效或错误行为，需立即修复 |
| P1   | 7    | 高概率问题，在特定条件下触发，应尽快修复 |
| P2   | 5    | 低概率或设计债务，可排期修复 |

---

## P0 — 必须立即修复

### P0-1: health 信号值域与干预阈值严重不匹配（intervention_loop.py）

**文件**: `core/intervention_loop.py` + `core/j_space_bootstrap.py` + `core/behavioral_health.py`  
**位置**: `j_space_bootstrap.py:40` (规则定义) / `behavioral_health.py:185-186` (信号发射) / `intervention_loop.py:58-62` (阈值判定)

**描述**:  
`j_space_bootstrap.py` 注册的 health 干预规则为：

```python
InterventionRule("health", threshold=0.3, direction_name="focused",
                 alpha=0.5, mode="uniform", trigger_above=False, cooldown=60.0)
```

`trigger_above=False` 意味着当 `score < 0.3` 时触发干预。  
但 `behavioral_health.py:185-186` 发射的 health 信号值为 `float(score_val)`，其中 `score_val` 取值范围 **1–5**（HealthLevel IntEnum）。

`intervention_loop.py:58` 调用 `self._stream.aggregate("health", "mean_of_means")` 得到的均值必然 ≥ 1.0，**永远不可能 < 0.3**，因此 health 低分干预 **永远不会触发**。

更严重的是：在 **启动初期**（尚未发射任何 health 信号时），`aggregate()` 对空 buffer 返回 0.0，0.0 < 0.3 成立，会 **误触发一次 focused 方向干预**，之后因 cooldown=60s 不再触发，但首次触发是错误行为。

**修复建议**（二选一）：
- **方案 A**（推荐）：在 `behavioral_health.py:186` 归一化发射值：`"health", float(score_val) / 5.0`，使信号值域变为 0.2–1.0，与 0.3 阈值匹配。
- **方案 B**：将 `j_space_bootstrap.py:40` 的阈值改为 `threshold=2`（即 health < 2 时触发，对应 POOR/CRITICAL 级别），并在 `intervention_loop.py:58` 增加空 buffer 保护。

---

### P0-2: degradation_strategy 中 health 信号阈值同样不匹配

**文件**: `core/degradation_strategy.py`  
**行号**: 297–300

**描述**:  
```python
health_score = _signal_stream.aggregate("health", "mean_of_means")
if health_score < 0.3:
    # trigger signal-driven degradation
```

与 P0-1 同源：`aggregate("health", ...)` 返回 1–5 范围的值，`< 0.3` **永远不成立**，信号驱动降级逻辑成为死代码。

**修复建议**: 与 P0-1 同步修复。若采用方案 A 归一化，此处阈值可保持 0.3（对应 health score < 1.5/5 = 0.3）；若采用方案 B，此处改为 `health_score < 2`。

---

### P0-3: `_spawn()` 无事件循环保护，同步上下文调用必崩

**文件**: `core/background_tasks.py`  
**行号**: 63

**描述**:  
```python
task = asyncio.create_task(_wrapped())
```

`asyncio.create_task()` 要求当前线程有正在运行的 event loop。若 `_spawn()` 在同步上下文中被调用（例如从 `__init__` 或外部同步 API），将抛出 `RuntimeError: no running event loop` 且无任何捕获。

`BackgroundTaskManager.run_background_tasks()` 是非 async 方法，直接调用 `_spawn()`，调用方若未确保处于 async 上下文则会崩溃。

**修复建议**:
```python
def _spawn(coro: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error("bg.spawn_no_loop: cannot create task without running event loop")
        return
    task = loop.create_task(_wrapped())
    _bg_tasks.add(task)
    task.add_done_callback(_on_bg_task_done)
```

---

## P1 — 尽快修复

### P1-1: fire-and-forget Task 引用丢失，可能被 GC 中途回收

**文件**: `core/behavioral_health.py:185` / `core/agent_introspection.py:149`  
**行号**: behavioral_health.py:185, agent_introspection.py:149

**描述**:  
两处均使用 `loop.create_task(_signal_stream.emit(...))` 创建后台任务但未保存引用。Python 文档明确警告：event loop 仅持有 task 的弱引用，未引用的 task 可能被 GC 回收导致中途静默消失。

**修复建议**: 维护一个模块级 `_pending_tasks: set[asyncio.Task]`，创建 task 后加入集合，在 done callback 中移除：
```python
_pending_tasks: set[asyncio.Task] = set()

def _emit_safe(coro):
    task = loop.create_task(coro)
    _pending_tasks.add(task)
    task.add_done_callback(lambda t: _pending_tasks.discard(t))
```

---

### P1-2: BehavioralSignalStream.subscribe() 返回的 Event 无清理机制，内存泄漏

**文件**: `core/behavioral_signal.py`  
**行号**: 49–55

**描述**:  
`subscribe()` 向 `self._subscribers[signal_type]` 追加 `asyncio.Event`，但无 `unsubscribe()` 方法。订阅者销毁后 Event 对象仍留在列表中，`emit()` 每次迭代时会调用 `ev.set()`，长期运行导致：
1. 内存泄漏（Event 对象永不释放）
2. 性能退化（emit 遍历不断增长的列表）

**修复建议**: 增加 `unsubscribe(signal_type, event)` 方法，或改用弱引用（`weakref.WeakSet`）。

---

### P1-3: BehavioralHealthScorer.start_monitoring() 返回的 Task 无停止接口

**文件**: `core/behavioral_health.py`  
**行号**: 247–265

**描述**:  
`start_monitoring()` 创建一个永不退出的 `_loop()` 协程并返回 Task。调用方可 `cancel()` 该 Task，但类自身不持有引用、不提供 `stop_monitoring()` 方法。若需动态启停监控（如降级模式下关闭），无法从 Scorer 内部停止。

**修复建议**: 
```python
def __init__(self):
    self._monitor_task: asyncio.Task | None = None

def stop_monitoring(self) -> None:
    if self._monitor_task and not self._monitor_task.done():
        self._monitor_task.cancel()
    self._monitor_task = None
```

---

### P1-4: agent_introspection 访问 BehavioralHealthScorer 私有方法，封装破裂

**文件**: `core/agent_introspection.py`  
**行号**: 229–231

**描述**:  
```python
scorer = get_behavioral_health_scorer()
metrics = scorer._collect_runtime_metrics()  # ← 私有方法
score = scorer.calculate(metrics)
```

`_collect_runtime_metrics()` 是 `BehavioralHealthScorer` 的私有方法。若 Scorer 重构内部指标采集逻辑（如改用 push 模式），此调用将断裂且无编译期告警。

**修复建议**: 在 `BehavioralHealthScorer` 上暴露公共方法：
```python
def calculate_from_runtime(self) -> HealthScore:
    """从运行时指标自动计算健康评分（公共接口）"""
    return self.calculate(self._collect_runtime_metrics())
```
然后 introspection 调用 `scorer.calculate_from_runtime()`。

---

### P1-5: degradation_detector.seed_baseline() 直接篡改 BehaviorBaseline 私有属性

**文件**: `core/degradation_detector.py`  
**行号**: 148–152

**描述**:  
```python
baseline._ewma_mean = float(mean)
baseline._ewma_var = float(std) ** 2
baseline._n = max(self._min_baseline_samples, 10)
```

直接修改 `BehaviorBaseline` 的 `_ewma_mean`、`_ewma_var`、`_n` 私有属性。若 `BehaviorBaseline` 重命名或改用不同方差公式（如 Welford 在线算法），此处会静默写入错误值。

**修复建议**: 在 `BehaviorBaseline` 上增加 `seed(mean, std, n)` 公共方法。

---

### P1-6: _wire_hooks() 向外部模块注入属性，from-import 模式下不可见

**文件**: `core/j_space_bootstrap.py`  
**行号**: 30–54

**描述**:  
`_wire_hooks()` 通过 `module._signal_stream = _signal_stream` 注入全局变量。若下游模块使用 `from core.agent_introspection import _signal_stream`，该绑定在 import 时已固定为 `None`，后续 `_wire_hooks()` 修改模块属性不会更新已绑定的局部名称。

当前代码中下游均使用模块级 `_signal_stream`（非 from-import），所以暂时安全，但属于脆弱设计——任何新增 from-import 都会引入隐蔽 Bug。

**修复建议**: 在文档或类型检查中标注这些变量为 "wired by j_space_bootstrap, do not from-import"；或改用 getter 函数模式：
```python
# agent_introspection.py
_signal_stream_holder: list = [None]
def _get_signal_stream():
    return _signal_stream_holder[0]
```

---

### P1-7: clear_bg_tasks() 仅清集合不取消任务，导致孤立 Task

**文件**: `core/background_tasks.py`  
**行号**: 247–249

**描述**:  
```python
@staticmethod
def clear_bg_tasks() -> None:
    _bg_tasks.clear()
```

清空集合后，已创建的 `asyncio.Task` 仍在 event loop 中运行，但失去跟踪。这些孤立 Task 的异常不会被 `_on_bg_task_done` 回调记录，且无法在 shutdown 时统一 cancel。

**修复建议**:
```python
@staticmethod
def clear_bg_tasks() -> None:
    for task in list(_bg_tasks):
        if not task.done():
            task.cancel()
    _bg_tasks.clear()
```

---

## P2 — 排期修复

### P2-1: DirectionVector.apply_to_context() 静默丢弃未知维度

**文件**: `core/behavioral_direction.py`  
**行号**: 42–50

**描述**: `apply_to_context()` 只处理 `prompt/tool/emotion/route` 四个维度，其他 key 被静默跳过。若 DirectionVector 包含自定义维度（如 `reasoning`），应用时无任何效果也无告警。

**修复建议**: 对未识别的维度，记录 `logger.debug` 或写入 `context["unapplied_dims"]` 供调试。

---

### P2-2: DecomposedOutput.sparsity 硬编码引用 IntentDecomposer 类变量

**文件**: `core/intent_decomposition.py`  
**行号**: 38

**描述**:  
```python
total = len(IntentDecomposer.INTENT_DIMENSIONS)
```

若子类化 `IntentDecomposer` 并扩展 `INTENT_DIMENSIONS`，`DecomposedOutput.sparsity` 仍使用父类的维度数计算稀疏度，结果不准确。

**修复建议**: 在 `DecomposedOutput` 上增加 `total_dimensions` 字段，由 `IntentDecomposer._rule_encode()` 写入。

---

### P2-3: j_space_bootstrap 中 _create_default_directions() 被重复调用

**文件**: `core/j_space_bootstrap.py`  
**行号**: 72–74

**描述**:  
```python
for direction in _create_default_directions():  # 第一次调用
    _direction_registry.register(direction)
logger.info(f"... count={len(_create_default_directions())}")  # 第二次调用，仅用于计数
```

`_create_default_directions()` 在注册和日志两处各调用一次，每次创建新的 DirectionVector 对象。虽然无功能影响，但违反 DRY 且浪费分配。

**修复建议**: 缓存为局部变量 `default_dirs = _create_default_directions()`，复用。

---

### P2-4: DirectionVector.__add__() 硬编码 magnitude=1.0，丢失物理语义

**文件**: `core/behavioral_direction.py`  
**行号**: 30

**描述**: 方向叠加时 `magnitude` 被重置为 1.0 而非基于两个向量的 magnitude 计算（如向量合成的范数），导致叠加后的方向丢失强度信息。

**修复建议**: `magnitude = math.sqrt(sum(v**2 for v in merged.values()))` 或采用两向量 magnitudes 的某种聚合。

---

### P2-5: _should_run() 在 DB 异常时返回 False，周期任务可能永久静默

**文件**: `core/background_tasks.py`  
**行号**: 200–205

**描述**:  
```python
except (OSError, RuntimeError):
    logger.warning(...)
    return False
```

若 DB 持续异常（如 locked），所有周期任务（dream_archive / memory_distill / learning_promote 等）将永远返回 False 不执行，且仅有 warning 级日志，难以察觉。

**修复建议**: 在连续失败 N 次后提升日志级别为 error，或在 DB 恢复后主动补偿执行。

---

## 架构级风险（非单文件 Bug，但值得注意）

### ARCH-1: 信号值域缺乏全局约定

当前系统中 health 信号存在 **两个值域**：
- `behavioral_health.py` 发射 1–5 整数（HealthLevel IntEnum）
- `intervention_loop` / `degradation_strategy` 的阈值按 0–1 浮点设计

建议建立全局 `SIGNAL_SPEC` 注册表，声明每个 signal_type 的值域范围和方向（higher-is-worse / lower-is-worse），在 `emit()` 和 `aggregate()` 中自动校验。

### ARCH-2: fire-and-forget Task 无统一生命周期管理

`behavioral_health.py`、`agent_introspection.py`、`background_tasks.py` 三处各自用不同方式创建后台 Task，缺少统一的 Task 生命周期管理器（注册 / 取消 / 异常收集 / shutdown 联动）。

建议在 `core/task_lifecycle.py` 中提供统一原语，替代散落的 `loop.create_task()`。

---

## 审计结论

| 维度 | 发现 |
|------|------|
| 运行时错误 | P0-3（无 loop 崩溃）、P1-4（私有 API 断裂）、P1-5（私有属性篡改） |
| 异步问题 | P0-3（create_task 无 loop）、P1-1（Task 引用丢失）、P1-3（监控不可停）、P1-7（孤立 Task） |
| 接口断裂 | P0-1/P0-2（信号值域不匹配）、P1-6（from-import 不可见）、P2-1（维度静默丢弃） |
| 逻辑正确性 | P0-1/P0-2（health 阈值永不触发 + 启动误触发）、P2-2（sparsity 硬编码）、P2-4（magnitude 丢失） |
| EventBus/Blackboard 安全 | P1-2（subscribe 无 unsubscribe 泄漏）、ARCH-1（值域无全局约定） |

**最严重问题**: P0-1/P0-2 — health 信号值域（1–5）与干预/降级阈值（0.3）不匹配，导致 **低健康度干预和信号驱动降级均永远不会触发**，同时启动时存在一次 **误触发**。这是整个 J-Space 闭环的核心断裂点。
