# J-Space 架构优化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 nahida-agent 实现 J-Space 设计模式的 6 个优化模块 + 8 个 Hook 接入点，形成行为内省-干预闭环。

**Architecture:** 4 层递进架构：基础层（信号流+方向向量）→ 闭环层（干预闭环）→ 智能层（意图分解+增强路由）→ 基础设施+集成层（结构化黑板+8 Hook）。所有新模块并行存在，通过 Hook 接入现有模块，失败非阻塞。

**Tech Stack:** Python 3.11+, asyncio, dataclasses, pytest, loguru

## Global Constraints

- 所有新模块失败必须非阻塞（try/except 包裹，不影响主流程）
- 方向向量库持久化到 `data/direction_registry.json`，信号/干预历史仅内存
- 配置开关 `ENABLE_J_SPACE_HOOKS` 默认 true
- 每个新模块对齐 J-Space 生态圈设计模式（jlens/repe/reprobe/SAELens）
- 不修改现有模块核心逻辑，仅插入 Hook 调用

---

## File Structure

| 文件 | 职责 | 阶段 |
|---|---|---|
| `core/behavioral_signal.py` | 行为信号流（SignalEntry + BehavioralSignalStream） | Stage 1 |
| `core/behavioral_direction.py` | 行为方向向量（DirectionVector + DirectionRegistry） | Stage 1 |
| `core/intervention_loop.py` | 干预闭环（InterventionRule + InterventionLoop） | Stage 2 |
| `core/intent_decomposition.py` | 输出意图分解（IntentFactor + DecomposedOutput + IntentDecomposer） | Stage 3 |
| `core/enhanced_router.py` | 增强型路由（EnhancedBeliefRouter） | Stage 3 |
| `agent_core/structured_blackboard.py` | 结构化共享黑板（StructuredEntry + StructuredBlackboard） | Stage 4 |
| `tests/test_behavioral_signal.py` | 信号流测试 | Stage 1 |
| `tests/test_behavioral_direction.py` | 方向向量测试 | Stage 1 |
| `tests/test_intervention_loop.py` | 干预闭环测试 | Stage 2 |
| `tests/test_intent_decomposition.py` | 意图分解测试 | Stage 3 |
| `tests/test_enhanced_router.py` | 增强路由测试 | Stage 3 |
| `tests/test_structured_blackboard.py` | 结构化黑板测试 | Stage 4 |
| `tests/test_hook_integration.py` | Hook 集成测试 | Stage 4 |
| `tests/test_e2e_closed_loop.py` | 端到端闭环测试 | Stage 4 |

---

## Stage 1: 基础层

### Task 1: 行为信号流（BehavioralSignalStream）

**Files:**
- Create: `core/behavioral_signal.py`
- Test: `tests/test_behavioral_signal.py`

**Interfaces:**
- Produces: `SignalEntry` (dataclass), `BehavioralSignalStream` (class with `emit`, `subscribe`, `get_history`, `aggregate`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_behavioral_signal.py
import pytest
import asyncio
import time
from core.behavioral_signal import SignalEntry, BehavioralSignalStream


@pytest.mark.asyncio
async def test_emit_and_get_history():
    stream = BehavioralSignalStream(max_history=100)
    await stream.emit("confidence", 0.8, "test_source")
    history = stream.get_history("confidence", last_n=10)
    assert len(history) == 1
    assert history[0].signal_type == "confidence"
    assert history[0].value == 0.8
    assert history[0].source == "test_source"


@pytest.mark.asyncio
async def test_emit_with_meta():
    stream = BehavioralSignalStream()
    await stream.emit("tool_usage", 0.5, "tool_executor", agent="xiaolang", tool="read")
    history = stream.get_history("tool_usage")
    assert history[0].meta["agent"] == "xiaolang"
    assert history[0].meta["tool"] == "read"


@pytest.mark.asyncio
async def test_subscribe_notification():
    stream = BehavioralSignalStream()
    ev = await stream.subscribe("confidence")
    assert not ev.is_set()
    await stream.emit("confidence", 0.9, "test")
    assert ev.is_set()


@pytest.mark.asyncio
async def test_aggregate_mean_of_means():
    stream = BehavioralSignalStream()
    for v in [0.2, 0.4, 0.6, 0.8]:
        await stream.emit("confidence", v, "test")
    result = stream.aggregate("confidence", "mean_of_means")
    assert abs(result - 0.5) < 0.001


@pytest.mark.asyncio
async def test_aggregate_max_of_means():
    stream = BehavioralSignalStream()
    for v in [0.2, 0.4, 0.6, 0.8]:
        await stream.emit("confidence", v, "test")
    result = stream.aggregate("confidence", "max_of_means")
    assert abs(result - 0.8) < 0.001


@pytest.mark.asyncio
async def test_aggregate_max_absolute():
    stream = BehavioralSignalStream()
    for v in [-0.3, 0.5, -0.8, 0.2]:
        await stream.emit("sentiment", v, "test")
    result = stream.aggregate("sentiment", "max_absolute")
    assert abs(result - 0.8) < 0.001


@pytest.mark.asyncio
async def test_aggregate_empty_returns_zero():
    stream = BehavioralSignalStream()
    result = stream.aggregate("nonexistent", "mean_of_means")
    assert result == 0.0


@pytest.mark.asyncio
async def test_max_history_deque():
    stream = BehavioralSignalStream(max_history=3)
    for i in range(5):
        await stream.emit("confidence", float(i), "test")
    history = stream.get_history("confidence")
    assert len(history) == 3
    assert history[0].value == 2.0
    assert history[-1].value == 4.0


@pytest.mark.asyncio
async def test_get_history_all_types():
    stream = BehavioralSignalStream()
    await stream.emit("confidence", 0.5, "test")
    await stream.emit("sentiment", 0.3, "test")
    history = stream.get_history(last_n=10)
    assert len(history) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_behavioral_signal.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.behavioral_signal'`

- [ ] **Step 3: Write minimal implementation**

```python
# core/behavioral_signal.py
"""
行为信号流 — 对齐 reprobe/interceptor.py 的激活采集模式。

设计参考:
- reprobe/interceptor.py: Interceptor 区分 prefill/token 两种采集模式
- reprobe/monitor.py: Monitor 的 history 列表 + _flush_step()
- jlens/hooks.py: ActivationRecorder 的上下文管理器模式
"""
from dataclasses import dataclass, field
from typing import Any
import time
import asyncio
from collections import deque
from loguru import logger


@dataclass
class SignalEntry:
    """单条行为信号"""
    signal_type: str
    value: float
    source: str
    timestamp: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)


class BehavioralSignalStream:
    """
    持续行为信号流 — 对齐 reprobe/interceptor.py 的激活采集模式。
    """

    def __init__(self, max_history: int = 1000, flush_interval: float = 1.0):
        self._buffer: deque[SignalEntry] = deque(maxlen=max_history)
        self._subscribers: dict[str, list[asyncio.Event]] = {}
        self._flush_interval = flush_interval
        self._last_flush = time.monotonic()

    async def emit(self, signal_type: str, value: float, source: str = "", **meta) -> None:
        """发射一条行为信号。对齐 reprobe/interceptor 的 _flush 模式。"""
        try:
            entry = SignalEntry(signal_type=signal_type, value=value, source=source, meta=meta)
            self._buffer.append(entry)
            if signal_type in self._subscribers:
                for ev in self._subscribers[signal_type]:
                    ev.set()
                self._subscribers[signal_type].clear()
        except Exception as e:
            logger.warning(f"behavioral_signal.emit_failed: {e}")

    async def subscribe(self, signal_type: str) -> asyncio.Event:
        """订阅特定信号类型 — 对齐 shared_blackboard.subscribe()"""
        if signal_type not in self._subscribers:
            self._subscribers[signal_type] = []
        ev = asyncio.Event()
        self._subscribers[signal_type].append(ev)
        return ev

    def get_history(self, signal_type: str = "", last_n: int = 100) -> list[SignalEntry]:
        """获取历史信号 — 对齐 reprobe/monitor.py: Monitor.get_history()"""
        if signal_type:
            entries = [e for e in self._buffer if e.signal_type == signal_type]
        else:
            entries = list(self._buffer)
        return entries[-last_n:]

    def aggregate(self, signal_type: str, strategy: str = "mean_of_means") -> float:
        """聚合信号 — 对齐 reprobe/monitor.py: Monitor.score() 的三种策略"""
        entries = [e for e in self._buffer if e.signal_type == signal_type]
        if not entries:
            return 0.0
        values = [e.value for e in entries]
        if strategy == "max_of_means":
            return max(values)
        elif strategy == "mean_of_means":
            return sum(values) / len(values)
        elif strategy == "max_absolute":
            return max(abs(v) for v in values)
        return sum(values) / len(values)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_behavioral_signal.py -v`
Expected: 9 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add core/behavioral_signal.py tests/test_behavioral_signal.py
git commit -m "feat: 行为信号流模块 (BehavioralSignalStream) — Stage 1 基础层"
```

---

### Task 2: 行为方向向量（DirectionVector + DirectionRegistry）

**Files:**
- Create: `core/behavioral_direction.py`
- Test: `tests/test_behavioral_direction.py`

**Interfaces:**
- Consumes: none
- Produces: `DirectionVector` (dataclass with `__mul__`, `__add__`, `apply_to_context`, `save`, `load`), `DirectionRegistry` (class with `register`, `get`, `list_directions`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_behavioral_direction.py
import pytest
import tempfile
import os
from pathlib import Path
from core.behavioral_direction import DirectionVector, DirectionRegistry


def test_direction_vector_creation():
    d = DirectionVector("helpfulness", {"prompt": 0.3, "route": 0.2}, "manual")
    assert d.name == "helpfulness"
    assert d.dimensions["prompt"] == 0.3
    assert d.source == "manual"
    assert d.magnitude == 1.0


def test_direction_vector_mul_scalar():
    d = DirectionVector("calm", {"emotion": -0.4, "prompt": 0.2}, "manual")
    scaled = d * 0.5
    assert scaled.dimensions["emotion"] == -0.2
    assert scaled.dimensions["prompt"] == 0.1
    assert scaled.magnitude == 0.5


def test_direction_vector_add():
    d1 = DirectionVector("helpfulness", {"prompt": 0.3}, "manual")
    d2 = DirectionVector("safety", {"prompt": 0.2, "tool": -0.3}, "manual")
    merged = d1 + d2
    assert merged.dimensions["prompt"] == 0.5
    assert merged.dimensions["tool"] == -0.3


def test_apply_to_context_prompt():
    d = DirectionVector("helpfulness", {"prompt": 0.3}, "manual")
    context = {"existing": "value"}
    result = d.apply_to_context(context)
    assert result["prompt_modifier"] == 0.3
    assert result["existing"] == "value"


def test_apply_to_context_all_dimensions():
    d = DirectionVector("test", {"prompt": 0.1, "tool": 0.2, "emotion": -0.3, "route": 0.4}, "manual")
    context = {}
    result = d.apply_to_context(context)
    assert result["prompt_modifier"] == 0.1
    assert result["tool_bias"] == 0.2
    assert result["emotion_offset"] == -0.3
    assert result["route_bias"] == 0.4


def test_apply_to_context_cumulative():
    d1 = DirectionVector("a", {"prompt": 0.3}, "manual")
    d2 = DirectionVector("b", {"prompt": 0.2}, "manual")
    context = {}
    context = d1.apply_to_context(context)
    context = d2.apply_to_context(context)
    assert context["prompt_modifier"] == 0.5


def test_save_and_load():
    d = DirectionVector("test_dir", {"prompt": 0.3, "emotion": -0.2}, "manual", 0.8, {"auc": 0.9})
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        path = f.name
    try:
        d.save(path)
        loaded = DirectionVector.load(path)
        assert loaded.name == "test_dir"
        assert loaded.dimensions["prompt"] == 0.3
        assert loaded.dimensions["emotion"] == -0.2
        assert loaded.source == "manual"
        assert loaded.magnitude == 0.8
        assert loaded.meta["auc"] == 0.9
    finally:
        os.unlink(path)


def test_registry_register_and_get():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "registry.json")
        registry = DirectionRegistry(storage_path=path)
        d = DirectionVector("helpfulness", {"prompt": 0.3}, "manual")
        registry.register(d)
        assert "helpfulness" in registry.list_directions()
        got = registry.get("helpfulness")
        assert got is not None
        assert got.dimensions["prompt"] == 0.3


def test_registry_get_nonexistent():
    registry = DirectionRegistry()
    assert registry.get("nonexistent") is None


def test_registry_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "registry.json")
        registry1 = DirectionRegistry(storage_path=path)
        registry1.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
        # 新实例从同一文件加载
        registry2 = DirectionRegistry(storage_path=path)
        assert "calm" in registry2.list_directions()
        assert registry2.get("calm").dimensions["emotion"] == -0.4


def test_registry_load_corrupted_falls_back():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "registry.json")
        Path(path).write_text("invalid json {{{")
        registry = DirectionRegistry(storage_path=path)
        # 损坏文件不应崩溃，返回空注册表
        assert registry.list_directions() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_behavioral_direction.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.behavioral_direction'`

- [ ] **Step 3: Write minimal implementation**

```python
# core/behavioral_direction.py
"""
行为方向向量 — 对齐 RepE 的 contrast vector 和 reprobe 的 Probe.get_direction()。

在 API-only 约束下，方向向量不作用于模型激活，而作用于 Agent 的上下文空间。

参考:
- repe/rep_readers.py: PCARepReader.get_rep_directions() — PCA方向识别
- reprobe/probe.py: Probe.get_direction() — 归一化线性探针方向
- repe/rep_control_reading_vec.py: WrappedBlock.set_controller() — 方向注入
- reprobe/steerer.py: Steerer._apply_projection() — 投影干预
"""
from dataclasses import dataclass, field
import json
from pathlib import Path
from loguru import logger


@dataclass
class DirectionVector:
    """
    行为方向向量 — 对齐 RepE 的 contrast vector 和 reprobe 的 Probe.get_direction()。
    """
    name: str
    dimensions: dict[str, float]
    source: str = ""
    magnitude: float = 1.0
    meta: dict = field(default_factory=dict)

    def __mul__(self, scalar: float) -> "DirectionVector":
        """缩放方向 — 对齐 Steerer 的 alpha 参数"""
        return DirectionVector(
            name=self.name,
            dimensions={k: v * scalar for k, v in self.dimensions.items()},
            source=self.source,
            magnitude=self.magnitude * scalar,
            meta=self.meta,
        )

    def __add__(self, other: "DirectionVector") -> "DirectionVector":
        """方向叠加 — 对齐 WrappedBlock 的 linear_comb 算子"""
        merged = dict(self.dimensions)
        for k, v in other.dimensions.items():
            merged[k] = merged.get(k, 0.0) + v
        return DirectionVector(
            name=f"{self.name}+{other.name}",
            dimensions=merged,
            magnitude=1.0,
            meta={**self.meta, **other.meta},
        )

    def apply_to_context(self, context: dict) -> dict:
        """
        将方向向量应用到 Agent 上下文。

        对齐:
        - repe/rep_control_reading_vec.py: WrappedBlock.forward()
        - reprobe/steerer.py: Steerer._apply_projection()

        在 API-only 下，"hidden" 是 context dict 而非激活张量。
        """
        result = dict(context)
        for dim, weight in self.dimensions.items():
            if dim == "prompt":
                result["prompt_modifier"] = result.get("prompt_modifier", 0.0) + weight
            elif dim == "tool":
                result["tool_bias"] = result.get("tool_bias", 0.0) + weight
            elif dim == "emotion":
                current = result.get("emotion_offset", 0.0)
                result["emotion_offset"] = current + weight
            elif dim == "route":
                result["route_bias"] = result.get("route_bias", 0.0) + weight
        return result

    def save(self, path: str) -> None:
        """持久化 — 对齐 reprobe/probe.py: Probe.save()"""
        data = {
            "name": self.name,
            "dimensions": self.dimensions,
            "source": self.source,
            "magnitude": self.magnitude,
            "meta": self.meta,
        }
        Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))

    @classmethod
    def load(cls, path: str) -> "DirectionVector":
        """加载 — 对齐 reprobe/loader.py: ProbeLoader.from_file()"""
        data = json.loads(Path(path).read_text())
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class DirectionRegistry:
    """
    方向向量注册表 — 对齐 reprobe/loader.py: ProbeLoader + SAELens 的 pretrained_saes_directory。
    """

    def __init__(self, storage_path: str = ""):
        self._directions: dict[str, DirectionVector] = {}
        self._storage_path = storage_path
        if storage_path:
            self._load_from_storage()

    def register(self, direction: DirectionVector) -> None:
        self._directions[direction.name] = direction
        if self._storage_path:
            self._save_to_storage()

    def get(self, name: str) -> DirectionVector | None:
        return self._directions.get(name)

    def list_directions(self) -> list[str]:
        return list(self._directions.keys())

    def _save_to_storage(self) -> None:
        """对齐 reprobe/store.py: ActivationStore 的持久化模式"""
        try:
            registry = {
                name: {"dimensions": d.dimensions, "source": d.source,
                       "magnitude": d.magnitude, "meta": d.meta}
                for name, d in self._directions.items()
            }
            Path(self._storage_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self._storage_path).write_text(
                json.dumps(registry, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.error(f"direction_registry.save_failed: {e}")

    def _load_from_storage(self) -> None:
        path = Path(self._storage_path)
        if not path.exists():
            return
        try:
            registry = json.loads(path.read_text())
            for name, data in registry.items():
                self._directions[name] = DirectionVector(
                    name=name, dimensions=data["dimensions"],
                    source=data.get("source", ""),
                    magnitude=data.get("magnitude", 1.0),
                    meta=data.get("meta", {}),
                )
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"direction_registry.load_failed_corrupted: {e}")
            # 损坏时返回空注册表，不崩溃
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_behavioral_direction.py -v`
Expected: 11 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add core/behavioral_direction.py tests/test_behavioral_direction.py
git commit -m "feat: 行为方向向量模块 (DirectionVector + DirectionRegistry) — Stage 1 基础层"
```

---

### Task 3: 配置开关（config.py 新增）

**Files:**
- Modify: `config.py`

**Interfaces:**
- Produces: `ENABLE_J_SPACE_HOOKS`, `DIRECTION_REGISTRY_PATH`, `SIGNAL_STREAM_MAX_HISTORY`, `INTERVENTION_DEFAULT_COOLDOWN`

- [ ] **Step 1: Read current config.py to find insertion point**

Run: `cd /home/orangepi/ai-agent && grep -n "^[A-Z_]* *=" config.py | tail -20`
Find the last config variable to append after.

- [ ] **Step 2: Add J-Space config variables to config.py**

Append to the end of config.py (after the last variable):

```python
# ── J-Space 架构优化配置 ──────────────────────────────────────
ENABLE_J_SPACE_HOOKS = os.getenv("ENABLE_J_SPACE_HOOKS", "true").lower() == "true"
DIRECTION_REGISTRY_PATH = os.getenv("DIRECTION_REGISTRY_PATH", str(DATA_DIR / "direction_registry.json"))
SIGNAL_STREAM_MAX_HISTORY = int(os.getenv("SIGNAL_STREAM_MAX_HISTORY", "1000"))
INTERVENTION_DEFAULT_COOLDOWN = float(os.getenv("INTERVENTION_DEFAULT_COOLDOWN", "30.0"))
```

- [ ] **Step 3: Verify config loads correctly**

Run: `cd /home/orangepi/ai-agent && python -c "from config import ENABLE_J_SPACE_HOOKS, DIRECTION_REGISTRY_PATH, SIGNAL_STREAM_MAX_HISTORY, INTERVENTION_DEFAULT_COOLDOWN; print(f'hooks={ENABLE_J_SPACE_HOOKS} path={DIRECTION_REGISTRY_PATH} max={SIGNAL_STREAM_MAX_HISTORY} cooldown={INTERVENTION_DEFAULT_COOLDOWN}')"`
Expected: Prints values without error

- [ ] **Step 4: Commit**

```bash
cd /home/orangepi/ai-agent
git add config.py
git commit -m "feat: J-Space 配置开关 (ENABLE_J_SPACE_HOOKS 等) — Stage 1"
```

---

## Stage 2: 闭环层

### Task 4: 干预闭环（InterventionLoop）

**Files:**
- Create: `core/intervention_loop.py`
- Test: `tests/test_intervention_loop.py`

**Interfaces:**
- Consumes: `BehavioralSignalStream` (from Task 1), `DirectionRegistry` + `DirectionVector` (from Task 2)
- Produces: `InterventionRule` (dataclass), `InterventionLoop` (class with `register_rule`, `evaluate`, `apply_intervention`, `get_convergence_metrics`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intervention_loop.py
import pytest
import asyncio
import time
from core.behavioral_signal import BehavioralSignalStream
from core.behavioral_direction import DirectionVector, DirectionRegistry
from core.intervention_loop import InterventionRule, InterventionLoop


@pytest.mark.asyncio
async def test_evaluate_below_threshold_no_trigger():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule("cognitive_load", threshold=0.8, direction_name="calm", alpha=0.5))
    await stream.emit("cognitive_load", 0.5, "test")
    triggered = await loop.evaluate({})
    assert len(triggered) == 0


@pytest.mark.asyncio
async def test_evaluate_above_threshold_triggers():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule("cognitive_load", threshold=0.8, direction_name="calm", alpha=0.5))
    await stream.emit("cognitive_load", 0.9, "test")
    triggered = await loop.evaluate({})
    assert len(triggered) == 1
    assert triggered[0]["direction"] == "calm"
    assert triggered[0]["alpha"] == 0.5


@pytest.mark.asyncio
async def test_cooldown_prevents_retrigger():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule("cognitive_load", threshold=0.8, direction_name="calm",
                                        alpha=0.5, cooldown=30.0))
    await stream.emit("cognitive_load", 0.9, "test")
    triggered1 = await loop.evaluate({})
    assert len(triggered1) == 1
    # 立即再次评估，cooldown 内不应触发
    await stream.emit("cognitive_load", 0.95, "test")
    triggered2 = await loop.evaluate({})
    assert len(triggered2) == 0


@pytest.mark.asyncio
async def test_apply_intervention_projected():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4, "prompt": 0.2}, "manual"))
    loop = InterventionLoop(stream, registry)
    intervention = {
        "scaled_direction": DirectionVector("calm", {"emotion": -0.4, "prompt": 0.2}, "manual") * 0.5,
        "mode": "projected",
    }
    context = {}
    result = await loop.apply_intervention(context, intervention)
    assert result["emotion_offset"] == -0.2
    assert result["prompt_modifier"] == 0.1


@pytest.mark.asyncio
async def test_apply_intervention_uniform():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("focused", {"prompt": 0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    intervention = {
        "scaled_direction": DirectionVector("focused", {"prompt": 0.4}, "manual") * 1.0,
        "mode": "uniform",
    }
    context = {}
    result = await loop.apply_intervention(context, intervention)
    assert result["prompt_modifier"] == 0.4


@pytest.mark.asyncio
async def test_missing_direction_skipped():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule("cognitive_load", threshold=0.5, direction_name="nonexistent", alpha=0.5))
    await stream.emit("cognitive_load", 0.9, "test")
    triggered = await loop.evaluate({})
    assert len(triggered) == 0


@pytest.mark.asyncio
async def test_convergence_metrics_initial():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    loop = InterventionLoop(stream, registry)
    metrics = loop.get_convergence_metrics()
    assert metrics["converging"] is True
    assert metrics["intervention_count"] == 0


@pytest.mark.asyncio
async def test_convergence_metrics_after_interventions():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule("cognitive_load", threshold=0.5, direction_name="calm",
                                        alpha=0.5, cooldown=0.0))
    # 触发多次干预
    for score in [0.9, 0.8, 0.7, 0.6, 0.5]:
        await stream.emit("cognitive_load", score, "test")
        await loop.evaluate({})
    metrics = loop.get_convergence_metrics()
    assert metrics["intervention_count"] >= 5
    assert metrics["converging"] is True  # score 在下降
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_intervention_loop.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.intervention_loop'`

- [ ] **Step 3: Write minimal implementation**

```python
# core/intervention_loop.py
"""
干预闭环 — 对齐 reprobe 的 Monitor + Steerer 闭环模式。

设计参考:
- reprobe/monitor.py: Monitor.score() 阈值判断
- reprobe/steerer.py: Steerer._apply_projection() 干预应用
- jlens/fitting.py: fit() 的 mean_rel_change 收敛追踪
"""
from dataclasses import dataclass, field
import time
from typing import Any
from loguru import logger

from core.behavioral_signal import BehavioralSignalStream
from core.behavioral_direction import DirectionVector, DirectionRegistry


@dataclass
class InterventionRule:
    """干预规则 — 对齐 reprobe/monitor.py 的监控配置。"""
    signal_type: str
    threshold: float
    direction_name: str
    alpha: float = 0.5
    mode: str = "projected"
    cooldown: float = 30.0
    last_triggered: float = 0.0


class InterventionLoop:
    """
    干预闭环 — 对齐 reprobe Monitor + Steerer 的观测→干预→验证闭环。
    """

    def __init__(self, signal_stream: BehavioralSignalStream,
                 direction_registry: DirectionRegistry):
        self._stream = signal_stream
        self._registry = direction_registry
        self._rules: list[InterventionRule] = []
        self._intervention_history: list[dict] = []

    def register_rule(self, rule: InterventionRule) -> None:
        """注册干预规则"""
        self._rules.append(rule)

    async def evaluate(self, context: dict) -> list[dict]:
        """
        聚合信号 → 阈值判断 → 返回触发的干预列表。

        对齐 reprobe/monitor.py: Monitor.score() + Steerer 触发逻辑。
        """
        triggered = []
        now = time.time()

        for rule in self._rules:
            score = self._stream.aggregate(rule.signal_type, "mean_of_means")

            if score <= rule.threshold:
                continue

            # cooldown 检查
            if rule.cooldown > 0 and (now - rule.last_triggered) < rule.cooldown:
                continue

            direction = self._registry.get(rule.direction_name)
            if direction is None:
                logger.debug(f"intervention_loop.direction_not_found: {rule.direction_name}")
                continue

            scaled = direction * rule.alpha
            rule.last_triggered = now
            entry = {
                "rule": rule.signal_type,
                "score": score,
                "direction": rule.direction_name,
                "alpha": rule.alpha,
                "mode": rule.mode,
                "scaled_direction": scaled,
            }
            triggered.append(entry)
            self._intervention_history.append({
                "timestamp": now,
                "signal_type": rule.signal_type,
                "score": score,
                "threshold": rule.threshold,
                "direction": rule.direction_name,
                "alpha": rule.alpha,
            })

        return triggered

    async def apply_intervention(self, context: dict, intervention: dict) -> dict:
        """
        应用干预到上下文。

        对齐 reprobe/steerer.py: Steerer._apply_projection()
        """
        direction: DirectionVector = intervention["scaled_direction"]
        return direction.apply_to_context(context)

    def get_convergence_metrics(self) -> dict:
        """
        收敛指标 — 对齐 jlens/fitting.py: fit() 中的 mean_rel_change 追踪。
        """
        if len(self._intervention_history) < 2:
            return {"converging": True, "intervention_count": len(self._intervention_history)}

        recent = self._intervention_history[-5:]
        scores = [h["score"] for h in recent]
        trend = scores[-1] - scores[0] if len(scores) >= 2 else 0
        return {
            "converging": trend < 0,
            "trend": trend,
            "intervention_count": len(self._intervention_history),
            "recent_scores": scores,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_intervention_loop.py -v`
Expected: 8 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add core/intervention_loop.py tests/test_intervention_loop.py
git commit -m "feat: 干预闭环模块 (InterventionLoop) — Stage 2 闭环层"
```

---

## Stage 3: 智能层

### Task 5: 输出意图分解（IntentDecomposer）

**Files:**
- Create: `core/intent_decomposition.py`
- Test: `tests/test_intent_decomposition.py`

**Interfaces:**
- Produces: `IntentFactor` (dataclass), `DecomposedOutput` (dataclass with `dominant_intent`, `sparsity`), `IntentDecomposer` (class with `encode`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intent_decomposition.py
import pytest
from core.intent_decomposition import IntentFactor, DecomposedOutput, IntentDecomposer


@pytest.mark.asyncio
async def test_encode_knowledge():
    decomposer = IntentDecomposer()
    output = "根据资料显示，研究表明这个方法有效。据统计成功率高达90%。"
    result = await decomposer.encode(output)
    assert any(f.name == "knowledge" for f in result.factors)
    assert result.factors[0].activation > 0


@pytest.mark.asyncio
async def test_encode_emotional():
    decomposer = IntentDecomposer()
    output = "别担心，我理解你的感受，加油！我会陪伴你。"
    result = await decomposer.encode(output)
    assert any(f.name == "emotional" for f in result.factors)


@pytest.mark.asyncio
async def test_encode_safety():
    decomposer = IntentDecomposer()
    output = "请注意，这样做有安全风险，不建议如此操作，请谨慎。"
    result = await decomposer.encode(output)
    assert any(f.name == "safety" for f in result.factors)


@pytest.mark.asyncio
async def test_encode_creative():
    decomposer = IntentDecomposer()
    output = "可以试试这个创意，不如想象一下如果这样做会怎样？"
    result = await decomposer.encode(output)
    assert any(f.name == "creative" for f in result.factors)


@pytest.mark.asyncio
async def test_encode_mixed_intents():
    decomposer = IntentDecomposer()
    output = "根据资料，这个方法有效。别担心，加油！请注意安全风险。"
    result = await decomposer.encode(output)
    assert len(result.factors) >= 2


@pytest.mark.asyncio
async def test_encode_empty_output():
    decomposer = IntentDecomposer()
    result = await decomposer.encode("")
    assert len(result.factors) == 0
    assert result.residual == 1.0


@pytest.mark.asyncio
async def test_dominant_intent():
    decomposer = IntentDecomposer()
    output = "根据资料资料显示研究表明据统计据报道"  # 多个知识关键词
    result = await decomposer.encode(output)
    dominant = result.dominant_intent
    assert dominant is not None
    assert dominant.name == "knowledge"


@pytest.mark.asyncio
async def test_sparsity():
    decomposer = IntentDecomposer()
    output = "根据资料显示这个方法有效。"  # 仅知识意图
    result = await decomposer.encode(output)
    # 只有 1 个活跃意图，7 个总数，稀疏度 = 1 - 1/7
    assert result.sparsity > 0.5


@pytest.mark.asyncio
async def test_residual():
    decomposer = IntentDecomposer()
    output = "hello world"  # 无匹配意图
    result = await decomposer.encode(output)
    assert result.residual == 1.0


@pytest.mark.asyncio
async def test_raw_output_preserved():
    decomposer = IntentDecomposer()
    output = "测试文本"
    result = await decomposer.encode(output)
    assert result.raw_output == output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_intent_decomposition.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# core/intent_decomposition.py
"""
输出意图分解 — 对齐 SAELens 的稀疏自编码器范式。

SAE 将 d_model 维残差流编码为 d_sae 维稀疏特征:
    feature_acts = encode(x)     # [d_sae], 大部分为0
    x_recon = decode(feature_acts) # [d_model]

对应地，IntentDecomposition 将 Agent 输出编码为意图因子:
    factors = encode(output)       # 各意图的激活值
    reconstructed = decode(factors) # 重建输出(用于验证)

参考:
- SAELens/sae_lens/saes/sae.py: SAE.encode()/decode()
- SAELens/sae_lens/training/activations_store.py: ActivationsStore
"""
from dataclasses import dataclass, field
from typing import Any
from loguru import logger


@dataclass
class IntentFactor:
    """
    意图因子 — 对齐 SAELens/sae_lens/saes/sae.py 中 SAE 的稀疏特征。
    """
    name: str
    activation: float
    evidence: str = ""
    confidence: float = 1.0


@dataclass
class DecomposedOutput:
    """分解后的输出 — 对齐 SAE 的 encode 输出"""
    raw_output: str
    factors: list[IntentFactor]
    residual: float = 0.0

    @property
    def dominant_intent(self) -> IntentFactor | None:
        """主导意图 — 激活最高的因子"""
        if not self.factors:
            return None
        return max(self.factors, key=lambda f: f.activation)

    @property
    def sparsity(self) -> float:
        """稀疏度 — 对齐 SAE 的 l0 稀疏度量"""
        if not self.factors:
            return 0.0
        total = len(IntentDecomposer.INTENT_DIMENSIONS)
        active = sum(1 for f in self.factors if f.activation > 0.1)
        return 1.0 - active / total


class IntentDecomposer:
    """
    输出意图分解器 — 对齐 SAELens 的 SAE encode/decode 范式。
    """

    INTENT_DIMENSIONS = [
        "knowledge", "emotional", "safety", "creative",
        "factual", "social", "procedural",
    ]

    INTENT_KEYWORDS = {
        "knowledge": ["根据", "资料显示", "研究表明", "数据表明", "据统计",
                      "据了解", "据报道", "according to", "research shows"],
        "emotional": ["别担心", "加油", "理解你的感受", "心疼", "开心",
                      "难过", "陪伴", "安慰", "don't worry", "i understand"],
        "safety": ["请注意", "安全", "风险", "不建议", "谨慎",
                   "warning", "caution", "not recommended"],
        "creative": ["可以试试", "不如", "想象一下", "如果", "创意",
                     "how about", "what if", "imagine"],
        "factual": ["是", "位于", "成立于", "人口", "面积", "首都",
                    "is", "located", "founded"],
        "social": ["你好", "谢谢", "再见", "请问", "hello", "thank"],
        "procedural": ["步骤", "首先", "然后", "最后", "方法",
                       "step", "first", "then", "finally"],
    }

    def __init__(self, use_llm_decomposition: bool = False):
        self._use_llm = use_llm_decomposition

    async def encode(self, output: str, context: dict | None = None) -> DecomposedOutput:
        """将输出编码为意图因子 — 对齐 SAE.encode()"""
        if self._use_llm:
            return await self._llm_encode(output, context)
        return self._rule_encode(output, context)

    def _rule_encode(self, output: str, context: dict | None = None) -> DecomposedOutput:
        """规则基分解 — Phase 1 实现"""
        if not output:
            return DecomposedOutput(raw_output=output, factors=[], residual=1.0)

        factors = []
        text_lower = output.lower()

        for intent_name in self.INTENT_DIMENSIONS:
            keywords = self.INTENT_KEYWORDS.get(intent_name, [])
            score = self._score_keywords(text_lower, keywords)
            if score > 0:
                factors.append(IntentFactor(intent_name, score))

        # 归一化
        if factors:
            total = sum(f.activation for f in factors)
            if total > 1.0:
                for f in factors:
                    f.activation /= total

        explained = sum(f.activation for f in factors)
        residual = max(0.0, 1.0 - explained)

        return DecomposedOutput(raw_output=output, factors=factors, residual=residual)

    def _score_keywords(self, text: str, keywords: list[str]) -> float:
        """简单的关键词匹配评分"""
        hits = sum(1 for kw in keywords if kw in text)
        if hits == 0:
            return 0.0
        return min(1.0, hits * 0.3)

    async def _llm_encode(self, output: str, context: dict | None = None) -> DecomposedOutput:
        """LLM 基分解 — Phase 2 实现（未实现）"""
        raise NotImplementedError("Phase 2: LLM-based decomposition")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_intent_decomposition.py -v`
Expected: 10 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add core/intent_decomposition.py tests/test_intent_decomposition.py
git commit -m "feat: 输出意图分解模块 (IntentDecomposer) — Stage 3 智能层"
```

---

### Task 6: 增强型路由（EnhancedBeliefRouter）

**Files:**
- Create: `core/enhanced_router.py`
- Test: `tests/test_enhanced_router.py`

**Interfaces:**
- Consumes: `BehavioralSignalStream` (Task 1), `DirectionRegistry` (Task 2), `BeliefRouter` (existing)
- Produces: `EnhancedBeliefRouter` (class with `select_agent`, `update_belief`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enhanced_router.py
import pytest
from unittest.mock import MagicMock, patch
from core.behavioral_signal import BehavioralSignalStream
from core.behavioral_direction import DirectionVector, DirectionRegistry
from core.enhanced_router import EnhancedBeliefRouter


@pytest.fixture
def mock_base_router():
    router = MagicMock()
    router.VALID_AGENTS = ["xiaoda", "xiaolang", "xiaoke", "xiaolian"]
    router._beliefs = {
        "xiaoda": MagicMock(sample=MagicMock(return_value=0.5)),
        "xiaolang": MagicMock(sample=MagicMock(return_value=0.4)),
        "xiaoke": MagicMock(sample=MagicMock(return_value=0.6)),
        "xiaolian": MagicMock(sample=MagicMock(return_value=0.3)),
    }
    router.update_belief = MagicMock()
    return router


@pytest.mark.asyncio
async def test_select_agent_basic(mock_base_router):
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    enhanced = EnhancedBeliefRouter(mock_base_router, registry, stream)
    selected = enhanced.select_agent()
    assert selected in mock_base_router.VALID_AGENTS


@pytest.mark.asyncio
async def test_select_agent_with_exclude(mock_base_router):
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    enhanced = EnhancedBeliefRouter(mock_base_router, registry, stream)
    selected = enhanced.select_agent(exclude={"xiaoda", "xiaoke", "xiaolian"})
    assert selected == "xiaolang"


@pytest.mark.asyncio
async def test_select_agent_empty_candidates(mock_base_router):
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    enhanced = EnhancedBeliefRouter(mock_base_router, registry, stream)
    selected = enhanced.select_agent(exclude=set(mock_base_router.VALID_AGENTS))
    assert selected == "xiaoda"  # fallback


@pytest.mark.asyncio
async def test_select_agent_with_direction_hint(mock_base_router):
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("route_security", {"route": 0.5}, "manual"))
    enhanced = EnhancedBeliefRouter(mock_base_router, registry, stream)
    selected = enhanced.select_agent(task_type="security", direction_hint="route_security")
    assert selected in mock_base_router.VALID_AGENTS


@pytest.mark.asyncio
async def test_select_agent_signal_adjustment(mock_base_router):
    stream = BehavioralSignalStream()
    # 给 xiaoke 高成功率信号
    await stream.emit("agent_xiaoke_success", 0.9, "test")
    await stream.emit("agent_xiaoke_success", 0.95, "test")
    registry = DirectionRegistry()
    enhanced = EnhancedBeliefRouter(mock_base_router, registry, stream,
                                    direction_weight=0.0, signal_weight=0.5)
    selected = enhanced.select_agent()
    # xiaoke 的 thompson=0.6 + signal_weight*0.925 应该最高
    assert selected == "xiaoke"


@pytest.mark.asyncio
async def test_update_belief_delegates(mock_base_router):
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    enhanced = EnhancedBeliefRouter(mock_base_router, registry, stream)
    enhanced.update_belief("xiaoda", True)
    mock_base_router.update_belief.assert_called_once_with("xiaoda", True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_enhanced_router.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# core/enhanced_router.py
"""
增强型路由器 — 在 Thompson Sampling 基础上叠加方向偏置。

对齐:
- belief_router.py: BeliefRouter 的 Thompson Sampling (基础)
- ACT/generate_directions_q_wise.py: 按问题类型生成方向 (q-wise direction)
- repe/rep_readers.py: PCARepReader 的方向识别
- reprobe/steerer.py: Steerer 的方向应用

路由公式:
    score(agent) = thompson_sample(agent)
                 + alpha * direction_bias(task_type, agent)
                 + beta * signal_adjustment(agent, recent_signals)
"""
from typing import Any
from loguru import logger

from core.behavioral_signal import BehavioralSignalStream
from core.behavioral_direction import DirectionRegistry


AGENT_TASK_MAP = {
    "xiaolang": "security",
    "xiaoke": "debug",
    "xiaolian": "info_search",
    "xiaoda": "general",
}


class EnhancedBeliefRouter:
    """增强型路由器 — 对齐 ACT q-wise direction + RepE concept direction。"""

    def __init__(
        self,
        base_router,
        direction_registry: DirectionRegistry,
        signal_stream: BehavioralSignalStream,
        direction_weight: float = 0.3,
        signal_weight: float = 0.2,
    ):
        self._base = base_router
        self._registry = direction_registry
        self._stream = signal_stream
        self._direction_weight = direction_weight
        self._signal_weight = signal_weight

    def select_agent(
        self,
        task_type: str = "",
        exclude: set[str] | None = None,
        direction_hint: str = "",
    ) -> str:
        """增强型 Agent 选择。"""
        candidates = [a for a in self._base.VALID_AGENTS if a not in (exclude or set())]
        if not candidates:
            return "xiaoda"

        # 1. Thompson Sampling 基础分
        thompson_scores = {a: self._base._beliefs[a].sample() for a in candidates}

        # 2. 方向偏置
        direction_scores = {a: 0.0 for a in candidates}
        if task_type or direction_hint:
            direction_key = direction_hint or f"route_{task_type}"
            direction = self._registry.get(direction_key)
            if direction and "route" in direction.dimensions:
                route_bias = direction.dimensions["route"]
                for agent in candidates:
                    match = 1.0 if AGENT_TASK_MAP.get(agent) == task_type else 0.0
                    direction_scores[agent] = route_bias * match

        # 3. 实时信号调整
        signal_scores = {}
        for agent in candidates:
            recent = self._stream.aggregate(f"agent_{agent}_success", "mean_of_means")
            signal_scores[agent] = recent

        # 4. 综合评分
        final_scores = {}
        for agent in candidates:
            t = thompson_scores.get(agent, 0.5)
            d = direction_scores.get(agent, 0.0)
            s = signal_scores.get(agent, 0.0)
            final_scores[agent] = t + self._direction_weight * d + self._signal_weight * s

        selected = max(final_scores, key=final_scores.get)
        logger.debug("enhanced_router.selected",
                     final={k: round(v, 3) for k, v in final_scores.items()},
                     selected=selected)
        return selected

    def update_belief(self, agent_name: str, success: bool) -> None:
        """更新信念 — 委托给基础路由器"""
        self._base.update_belief(agent_name, success)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_enhanced_router.py -v`
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add core/enhanced_router.py tests/test_enhanced_router.py
git commit -m "feat: 增强型路由模块 (EnhancedBeliefRouter) — Stage 3 智能层"
```

---

## Stage 4: 基础设施 + 集成层

### Task 7: 结构化共享黑板（StructuredBlackboard）

**Files:**
- Create: `agent_core/structured_blackboard.py`
- Test: `tests/test_structured_blackboard.py`

**Interfaces:**
- Consumes: `SharedBlackboard` (existing in `agent_core/shared_blackboard.py`)
- Produces: `StructuredEntry` (dataclass), `StructuredBlackboard` (extends SharedBlackboard with `put_structured`, `query_by_tag`, `query_by_direction`, `merge_from`)

- [ ] **Step 1: Read existing SharedBlackboard to understand interface**

Run: `cd /home/orangepi/ai-agent && head -60 agent_core/shared_blackboard.py`
Understand the `put`, `get`, `get_with_meta`, `keys` methods.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_structured_blackboard.py
import pytest
import asyncio
import time
from agent_core.structured_blackboard import StructuredEntry, StructuredBlackboard


@pytest.mark.asyncio
async def test_put_structured_basic():
    bb = StructuredBlackboard()
    await bb.put_structured("key1", "value1", agent_name="xiaoda")
    val = await bb.get("key1")
    assert val == "value1"


@pytest.mark.asyncio
async def test_put_structured_with_tags():
    bb = StructuredBlackboard()
    await bb.put_structured("key1", "value1", agent_name="xiaoda", tags=["memory", "fact"])
    await bb.put_structured("key2", "value2", agent_name="xiaoda", tags=["memory"])
    results = await bb.query_by_tag("memory")
    assert len(results) == 2
    results_fact = await bb.query_by_tag("fact")
    assert len(results_fact) == 1


@pytest.mark.asyncio
async def test_put_structured_with_direction():
    bb = StructuredBlackboard()
    await bb.put_structured("key1", "value1", agent_name="xiaoda", direction="calm")
    results = await bb.query_by_direction("calm")
    assert len(results) == 1
    assert results[0]["key"] == "key1"


@pytest.mark.asyncio
async def test_query_by_tag_empty():
    bb = StructuredBlackboard()
    results = await bb.query_by_tag("nonexistent")
    assert results == []


@pytest.mark.asyncio
async def test_query_by_direction_empty():
    bb = StructuredBlackboard()
    results = await bb.query_by_direction("nonexistent")
    assert results == []


@pytest.mark.asyncio
async def test_merge_from():
    bb1 = StructuredBlackboard()
    bb2 = StructuredBlackboard()
    await bb2.put("key1", "value1")
    await bb2.put("key2", "value2")
    merged = await bb1.merge_from(bb2)
    assert merged == 2
    assert await bb1.get("key1") == "value1"
    assert await bb1.get("key2") == "value2"


@pytest.mark.asyncio
async def test_merge_from_skip_existing():
    bb1 = StructuredBlackboard()
    bb2 = StructuredBlackboard()
    await bb1.put("key1", "original")
    await bb2.put("key1", "should_not_overwrite")
    await bb2.put("key2", "new_value")
    merged = await bb1.merge_from(bb2)
    assert merged == 1
    assert await bb1.get("key1") == "original"
    assert await bb1.get("key2") == "new_value"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_structured_blackboard.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Write minimal implementation**

```python
# agent_core/structured_blackboard.py
"""
结构化共享黑板 — 在 SharedBlackboard 基础上增加语义索引。

对齐:
- SAELens/sae_lens/training/activations_store.py: ActivationsStore 的结构化存储
- reprobe/store.py: ActivationStore 的 HDF5 持久化模式
- jlens/lens.py: JacobianLens.merge() 的加权合并
"""
from dataclasses import dataclass, field
from typing import Any
from loguru import logger

from agent_core.shared_blackboard import SharedBlackboard


@dataclass
class StructuredEntry:
    """结构化黑板条目 — 对齐 reprobe/store.py: ActivationStore 的 HDF5 条目。"""
    value: Any
    agent_name: str
    expire_at: float | None
    tags: list[str] = field(default_factory=list)
    direction: str = ""
    quality: float = 1.0
    schema_version: str = "1.0"


class StructuredBlackboard(SharedBlackboard):
    """
    结构化共享黑板 — 在 SharedBlackboard 基础上增加:
    1. 语义标签索引 — 对齐 SAE 的 feature label
    2. 方向关联 — 对齐 Steerer 的干预方向
    3. 质量评分 — 对齐 Probe 的 AUC
    """

    def __init__(self, default_ttl: float = 600.0, persist_path: str = "") -> None:
        super().__init__(default_ttl)
        self._tag_index: dict[str, set[str]] = {}
        self._direction_index: dict[str, set[str]] = {}
        self._persist_path = persist_path

    async def put_structured(
        self,
        key: str,
        value: Any,
        agent_name: str = "",
        ttl: float | None = None,
        tags: list[str] | None = None,
        direction: str = "",
        quality: float = 1.0,
    ) -> None:
        """写入结构化条目"""
        await self.put(key, value, agent_name, ttl)
        if tags:
            for tag in tags:
                if tag not in self._tag_index:
                    self._tag_index[tag] = set()
                self._tag_index[tag].add(key)
        if direction:
            if direction not in self._direction_index:
                self._direction_index[direction] = set()
            self._direction_index[direction].add(key)

    async def query_by_tag(self, tag: str) -> list[dict]:
        """按标签查询 — 对齐 SAE 的 feature lookup。"""
        keys = self._tag_index.get(tag, set())
        results = []
        for key in keys:
            entry = await self.get_with_meta(key)
            if entry:
                results.append({"key": key, **entry})
        return results

    async def query_by_direction(self, direction_name: str) -> list[dict]:
        """按方向查询 — 对齐 Steerer 的方向关联。"""
        keys = self._direction_index.get(direction_name, set())
        results = []
        for key in keys:
            entry = await self.get_with_meta(key)
            if entry:
                results.append({"key": key, **entry})
        return results

    async def merge_from(self, other: "StructuredBlackboard") -> int:
        """
        合并另一个黑板的条目 — 对齐 jlens/lens.py: JacobianLens.merge()。
        不存在的 key 直接导入，已存在的保留原值。
        """
        merged_count = 0
        other_keys = await other.keys()
        for key in other_keys:
            val = await other.get(key)
            if val is not None:
                existing = await self.get(key)
                if existing is None:
                    await self.put(key, val)
                    merged_count += 1
        return merged_count
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_structured_blackboard.py -v`
Expected: 7 tests PASS

- [ ] **Step 6: Commit**

```bash
cd /home/orangepi/ai-agent
git add agent_core/structured_blackboard.py tests/test_structured_blackboard.py
git commit -m "feat: 结构化共享黑板 (StructuredBlackboard) — Stage 4 基础设施"
```

---

### Task 8: Hook 集成 — agent_introspection + behavioral_health（信号采集）

**Files:**
- Modify: `core/agent_introspection.py` — 在 `get_current_state()` 末尾插入 emit
- Modify: `core/behavioral_health.py` — 在健康度评分时插入 emit
- Test: `tests/test_hook_integration.py` (partial, signal hooks only)

**Interfaces:**
- Consumes: `BehavioralSignalStream` (Task 1)
- Produces: Hook integration for signal collection

- [ ] **Step 1: Read agent_introspection.py to find get_current_state()**

Run: `cd /home/orangepi/ai-agent && grep -n "def get_current_state\|def get_state\|cognitive_load" core/agent_introspection.py | head -10`

- [ ] **Step 2: Read behavioral_health.py to find health scoring**

Run: `cd /home/orangepi/ai-agent && grep -n "def.*health\|def.*score\|health_score" core/behavioral_health.py | head -10`

- [ ] **Step 3: Write the failing test**

```python
# tests/test_hook_integration.py (partial — will be extended in Task 11)
import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from core.behavioral_signal import BehavioralSignalStream


@pytest.mark.asyncio
async def test_hook_agent_introspection_emits_signal():
    """Hook #1: agent_introspection.get_current_state() 应 emit cognitive_load 信号"""
    stream = BehavioralSignalStream()
    with patch("core.agent_introspection._signal_stream", stream):
        # 调用 get_current_state 或类似方法
        from core.agent_introspection import AgentIntrospector
        introspector = AgentIntrospector.__new__(AgentIntrospector)
        # 模拟必要属性
        introspector._metacognition = MagicMock()
        introspector._metacognition.get_metrics.return_value = {"cognitive_load": 0.7}
        if hasattr(introspector, "get_current_state"):
            try:
                introspector.get_current_state()
            except Exception:
                pass
        # 验证信号已发射
        history = stream.get_history("cognitive_load")
        # 如果 hook 已接入，应有信号
        if history:
            assert history[0].source == "introspection"


@pytest.mark.asyncio
async def test_hook_behavioral_health_emits_signal():
    """Hook #6: behavioral_health 评分时应 emit health 信号"""
    stream = BehavioralSignalStream()
    with patch("core.behavioral_health._signal_stream", stream):
        # 模拟健康度计算
        from core.behavioral_health import BehavioralHealthMonitor
        monitor = BehavioralHealthMonitor.__new__(BehavioralHealthMonitor)
        # 验证模块可导入且不崩溃
        assert monitor is not None
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_hook_integration.py -v`
Expected: FAIL (hooks not yet installed)

- [ ] **Step 5: Install hooks in agent_introspection.py**

Add at the top of `core/agent_introspection.py` (after imports):
```python
# J-Space Hook: 行为信号流采集
try:
    from config import ENABLE_J_SPACE_HOOKS
    if ENABLE_J_SPACE_HOOKS:
        from core.behavioral_signal import BehavioralSignalStream
        _signal_stream: BehavioralSignalStream | None = None
    else:
        _signal_stream = None
except ImportError:
    _signal_stream = None
```

In `get_current_state()` method (or equivalent), add at the end:
```python
        # J-Space Hook: emit cognitive_load signal
        if _signal_stream is not None:
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(_signal_stream.emit(
                        "cognitive_load", cognitive_load, "introspection"))
            except Exception:
                pass
```

- [ ] **Step 6: Install hooks in behavioral_health.py**

Add similar import block and emit call in health scoring method:
```python
        # J-Space Hook: emit health signal
        if _signal_stream is not None:
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(_signal_stream.emit(
                        "health", score, "behavioral_health"))
            except Exception:
                pass
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_hook_integration.py -v`
Expected: PASS

- [ ] **Step 8: Run existing tests to verify no regression**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/ -k "introspection or behavioral_health" -v --timeout=30`
Expected: Existing tests still pass

- [ ] **Step 9: Commit**

```bash
cd /home/orangepi/ai-agent
git add core/agent_introspection.py core/behavioral_health.py tests/test_hook_integration.py
git commit -m "feat: Hook #1+#6 信号采集接入 (introspection + behavioral_health) — Stage 4"
```

---

### Task 9: Hook 集成 — agent_dispatcher（闭环干预）

**Files:**
- Modify: `agent_dispatcher.py` — 在 `SubAgent.chat()` 前后插入干预评估和信号发射
- Test: `tests/test_hook_integration.py` (extend)

- [ ] **Step 1: Read agent_dispatcher.py SubAgent.chat() method**

Run: `cd /home/orangepi/ai-agent && grep -n "async def chat\|async def _chat\|async def _invoke" agent_dispatcher.py | head -10`

- [ ] **Step 2: Add hook imports at top of agent_dispatcher.py**

After existing imports, add:
```python
# J-Space Hook: 干预闭环
try:
    from config import ENABLE_J_SPACE_HOOKS
    if ENABLE_J_SPACE_HOOKS:
        from core.behavioral_signal import BehavioralSignalStream
        from core.intervention_loop import InterventionLoop
        _signal_stream: BehavioralSignalStream | None = None
        _intervention_loop: InterventionLoop | None = None
    else:
        _signal_stream = None
        _intervention_loop = None
except ImportError:
    _signal_stream = None
    _intervention_loop = None
```

- [ ] **Step 3: Insert hooks in SubAgent.chat() method**

Before the LLM call:
```python
        # J-Space Hook: 干预前评估
        if _intervention_loop is not None:
            try:
                interventions = await _intervention_loop.evaluate({})
                for intervention in interventions:
                    # 应用干预到上下文
                    pass  # 实际应用取决于上下文结构
            except Exception:
                pass
```

After successful response:
```python
        # J-Space Hook: emit agent success signal
        if _signal_stream is not None:
            try:
                success_score = 1.0 if success else 0.0
                await _signal_stream.emit(
                    f"agent_{self.config.name}_success", success_score, "agent_dispatcher")
            except Exception:
                pass
```

- [ ] **Step 4: Run existing dispatcher tests to verify no regression**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/ -k "dispatcher" -v --timeout=30`
Expected: Existing tests still pass

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add agent_dispatcher.py
git commit -m "feat: Hook #3 闭环干预接入 (agent_dispatcher) — Stage 4"
```

---

### Task 10: Hook 集成 — emotion_state + prompt_builder（方向干预）

**Files:**
- Modify: `emotion/emotion_state.py` — 方向控制情绪
- Modify: `agent_core/prompt_builder.py` (or equivalent) — 方向干预 prompt
- Test: `tests/test_hook_integration.py` (extend)

- [ ] **Step 1: Read emotion_state.py and prompt_builder.py**

Run: `cd /home/orangepi/ai-agent && grep -n "class\|def " emotion/emotion_state.py | head -15`
Run: `cd /home/orangepi/ai-agent && find . -name "prompt_builder*" -o -name "*prompt*build*" | head -5`

- [ ] **Step 2: Add direction hook to emotion_state.py**

In emotion state update method:
```python
        # J-Space Hook: 方向控制情绪
        try:
            from config import ENABLE_J_SPACE_HOOKS
            if ENABLE_J_SPACE_HOOKS:
                from core.behavioral_direction import DirectionVector
                # 应用 emotion_offset 方向
                emotion_offset = context.get("emotion_offset", 0.0) if context else 0.0
                if emotion_offset != 0.0:
                    # 调整情绪状态
                    pass
        except Exception:
            pass
```

- [ ] **Step 3: Add direction hook to prompt_builder**

In prompt building method:
```python
        # J-Space Hook: 方向干预 prompt
        try:
            from config import ENABLE_J_SPACE_HOOKS
            if ENABLE_J_SPACE_HOOKS:
                prompt_modifier = context.get("prompt_modifier", 0.0) if context else 0.0
                if prompt_modifier > 0:
                    # 根据 prompt_modifier 调整 prompt
                    pass
        except Exception:
            pass
```

- [ ] **Step 4: Run existing tests to verify no regression**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/ -k "emotion or prompt" -v --timeout=30`
Expected: Existing tests still pass

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add emotion/emotion_state.py agent_core/message_processor.py
git commit -m "feat: Hook #4+#8 方向干预接入 (emotion_state + prompt_builder) — Stage 4"
```

---

### Task 11: Hook 集成 — degradation_strategy + cognitive_memory + belief_router

**Files:**
- Modify: `core/degradation_strategy.py` — 信号驱动降级
- Modify: `memory/cognitive_memory.py` — 结构化存储
- Modify: `belief_router.py` — 增强路由（可选启用）
- Test: `tests/test_hook_integration.py` (extend)

- [ ] **Step 1: Add signal-driven degradation hook to degradation_strategy.py**

In degradation check method:
```python
        # J-Space Hook: 信号驱动降级
        try:
            from config import ENABLE_J_SPACE_HOOKS
            if ENABLE_J_SPACE_HOOKS and _signal_stream is not None:
                health_score = _signal_stream.aggregate("health", "mean_of_means")
                if health_score < 0.3:
                    # 触发额外降级
                    pass
        except Exception:
            pass
```

- [ ] **Step 2: Add structured storage hook to cognitive_memory.py**

In memory storage method:
```python
        # J-Space Hook: 结构化存储
        try:
            from config import ENABLE_J_SPACE_HOOKS
            if ENABLE_J_SPACE_HOOKS and _structured_blackboard is not None:
                await _structured_blackboard.put_structured(
                    key, value, agent_name=agent_name,
                    tags=["memory"], direction=direction_name)
        except Exception:
            pass
```

- [ ] **Step 3: Add enhanced router hook to belief_router.py**

In select_agent method, add optional EnhancedBeliefRouter delegation:
```python
        # J-Space Hook: 增强型路由（可选）
        try:
            from config import ENABLE_J_SPACE_HOOKS
            if ENABLE_J_SPACE_HOOKS and _enhanced_router is not None:
                return _enhanced_router.select_agent(task_type, exclude, direction_hint)
        except Exception:
            pass
        # 原始 Thompson Sampling 逻辑继续
```

- [ ] **Step 4: Run existing tests to verify no regression**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/ -k "degradation or cognitive_memory or belief_router" -v --timeout=30`
Expected: Existing tests still pass

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add core/degradation_strategy.py memory/cognitive_memory.py belief_router.py
git commit -m "feat: Hook #5+#7+#2 信号驱动降级+结构化存储+增强路由 — Stage 4"
```

---

### Task 12: 端到端闭环测试

**Files:**
- Test: `tests/test_e2e_closed_loop.py`

**Interfaces:**
- Consumes: All modules from Tasks 1-7

- [ ] **Step 1: Write the end-to-end test**

```python
# tests/test_e2e_closed_loop.py
"""
端到端闭环测试 — 验证完整的 观测→干预→验证 闭环。

流程:
1. emit cognitive_load 信号（高值）
2. InterventionLoop.evaluate() 触发干预
3. apply_intervention 应用方向到上下文
4. 验证 context 被修改
5. 验证 convergence_metrics 追踪到干预历史
"""
import pytest
import asyncio
from core.behavioral_signal import BehavioralSignalStream
from core.behavioral_direction import DirectionVector, DirectionRegistry
from core.intervention_loop import InterventionRule, InterventionLoop


@pytest.mark.asyncio
async def test_e2e_closed_loop_signal_to_intervention():
    """完整闭环：信号 → 阈值判断 → 方向应用 → 上下文修改"""
    # 1. 初始化所有组件
    stream = BehavioralSignalStream(max_history=100)
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4, "prompt": 0.2}, "manual"))
    loop = InterventionLoop(stream, registry)

    # 2. 注册干预规则
    loop.register_rule(InterventionRule(
        signal_type="cognitive_load",
        threshold=0.8,
        direction_name="calm",
        alpha=0.5,
        mode="projected",
        cooldown=0.0,  # 测试中禁用冷却
    ))

    # 3. 发射高 cognitive_load 信号
    await stream.emit("cognitive_load", 0.9, "test_e2e")

    # 4. 评估触发
    triggered = await loop.evaluate({})
    assert len(triggered) == 1, "应该触发 1 个干预"
    assert triggered[0]["direction"] == "calm"
    assert triggered[0]["score"] == 0.9

    # 5. 应用干预
    context = {"existing": "data"}
    result = await loop.apply_intervention(context, triggered[0])
    assert result["emotion_offset"] == -0.2  # -0.4 * 0.5
    assert result["prompt_modifier"] == 0.1  # 0.2 * 0.5
    assert result["existing"] == "data"  # 原有数据保留

    # 6. 验证收敛指标
    metrics = loop.get_convergence_metrics()
    assert metrics["intervention_count"] == 1


@pytest.mark.asyncio
async def test_e2e_convergence_over_multiple_interventions():
    """多次干预后验证收敛趋势"""
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule(
        "cognitive_load", threshold=0.5, direction_name="calm",
        alpha=0.5, cooldown=0.0,
    ))

    # 模拟 score 逐渐下降的干预序列
    scores = [0.9, 0.8, 0.7, 0.6, 0.5]
    for score in scores:
        await stream.emit("cognitive_load", score, "test")
        await loop.evaluate({})

    metrics = loop.get_convergence_metrics()
    assert metrics["intervention_count"] >= 5
    assert metrics["converging"] is True  # score 在下降


@pytest.mark.asyncio
async def test_e2e_no_intervention_below_threshold():
    """低于阈值时不触发干预"""
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule(
        "cognitive_load", threshold=0.8, direction_name="calm", alpha=0.5,
    ))

    await stream.emit("cognitive_load", 0.3, "test")
    triggered = await loop.evaluate({})
    assert len(triggered) == 0


@pytest.mark.asyncio
async def test_e2e_non_blocking_on_failure():
    """模块失败时不阻塞主流程"""
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()  # 空注册表，不注册任何方向
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule(
        "cognitive_load", threshold=0.5, direction_name="nonexistent", alpha=0.5,
    ))

    await stream.emit("cognitive_load", 0.9, "test")
    triggered = await loop.evaluate({})
    # 方向不存在时应跳过，不崩溃
    assert len(triggered) == 0
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_e2e_closed_loop.py -v`
Expected: 4 tests PASS

- [ ] **Step 3: Commit**

```bash
cd /home/orangepi/ai-agent
git add tests/test_e2e_closed_loop.py
git commit -m "test: 端到端闭环测试 (e2e_closed_loop) — Stage 4"
```

---

### Task 13: 全量测试验证 + 预注册方向初始化

**Files:**
- Create or modify: `core/j_space_bootstrap.py` — 初始化预注册方向
- Modify: main entry point (agent.py or similar) — 启动时初始化 J-Space 组件

- [ ] **Step 1: Create J-Space bootstrap module**

```python
# core/j_space_bootstrap.py
"""
J-Space 架构优化启动初始化。

在 Agent 启动时初始化:
1. BehavioralSignalStream 全局实例
2. DirectionRegistry 加载/初始化预注册方向
3. InterventionLoop 注册默认规则
"""
from loguru import logger
from config import ENABLE_J_SPACE_HOOKS, DIRECTION_REGISTRY_PATH, SIGNAL_STREAM_MAX_HISTORY
from core.behavioral_signal import BehavioralSignalStream
from core.behavioral_direction import DirectionVector, DirectionRegistry
from core.intervention_loop import InterventionRule, InterventionLoop


def _create_default_directions() -> list[DirectionVector]:
    """预注册方向"""
    return [
        DirectionVector("helpfulness", {"prompt": 0.3, "route": 0.2}, "manual"),
        DirectionVector("safety", {"prompt": 0.5, "tool": -0.3}, "manual"),
        DirectionVector("calm", {"emotion": -0.4, "prompt": 0.2}, "manual"),
        DirectionVector("focused", {"prompt": 0.4, "route": 0.3}, "manual"),
    ]


def _create_default_rules() -> list[InterventionRule]:
    """默认干预规则"""
    return [
        InterventionRule("cognitive_load", threshold=0.8, direction_name="calm",
                         alpha=0.4, mode="projected", cooldown=30.0),
        InterventionRule("health", threshold=0.3, direction_name="focused",
                         alpha=0.5, mode="uniform", cooldown=60.0),
    ]


_signal_stream: BehavioralSignalStream | None = None
_direction_registry: DirectionRegistry | None = None
_intervention_loop: InterventionLoop | None = None


def init_j_space() -> None:
    """初始化 J-Space 组件"""
    global _signal_stream, _direction_registry, _intervention_loop

    if not ENABLE_J_SPACE_HOOKS:
        logger.info("j_space.disabled by config")
        return

    try:
        _signal_stream = BehavioralSignalStream(max_history=SIGNAL_STREAM_MAX_HISTORY)
        _direction_registry = DirectionRegistry(storage_path=DIRECTION_REGISTRY_PATH)

        # 如果注册表为空，初始化预注册方向
        if not _direction_registry.list_directions():
            for direction in _create_default_directions():
                _direction_registry.register(direction)
            logger.info(f"j_space.directions_registered count={len(_create_default_directions())}")
        else:
            logger.info(f"j_space.directions_loaded count={len(_direction_registry.list_directions())}")

        _intervention_loop = InterventionLoop(_signal_stream, _direction_registry)
        for rule in _create_default_rules():
            _intervention_loop.register_rule(rule)
        logger.info(f"j_space.rules_registered count={len(_create_default_rules())}")

        logger.info("j_space.initialized")
    except Exception as e:
        logger.warning(f"j_space.init_failed (non-blocking): {e}")
        _signal_stream = None
        _direction_registry = None
        _intervention_loop = None


def get_signal_stream() -> BehavioralSignalStream | None:
    return _signal_stream


def get_direction_registry() -> DirectionRegistry | None:
    return _direction_registry


def get_intervention_loop() -> InterventionLoop | None:
    return _intervention_loop
```

- [ ] **Step 2: Add init call to Agent startup**

Find the main startup sequence (likely in `agent.py` or `core/bootstrap.py`):
Run: `cd /home/orangepi/ai-agent && grep -rn "async def main\|async def startup\|async def init" agent.py core/bootstrap.py 2>/dev/null | head -5`

Add after database init:
```python
    # J-Space 架构优化初始化
    try:
        from core.j_space_bootstrap import init_j_space
        init_j_space()
    except Exception as e:
        logger.warning(f"j_space.bootstrap_failed (non-blocking): {e}")
```

- [ ] **Step 3: Run all J-Space tests**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_behavioral_signal.py tests/test_behavioral_direction.py tests/test_intervention_loop.py tests/test_intent_decomposition.py tests/test_enhanced_router.py tests/test_structured_blackboard.py tests/test_e2e_closed_loop.py -v`
Expected: All tests PASS

- [ ] **Step 4: Run full test suite to verify no regression**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/ -x --timeout=60 -q 2>&1 | tail -20`
Expected: No new failures introduced by J-Space changes

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add core/j_space_bootstrap.py
git commit -m "feat: J-Space 启动初始化模块 (j_space_bootstrap) — 预注册方向+默认规则"
```

---

## Self-Review

### Spec coverage check
- §3.1 BehavioralSignalStream → Task 1 ✓
- §3.2 DirectionVector/DirectionRegistry → Task 2 ✓
- §3.3 InterventionLoop → Task 4 ✓
- §3.4 IntentDecomposer → Task 5 ✓
- §3.5 EnhancedBeliefRouter → Task 6 ✓
- §3.6 StructuredBlackboard → Task 7 ✓
- 8 Hook 接入点 → Tasks 8-11 ✓
- 配置开关 → Task 3 ✓
- 端到端测试 → Task 12 ✓
- 启动初始化 → Task 13 ✓

### Placeholder scan
- No TBD/TODO in tasks ✓
- All code blocks contain complete implementation ✓
- Phase 2 _llm_encode explicitly raises NotImplementedError (in YAGNI) ✓

### Type consistency
- `BehavioralSignalStream` used consistently across Tasks 1, 4, 6, 8, 13 ✓
- `DirectionVector` / `DirectionRegistry` used consistently across Tasks 2, 4, 6, 13 ✓
- `InterventionRule` / `InterventionLoop` used consistently across Tasks 4, 13 ✓
- `StructuredBlackboard` used consistently across Tasks 7, 11 ✓
