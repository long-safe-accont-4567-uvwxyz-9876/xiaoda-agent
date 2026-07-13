# J-Space 生态圈源码分析 & nahida-agent 架构优化 Spec

> 完成日期: 2026-07-12
> 版本: v1.0
> 作者: 源码级深度分析产出

---

## 1. 背景与动机

### 1.1 J-Space 发现的逻辑链

J-Space（Jacobain Space）的核心发现是：**Transformer 每一层的残差流可以通过平均输入-输出 Jacobian 矩阵 J_l 线性映射到最终层空间**，即 `lens_l(h) = unembed(J_l @ h)`。这意味着：

1. **中间层表征可被可靠读取** — 通过 J_l 传输，任意层的残差流都可以在最终层的语义空间中被解码（`jlens/lens.py: JacobianLens.transport()`）
2. **概念方向可以被识别和操作** — PCA/对比向量/线性探针等方法可以在 J-Space 中找到对应特定概念的方向向量（`repe/rep_readers.py: PCARepReader`, `reprobe/probe.py: Probe`）
3. **行为可以被实时干预** — 沿概念方向加减残差流，可以系统性地改变模型输出行为（`repe/rep_control_reading_vec.py: WrappedBlock.set_controller()`, `reprobe/steerer.py: Steerer`）
4. **内部状态可以被持续监控** — 通过 hook + probe 组合，可以在生成过程中实时检测模型是否处于某种概念状态（`reprobe/monitor.py: Monitor`）
5. **特征可以被稀疏分解** — SAE 将稠密表征分解为可解释的稀疏特征，提供更细粒度的语义分解（`SAELens/sae_lens/saes/sae.py: SAE.encode()`）

### 1.2 从 J-Space 到 Agent 架构优化的映射

nahida-agent 使用远程 API 模型，**无法直接访问模型内部激活**。但 J-Space 生态圈揭示的设计模式和接口抽象，在 Agent 层面有精确的对应物：

| J-Space 原始能力 | Agent 层面对应 | nahida-agent 当前模块 |
|---|---|---|
| 激活采集（Interceptor） | 输出/行为信号采集 | `agent_introspection.py` (仅静态快照) |
| 方向识别（RepReader/Probe） | 行为模式/意图方向识别 | `belief_router.py` (仅 Thompson Sampling) |
| 行为干预（Steerer/ContrastVec） | Prompt/Context 动态干预 | `prompt_builder.py` (隐式, 无方向控制) |
| 状态监控（Monitor） | Agent 状态实时监控 | `agent_introspection.py` (轮询式) |
| 特征分解（SAE） | 输出语义分解/意图因子化 | 无对应 |
| 层间传输（JacobianLens） | 多级表征对齐/传递 | `shared_blackboard.py` (KV, 无语义对齐) |

**核心动机**：将 J-Space 的**结构化内省-干预闭环**移植到 Agent 层面，使 nahida-agent 获得「理解自身行为模式 → 识别偏差方向 → 主动干预纠偏」的能力，而非仅依赖静态规则和简单采样。

---

## 2. 现状分析：nahida-agent 对照 J-Space 五大属性的不足

### 2.1 五大属性评估

#### 属性 1: 可观测性（Observability）— 当前 ★☆☆☆☆

**J-Space 的做法**：通过 `ActivationRecorder` + `Interceptor` 在每一层、每个 token 位置捕获残差流，提供完整的内部状态可见性。

- `jlens/hooks.py: ActivationRecorder` — 上下文管理器，注册 forward hook 后自动收集指定层的激活
- `reprobe/interceptor.py: Interceptor` — 区分 prefill/token 模式，按层收集激活存入 HDF5

**nahida-agent 的不足**：
- `agent_introspection.py` 仅提供**轮询式快照**（`get_current_state()`），无实时流
- 采集的字段（`cognitive_load`, `confidence`）是**硬编码的固定维度**，无法按需扩展
- 无**行为信号的时间序列**采集能力（Monitor 的 `history` 列表模式）
- 各模块状态分散在 `metacognition`, `degradation_strategy`, `behavioral_health` 等处，无统一采集通道

#### 属性 2: 方向可控性（Directional Control）— 当前 ★☆☆☆☆

**J-Space 的做法**：通过 `RepReader`/`Probe` 识别概念方向，再通过 `WrappedBlock.set_controller()` 或 `Steerer._apply_projection()` 沿方向精确干预。

- `repe/rep_control_reading_vec.py: WrappedBlock` — 支持 `linear_comb`（加法）和 `piecewise_linear`（符号相关加法）两种干预算子
- `reprobe/steerer.py: Steerer` — 支持 `projected`（仅减投影分量）和 `uniform`（减全量）两种模式
- `repe/rep_control_contrast_vec.py` — 在生成过程中实时计算正负对比向量并注入

**nahida-agent 的不足**：
- 无**方向向量**概念 — `prompt_builder.py` 的干预是隐式的（拼接文本），无法量化"沿什么方向"干预了多少
- `belief_router.py` 的 Thompson Sampling 是**黑盒路由** — 无法解释"为什么选了这个 Agent"
- 情绪系统 `emotion/` 无方向控制 — 无法"降低焦虑方向0.3，增加平静方向0.5"

#### 属性 3: 闭环性（Closed-Loop）— 当前 ★★☆☆☆

**J-Space 的做法**：`Monitor` 实时观测 → 阈值判断 → `Steerer` 自动干预，形成观测-干预闭环。

- `reprobe/monitor.py: Monitor.score()` — 多种聚合策略（`max_of_means`, `mean_of_means`, `max_absolute`）
- `reprobe/steerer.py: Steerer._apply_projection()` — 投影干预后 Monitor 可立即观测到效果变化

**nahida-agent 的不足**：
- `agent_introspection.py` 采集 → 无自动决策 → 无自动干预的闭环
- `degradation_strategy.py` 有4级降级但仅基于异常计数触发，非基于实时行为信号
- `belief_router.py` 有 update_belief 但无"观测→干预→验证"的完整闭环

#### 属性 4: 可分解性（Decomposability）— 当前 ★☆☆☆☆

**J-Space 的做法**：SAE 将 d_model 维残差流分解为 d_sae 维稀疏特征（通常 d_sae >> d_model），每个特征可独立解释。

- `SAELens/sae_lens/saes/sae.py: SAE.encode()/decode()` — 编码为稀疏特征激活，解码回原始空间
- `SAELens/sae_lens/training/activations_store.py: ActivationsStore` — 流式激活采集与缓存

**nahida-agent 的不足**：
- Agent 输出是**不可分解的整体** — 无法分解"这条回复中 30% 是知识检索，50% 是情感回应，20% 是安全护栏"
- `shared_blackboard.py` 是 KV 存储，无语义分解能力
- 无**意图因子化**概念 — 无法识别和分离多意图混合的输出

#### 属性 5: 可迁移性（Transferability）— 当前 ★★☆☆☆

**J-Space 的做法**：`JacobianLens.merge()` 可以将不同 prompt 子集上拟合的 lens 合并；`JacobianLens.from_pretrained()` 支持从 HuggingFace Hub 加载预训练 lens。

- `jlens/lens.py: JacobianLens.merge()` — n_prompts 加权平均合并
- `jlens/lens.py: JacobianLens.from_pretrained()` — Hub 加载
- `reprobe/loader.py: ProbeLoader` — 支持 registry.json 和 .pt 两种格式加载

**nahida-agent 的不足**：
- `belief_router.py` 的信念参数（alpha/beta）是**单模型单 Agent 的**，无法跨场景迁移
- `shared_blackboard.py` 的数据无结构化 schema，无法被其他 Agent 复用
- 无**预训练行为方向库**的概念

---

## 3. 目标架构：6 个优化方向

### 3.1 优化方向 1: 行为信号流（Behavioral Signal Stream）

**目标**：将 Agent 的行为信号从"轮询式快照"升级为"持续流式采集"，对齐 J-Space 的 `Interceptor + Monitor` 模式。

#### 设计

```python
# 文件: core/behavioral_signal.py (新建)

from dataclasses import dataclass, field
from typing import Any, Callable
import time
import asyncio
from collections import deque

@dataclass
class SignalEntry:
    """单条行为信号"""
    signal_type: str          # "confidence", "sentiment", "tool_usage", "token_latency"...
    value: float              # 归一化值 0-1
    source: str               # 来源模块名
    timestamp: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)  # 附加上下文

class BehavioralSignalStream:
    """
    持续行为信号流 — 对齐 reprobe/interceptor.py 的激活采集模式。
    
    设计参考:
    - reprobe/interceptor.py: Interceptor 区分 prefill/token 两种采集模式
    - reprobe/monitor.py: Monitor 的 history 列表 + _flush_step()
    - jlens/hooks.py: ActivationRecorder 的上下文管理器模式
    """
    
    def __init__(self, max_history: int = 1000, flush_interval: float = 1.0):
        self._buffer: deque[SignalEntry] = deque(maxlen=max_history)
        self._subscribers: dict[str, list[asyncio.Event]] = {}
        self._flush_interval = flush_interval
        self._last_flush = time.monotonic()
    
    async def emit(self, signal_type: str, value: float, source: str = "", **meta) -> None:
        """发射一条行为信号。对齐 reprobe/interceptor 的 _flush 模式。"""
        entry = SignalEntry(signal_type=signal_type, value=value, source=source, meta=meta)
        self._buffer.append(entry)
        # 通知订阅者 — 对齐 shared_blackboard.py 的 subscribe 模式
        if signal_type in self._subscribers:
            for ev in self._subscribers[signal_type]:
                ev.set()
            self._subscribers[signal_type].clear()
    
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
            # 按时间窗口分桶后取均值再取最大
            return max(values)
        elif strategy == "mean_of_means":
            return sum(values) / len(values)
        elif strategy == "max_absolute":
            return max(abs(v) for v in values)
        return sum(values) / len(values)
```

#### 接口定义

| 接口 | 方法 | 对齐参考 |
|---|---|---|
| `emit(signal_type, value, source)` | 发射信号 | `reprobe/interceptor.py: _flush()` |
| `subscribe(signal_type)` | 订阅信号变更 | `agent_core/shared_blackboard.py: subscribe()` |
| `get_history(signal_type, last_n)` | 获取历史 | `reprobe/monitor.py: get_history()` |
| `aggregate(signal_type, strategy)` | 聚合计算 | `reprobe/monitor.py: score()` |

#### 数据流

```
各模块(AgentIntrospector/EmotionSystem/ToolExecutor)
  │ emit(signal_type, value)
  ▼
BehavioralSignalStream._buffer: deque[SignalEntry]
  │ subscribe → asyncio.Event
  ▼
MonitorAgent (消费者): 聚合/阈值判断 → 触发干预
```

---

### 3.2 优化方向 2: 行为方向向量（Behavioral Direction Vectors）

**目标**：引入"方向向量"概念，使 Agent 的行为干预从隐式文本拼接变为可量化的向量操作，对齐 RepE 的 `WrappedBlock.set_controller()` 和 reprobe 的 `Steerer`。

#### 设计

```python
# 文件: core/behavioral_direction.py (新建)

from dataclasses import dataclass, field
from typing import Any, Callable, Literal
import json
from pathlib import Path

@dataclass
class DirectionVector:
    """
    行为方向向量 — 对齐 RepE 的 contrast vector 和 reprobe 的 Probe.get_direction()。
    
    在 API-only 约束下，方向向量不作用于模型激活，而作用于 Agent 的上下文空间：
    - prompt 维度：特定提示词模板的权重
    - 工具维度：工具选择偏置
    - 情绪维度：情绪状态空间的偏移
    - 路由维度：Agent 路由的软偏好
    
    参考:
    - repe/rep_readers.py: PCARepReader.get_rep_directions() — PCA方向识别
    - reprobe/probe.py: Probe.get_direction() — 归一化线性探针方向
    - repe/rep_control_reading_vec.py: WrappedBlock.set_controller() — 方向注入
    - reprobe/steerer.py: Steerer._apply_projection() — 投影干预
    """
    name: str                                    # 方向名: "helpfulness", "safety", "calm"...
    dimensions: dict[str, float]                 # 各维度权重: {"prompt": 0.3, "tool": 0.5, "emotion": -0.2}
    source: str = ""                             # 来源: "pca", "manual", "learned"
    magnitude: float = 1.0                       # 整体强度
    meta: dict = field(default_factory=dict)     # 元信息: auc, n_samples...
    
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
        - repe/rep_control_reading_vec.py: WrappedBlock.forward() — 
          hidden += alpha * (pos_act - neg_act)
        - reprobe/steerer.py: Steerer._apply_projection() — 
          hidden -= alpha * projection
        
        在 API-only 下，"hidden" 是 context dict 而非激活张量。
        """
        result = dict(context)
        for dim, weight in self.dimensions.items():
            if dim == "prompt":
                # 对齐 WrappedBlock.linear_comb: current + controller
                result["prompt_modifier"] = result.get("prompt_modifier", 0.0) + weight
            elif dim == "tool":
                result["tool_bias"] = result.get("tool_bias", 0.0) + weight
            elif dim == "emotion":
                # 对齐 Steerer._apply_projection: 减去投影分量
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
    
    管理 Agent 可用的所有行为方向，支持:
    - 手动注册（人工定义的方向）
    - 自动发现（从历史行为数据中提取，对齐 PCARepReader）
    - 持久化存储（对齐 ProbeLoader 的 registry.json 模式）
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
        """对齐 reprobe/store.py: ActivationStore 的 HDF5 持久化模式"""
        registry = {
            name: {"dimensions": d.dimensions, "source": d.source, 
                   "magnitude": d.magnitude, "meta": d.meta}
            for name, d in self._directions.items()
        }
        Path(self._storage_path).write_text(
            json.dumps(registry, indent=2, ensure_ascii=False))
    
    def _load_from_storage(self) -> None:
        path = Path(self._storage_path)
        if not path.exists():
            return
        registry = json.loads(path.read_text())
        for name, data in registry.items():
            self._directions[name] = DirectionVector(
                name=name, dimensions=data["dimensions"],
                source=data.get("source", ""),
                magnitude=data.get("magnitude", 1.0),
                meta=data.get("meta", {}),
            )
```

#### 接口定义

| 接口 | 方法 | 对齐参考 |
|---|---|---|
| `DirectionVector(dimensions)` | 构造方向 | `repe/rep_readers.py: PCARepReader.get_rep_directions()` |
| `dv * scalar` | 缩放方向 | `reprobe/steerer.py: alpha` |
| `dv1 + dv2` | 叠加方向 | `repe/rep_control_reading_vec.py: linear_comb` |
| `dv.apply_to_context(ctx)` | 应用到上下文 | `reprobe/steerer.py: _apply_projection()` |
| `DirectionRegistry.register/load` | 注册/加载 | `reprobe/loader.py: ProbeLoader` |

---

### 3.3 优化方向 3: 自适应干预闭环（Adaptive Intervention Loop）

**目标**：建立"观测→判断→干预→验证"的闭环，对齐 reprobe 的 `Monitor + Steerer` 组合模式。

#### 设计

```python
# 文件: core/intervention_loop.py (新建)

from typing import Any, Callable, Literal
from dataclasses import dataclass
import asyncio
import time
from loguru import logger

from core.behavioral_signal import BehavioralSignalStream
from core.behavioral_direction import DirectionVector, DirectionRegistry

@dataclass
class InterventionRule:
    """
    干预规则 — 对齐 reprobe/monitor.py: Monitor 的 score() 阈值逻辑 + 
    reprobe/steerer.py: Steerer 的干预逻辑。
    
    参考:
    - reprobe/monitor.py: Monitor.score() 聚合后与阈值比较
    - reprobe/steerer.py: Steerer._apply_projection() / _apply_uniform() 两种干预模式
    - core/degradation_strategy.py: 4级降级策略的阈值触发模式
    """
    signal_type: str                    # 监控的信号类型
    threshold: float                    # 触发阈值
    direction_name: str                 # 触发后应用的方向向量
    alpha: float = 1.0                  # 干预强度 — 对齐 Steerer.alpha
    mode: Literal["projected", "uniform"] = "projected"  # 干预模式
    cooldown: float = 5.0               # 冷却时间(秒)
    last_triggered: float = 0.0         # 上次触发时间

class InterventionLoop:
    """
    自适应干预闭环 — 对齐 reprobe 的 Monitor + Steerer 组合。
    
    核心循环:
    1. 观测: BehavioralSignalStream.aggregate() 获取当前信号值
    2. 判断: InterventionRule 阈值比较
    3. 干预: DirectionVector.apply_to_context() 修改上下文
    4. 验证: 下一轮观测确认干预效果
    
    参考:
    - reprobe/monitor.py: Monitor._flush_step() + score() 
    - reprobe/steerer.py: Steerer 的 projected/uniform 两种模式
    - jlens/fitting.py: fit() 的 running mean 收敛追踪 — 
      "mean_rel_change tracks convergence (falls ~1/n once settled)"
    """
    
    def __init__(
        self,
        signal_stream: BehavioralSignalStream,
        direction_registry: DirectionRegistry,
        rules: list[InterventionRule] | None = None,
    ):
        self._stream = signal_stream
        self._registry = direction_registry
        self._rules = rules or []
        self._intervention_history: list[dict] = []
    
    def add_rule(self, rule: InterventionRule) -> None:
        self._rules.append(rule)
    
    async def evaluate(self) -> list[dict]:
        """
        评估所有规则，返回触发的干预列表。
        
        对齐 reprobe/monitor.py: Monitor.score(strategy) 的聚合逻辑:
        - max_of_means: 适合检测突发异常
        - mean_of_means: 适合检测持续偏移
        - max_absolute: 适合检测任何极端值
        """
        triggered = []
        for rule in self._rules:
            now = time.monotonic()
            if now - rule.last_triggered < rule.cooldown:
                continue
            
            score = self._stream.aggregate(rule.signal_type, strategy="mean_of_means")
            
            if score > rule.threshold:
                direction = self._registry.get(rule.direction_name)
                if direction is None:
                    continue
                
                # 缩放方向 — 对齐 reprobe/steerer.py: Steerer 的 alpha 参数
                scaled = direction * rule.alpha
                
                rule.last_triggered = now
                triggered.append({
                    "rule": rule.signal_type,
                    "score": score,
                    "direction": rule.direction_name,
                    "alpha": rule.alpha,
                    "mode": rule.mode,
                    "scaled_direction": scaled,
                })
                
                # 记录干预历史 — 对齐 reprobe/monitor.py: Monitor.history
                self._intervention_history.append({
                    "timestamp": time.time(),
                    "signal_type": rule.signal_type,
                    "score": score,
                    "threshold": rule.threshold,
                    "direction": rule.direction_name,
                    "alpha": rule.alpha,
                })
        
        return triggered
    
    async def apply_intervention(
        self, context: dict, intervention: dict
    ) -> dict:
        """
        应用干预到上下文。
        
        对齐:
        - reprobe/steerer.py: Steerer._apply_projection() — 仅减去投影分量
        - reprobe/steerer.py: Steerer._apply_uniform() — 减去全量方向
        """
        direction: DirectionVector = intervention["scaled_direction"]
        mode = intervention["mode"]
        
        if mode == "projected":
            # 投影模式: 仅修改方向相关维度
            # 对齐 Steerer._apply_projection: 
            #   dot_product = matmul(hidden, direction)
            #   projection = dot_product.unsqueeze(-1) * direction
            #   hidden = hidden - alpha * projection
            return direction.apply_to_context(context)
        else:
            # 均匀模式: 全量应用
            # 对齐 Steerer._apply_uniform: hidden = hidden - alpha * direction
            return direction.apply_to_context(context)
    
    def get_convergence_metrics(self) -> dict:
        """
        收敛指标 — 对齐 jlens/fitting.py: fit() 中的 mean_rel_change 追踪。
        
        如果连续干预的 score 下降，说明干预有效；否则需要调整 alpha。
        """
        if len(self._intervention_history) < 2:
            return {"converging": True, "intervention_count": len(self._intervention_history)}
        
        recent = self._intervention_history[-5:]
        scores = [h["score"] for h in recent]
        # 简单趋势: 最近分数是否在下降
        trend = scores[-1] - scores[0] if len(scores) >= 2 else 0
        return {
            "converging": trend < 0,
            "trend": trend,
            "intervention_count": len(self._intervention_history),
            "recent_scores": scores,
        }
```

#### 数据流

```
BehavioralSignalStream ──aggregate()──▶ InterventionLoop.evaluate()
                                              │
                                         threshold 判断
                                              │
                                    DirectionRegistry.get()
                                              │
                                    DirectionVector * alpha
                                              │
                                    apply_to_context(context)
                                              │
                                    下一轮 evaluate() 验证
```

---

### 3.4 优化方向 4: 输出意图分解（Output Intent Decomposition）

**目标**：将 Agent 输出分解为可解释的意图因子，对齐 SAELens 的稀疏自编码器范式。

#### 设计

```python
# 文件: core/intent_decomposition.py (新建)

from dataclasses import dataclass, field
from typing import Any
import re

@dataclass
class IntentFactor:
    """
    意图因子 — 对齐 SAELens/sae_lens/saes/sae.py 中 SAE 的稀疏特征。
    
    SAE 将 d_model 维残差流编码为 d_sae 维稀疏特征:
        feature_acts = encode(x)     # [d_sae], 大部分为0
        x_recon = decode(feature_acts) # [d_model]
    
    对应地，IntentDecomposition 将 Agent 输出编码为意图因子:
        factors = encode(output)       # 各意图的激活值
        reconstructed = decode(factors) # 重建输出(用于验证)
    """
    name: str                  # 意图名: "knowledge", "emotion", "safety", "creative"...
    activation: float          # 激活强度 0-1 — 对齐 SAE 的 feature_acts
    evidence: str = ""         # 支持该意图的输出片段
    confidence: float = 1.0    # 分解置信度

@dataclass  
class DecomposedOutput:
    """分解后的输出 — 对齐 SAE 的 encode 输出"""
    raw_output: str
    factors: list[IntentFactor]
    residual: float = 0.0      # 不可解释的残差 — 对齐 SAE 的 error term
    
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
        active = sum(1 for f in self.factors if f.activation > 0.1)
        return 1.0 - active / len(self.factors)

class IntentDecomposer:
    """
    输出意图分解器 — 对齐 SAELens 的 SAE encode/decode 范式。
    
    在 API-only 约束下，无法像 SAE 那样对模型激活做稀疏编码。
    替代方案: 利用 LLM 自身对输出做结构化分析，或基于规则的意图识别。
    
    参考:
    - SAELens/sae_lens/saes/sae.py: SAE.encode(x) → feature_acts
    - SAELens/sae_lens/saes/sae.py: SAE.decode(feature_acts) → sae_out
    - SAELens/sae_lens/training/activations_store.py: ActivationsStore 的流式采集
    """
    
    # 预定义意图维度 — 对齐 SAE 的 d_sae (特征数量)
    INTENT_DIMENSIONS = [
        "knowledge",       # 知识检索
        "emotional",       # 情感回应
        "safety",          # 安全护栏
        "creative",        # 创意生成
        "factual",         # 事实陈述
        "social",          # 社交互动
        "procedural",      # 操作指导
    ]
    
    def __init__(self, use_llm_decomposition: bool = False):
        self._use_llm = use_llm_decomposition
    
    async def encode(self, output: str, context: dict | None = None) -> DecomposedOutput:
        """
        将输出编码为意图因子 — 对齐 SAE.encode()
        
        Phase 1: 基于规则的快速分解
        Phase 2+: 基于 LLM 的深度分解
        """
        if self._use_llm:
            return await self._llm_encode(output, context)
        return self._rule_encode(output, context)
    
    def _rule_encode(self, output: str, context: dict | None = None) -> DecomposedOutput:
        """
        规则基分解 — Phase 1 实现。
        
        对齐 PCARepReader 的思路: 用简单统计方法识别方向。
        这里用关键词匹配 + 启发式权重作为最简实现。
        """
        factors = []
        text_lower = output.lower()
        
        # 知识意图
        knowledge_score = self._score_keywords(text_lower, [
            "根据", "资料显示", "研究表明", "数据表明", "据统计",
            "据了解", "据报道", "according to", "research shows",
        ])
        if knowledge_score > 0:
            factors.append(IntentFactor("knowledge", knowledge_score))
        
        # 情感意图
        emotion_score = self._score_keywords(text_lower, [
            "别担心", "加油", "理解你的感受", "心疼", "开心",
            "难过", "陪伴", "安慰", "don't worry", "I understand",
        ])
        if emotion_score > 0:
            factors.append(IntentFactor("emotional", emotion_score))
        
        # 安全意图
        safety_score = self._score_keywords(text_lower, [
            "请注意", "安全", "风险", "不建议", "谨慎",
            "warning", "caution", "not recommended",
        ])
        if safety_score > 0:
            factors.append(IntentFactor("safety", safety_score))
        
        # 创意意图
        creative_score = self._score_keywords(text_lower, [
            "可以试试", "不如", "想象一下", "如果", "创意",
            "how about", "what if", "imagine",
        ])
        if creative_score > 0:
            factors.append(IntentFactor("creative", creative_score))
        
        # 归一化
        if factors:
            total = sum(f.activation for f in factors)
            if total > 1.0:
                for f in factors:
                    f.activation /= total
        
        # 计算残差 — 对齐 SAE 的 error term (x - x_recon)
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
        """
        LLM 基分解 — Phase 2 实现。
        
        对齐 SAE 的编码能力: 用 LLM 自身对输出做意图打分。
        这是最精确但也是最慢的方法。
        
        [INFO_GAP]: Phase 2 实现，需与 model_router 集成
        """
        # Phase 2: 使用 LLM 对输出做意图分解
        # 构造 prompt: "分析以下回复中各意图的权重: knowledge/emotional/safety/creative"
        # 解析 LLM 返回的 JSON
        raise NotImplementedError("Phase 2: LLM-based decomposition")
```

#### 接口定义

| 接口 | 方法 | 对齐参考 |
|---|---|---|
| `IntentDecomposer.encode(output)` | 编码为意图因子 | `SAELens: SAE.encode(x)` |
| `DecomposedOutput.dominant_intent` | 获取主导意图 | SAE 的 top-k 特征 |
| `DecomposedOutput.sparsity` | 稀疏度 | SAE 的 l0 度量 |
| `DecomposedOutput.residual` | 不可解释残差 | SAE 的 error term |

---

### 3.5 优化方向 5: 增强型路由（Enhanced Routing with Directional Bias）

**目标**：将 Agent 路由从纯 Thompson Sampling 升级为"方向向量 + 历史信念 + 实时信号"的多因素路由，对齐 ACT 的 q-wise direction 和 RepE 的 concept direction。

#### 设计

```python
# 文件: core/enhanced_router.py (新建)

from dataclasses import dataclass
from typing import Any
import math
import random
from loguru import logger

from belief_router import AgentBelief
from core.behavioral_signal import BehavioralSignalStream
from core.behavioral_direction import DirectionVector, DirectionRegistry

class EnhancedBeliefRouter:
    """
    增强型路由器 — 在 Thompson Sampling 基础上叠加方向偏置。
    
    对齐:
    - belief_router.py: BeliefRouter 的 Thompson Sampling (基础)
    - ACT/generate_directions_q_wise.py: 按问题类型生成方向 (q-wise direction)
    - repe/rep_readers.py: PCARepReader 的方向识别
    - reprobe/steerer.py: Steerer 的方向应用
    
    路由公式 (对齐 ACT 的方向加权思路):
        score(agent) = thompson_sample(agent) 
                     + alpha * direction_bias(task_type, agent)
                     + beta * signal_adjustment(agent, recent_signals)
    """
    
    def __init__(
        self,
        base_router,                          # 原始 BeliefRouter
        direction_registry: DirectionRegistry,
        signal_stream: BehavioralSignalStream,
        direction_weight: float = 0.3,         # 方向偏置权重
        signal_weight: float = 0.2,            # 信号调整权重
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
        direction_hint: str = "",               # 方向提示 (对齐 ACT 的 q-wise)
    ) -> str:
        """
        增强型 Agent 选择。
        
        对齐 ACT/generate_directions_q_wise.py 的思路:
        原始: head_wise_activation_directions = mean(pos) - mean(neg) per question
        对应: agent_direction_bias = f(task_type, agent) — 每种任务类型对每个 Agent 的方向偏好
        """
        # 基础 Thompson Sampling
        candidates = [a for a in self._base.VALID_AGENTS if a not in (exclude or set())]
        if not candidates:
            return "xiaoda"
        
        # 1. Thompson Sampling 基础分
        thompson_scores = {a: self._base._beliefs[a].sample() for a in candidates}
        
        # 2. 方向偏置 — 对齐 ACT 的 q-wise direction
        direction_scores = {}
        if task_type or direction_hint:
            direction_key = direction_hint or f"route_{task_type}"
            direction = self._registry.get(direction_key)
            if direction and "route" in direction.dimensions:
                route_bias = direction.dimensions["route"]
                for agent in candidates:
                    # 简化: 方向偏置按 Agent 类型分配
                    agent_task_map = {
                        "xiaolang": "security",
                        "xiaoke": "debug",
                        "xiaolian": "info_search",
                    }
                    match = 1.0 if agent_task_map.get(agent) == task_type else 0.0
                    direction_scores[agent] = route_bias * match
        
        # 3. 实时信号调整 — 对齐 Monitor 的实时观测
        signal_scores = {}
        for agent in candidates:
            # 最近该 Agent 的成功信号
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

---

### 3.6 优化方向 6: 结构化共享黑板（Structured Shared Blackboard）

**目标**：将共享黑板从简单 KV 存储升级为支持语义索引、方向感知的结构化存储，对齐 SAELens 的 `ActivationsStore` 和 reprobe 的 `ActivationStore`。

#### 设计

```python
# 文件: agent_core/structured_blackboard.py (新建)

from dataclasses import dataclass, field
from typing import Any, Literal
import asyncio
import time
import json
from pathlib import Path

from agent_core.shared_blackboard import SharedBlackboard
from core.behavioral_direction import DirectionVector

@dataclass
class StructuredEntry:
    """
    结构化黑板条目 — 对齐 reprobe/store.py: ActivationStore 的 HDF5 条目。
    
    扩展原有 _Entry:
    - 增加语义标签 (tags) — 对齐 SAE 的 feature label
    - 增加方向标记 (direction) — 对齐 Steerer 的干预方向
    - 增加质量评分 (quality) — 对齐 Probe 的 AUC
    """
    value: Any
    agent_name: str
    expire_at: float | None
    tags: list[str] = field(default_factory=list)        # 语义标签
    direction: str = ""                                    # 关联的方向向量名
    quality: float = 1.0                                   # 数据质量评分
    schema_version: str = "1.0"                            # schema 版本

class StructuredBlackboard(SharedBlackboard):
    """
    结构化共享黑板 — 在 SharedBlackboard 基础上增加:
    1. 语义标签索引 — 对齐 SAE 的 feature label
    2. 方向关联 — 对齐 Steerer 的干预方向
    3. 质量评分 — 对齐 Probe 的 AUC
    4. Schema 版本控制 — 对齐 reprobe/store.py 的持久化 schema
    
    参考:
    - agent_core/shared_blackboard.py: SharedBlackboard (基础)
    - reprobe/store.py: ActivationStore 的 HDF5 结构化存储
    - SAELens/sae_lens/training/activations_store.py: ActivationsStore 的流式采集
    - jlens/lens.py: JacobianLens.merge() 的加权合并
    """
    
    def __init__(self, default_ttl: float = 600.0, persist_path: str = "") -> None:
        super().__init__(default_ttl)
        self._tag_index: dict[str, set[str]] = {}     # tag → set of keys
        self._direction_index: dict[str, set[str]] = {}  # direction → set of keys
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
        # 更新标签索引
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
        """
        按标签查询 — 对齐 SAE 的 feature lookup。
        类似于在 SAE 的稀疏特征空间中查找特定特征的激活。
        """
        keys = self._tag_index.get(tag, set())
        results = []
        for key in keys:
            entry = await self.get_with_meta(key)
            if entry:
                results.append({"key": key, **entry})
        return results
    
    async def query_by_direction(self, direction_name: str) -> list[dict]:
        """
        按方向查询 — 对齐 Steerer 的方向关联。
        找到所有与某个干预方向相关的黑板条目。
        """
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
        
        JacobianLens.merge 使用 n_prompts 加权平均合并 J_l 矩阵。
        这里简化为: 不存在的 key 直接导入, 已存在的保留质量更高的。
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

---

## 4. 实施路线

### Phase 1: 最小可实施（1-2 天）

**目标**：建立信号流 + 方向向量基础设施，使现有模块可以开始产出和消费行为信号。

| 任务 | 改动文件 | 验收标准 |
|---|---|---|
| 新建 `core/behavioral_signal.py` | 新文件 | `BehavioralSignalStream` 可 emit/subscribe/aggregate |
| 在 `agent_introspection.py` 中集成信号发射 | `core/agent_introspection.py` | `get_current_state()` 同时向 SignalStream emit 信号 |
| 新建 `core/behavioral_direction.py` | 新文件 | `DirectionVector` 支持构造/缩放/叠加/apply_to_context |
| 新建 `config/behavioral_directions/` 目录 | 新文件 | 包含 3+ 个预定义方向 JSON 文件 |
| 单元测试 | `tests/test_behavioral_signal.py`, `tests/test_behavioral_direction.py` | 覆盖核心接口 |

**改动文件清单**:
- 新建: `core/behavioral_signal.py`
- 新建: `core/behavioral_direction.py`
- 新建: `config/behavioral_directions/helpfulness.json`
- 新建: `config/behavioral_directions/safety.json`
- 新建: `config/behavioral_directions/calm.json`
- 修改: `core/agent_introspection.py` — 添加 SignalStream 集成
- 新建: `tests/test_behavioral_signal.py`
- 新建: `tests/test_behavioral_direction.py`

### Phase 2: 干预闭环 + 意图分解（3-5 天）

**目标**：建立"观测→干预→验证"闭环，使 Agent 具备自适应行为调整能力。

| 任务 | 改动文件 | 验收标准 |
|---|---|---|
| 新建 `core/intervention_loop.py` | 新文件 | `InterventionLoop` 可 evaluate + apply_intervention |
| 新建 `core/intent_decomposition.py` | 新文件 | `IntentDecomposer._rule_encode()` 可分解输出 |
| 在 `emotion/` 中集成方向向量干预 | `emotion/emotion_state.py` | 情绪偏移可通过 DirectionVector 表达 |
| 在 `belief_router.py` 中发射路由信号 | `belief_router.py` | 路由成功/失败时 emit 信号 |
| 在 `agent_dispatcher.py` 中集成干预闭环 | `agent_dispatcher.py` | SubAgent.chat() 前可应用干预 |
| 集成测试 | `tests/test_intervention_loop.py` | 闭环可运行并记录干预历史 |

**改动文件清单**:
- 新建: `core/intervention_loop.py`
- 新建: `core/intent_decomposition.py`
- 修改: `emotion/emotion_state.py`
- 修改: `belief_router.py`
- 修改: `agent_dispatcher.py`
- 新建: `tests/test_intervention_loop.py`

### Phase 3: 增强路由 + 结构化黑板 + 持久化（5-7 天）

**目标**：完成全部 6 个优化方向的集成，建立可迁移的行为知识库。

| 任务 | 改动文件 | 验收标准 |
|---|---|---|
| 新建 `core/enhanced_router.py` | 新文件 | `EnhancedBeliefRouter` 多因素路由可工作 |
| 新建 `agent_core/structured_blackboard.py` | 新文件 | `StructuredBlackboard` 支持标签/方向查询 |
| 方向向量持久化 + 自动学习 | `core/behavioral_direction.py` | 可从历史行为数据中 PCA 提取方向 |
| 意图分解 Phase 2 (LLM 分解) | `core/intent_decomposition.py` | `_llm_encode()` 可用 |
| 收敛监控仪表板 | `core/intervention_loop.py` | `get_convergence_metrics()` 可输出到 /health/self |
| 端到端集成测试 | `tests/test_jspace_integration.py` | 完整闭环: 信号→方向→干预→验证 |

**改动文件清单**:
- 新建: `core/enhanced_router.py`
- 新建: `agent_core/structured_blackboard.py`
- 修改: `core/behavioral_direction.py` — 添加 PCA 自动学习
- 修改: `core/intent_decomposition.py` — 添加 LLM 分解
- 修改: `core/intervention_loop.py` — 添加收敛监控
- 新建: `tests/test_jspace_integration.py`

---

## 5. 参考实现映射

### 5.1 优化方向 → 参考项目映射表

| 优化方向 | 参考项目 | 核心源码文件 | 可借鉴设计 |
|---|---|---|---|
| 行为信号流 | reprobe | `src/reprobe/interceptor.py` | 区分 prefill/token 两种采集模式; `_flush_step()` 的批次刷写; `allow_one_capture()` 的门控模式 |
| 行为信号流 | reprobe | `src/reprobe/monitor.py` | `history` 列表式时间序列存储; `score()` 的三种聚合策略; `_flush_step()` 的缓冲区管理 |
| 行为信号流 | jlens | `jlens/hooks.py` | `ActivationRecorder` 的上下文管理器模式; forward hook 注册/移除的异常安全; `start_graph_at` 的图裁剪 |
| 行为方向向量 | repe | `repe/rep_readers.py` | `PCARepReader.get_rep_directions()` 的 PCA 方向提取; `project_onto_direction()` 的投影计算; `get_signs()` 的方向正负校准 |
| 行为方向向量 | reprobe | `src/reprobe/probe.py` | `Probe.get_direction()` 的归一化方向提取; `get_raw_direction()` 的非标准化空间方向; `mean_act/std_act` 的归一化预处理 |
| 行为方向向量 | repe | `repe/rep_control_reading_vec.py` | `WrappedBlock.set_controller()` 的算子选择(`linear_comb`/`piecewise_linear`); `token_pos` 的位置选择(start/end/specific); mask 机制的 padding token 处理 |
| 干预闭环 | reprobe | `src/reprobe/steerer.py` | `_apply_projection()` 的投影干预(仅减投影分量); `_apply_uniform()` 的均匀干预(减全量); alpha 缩放参数 |
| 干预闭环 | reprobe | `src/reprobe/monitor.py` | `Monitor` + `Steerer` 的组合使用模式; `_flush_step()` 的自动触发; 多探针跨层聚合 |
| 干预闭环 | jlens | `jlens/fitting.py` | `fit()` 中的 `mean_rel_change` 收敛追踪; `_atomic_save()` 的原子写入; `checkpoint_every` 的增量保存 |
| 输出意图分解 | SAELens | `sae_lens/saes/sae.py` | `SAE.encode()`/`decode()` 的编码-解码范式; `TrainStepOutput` 的结构化输出; `use_error_term` 的残差处理 |
| 输出意图分解 | SAELens | `sae_lens/training/activations_store.py` | `ActivationsStore` 的流式采集模式; `from_config()` 的配置驱动构建; `get_data_loader()` 的自动填充迭代器 |
| 增强型路由 | ACT | `generate_directions_q_wise.py` | 按 question type 分别计算方向: `mean(pos) - mean(neg)` per question; 分离激活 → 逐问题方向生成 |
| 增强型路由 | ACT | `utils.py` | `get_separated_activations()` 的按标签分组; `train_probes_cluster()` 的 K-means 聚类后训练探针; `get_top_heads_cluster()` 的 top-k 选择 |
| 结构化黑板 | SAELens | `sae_lens/training/activations_store.py` | HDF5 结构化存储的 schema 设计; prefill/token 双模式存储; cursor 追踪的恢复机制 |
| 结构化黑板 | reprobe | `src/reprobe/store.py` | `ActivationStore` 的 HDF5 持久化; `append()` 的增量写入; `_resume()` 的崩溃恢复 |
| 结构化黑板 | jlens | `jlens/lens.py` | `JacobianLens.merge()` 的 n_prompts 加权合并; `from_pretrained()` 的 Hub 加载; `save()`/`load()` 的序列化 |

### 5.2 关键设计模式提取

#### 模式 1: Hook + Context Manager（来自 jlens/hooks.py）

```python
# 原始实现: jlens/hooks.py: ActivationRecorder
# Agent 层面移植: BehavioralSignalStream + InterventionLoop

class ActivationRecorder:
    def __enter__(self):
        for index in self._indices:
            self._handles.append(
                self._blocks[index].register_forward_hook(self._make_hook(index)))
        return self
    
    def __exit__(self, *exc):
        for handle in self._handles:
            handle.remove()
```

**可借鉴**: 
- 上下文管理器的异常安全 — hook 注册失败时自动清理已注册的 hook
- 一次性使用模式 — `__enter__` 注册，`__exit__` 清理，避免泄漏
- 应用: `InterventionLoop` 可用类似模式管理干预的启用/禁用

#### 模式 2: Probe + Monitor + Steerer 三件套（来自 reprobe）

```python
# 原始实现: reprobe 的核心三件套
# Probe: 识别方向 (get_direction / get_raw_direction)
# Monitor: 持续观测 (history + score + flush)
# Steerer: 主动干预 (_apply_projection / _apply_uniform)

# Agent 层面移植: DirectionVector + BehavioralSignalStream + InterventionLoop
```

**可借鉴**:
- 职责分离: 识别/观测/干预三者独立，通过数据结构(方向向量)连接
- Probe 的双层方向: `get_direction()`(归一化) vs `get_raw_direction()`(原始空间) — 不同消费者需要不同归一化级别
- Monitor 的三种聚合策略 — 不同场景需要不同的信号聚合方式
- Steerer 的两种干预模式 — 投影干预(精准但弱) vs 均匀干预(粗糙但强)

#### 模式 3: 稀疏编码 + 残差（来自 SAELens）

```python
# 原始实现: SAELens/sae_lens/saes/sae.py
feature_acts = self.encode(x)       # 稀疏编码
sae_out = self.decode(feature_acts)  # 重建
if self.use_error_term:
    sae_error = self.hook_sae_error(x - x_reconstruct_clean)  # 残差
    sae_out = sae_out + sae_error
```

**可借鉴**:
- 编码-解码-残差范式: 任何"解释性分解"都应保留残差项
- `use_error_term` 开关: 可选的残差保留，Phase 1 可关闭
- 应用: `IntentDecomposer.encode()` → `DecomposedOutput.residual`

#### 模式 4: 多模式采集 + 持久化（来自 reprobe/store.py）

```python
# 原始实现: reprobe/store.py: ActivationStore
# - 区分 prefill/token 两种模式
# - HDF5 持久化 (append + resume)
# - cursor 追踪 (崩溃恢复)

# Agent 层面移植: StructuredBlackboard
# - 区分 实时信号/历史状态 两种模式
# - JSON 持久化 (append + resume)  
# - cursor 追踪 (崩溃恢复)
```

#### 模式 5: Protocol 抽象（来自 jlens/protocol.py）

```python
# 原始实现: jlens/protocol.py: LensModel
class LensModel(Protocol):
    n_layers: int
    d_model: int
    layers: Sequence[nn.Module]
    tokenizer: Any
    
    def encode(self, text, *, max_length=...) -> torch.Tensor: ...
    def forward(self, input_ids) -> Any: ...
    def unembed(self, residual) -> torch.Tensor: ...
```

**可借鉴**:
- Protocol (structural typing) 而非继承 — 任何模型只需实现这几个方法就能被 lens 使用
- 最小接口: 只有 `encode`, `forward`, `unembed` 三个方法
- 应用: `BehavioralSignalStream`, `DirectionVector`, `InterventionLoop` 都应定义 Protocol 接口

#### 模式 6: 加权合并（来自 jlens/lens.py: merge()）

```python
# 原始实现
n_total = sum(lens.n_prompts for lens in lenses)
for layer in first.source_layers:
    weighted_sum = sum(lens.jacobians[layer] * lens.n_prompts for lens in lenses)
    merged[layer] = weighted_sum / n_total
```

**可借鉴**:
- n_prompts 加权平均 — 样本量大的子集权重更高
- 前置校验: 检查 source_layers 和 d_model 一致性
- 应用: `StructuredBlackboard.merge_from()` 的合并策略

---

## 6. 风险与局限

### 6.1 API-only 约束下的根本局限

| J-Space 原始能力 | API-only 下能否实现 | 替代方案 | 效果差距 |
|---|---|---|---|
| 模型内部激活读取 | ❌ 不可能 | 仅采集输入/输出层面的行为信号 | 无法获取中间层表征，方向精度受限 |
| 残差流干预 (h += αd) | ❌ 不可能 | Prompt/Context 层面的方向干预 | 干预是间接的，效果依赖模型对 Prompt 的敏感度 |
| 逐 token 生成监控 | ❌ 不可能 | 逐轮次/逐回复监控 | 时间粒度粗糙，无法在单次生成中干预 |
| Jacobian 矩阵计算 | ❌ 不可能 | 行为统计分析 (PCA/对比) | 无理论保证，但实践中可用 |
| SAE 稀疏分解 | ❌ 不可能 | 输出意图分解 (规则/LLM) | 分解精度远低于 SAE，但可提供可解释性 |

### 6.2 性能开销评估

| 模块 | 预期开销 | 评估依据 |
|---|---|---|
| `BehavioralSignalStream.emit()` | <0.1ms/次 | deque.append + Event.set，无IO |
| `BehavioralSignalStream.aggregate()` | <1ms/次 | 遍历最近100条，纯CPU |
| `DirectionVector.apply_to_context()` | <0.01ms/次 | dict操作，纯CPU |
| `InterventionLoop.evaluate()` | <5ms/次 | 遍历规则 + aggregate + 阈值比较 |
| `IntentDecomposer._rule_encode()` | <1ms/次 | 关键词匹配，纯CPU |
| `IntentDecomposer._llm_encode()` | 500-2000ms/次 | 额外LLM调用，Phase 2 |
| `EnhancedBeliefRouter.select_agent()` | <1ms/次 | Thompson Sampling + 简单加权 |
| `StructuredBlackboard.put_structured()` | <0.2ms/次 | dict操作 + 索引更新 |

**总体评估**: Phase 1 的所有操作均在亚毫秒级，不会成为性能瓶颈。Phase 2 的 LLM 意图分解是唯一可能的高开销操作，需要做异步+缓存。

### 6.3 方向向量在远程 API 模型上的局限

1. **方向精度受限**：J-Space 的方向向量是在模型激活空间中定义的，具有明确的几何意义。Agent 层面的方向向量是在 Prompt/Context 空间中定义的，是启发式的，无理论保证。

2. **干预可控性受限**：J-Space 的干预是确定性的（`h += αd`），而 Prompt 层面的干预是概率性的——同样的 Prompt 修改可能导致模型输出的不同变化。

3. **跨模型迁移性受限**：J-Space 的方向向量在同一模型架构内可迁移，但 Agent 层面的方向向量高度依赖特定模型的 Prompt 模式，跨模型迁移需要重新校准。

4. **评估困难**：J-Space 可用 logit 变化量化干预效果，Agent 层面只能通过下游任务表现间接评估。

### 6.4 缓解措施

1. **方向向量的经验校准**：Phase 1 使用手工定义的方向，Phase 2/3 通过 A/B 测试和统计分析自动校准方向权重。

2. **渐进式部署**：每个优化方向独立可开关，默认全部关闭，逐个启用并验证效果。

3. **回退机制**：所有增强模块均 fallback 到现有行为（如 `EnhancedBeliefRouter` fallback 到 `BeliefRouter` 的纯 Thompson Sampling）。

4. **监控先行**：Phase 1 仅采集信号，不做干预。先积累数据，再基于数据设计干预规则。

---

## 附录 A: 五个项目架构速览

### A.1 jacobian-lens

**核心模块**:
- `jlens/lens.py: JacobianLens` — 持有 J_l 矩阵，提供 `transport()`(传输) 和 `apply()`(应用) 方法
- `jlens/fitting.py: fit()` — 通过 Jacobian 估计拟合 J_l，支持检查点恢复
- `jlens/hooks.py: ActivationRecorder` — 上下文管理器，注册 forward hook 采集激活
- `jlens/protocol.py: LensModel` — Protocol 接口，定义模型适配契约
- `jlens/hf.py: HFLensModel` — HuggingFace 适配器，自动检测模型布局

**关键算法**: 对每个 prompt，复制 dim_batch 份，前向传播后对每个输出维度做 one-hot cotangent 反向传播，累积 J_l 的行向量，最终取均值。

### A.2 representation-engineering

**核心模块**:
- `repe/rep_readers.py` — `PCARepReader`(PCA方向), `ClusterMeanRepReader`(聚类均值方向), `RandomRepReader`(随机基线)
- `repe/rep_control_reading_vec.py: WrappedBlock` — 支持 `linear_comb`/`piecewise_linear` 算子的干预模块
- `repe/rep_control_reading_vec.py: WrappedReadingVecModel` — wrap/unwrap 模型层的管理器
- `repe/rep_control_pipeline.py: RepControlPipeline` — 组合 Reader + Controller 的 Pipeline
- `repe/rep_control_contrast_vec.py` — 在线对比向量生成 (正负 pair 实时计算并注入)

**关键算法**: 收集正/负样本的隐藏状态，取差值后 PCA 提取主方向，再沿方向加减干预。

### A.3 SAELens

**核心模块**:
- `sae_lens/saes/sae.py: SAE` — 稀疏自编码器基类，定义 `encode()`/`decode()` 接口
- `sae_lens/saes/standard_sae.py: StandardSAE` — 标准 SAE 实现
- `sae_lens/training/activations_store.py: ActivationsStore` — 流式激活采集+缓存
- `sae_lens/training/sae_trainer.py: SAETrainer` — 训练循环
- `sae_lens/config.py` — 丰富的配置 dataclass

**关键算法**: `x_centered = x - b_dec; feature_acts = ReLU(W_enc @ x_centered + b_enc); x_recon = feature_acts @ W_dec + b_dec`

### A.4 reprobe

**核心模块**:
- `src/reprobe/probe.py: Probe` — 线性探针 (nn.Linear + 归一化)
- `src/reprobe/probe.py: ProbesTrainer` — 探针训练器
- `src/reprobe/monitor.py: Monitor` — 实时监控 (hook + probe + 聚合)
- `src/reprobe/steerer.py: Steerer` — 行为干预 (projected/uniform)
- `src/reprobe/interceptor.py: Interceptor` — 激活采集 (prefill/token 双模式)
- `src/reprobe/store.py: ActivationStore` — HDF5 持久化存储
- `src/reprobe/hook.py: Hook` — 基类，自动检测模型层路径
- `src/reprobe/loader.py: ProbeLoader` — 探针加载 (registry.json/.pt/HF)

**关键设计**: Probe + Monitor + Steerer 三件套，通过 `Hook` 基类统一 hook 管理，`ProbeLoader` 提供声明式 API (`ProbeLoader.monitor(model, path)`)。

### A.5 ACT

**核心模块**:
- `collect_activations.py` — 收集 TruthfulQA 数据集上的逐层激活
- `generate_directions_q_wise.py` — 按问题类型分别计算方向向量
- `utils.py` — 探针训练 (LogisticRegression + K-means 聚类)、干预执行、评估

**关键算法**: 
1. `get_llama_activations_bau()` — 使用 baukit.TraceDict 收集逐头激活
2. `generate_directions_q_wise.py` — `mean(正例) - mean(负例)` per question
3. `train_probes_cluster()` — 先 K-means 聚类，再在聚类内训练 LogisticRegression 探针

---

## 附录 B: nahida-agent 现有模块对接清单

| 现有模块 | 优化方向 | 对接方式 |
|---|---|---|
| `agent_core/shared_blackboard.py` | 结构化黑板 | 继承 `SharedBlackboard`，扩展标签/方向索引 |
| `core/agent_introspection.py` | 行为信号流 | `get_current_state()` 中 emit 信号到 SignalStream |
| `belief_router.py` | 增强型路由 | 封装 `BeliefRouter`，叠加方向偏置和信号调整 |
| `agent_dispatcher.py` | 干预闭环 | `SubAgent.chat()` 前后应用 InterventionLoop |
| `emotion/emotion_state.py` | 行为方向向量 | 情绪偏移用 DirectionVector 表达 |
| `memory/cognitive_memory.py` | 结构化黑板 | 用 StructuredBlackboard 替代部分存储 |
| `core/degradation_strategy.py` | 干预闭环 | 降级触发条件用 BehavioralSignalStream 替代异常计数 |
| `core/behavioral_health.py` | 行为信号流 | 健康度评分自动 emit 到 SignalStream |
