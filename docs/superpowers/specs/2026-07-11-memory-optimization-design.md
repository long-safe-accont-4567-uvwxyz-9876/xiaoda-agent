# xiaoda-agent 记忆系统优化设计文档

> 基于 Coze Spec (NToH5sdy1xs) 和 NirDiamant/Agent_Memory_Techniques 30种记忆技术
> 版本：v1.0 | 目标版本：xiaoda-agent v0.6.0 | 编写日期：2026-07-11

---

## 1. 项目背景

### 1.1 当前覆盖度

xiaoda-agent 当前记忆系统覆盖 AMT 30种技术中的约 47%（14/30 完整或部分覆盖）。本次优化聚焦 P0+P1 共 6 个核心技术，目标将覆盖度提升至 ~75%。

### 1.2 决策摘要

| 决策项 | 选择 |
|--------|------|
| 实现范围 | P0+P1 全部 6 个技术 |
| 实现策略 | 增量式集成（扩展现有模块为主，新建模块仅在必要时） |
| 测试策略 | TDD 全覆盖（先写失败测试再实现） |
| 实现顺序 | 严格 P0→P1：#13→#12→#17 → #21→#18→#16 |

### 1.3 约束

- 不破坏现有线上稳定性（QQ Bot + Web UI 双进程）
- 保持与现有代码风格一致（Python 3.11 + asyncio + aiosqlite）
- 不引入 Neo4j 等重依赖，仅用 sqlite-vec
- 所有时间相关函数使用 `ZoneInfo("Asia/Shanghai")`

---

## 2. 架构设计

### 2.1 现状架构（扁平）

```
MemoryManager (memory_manager.py)
├── VectorStore (vector_store.py)        ← 语义检索
├── FluidMemory (fluid_memory.py)        ← 遗忘曲线
├── MemoryDistiller (memory_distiller.py) ← 蒸馏/摘要
├── KnowledgeGraph (knowledge_graph.py)   ← 实体关系
├── EmotionalMemory (emotional_memory.py) ← 情感标记
├── ContextGovernance (context_governance.py) ← 版本链/审计
├── ContextCompressor (context_compressor.py) ← 压缩
├── EpisodicLimiter (episodic_limiter.py) ← LRU+重要性淘汰
└── Reranker (reranker.py)               ← 重排序
```

所有模块并列，查询时全量搜索或手动指定，无分层、无路由。

### 2.2 目标架构（分层+路由）

```
AgentCore.process()
    ↓
MemoryRouter (新建) ──→ 分类查询 → 路由到对应记忆类型
    ├── EMOTIONAL → EmotionalMemory
    ├── EPISODIC  → EpisodicStore (扩展 fluid_memory)
    ├── SEMANTIC  → KnowledgeGraph
    ├── PROCEDURAL → ProceduralStore (新建，轻量)
    └── TEMPORAL  → TemporalMemoryStore (新建)
    ↓
HierarchicalMemoryManager (新建)
    ├── L1 HOT   ← AgentContext 扩展（工作记忆管理）
    ├── L2 WARM  ← sqlite-vec + 内存缓存
    └── L3 COLD  ← sqlite-vec 冷分区
    ↓
CrossSessionManager (新建) ──→ 会话恢复/加载策略/冷启动
    ↓
ReflectionStore (新建/扩展 meta_cognition) ──→ 反思循环
```

### 2.3 文件变更清单

**新建文件（5个）**：

| 文件 | 职责 |
|------|------|
| `memory/hierarchical_memory.py` | 三层记忆管理器（L1/L2/L3） |
| `memory/memory_router.py` | 记忆路由分发器 |
| `memory/temporal_memory.py` | 时间推理记忆存储 |
| `memory/cross_session_manager.py` | 跨会话桥接管理器 |
| `memory/reflection_store.py` | 反思存储与检索 |

**扩展文件（4个）**：

| 文件 | 扩展内容 |
|------|---------|
| `agent_context.py` | 增加 WorkingMemoryItem、SalienceScorer |
| `memory/episodic_limiter.py` | 增加 EvictionEngine 动态逐出 |
| `memory/memory_manager.py` | 集成路由器和分层管理器 |
| `core/meta_cognition.py` | 增加 ReflectiveAgent 反思循环 |

**DB 迁移**：

```sql
-- episodic_memories 表扩展
ALTER TABLE episodic_memories ADD COLUMN tier TEXT DEFAULT 'l2_warm';
ALTER TABLE episodic_memories ADD COLUMN access_count INTEGER DEFAULT 0;
ALTER TABLE episodic_memories ADD COLUMN importance REAL DEFAULT 0.5;
ALTER TABLE episodic_memories ADD COLUMN is_pinned INTEGER DEFAULT 0;
ALTER TABLE episodic_memories ADD COLUMN event_time REAL;
ALTER TABLE episodic_memories ADD COLUMN is_stable INTEGER DEFAULT 0;

-- reflection_store 新表
CREATE TABLE IF NOT EXISTS reflection_store (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    insight TEXT NOT NULL,
    context_hash TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reflection_type ON reflection_store(task_type);
```

---

## 3. P0 技术详细设计

### 3.1 分层记忆架构 (#13)

**新建文件**：`memory/hierarchical_memory.py`

**核心类**：

```python
class MemoryTier(Enum):
    L1_HOT = "l1_hot"       # 上下文窗口内，内存
    L2_WARM = "l2_warm"     # 近期高频，sqlite-vec + 内存缓存
    L3_COLD = "l3_cold"     # 历史归档，sqlite-vec 冷分区

@dataclass
class TieredMemory:
    id: str
    content: str
    embedding: list[float]
    tier: MemoryTier
    access_count: int
    last_accessed: datetime
    created_at: datetime
    importance: float           # 0-1
    emotional_weight: float     # 情感权重，防止降级
    is_pinned: bool             # 永不降级
    metadata: dict
```

**HierarchicalMemoryManager 核心方法**：

- `store(content, embedding, importance, emotional_weight, metadata)` — 新记忆默认入 L2，高情感权重+高重要性直接入 L1
- `retrieve(query, embedding)` — 级联检索：L1→L2→L3，先命中先返回
- `promote(memory_id)` — 访问次数超阈值时 L3→L2 或 L2→L1 晋升
- `demote()` — 定时扫描，超时未访问降级（跳过 is_pinned 和高 emotional_weight）
- `pin(memory_id)` — 固定记忆到 L1（如用户核心情感状态）
- `get_timeline(start, end)` — 时间范围查询（为 #18 预留接口）

**情感陪伴特殊逻辑**：
- 高 emotional_weight（>0.7）+ 高 importance（>0.7）→ 直接入 L1
- is_pinned 的创伤记忆永不降级到 L3
- 接近特殊日期（生日/周年）时相关记忆自动晋升到 L2

**集成点**：
- 读取 `agent_context.py` 的 `MAX_HISTORY_TOKENS` 作为 L1 容量参考
- 使用现有 `sqlite-vec` 作为 L2/L3 后端，通过 tier 字段区分
- 读取 `emotional_memory.py` 的 emotional_weight 影响晋升/降级
- 通过 `db/db_memory.py` 新增 tier 列到 episodic_memories 表

**配置参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `l1_capacity_tokens` | 4096 | L1 热层 token 容量 |
| `l2_capacity` | 1000 | L2 温层最大条目数 |
| `promote_threshold` | 3 | 访问 N 次后晋升 |
| `demote_staleness_hours` | 48 | N 小时未访问后降级 |

### 3.2 工作记忆上下文窗口管理 (#12)

**扩展文件**：`agent_context.py` + `memory/episodic_limiter.py`

**现状分析**：
- `agent_context.py` 的 `AgentContext` 类管理对话上下文，`MAX_HISTORY_TOKENS = 6000`，有压缩逻辑
- `episodic_limiter.py` 已有 `importance * 0.5 + access_count * 0.3 + recency * 0.2` 的淘汰评分
- `context_governance.py` 实际是版本链/审计（ContextNest），非上下文窗口管理

**扩展 `agent_context.py`**：

```python
@dataclass
class WorkingMemoryItem:
    content: str
    role: str               # user/assistant/system
    token_count: int
    salience_score: float   # 0-1 显著性分数
    is_pinned: bool         # 固定标记
    timestamp: float
    emotional_weight: float # 情感权重

class SalienceScorer:
    """三维融合评分：embedding相似度 x 指数衰减 x 来源权重"""
    def score(self, item, current_query_embedding, current_context):
        relevance = cosine_sim(item.embedding, current_query_embedding)
        recency = math.exp(-alpha * (now - item.timestamp))
        source_weight = SOURCE_WEIGHTS[item.role]
        return relevance * recency * source_weight
```

**扩展 `episodic_limiter.py`**：

```python
class EvictionEngine:
    """两种逐出策略 — 扩展现有 EpisodicLimiter 的评分公式"""
    def evict_lru(self, items): ...           # 原有 LRU
    def evict_importance_weighted(self, items):
        # 扩展公式：salience * 0.3 + emotional_weight * 0.4 + recency * 0.3
        # 跳过 is_pinned=True 的项目
        # 注意：原有 EpisodicLimiter 公式 (importance*0.5 + access_count*0.3 + recency*0.2)
        # 保留为 fallback，EvictionEngine 优先使用新公式
```

**SalienceScorer 的 embedding 来源**：复用现有 `VectorStore` 的 embedding 函数（`memory/vector_store.py` 中已有 embedding 生成能力），不引入新的 embedding 模型。

**情感陪伴特殊逻辑**：
- 用户核心情感状态（如"用户最近抑郁"）→ `is_pinned=True`
- 关系里程碑（首次深聊、创伤倾诉）→ `is_pinned=True`
- 逐出时检查情绪连贯性——不逐出当前情绪对话的关键语境

**集成点**：
- `AgentContext.build_messages()` 调用 `SalienceScorer` 对历史消息评分
- 超出 `MAX_HISTORY_TOKENS` 时用 `EvictionEngine` 逐出最低分项
- 被逐出项归档到 `HierarchicalMemoryManager` 的 L2 层

### 3.3 记忆路由 (#17)

**新建文件**：`memory/memory_router.py`

```python
class MemoryType(Enum):
    EPISODIC   = "episodic"     # 事件回忆
    SEMANTIC   = "semantic"     # 事实知识
    PROCEDURAL = "procedural"   # 过程性知识
    EMOTIONAL  = "emotional"    # 情感状态
    TEMPORAL   = "temporal"     # 时间推理

class MemoryRouter:
    """记忆路由分发器 — 按内容类型路由到对应记忆后端"""

    def __init__(self, stores: dict[MemoryType, Any],
                 fallback_store: Any = None):
        self._stores = stores
        self._fallback = fallback_store

    async def route(self, query: str,
                    embedding: list[float] | None = None) -> list:
        """路由查询到对应记忆存储"""
        mem_type = await self._classify(query)
        store = self._stores.get(mem_type)
        if store:
            results = await store.retrieve(query, embedding)
            if results:
                return results
        return await self._fallback_search(query, embedding)

    async def _classify(self, query: str) -> MemoryType:
        """轻量分类器：规则优先，LLM fallback"""
        if self._has_temporal_keywords(query):
            return MemoryType.TEMPORAL
        if self._has_emotional_keywords(query):
            return MemoryType.EMOTIONAL
        return await self._llm_classify(query)
```

**路由分类规则**：

| 输入示例 | 路由目标 | 说明 |
|---------|---------|------|
| "用户提到妈妈住院了" | EMOTIONAL + EPISODIC | 情感事件双写 |
| "用户的猫叫小橘" | SEMANTIC | 事实知识 |
| "上次你说的那件事后来怎样了" | TEMPORAL + EPISODIC | 时间+事件 |
| "用户现在心情怎样" | EMOTIONAL | 情感状态查询 |
| "安慰用户焦虑时应该先倾听" | PROCEDURAL | 过程性知识 |

**分类策略**（分层，避免每次调 LLM）：
1. **规则层**（毫秒级）：时间词检测、情绪词检测、实体词检测
2. **缓存层**：相似查询的分类结果缓存（TTL 300s）
3. **LLM 层**（仅在规则未命中时）：轻量分类 prompt

**集成点**：
- `MemoryManager.retrieve()` 改为先调 `MemoryRouter.route()`
- 路由结果合并后进入 `Reranker` 重排序
- 情感事件触发双写：同时写入 `EmotionalMemory` 和 `EpisodicStore`

---

## 4. P1 技术详细设计

### 4.1 跨会话记忆桥接 (#21)

**新建文件**：`memory/cross_session_manager.py`

**现状**：`db/session_store.py` 已有 `SessionStoreProtocol`（append/load/list/delete）和 `SessionSummaryData`，但缺少加载策略选择、冷启动处理和情感状态快照。

```python
@dataclass
class SessionState:
    """会话状态快照"""
    facts: list[dict]              # 关键事实
    conversation_summary: str      # 对话摘要
    recent_messages: list[dict]    # 近期消息
    user_preferences: dict         # 用户偏好
    emotional_snapshot: dict       # 情感状态快照 {dominant_emotion, intensity, pad_values}
    last_active: float             # 最后活跃时间
    metadata: dict

class CrossSessionManager:
    """跨会话记忆管理器"""

    async def save_session(self, session_id: str, state: SessionState):
        """会话结束时序列化状态"""

    async def restore_session(self, session_id: str) -> SessionState | None:
        """根据离线时间选择加载策略"""
        offline_hours = self._calc_offline_hours(session_id)
        if offline_hours < 1:
            return await self._load_full(session_id)
        elif offline_hours < 24:
            return await self._load_last_n(session_id, n=10)
        else:
            return await self._load_summary(session_id)

    async def cold_start(self, session_id: str) -> SessionState:
        """冷启动：数据损坏或首次使用时的优雅降级"""
        return SessionState(
            facts=[], conversation_summary="",
            recent_messages=[], user_preferences={},
            emotional_snapshot={}, last_active=0,
            metadata={"cold_start": True}
        )
```

**加载策略选择逻辑**：

| 离线时间 | 策略 | 加载内容 |
|---------|------|---------|
| < 1小时 | `full` | 全量加载，无缝衔接 |
| < 24小时 | `last_n` | 最近N轮 + 摘要 |
| >= 24小时 | `summary` | 仅摘要 + 关键事实 + 情感状态快照 |
| 数据损坏 | `cold_start` | 空状态，优雅降级 |

**情感陪伴场景**：
- **归巢问候**：离线>24h 时，restore 后生成"好久不见，上次你说要面试，怎么样了？"
- **情感连续性**：恢复上次对话的用户情绪状态，新一轮开始时延续关怀
- **渐进式了解**：每次会话积累用户信息，不要求重复自我介绍

**集成点**：
- `AgentCore` 启动时调 `CrossSessionManager.restore_session()`
- 会话结束时（超时或显式结束）调 `save_session()`
- 与 #13 联动：加载策略决定从 L2/L3 恢复多少记忆
- 扩展 `db/session_store.py` 增加 `emotional_snapshot` 字段

### 4.2 时间推理记忆 (#18)

**新建文件**：`memory/temporal_memory.py`

```python
@dataclass
class TemporalMemory:
    content: str
    created_at: datetime
    event_time: datetime | None    # 事件发生时间（回忆过去时 != created_at）
    last_accessed: datetime
    is_stable: bool                # 稳定事实跳过衰减
    half_life_hours: float         # 半衰期

class TemporalMemoryStore:
    """时间推理记忆存储"""

    async def query(self, query: str) -> list[TemporalMemory]:
        """标准语义查询"""

    async def query_range(self, start: datetime, end: datetime) -> list:
        """时间范围查询："上周三我们聊了什么" """

    async def query_as_of(self, as_of: datetime, topic: str) -> list:
        """截至查询："截至上个月用户住在哪里" """

    async def build_timeline(self, topic: str) -> list:
        """时间线构建："我们认识以来一起经历了什么" """
```

**时间推理场景**：
- "上次你说工作压力大，现在怎么样了？" → `query_range` + 事件关联
- "你记得我生日那天你说了什么吗？" → 精确时间点查询
- "我们认识以来一起经历了什么？" → `build_timeline`
- "你之前说推荐我看那本书，后来我又说了什么？" → 因果链时序

**关键设计**：
- `is_stable=True` 的事实（用户名、过敏信息）跳过衰减
- `is_stable=False` 的事实（情绪状态、当前项目）使用短半衰期
- 情感事件用 `event_time` 独立于 `created_at`（回忆过去事件时 event_time 更关键）

**集成点**：
- 扩展 `memory_manager.py` 的 `_parse_temporal_query()`（已有中文时间词解析）
- 路由器将时间词查询路由到 `TemporalMemoryStore`
- DB 新增 `event_time` 和 `is_stable` 列到 `episodic_memories` 表

**半衰期配置**：

| 记忆类型 | is_stable | half_life_hours |
|---------|-----------|-----------------|
| 用户名/生日/过敏信息 | True | 无限（跳过衰减） |
| 关系里程碑 | True | 无限 |
| 情绪状态 | False | 6 |
| 当前项目/工作 | False | 48 |
| 一般对话 | False | 168 (7天) |

### 4.3 自反思记忆 (#16)

**新建文件**：`memory/reflection_store.py`
**扩展文件**：`core/meta_cognition.py`

**现状**：`meta_cognition.py` 有 `MetaCognition` 类做状态追踪（confidence/fatigue/error_rate），但无任务后反思、无经验提取、无反思存储。

```python
# memory/reflection_store.py

@dataclass
class Reflection:
    task_type: str           # 场景类型
    outcome: str             # positive/negative/neutral
    insight: str             # 可复用经验
    context_hash: str        # 上下文指纹
    created_at: datetime

class ReflectionStore:
    """反思存储与检索"""
    async def add(self, reflection: Reflection) -> int
    async def query_by_scene(self, task_type: str, context: str) -> list[Reflection]
    async def get_effective_strategies(self, task_type: str) -> list[str]
```

```python
# core/meta_cognition.py 扩展

class ReflectiveAgent:
    """反思循环：评估→反思→提取→存储→注入"""

    async def post_task_reflect(self, conversation_turn, user_reaction):
        # 1. 评估：用户情绪是否改善？对话是否自然？
        outcome = await self._evaluate(conversation_turn, user_reaction)
        # 2. 反思：什么策略有效/无效？
        analysis = await self._analyze(conversation_turn, outcome)
        # 3. 提取：1-2条可复用经验
        insights = await self._extract_insights(analysis)
        # 4. 存储
        for insight in insights:
            await self._reflection_store.add(insight)

    async def pre_task_inject(self, task_type: str, context: str) -> str:
        """下次对话前：检索相关经验，注入系统提示"""
        reflections = await self._reflection_store.query_by_scene(task_type, context)
        return self._format_as_prompt(reflections)
```

**反思场景类型**：

| task_type | 场景 | 示例 insight |
|-----------|------|-------------|
| `comfort` | 安慰策略 | "直接给建议不如先共情" |
| `topic_avoidance` | 话题回避 | "该话题需谨慎引入" |
| `emotion_transition` | 情感节奏 | "情感过渡需要缓冲" |
| `style` | 表达风格 | "用比喻用户特别认可" |

**反思循环触发条件**：
- 每 5 轮对话触发一次自动反思
- 用户明确表达满意/不满时立即触发
- 检测到对话冷场（用户回复变短/延迟增加）时触发

**集成点**：
- `AgentCore.process()` 结束后异步调用 `ReflectiveAgent.post_task_reflect()`
- `AgentContext.build_messages()` 开头调用 `ReflectiveAgent.pre_task_inject()` 注入经验
- DB 新建 `reflection_store` 表

---

## 5. 测试策略

### 5.1 TDD 流程

每个技术遵循：先写失败测试 → 实现代码 → 验证通过。

### 5.2 测试文件

| 测试文件 | 覆盖技术 |
|---------|---------|
| `tests/test_hierarchical_memory.py` | #13 分层记忆 |
| `tests/test_working_memory.py` | #12 工作记忆 |
| `tests/test_memory_router.py` | #17 记忆路由 |
| `tests/test_cross_session.py` | #21 跨会话 |
| `tests/test_temporal_memory.py` | #18 时间推理 |
| `tests/test_reflection_store.py` | #16 自反思 |

### 5.3 测试覆盖维度

- **单元测试**：每个核心类的独立功能
- **集成测试**：模块间联动（如分层+路由、跨会话+分层）
- **情感陪伴场景测试**：验证特殊逻辑（创伤记忆不降级、归巢问候等）
- **回归测试**：确保不破坏现有记忆功能

---

## 6. 实施路线图

### Phase 1: P0 基础层

| 步骤 | 技术 | 依赖 | 产出 |
|------|------|------|------|
| 1.1 | #13 分层记忆 | DB 迁移 | `hierarchical_memory.py` + 测试 |
| 1.2 | #12 工作记忆 | #13 L1 层 | `agent_context.py` 扩展 + `episodic_limiter.py` 扩展 + 测试 |
| 1.3 | #17 记忆路由 | #13 多层存储 | `memory_router.py` + `memory_manager.py` 集成 + 测试 |

### Phase 2: P1 核心体验

| 步骤 | 技术 | 依赖 | 产出 |
|------|------|------|------|
| 2.1 | #21 跨会话 | #13 分层加载 | `cross_session_manager.py` + `session_store.py` 扩展 + 测试 |
| 2.2 | #18 时间推理 | #17 路由到 TEMPORAL | `temporal_memory.py` + `memory_manager.py` 扩展 + 测试 |
| 2.3 | #16 自反思 | 全部就位 | `reflection_store.py` + `meta_cognition.py` 扩展 + 测试 |

### 验收标准

每个技术完成后需满足：
1. 所有 TDD 测试通过
2. 现有测试无回归
3. `py_compile` 语法检查通过
4. 与现有模块集成后功能正常

---

## 7. 不采纳的技术

| 技术 | 原因 |
|------|------|
| #22 Multi-Agent Shared Memory | 单用户本地部署，无多Agent协作需求 |
| #24 Graphiti | 需 Neo4j，本地部署过重 |
| #26 Letta/MemGPT | 虚拟上下文过重，本地资源受限 |

## 8. 参考技术（不直接实现）

| 技术 | 参考价值 |
|------|---------|
| #25 Mem0 Patterns | 提取/更新模式值得参考 |
| #27 Zep Memory | 时序KG思路值得参考 |
| #30 Production Patterns | TTL/备份/GDPR合规参考 |
