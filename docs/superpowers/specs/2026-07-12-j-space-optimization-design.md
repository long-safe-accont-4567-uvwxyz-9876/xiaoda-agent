# J-Space 架构优化设计文档

> 日期: 2026-07-12
> 状态: 设计已批准，待实现
> 基于: [J-Space 生态圈源码分析 & nahida-agent 架构优化 Spec](https://www.coze.cn/s/w_rg0SadvDE/) v1.0
> 分支: `main`（将创建 `feature/j-space-optimization` 分支实现）

---

## 1. 背景与动机

### 1.1 J-Space 核心发现

J-Space（Jacobian Space）是 Anthropic 于 2026-07-06 在 *Verbalizable Representations Form a Global Workspace in Language Models* 论文中提出的概念。核心发现：Transformer 每一层的残差流可通过平均输入-输出 Jacobian 矩阵 `J_l = E[∂h_final/∂h_l]` 线性映射到最终层空间，形成"全局工作空间"。

### 1.2 J-Space 生态圈四库

| 库 | 仓库 | 论文 | 依赖模型内部？ | API-only 可用？ |
|---|---|---|---|---|
| jlens | [anthropics/jacobian-lens](https://github.com/anthropics/jacobian-lens) | [Transformer Circuits 2026](https://transformer-circuits.pub/2026/workspace/index.html) | 是（需反向传播） | 否 |
| repe | [andyzoujm/representation-engineering](https://github.com/andyzoujm/representation-engineering) | [arXiv:2310.01405](https://arxiv.org/abs/2310.01405) | 是（需 forward hooks） | 否 |
| reprobe | [levashi/reprobe](https://github.com/levashi/reprobe) | [LessWrong](https://www.lesswrong.com/posts/3pJiK2audjfAzB6SQ/reprobe-a-practical-library-for-scaled-representation) | 是（需 PyTorch hooks） | 否 |
| SAELens | [jbloomAus/SAELens](https://github.com/jbloomAus/SAELens) | [GitHub 2024](https://jbloomaus.github.io/SAELens/) | 是（需中间层激活） | 否 |

**关键结论**：四库技术核心都依赖模型内部激活访问，**无法直接用于 API-only Agent**。但 J-Space 生态圈揭示的**设计模式和接口抽象**在 Agent 层面有精确对应物。

### 1.3 从 J-Space 到 Agent 架构优化的映射

| J-Space 原始能力 | Agent 层面对应 | nahida-agent 当前模块 | 当前评分 |
|---|---|---|---|
| 激活采集（Interceptor） | 输出/行为信号采集 | `agent_introspection.py`（仅静态快照） | ★☆☆☆☆ |
| 方向识别（RepReader/Probe） | 行为模式/意图方向识别 | `belief_router.py`（仅 Thompson Sampling） | ★☆☆☆☆ |
| 行为干预（Steerer/ContrastVec） | Prompt/Context 动态干预 | `prompt_builder.py`（隐式，无方向控制） | ★☆☆☆☆ |
| 状态监控（Monitor） | Agent 状态实时监控 | `agent_introspection.py`（轮询式） | ★☆☆☆☆ |
| 特征分解（SAE） | 输出语义分解/意图因子化 | 无对应 | ★☆☆☆☆ |
| 层间传输（JacobianLens） | 多级表征对齐/传递 | `shared_blackboard.py`（KV，无语义对齐） | ★★☆☆☆ |

**核心动机**：将 J-Space 的**结构化内省-干预闭环**移植到 Agent 层面，使 nahida-agent 获得「理解自身行为模式 → 识别偏差方向 → 主动干预纠偏」的能力。

---

## 2. 设计决策

### 2.1 实现范围

**全部 6 个优化方向完整实现**，分 4 个阶段递进完成，每阶段用 subagent-driven-development 多子代理工业流程。

### 2.2 集成方式

**并行存在 + Hook 接入**：6 个新模块独立存在，不修改现有模块核心逻辑，仅在 8 个关键点插入轻量 Hook 调用（1-3 行代码，try/except 包裹，配置开关启用/禁用）。

### 2.3 持久化策略

- **方向向量库**（DirectionRegistry）：持久化到 `data/direction_registry.json`
- **信号历史**（BehavioralSignalStream）：仅在内存 deque 中（maxlen=1000），重启丢失
- **干预历史**（InterventionLoop）：仅在内存列表中，重启丢失

---

## 3. 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│  Stage 4: 基础设施 + 集成层                                   │
│  ┌─────────────────────┐  ┌──────────────────────────────┐  │
│  │ StructuredBlackboard │  │ 8 Hook 接入点                │  │
│  │ (§3.6, 继承          │  │ agent_introspection → emit   │  │
│  │  SharedBlackboard)   │  │ belief_router → EnhancedRtr  │  │
│  └─────────────────────┘  │ agent_dispatcher → IntervLoop │  │
│                            │ emotion_state → DirectionVec │  │
│                            │ degradation_strategy → Signal│  │
│                            │ behavioral_health → emit     │  │
│                            │ cognitive_memory → StructBrd │  │
│                            │ prompt_builder → apply_dir   │  │
│                            └──────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  Stage 3: 智能层                                             │
│  ┌─────────────────────┐  ┌──────────────────────────────┐  │
│  │ IntentDecomposer     │  │ EnhancedBeliefRouter         │  │
│  │ (§3.4, 对齐 SAE)     │  │ (§3.5, Thompson+Dir+Signal)  │  │
│  │ encode/decode/spars  │  │ score = thompson + α·dir     │  │
│  └─────────────────────┘  └──────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  Stage 2: 闭环层                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ InterventionLoop (§3.3)                               │   │
│  │ evaluate() → threshold → DirectionVector*alpha       │   │
│  │ apply_intervention() → context → 验证收敛             │   │
│  └──────────────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────────────┤
│  Stage 1: 基础层                                              │
│  ┌─────────────────────┐  ┌──────────────────────────────┐  │
│  │ BehavioralSignalStr │  │ DirectionVector/Registry     │  │
│  │ (§3.1)              │  │ (§3.2)                       │  │
│  │ emit/subscribe/     │  │ dimensions: prompt/tool/     │  │
│  │ aggregate/history   │  │ emotion/route                │  │
│  │ deque(1000) + Event │  │ save/load JSON               │  │
│  └─────────────────────┘  └──────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

**设计原则**：
- 并行存在：6 个新模块独立存在，不修改现有模块核心逻辑
- Hook 接入：8 个现有模块的关键点插入轻量 hook 调用
- 非阻塞：所有新模块失败都是非阻塞的，J-Space 优化层是"锦上添花"
- 配置开关：通过 `ENABLE_J_SPACE_HOOKS` 环境变量全局启用/禁用

---

## 4. Stage 1: 基础层

### 4.1 `core/behavioral_signal.py`（§3.1，对齐 reprobe Interceptor+Monitor）

**核心类**：`SignalEntry`（dataclass）+ `BehavioralSignalStream`

**SignalEntry**：
```python
@dataclass
class SignalEntry:
    signal_type: str          # "confidence", "sentiment", "tool_usage", "token_latency"...
    value: float              # 归一化值 0-1
    source: str               # 来源模块名
    timestamp: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)
```

**BehavioralSignalStream**：

| 方法 | 签名 | 对齐参考 | 说明 |
|---|---|---|---|
| `emit` | `async emit(signal_type, value, source="", **meta)` | reprobe Interceptor._flush | 发射信号到 deque + 通知订阅者 |
| `subscribe` | `async subscribe(signal_type) -> asyncio.Event` | shared_blackboard.subscribe | 订阅特定信号类型 |
| `get_history` | `get_history(signal_type="", last_n=100) -> list[SignalEntry]` | reprobe Monitor.get_history | 获取历史信号 |
| `aggregate` | `aggregate(signal_type, strategy="mean_of_means") -> float` | reprobe Monitor.score | 3 种策略：max_of_means/mean_of_means/max_absolute |

**内部实现**：
- `_buffer: deque[SignalEntry]` maxlen=1000（内存，重启丢失）
- `_subscribers: dict[str, list[asyncio.Event]]`（订阅通知）

**预定义信号类型**：
- `confidence` — Agent 回复置信度
- `sentiment` — 情感极性
- `tool_usage` — 工具使用率
- `token_latency` — Token 延迟
- `cognitive_load` — 认知负载
- `agent_{name}_success` — 各子 Agent 成功率
- `health` — 行为健康度

### 4.2 `core/behavioral_direction.py`（§3.2，对齐 RepE+reprobe Steerer）

**核心类**：`DirectionVector`（dataclass）+ `DirectionRegistry`

**DirectionVector**：
- 字段：`name`、`dimensions: dict[str, float]`（prompt/tool/emotion/route）、`source`、`magnitude`、`meta`
- 算子：
  - `__mul__(scalar)` — 缩放方向，对齐 Steerer alpha 参数
  - `__add__(other)` — 方向叠加，对齐 WrappedBlock linear_comb 算子
- `apply_to_context(context: dict) -> dict`：将方向应用到上下文
  - `prompt` 维度 → `context["prompt_modifier"] += weight`
  - `tool` 维度 → `context["tool_bias"] += weight`
  - `emotion` 维度 → `context["emotion_offset"] += weight`
  - `route` 维度 → `context["route_bias"] += weight`
- `save(path)` / `load(path)`：JSON 持久化

**DirectionRegistry**：
- `_directions: dict[str, DirectionVector]`
- `storage_path = "data/direction_registry.json"`（持久化）
- `register(direction)` / `get(name)` / `list_directions()`
- 启动时自动加载 JSON；register 时自动保存

**预注册方向**（启动时初始化）：
```python
DirectionVector("helpfulness", {"prompt": 0.3, "route": 0.2}, "manual")
DirectionVector("safety", {"prompt": 0.5, "tool": -0.3}, "manual")
DirectionVector("calm", {"emotion": -0.4, "prompt": 0.2}, "manual")
DirectionVector("focused", {"prompt": 0.4, "route": 0.3}, "manual")
```

### 4.3 Stage 1 测试策略

- `tests/test_behavioral_signal.py`：emit/subscribe 通知、3 种 aggregate 策略、deque maxlen、get_history
- `tests/test_behavioral_direction.py`：`__mul__`/`__add__` 算子、apply_to_context 各维度、save/load 往返、DirectionRegistry 注册/获取/列表/持久化

---

## 5. Stage 2: 闭环层

### 5.1 `core/intervention_loop.py`（§3.3，对齐 reprobe Monitor+Steerer）

**核心类**：`InterventionRule`（dataclass）+ `InterventionLoop`

**InterventionRule**：
```python
@dataclass
class InterventionRule:
    signal_type: str           # 监听的信号类型
    threshold: float           # 触发阈值
    direction_name: str        # 触发时应用的方向向量名
    alpha: float = 0.5         # 干预强度（对齐 Steerer alpha）
    mode: str = "projected"    # "projected" | "uniform"（对齐 Steerer）
    cooldown: float = 30.0     # 冷却时间（秒），避免频繁触发
    last_triggered: float = 0.0
```

**InterventionLoop**：

| 方法 | 说明 | 对齐参考 |
|---|---|---|
| `register_rule(rule)` | 注册干预规则 | reprobe Monitor 配置 |
| `evaluate(context: dict) -> list[dict]` | 聚合信号 → 阈值判断 → 返回触发的干预列表 | reprobe Monitor.score + Steerer |
| `apply_intervention(context, intervention) -> dict` | 应用方向向量到上下文 | Steerer._apply_projection |
| `get_convergence_metrics() -> dict` | 收敛指标（最近 5 次 score 趋势） | jlens fit mean_rel_change |

**数据流**：
```
BehavioralSignalStream.aggregate(signal_type)
    │
    ▼
InterventionLoop.evaluate(context)
    │ score > threshold && cooldown 已过?
    ▼
DirectionRegistry.get(direction_name) → DirectionVector * alpha
    │
    ▼
direction.apply_to_context(context) → 修改后的 context
    │
    ▼
下一轮 evaluate() 验证 score 是否下降 → convergence_metrics
```

**典型规则示例**：
```python
InterventionRule("cognitive_load", threshold=0.8, direction_name="calm",
                 alpha=0.4, mode="projected", cooldown=30.0)
InterventionRule("agent_xiaolang_success", threshold=0.3, direction_name="focused",
                 alpha=0.5, mode="uniform", cooldown=60.0)
```

### 5.2 Stage 2 测试策略

- `tests/test_intervention_loop.py`：
  - 规则注册/触发/冷却
  - projected vs uniform 模式
  - convergence_metrics 趋势计算
- 集成测试：SignalStream emit → InterventionLoop evaluate → DirectionVector apply 全链路

---

## 6. Stage 3: 智能层

### 6.1 `core/intent_decomposition.py`（§3.4，对齐 SAELens SAE）

**核心类**：`IntentFactor` + `DecomposedOutput` + `IntentDecomposer`

**IntentFactor**：
- `name: str` — 意图名
- `activation: float` — 激活强度 0-1（对齐 SAE feature_acts）
- `evidence: str` — 支持该意图的输出片段
- `confidence: float` — 分解置信度

**DecomposedOutput**：
- `raw_output: str` — 原始输出
- `factors: list[IntentFactor]` — 意图因子列表
- `residual: float` — 不可解释的残差（对齐 SAE error term）
- `dominant_intent: IntentFactor | None` — 激活最高的因子（对齐 SAE top-k）
- `sparsity: float` — 稀疏度 1 - active_count/total（对齐 SAE l0 度量）

**IntentDecomposer**：
- 7 个预定义意图维度：`knowledge/emotional/safety/creative/factual/social/procedural`
- `encode(output, context) -> DecomposedOutput`：对齐 SAE.encode()
- **Phase 1（本次实现）**：`_rule_encode` — 关键词匹配 + 启发式权重
- **Phase 2（后续迭代）**：`_llm_encode` — LLM 结构化分析（留接口，不实现）

### 6.2 `core/enhanced_router.py`（§3.5，对齐 ACT+RepE）

**核心类**：`EnhancedBeliefRouter`

**路由公式**（对齐 ACT q-wise direction）：
```
score(agent) = thompson_sample(agent) 
             + direction_weight * direction_bias(task_type, agent)
             + signal_weight * signal_adjustment(agent)
```

**构造参数**：
- `base_router`：现有 BeliefRouter 实例（包装，不修改）
- `direction_registry: DirectionRegistry`（Stage 1）
- `signal_stream: BehavioralSignalStream`（Stage 1）
- `direction_weight=0.3`、`signal_weight=0.2`

**方法**：
- `select_agent(task_type, exclude, direction_hint) -> str`：综合评分选择
- `update_belief(agent_name, success)`：委托给 base_router

**Agent 任务映射**（对齐 ACT q-wise）：
```python
{"xiaolang": "security", "xiaoke": "debug", 
 "xiaolian": "info_search", "xiaoda": "general"}
```

### 6.3 Stage 3 测试策略

- `tests/test_intent_decomposition.py`：规则分解、sparsity、dominant_intent、residual
- `tests/test_enhanced_router.py`：三因素评分、direction_hint 影响、signal 调整

---

## 7. Stage 4: 基础设施 + 集成层

### 7.1 `agent_core/structured_blackboard.py`（§3.6，对齐 SAELens+reprobe Store）

**核心类**：`StructuredEntry` + `StructuredBlackboard(SharedBlackboard)`

**StructuredEntry**（扩展原 `_Entry`）：
- `value`、`agent_name`、`expire_at`（原字段）
- `tags: list[str]`（对齐 SAE feature label）
- `direction: str`（对齐 Steerer 干预方向）
- `quality: float`（对齐 Probe AUC）
- `schema_version: str`

**StructuredBlackboard** 继承 `SharedBlackboard`：
- `put_structured(key, value, agent_name, ttl, tags, direction, quality)`：写入 + 更新索引
- `query_by_tag(tag) -> list[dict]`：按标签查询（对齐 SAE feature lookup）
- `query_by_direction(direction_name) -> list[dict]`：按方向查询（对齐 Steerer 方向关联）
- `merge_from(other) -> int`：合并黑板（对齐 JacobianLens.merge 加权合并）
- 内部索引：`_tag_index: dict[str, set[str]]`、`_direction_index: dict[str, set[str]]`

### 7.2 8 个 Hook 接入点

| # | 现有模块 | Hook 位置 | 调用 | 影响 |
|---|---|---|---|---|
| 1 | `agent_introspection.py` | `get_current_state()` 末尾 | `signal_stream.emit("cognitive_load", load, "introspection")` | 采集信号 |
| 2 | `belief_router.py` | `select_agent()` 入口 | 用 `EnhancedBeliefRouter` 包装（可选启用） | 增强路由 |
| 3 | `agent_dispatcher.py` | `SubAgent.chat()` 前后 | `intervention_loop.evaluate(context)` 前 / `signal_stream.emit("agent_X_success", score)` 后 | 闭环干预 |
| 4 | `emotion/emotion_state.py` | 状态更新时 | `direction.apply_to_context({"emotion_offset": val})` | 方向控制情绪 |
| 5 | `degradation_strategy.py` | 触发降级判断时 | `intervention_loop.evaluate()` 替代纯异常计数 | 信号驱动降级 |
| 6 | `behavioral_health.py` | 健康度评分时 | `signal_stream.emit("health", score, "behavioral_health")` | 采集信号 |
| 7 | `cognitive_memory.py` | 存储记忆时 | `structured_blackboard.put_structured(..., tags=["memory"], direction=...)` | 结构化存储 |
| 8 | `prompt_builder.py` | 构建 prompt 时 | `direction.apply_to_context(context)` → 注入 prompt_modifier | 方向干预 prompt |

**Hook 接入原则**：
- 每个接入点仅 1-3 行代码（emit/evaluate/apply 调用）
- 使用 try/except 包裹，Hook 失败不影响主流程
- 通过配置开关启用/禁用（`config.ENABLE_J_SPACE_HOOKS`）

### 7.3 Stage 4 测试策略

- `tests/test_structured_blackboard.py`：put/query_by_tag/query_by_direction/merge/TTL 过期
- `tests/test_hook_integration.py`：8 个 Hook 接入点的集成测试 + 配置开关 + try/except 安全性
- `tests/test_e2e_closed_loop.py`：signal emit → intervention triggered → direction applied → score 下降验证

---

## 8. 测试策略

### 8.1 测试分层

| 层级 | 范围 | 工具 | 目标 |
|---|---|---|---|
| **单元测试** | 每个新模块的每个方法 | pytest | 覆盖率 ≥ 90% |
| **集成测试** | 模块间协作（Signal→Intervention→Direction） | pytest + asyncio | 验证数据流 |
| **Hook 测试** | 8 个接入点不破坏现有功能 | pytest + mock | 回归安全 |
| **端到端测试** | 完整闭环：emit→evaluate→apply→verify | pytest | 验证收敛 |

### 8.2 测试文件清单

| 文件 | 覆盖内容 |
|---|---|
| `tests/test_behavioral_signal.py` | emit→subscribe 通知、3 种 aggregate 策略、deque maxlen、get_history |
| `tests/test_behavioral_direction.py` | `__mul__`/`__add__` 算子、apply_to_context 各维度、save/load 往返、DirectionRegistry |
| `tests/test_intervention_loop.py` | 阈值触发/未触发、cooldown 抑制、projected vs uniform、convergence 趋势 |
| `tests/test_intent_decomposition.py` | 7 意图识别、sparsity 计算、residual、dominant_intent |
| `tests/test_enhanced_router.py` | 三因素评分、direction_hint 影响、signal 调整 |
| `tests/test_structured_blackboard.py` | tag/direction 索引、merge 合并、TTL 过期 |
| `tests/test_hook_integration.py` | 8 个接入点 + 配置开关 + try/except 安全性 |
| `tests/test_e2e_closed_loop.py` | signal emit → intervention triggered → direction applied → score 下降 |

---

## 9. 错误处理

### 9.1 错误处理策略

| 场景 | 处理方式 |
|---|---|
| `signal_stream.emit()` 失败 | try/except 吞掉，记 warning 日志，不影响主流程 |
| `DirectionRegistry.get(name)` 返回 None | InterventionLoop 跳过该规则，记 debug 日志 |
| `intervention_loop.evaluate()` 异常 | 返回空列表，不阻塞主流程 |
| `StructuredBlackboard` 索引损坏 | 重建索引，记 warning |
| `direction_registry.json` 损坏 | 加载失败时用预注册方向重置，记 error |
| Hook 代码异常 | try/except 包裹，失败不影响宿主模块 |

**核心原则**：所有新模块的失败都是**非阻塞的**——J-Space 优化层是"锦上添花"，不能让优化层故障导致 Agent 不可用。

### 9.2 配置开关

```python
# config.py 新增
ENABLE_J_SPACE_HOOKS = os.getenv("ENABLE_J_SPACE_HOOKS", "true").lower() == "true"
DIRECTION_REGISTRY_PATH = os.getenv("DIRECTION_REGISTRY_PATH", "data/direction_registry.json")
SIGNAL_STREAM_MAX_HISTORY = int(os.getenv("SIGNAL_STREAM_MAX_HISTORY", "1000"))
INTERVENTION_DEFAULT_COOLDOWN = float(os.getenv("INTERVENTION_DEFAULT_COOLDOWN", "30.0"))
```

---

## 10. 实现阶段规划

### Stage 1: 基础层（§3.1 + §3.2）
- **模块**：`core/behavioral_signal.py` + `core/behavioral_direction.py`
- **子代理**：2 个并行（信号流 / 方向向量）
- **测试**：`test_behavioral_signal.py` + `test_behavioral_direction.py`
- **依赖**：无

### Stage 2: 闭环层（§3.3）
- **模块**：`core/intervention_loop.py`
- **子代理**：1 个（连接信号+方向）
- **测试**：`test_intervention_loop.py`
- **依赖**：Stage 1

### Stage 3: 智能层（§3.4 + §3.5）
- **模块**：`core/intent_decomposition.py` + `core/enhanced_router.py`
- **子代理**：2 个并行（意图分解 / 增强路由）
- **测试**：`test_intent_decomposition.py` + `test_enhanced_router.py`
- **依赖**：Stage 1

### Stage 4: 基础设施 + 集成（§3.6 + 8 Hook）
- **模块**：`agent_core/structured_blackboard.py` + 8 个 Hook 接入
- **子代理**：2 个（结构化黑板 / 集成 hook）
- **测试**：`test_structured_blackboard.py` + `test_hook_integration.py` + `test_e2e_closed_loop.py`
- **依赖**：Stage 1-3

每阶段使用 subagent-driven-development 技能，子代理按依赖关系并行/串行调度。每阶段完成后使用 verification-before-completion 验证，全部完成后使用 requesting-code-review 和 receiving-code-review。

---

## 11. 不实现的内容（YAGNI）

- **Phase 2 LLM 意图分解**：`IntentDecomposer._llm_encode` 留接口不实现，Phase 1 规则分解已足够
- **信号历史持久化**：信号仅内存 deque，不持久化到 DB
- **干预历史持久化**：干预历史仅内存列表，不持久化
- **SAE 真实训练**：不训练真正的稀疏自编码器，仅借鉴 encode/decode 范式
- **Jacobian 真实计算**：不计算真实 Jacobian 矩阵，仅借鉴 transport/merge 范式
- **跨 Agent 方向迁移**：DirectionRegistry 不支持跨 Agent 实例迁移（后续迭代）

---

## 附录 A: J-Space 生态圈原仓库关键模块

### A.1 jlens (JacobianLens)
- `jlens/lens.py: JacobianLens.transport()` — 线性运输
- `jlens/lens.py: JacobianLens.merge()` — n_prompts 加权平均合并
- `jlens/lens.py: JacobianLens.from_pretrained()` — Hub 加载
- `jlens/hooks.py: ActivationRecorder` — 上下文管理器激活采集

### A.2 repe (Representation Engineering)
- `repe/rep_readers.py: PCARepReader` — PCA 方向识别
- `repe/rep_control_reading_vec.py: WrappedBlock.set_controller()` — 方向注入
- `repe/rep_control_reading_vec.py: WrappedBlock` — linear_comb/piecewise_linear 算子
- `repe/rep_control_contrast_vec.py` — 实时对比向量注入

### A.3 SAELens
- `SAELens/sae_lens/saes/sae.py: SAE.encode()/decode()` — 稀疏编码/解码
- `SAELens/sae_lens/training/activations_store.py: ActivationsStore` — 流式激活采集

### A.4 reprobe
- `src/reprobe/probe.py: Probe` — 线性探针
- `src/reprobe/monitor.py: Monitor` — 实时监控（3 种聚合策略）
- `src/reprobe/steerer.py: Steerer` — 行为干预（projected/uniform）
- `src/reprobe/interceptor.py: Interceptor` — 激活采集（prefill/token 双模式）
- `src/reprobe/loader.py: ProbeLoader` — 探针加载（registry.json/.pt/HF）

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
