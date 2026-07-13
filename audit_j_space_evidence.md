# J-Space 审计 Evidence 文件

## E-P0-1: health 信号值域与干预阈值不匹配

**声明**: intervention_loop 中 health 规则 threshold=0.3, trigger_above=False 永远不会触发，因为 behavioral_health 发射的信号值域为 1-5

**证据源**:
1. `core/j_space_bootstrap.py:40` — 规则定义: `InterventionRule("health", threshold=0.3, ..., trigger_above=False)`
2. `core/behavioral_health.py:185-186` — 信号发射: `_signal_stream.emit("health", float(score_val), ...)`, score_val 来自 `HealthLevel(IntEnum)` 取值 1-5
3. `core/intervention_loop.py:58` — 阈值判定: `score = self._stream.aggregate(rule.signal_type, "mean_of_means")`，随后 `if not rule.trigger_above and score >= rule.threshold: continue`
4. `core/behavioral_signal.py:69-72` — aggregate 对空 buffer 返回 0.0

**推理**: aggregate("health") 均值 ∈ [1.0, 5.0]（有信号时）或 0.0（无信号时）。0.0 < 0.3 → 启动误触发；[1.0, 5.0] 均 ≥ 0.3 → 永不触发。

---

## E-P0-2: degradation_strategy health 阈值同样不匹配

**声明**: degradation_strategy.evaluate_from_detector() 中 health_score < 0.3 永远不成立

**证据源**:
1. `core/degradation_strategy.py:297-300` — `health_score = _signal_stream.aggregate("health", "mean_of_means"); if health_score < 0.3`
2. 同 E-P0-1 证据 2-3

---

## E-P0-3: _spawn() 无事件循环保护

**声明**: asyncio.create_task() 在无 running loop 时抛 RuntimeError

**证据源**:
1. `core/background_tasks.py:63` — `task = asyncio.create_task(_wrapped())`，无 try/except
2. Python docs: `asyncio.create_task()` requires a running event loop

---

## E-P1-1: fire-and-forget Task 引用丢失

**声明**: loop.create_task() 返回的 Task 未保存引用，可能被 GC 回收

**证据源**:
1. `core/behavioral_health.py:185` — `loop.create_task(_signal_stream.emit(...))` 返回值未赋给任何变量
2. `core/agent_introspection.py:149` — 同上
3. Python docs: "Save a reference to the result of this function, to avoid a task disappearing mid-execution"

---

## E-P1-2: subscribe() 无 unsubscribe，内存泄漏

**声明**: BehavioralSignalStream._subscribers 列表只增不减

**证据源**:
1. `core/behavioral_signal.py:34` — `self._subscribers: dict[str, list[asyncio.Event]] = {}`
2. `core/behavioral_signal.py:53-54` — `ev = asyncio.Event(); self._subscribers[signal_type].append(ev)`
3. 全文件无 `unsubscribe` 或清理逻辑

---

## E-P1-3: start_monitoring() 无停止方法

**声明**: BehavioralHealthScorer 不持有 monitoring Task 引用

**证据源**:
1. `core/behavioral_health.py:260` — `task = loop.create_task(_loop())` 返回给调用方，self 不保存
2. 全类无 `stop_monitoring()` 或 `_monitor_task` 属性

---

## E-P1-4: introspection 访问私有方法

**声明**: agent_introspection.py 调用 scorer._collect_runtime_metrics()

**证据源**:
1. `core/agent_introspection.py:230` — `metrics = scorer._collect_runtime_metrics()`，方法名以 _ 开头

---

## E-P1-5: seed_baseline 篡改私有属性

**声明**: DegradationDetector.seed_baseline() 直接修改 BehaviorBaseline._ewma_mean / _ewma_var / _n

**证据源**:
1. `core/degradation_detector.py:148-152` — `baseline._ewma_mean = ...`, `baseline._ewma_var = ...`, `baseline._n = ...`
2. `security/anomaly_detector.py:58-61` — BehaviorBaseline 声明 `_ewma_mean`, `_ewma_var`, `_n` 为私有属性

---

## E-P1-6: _wire_hooks() 注入属性对 from-import 不可见

**声明**: Python from-import 在 import 时绑定名称，后续模块属性修改不影响已绑定名称

**证据源**:
1. `core/j_space_bootstrap.py:33` — `_ai._signal_stream = _signal_stream` 修改模块属性
2. Python language spec: `from X import Y` creates a new name binding at import time

---

## E-P1-7: clear_bg_tasks() 不取消任务

**声明**: _bg_tasks.clear() 后 Task 仍在 event loop 运行

**证据源**:
1. `core/background_tasks.py:248` — `_bg_tasks.clear()` 仅移除引用，不调用 task.cancel()
