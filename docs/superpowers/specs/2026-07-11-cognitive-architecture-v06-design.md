# xiaoda-agent v0.6.0 认知架构优化设计

> 基于 mazemaker (itsXactlY/mazemaker) 核心机制深度分析
> 方案: B - 自适应集成
> 日期: 2026-07-11

## 1. 背景与目标

### 1.1 现状
xiaoda-agent v0.5.03 的认知架构存在12项关键差距（对比 mazemaker）：
- 记忆仅2层（短期/长期），无 Episodic→Semantic→Hopfield 转化
- Salience 评分简单（sim×e^(-λ×days)），无 recency×frequency×emotion 融合
- 梦境整合仅 Ebbinghaus 衰减+归档，无 NREM/REM/Insight 阶段
- PAD 情绪模型已有但**不参与记忆检索权重**
- 知识图谱有实体/关系但**无扩散激活、无图遍历**
- 无桥接记忆、无冲突超驱、无 Hopfield 联想、无偏好发现

### 1.2 目标
全量实施12项差距修复，将 mazemaker 的认知架构机制移植到 xiaoda-agent 的 Python/SQLite 异步架构中，并增加情绪加权 Salience 作为 xiaoda 独有的超越点。

### 1.3 方案选择
**方案 B: 自适应集成** — 保留 xiaoda 的 SQLite 存储和异步架构，新增语义记忆/桥接记忆/连接图表，实现 mazemaker 算法的 Python 适配版本。

## 2. 架构设计

### 2.1 总体架构

```
用户交互层
    │
MemoryManager (改造)
  混合检索: FTS + Vector + KG + Hopfield联想
  情绪加权: PAD → Salience 调制
    │
    ├── EpisodicMemory (FIFO, capacity=10000)
    ├── SemanticMemory (聚类长期存储, max_clusters=256)
    ├── HopfieldLayer (Modern Hopfield, beta=20)
    ├── KnowledgeGraph (扩散激活, PPR)
    ├── BridgeMemory (跨会话桥接)
    └── PreferenceDiscovery (Stage C+Stage S)
    │
DreamEngineV2 (6阶段)
  NREM → SUPERSEDES → REM → Insight → AFE/StageS → DAE
  三切片采样: 50% recent + 30% random + 20% low_salience
    │
MetaCognition (改造)
  memory_pressure → 触发consolidation
  fatigue → 降低梦境采样量
```

### 2.2 数据流

```
用户消息 → 情绪分析(PAD) → 记忆编码(Episodic+Vector+FTS+KG)
                                        │
检索请求 → Salience评分(含情绪权重) → 混合检索(FTS+Vec+KG+Hopfield)
                                        │
空闲/定时 → DreamEngineV2 → NREM/REM/Insight/... → 图更新
```

## 3. 模块设计

### 3.1 新增模块

#### 3.1.1 memory/salience.py — 情绪加权 Salience 评分

```python
class SalienceScorer:
    """情绪加权 Salience 评分器

    mazemaker原版: 0.6×recency + 0.4×frequency
    xiaoda扩展: 0.4×recency + 0.3×frequency + 0.3×emotion
    """
    RECENCY_HALF_LIFE = 3600.0   # 1小时半衰期 (秒)
    FREQ_LOG_BASE = 10.0

    def compute(self, entry: MemoryEntry, now: float,
                pad_state: PADState | None = None) -> float:
        recency_score = exp(-recency_seconds / RECENCY_HALF_LIFE)
        freq_score = min(log1p(access_count) / FREQ_LOG_BASE, 1.0)
        emotion_score = self._emotion_score(entry, pad_state)
        return 0.4 * recency_score + 0.3 * freq_score + 0.3 * emotion_score

    def _emotion_score(self, entry, pad_state) -> float:
        """情绪评分: 记忆emotion_label与当前PAD状态的匹配度"""
        if not pad_state or not entry.emotion_label:
            return 0.5  # 默认中性
        # 高arousal记忆优先 (情感强烈的事件更重要)
        arousal_weight = abs(pad_state.arousal)
        # 同情绪标签匹配加成
        label_match = 1.0 if entry.emotion_label == pad_state.dominant_emotion else 0.3
        return min(1.0, arousal_weight * 0.6 + label_match * 0.4)
```

#### 3.1.2 memory/cognitive_memory.py — 3层记忆管理器

```python
class CognitiveMemory:
    """3层认知记忆管理器

    Layer 1: EpisodicMemory — FIFO热缓冲 (内存+SQLite)
    Layer 2: SemanticMemory — 聚类长期存储 (SQLite)
    Layer 3: HopfieldLayer — 联想记忆 (内存)
    """
    EPISODIC_CAPACITY = 10000
    SEMANTIC_MAX_CLUSTERS = 256
    AUTO_CONSOLIDATE_THRESHOLD = 0.8

    async def remember(self, content, embedding, emotion_label="") -> int:
        """存储新记忆到Episodic层"""

    async def recall(self, query_embedding, k=10) -> list[tuple[int, float]]:
        """混合检索: Episodic + Semantic + Hopfield"""

    async def consolidate(self, batch_size=64) -> int:
        """认知整合流程:
        1. 获取固化候选 (按access_count+age排序)
        2. self_attention_sweep 发现关联
        3. salience>0.3 或 access>=3 → 转移到Semantic+Hopfield
        4. 更新连接图
        5. 从Episodic移除
        6. 重建Semantic聚类
        """

    def connection_strength(self, a: MemoryEntry, b: MemoryEntry) -> float:
        """连接强度: sim×0.5 + temporal×0.3 + link_boost(max 0.3)"""

    def self_attention_sweep(self, candidates, threshold=0.5) -> list:
        """O(n²) 两两连接强度计算，返回strength>=threshold的三元组"""
```

#### 3.1.3 memory/hopfield_layer.py — Modern Hopfield 联想记忆

```python
class HopfieldLayer:
    """Modern Hopfield Network (Transformer Attention)

    核心算法: xi_new = sum_j softmax(beta × cos_sim(xi, xj)) × xj
    beta=20 使注意力分布极尖锐 (近似one-hot)
    """
    DIMENSIONS = 512
    CAPACITY = 1024
    BETA = 20.0
    MAX_ITERATIONS = 10
    CONVERGENCE_EPS = 1e-4
    DECAY_RATE = 0.999

    def store(self, pattern: np.ndarray, label="", source="episodic") -> int:
        """存储模式，满时驱逐最低salience"""

    def retrieve(self, cue: np.ndarray) -> RetrievalResult:
        """迭代注意力检索:
        1. 初始化 current = cue
        2. scores = beta × cosine_sim(patterns, current)
        3. weights = softmax(scores) (数值稳定: 减max)
        4. next = weights @ patterns
        5. 收敛检查: ||next - current|| < eps
        6. 返回: pattern, confidence, entropy, converged, iterations
        """

    def lookup(self, query: np.ndarray) -> RetrievalResult:
        """单次迭代检索 (不迭代)"""

    def update_salience(self):
        """salience *= decay_rate; salience = max(salience, recency+freq)"""
```

#### 3.1.4 memory/bridge_memory.py — 桥接记忆

```python
@dataclass
class BridgeMemory:
    """跨会话桥接记忆"""
    id: str
    source_memory_id: int
    target_memory_id: int
    weight: float                # [0, 1]
    bridge_type: str             # semantic | temporal | emotional
    source_session_id: str
    target_session_id: str
    cross_session: bool
    discovered_at: float
    discovery_reason: str        # rem_bridge | emotion_bridge | temporal_bridge

class BridgeMemoryManager:
    SIM_THRESHOLD = 0.3    # 桥接下界
    SIM_HIGH = 0.95        # 桥接上界 (过高=重复)
    BRIDGE_WEIGHT_FACTOR = 0.3
    MAX_CONNECTIONS = 3    # 孤立定义

    async def discover_bridges(self, isolated_memories, all_memories) -> list[BridgeMemory]:
        """REM桥接发现:
        1. 找孤立记忆 (linked < MAX_CONNECTIONS)
        2. 对每个orphan做cosine搜索 (k=10)
        3. sim在[0.3, 0.95)区间 → 桥接
        4. weight = similarity × BRIDGE_WEIGHT_FACTOR
        """
```

#### 3.1.5 memory/spreading_activation.py — 扩散激活

```python
class SpreadingActivation:
    """知识图谱扩散激活

    算法源自 mazemaker graph.h
    """
    DECAY = 0.85
    THRESHOLD = 0.01
    MAX_DEPTH = 5

    def spread(self, graph, seed_id: int,
               decay=0.85, threshold=0.01, max_depth=5) -> list[TraversalResult]:
        """优先队列扩散:
        activation[seed] = 1.0
        while queue:
            (act, current) = queue.pop()
            if act < threshold: continue
            if depth[current] >= max_depth: continue
            for edge in adjacency[current]:
                propagated = act * edge.weight * decay
                if propagated > activation[neighbor]:
                    activation[neighbor] = propagated
                    queue.push(propagated, neighbor)
        """

    def predict_links(self, graph, node_id: int, max_results=10) -> list:
        """链路预测: 0.3×common_neighbors + 0.4×adamic_adar + 0.3×embedding_sim"""
```

#### 3.1.6 memory/preference_discovery.py — 偏好发现

```python
class PreferenceDiscovery:
    """Stage C + Stage S 偏好结构发现

    mazemaker设计: 10%低产出率是有意为之
    """
    CLUSTER_THRESHOLD = 0.85  # cos >= 0.85 聚类
    PATTERN_SALIENCE = 2.0    # 高置信度模式记忆

    async def stage_c_extract(self, session_content: str) -> list[str]:
        """Stage C: LLM提取用户状态事实
        输入: session_content
        输出: ["user prefers X", "user owns Y", ...]
        """

    async def stage_s_synthesize(self, stage_c_outputs: list[str]) -> list[dict]:
        """Stage S: 聚类 + LLM蒸馏
        1. 按cos >= 0.85聚类Stage C输出
        2. 每个cluster LLM蒸馏为单一模式
        3. 存储为高置信度偏好记忆 (salience=2.0)
        """
```

#### 3.1.7 core/dream_engine_v2.py — 6阶段梦境引擎

```python
class DreamEngineV2:
    """6阶段梦境整合引擎

    阶段顺序: NREM → SUPERSEDES → REM → Insight → AFE/StageS → DAE
    """
    IDLE_THRESHOLD = 600       # 600秒空闲触发
    MEMORY_THRESHOLD = 50      # 50条新记忆触发
    SAMPLE_LIMIT = 2000
    RECENT_PCT = 0.5
    RANDOM_OLD_PCT = 0.3
    LOW_SALIENCE_PCT = 0.2

    async def run_cycle(self):
        """执行完整梦境周期"""
        await self._phase_nrem()
        await self._phase_supersedes()
        await self._phase_rem()
        await self._phase_insight()
        await self._phase_afe_stage_s()
        await self._phase_dae()

    async def _phase_nrem(self):
        """NREM: 强化+修剪
        1. 三切片采样 (50% recent + 30% random + 20% low_salience)
        2. 对每个seed做spreading_activation
        3. 簇内连接 weight += 0.05 (Hebbian)
        4. 簇外连接 weight -= 0.01
        5. prune connections where weight < 0.05
        """

    async def _phase_supersedes(self):
        """SUPERSEDES: 冲突超驱
        1. 对比记忆对: cos >= 0.85 且 数值token不同
        2. old → new 有向边 (type=supersedes)
        3. 标记old为SUPERSEDED
        """

    async def _phase_rem(self):
        """REM: 桥接发现
        1. 获取孤立记忆 (max_connections < 3, limit=800)
        2. 批量recall找相似
        3. 写入bridge边 (weight = sim × 0.3)
        """

    async def _phase_insight(self):
        """Insight: 社区物化
        1. Louvain社区检测 (networkx)
        2. size >= 4的社区 → 派生cluster摘要记忆
        3. 摘要记忆参与后续recall
        """

    async def _phase_afe_stage_s(self):
        """AFE/StageS: 偏好结晶
        1. Stage C: 每会话LLM提取用户状态
        2. Stage S: 聚类(cos>=0.85) + LLM蒸馏 (~10%产出率)
        """

    async def _phase_dae(self):
        """DAE: 图感知嵌入
        每条记忆DAE向量 = 图邻居BGE-M3嵌入的salience加权均值
        每5个周期重计算一次
        """

    def _sample_for_dream(self, limit, recent_pct, random_old_pct, low_salience_pct):
        """三切片采样: 对抗'表层陷阱'"""
```

#### 3.1.8 core/conflict_supersession.py — 冲突超驱

```python
class ConflictSupersession:
    """冲突检测与超驱

    mazemaker核心洞察: 仅靠语义相似度不够
    引入数值token差异作为冲突判据
    """
    SIMILARITY_THRESHOLD = 0.85

    async def detect_conflicts(self, memories: list) -> list[ConflictPair]:
        """检测冲突记忆对:
        1. cos_sim >= 0.85 (语义高度相似)
        2. numeric_tokens不同 (数值/金额/度量不同)
        3. 按时间排序: old → new
        """

    async def apply_supersession(self, conflicts: list[ConflictPair]):
        """应用超驱:
        1. 标记old记忆为SUPERSEDED
        2. 写入memory_revisions表
        3. 添加有向边 old→new (type=supersedes)
        """
```

### 3.2 改造模块

#### 3.2.1 memory/memory_manager.py
- 新增 CognitiveMemory 实例作为3层记忆管理
- 检索路径增加 Hopfield 联想回忆
- Salience 评分改用 SalienceScorer (含情绪权重)
- 记忆存储时同步写入 CognitiveMemory

#### 3.2.2 memory/knowledge_graph.py
- 新增 spread_activation() 方法 (调用 SpreadingActivation)
- 新增 predict_links() 方法
- 新增 hebbian_strengthen() 方法
- 实体检索增加图遍历增强

#### 3.2.3 memory/fluid_memory.py
- FluidMemory.score() 内部改为调用 SalienceScorer
- 保持接口兼容，旧调用方无需修改

#### 3.2.4 core/dream_consolidation.py
- DreamConsolidator.consolidate_from_db() 改为调用 DreamEngineV2.run_cycle()
- 保持旧接口兼容，新增 dream_engine 参数

#### 3.2.5 core/meta_cognition.py
- memory_pressure > 0.8 时触发 consolidate()
- fatigue 高时降低 SAMPLE_LIMIT

#### 3.2.6 emotion/pad_model.py
- 新增 get_emotion_weight() 方法供记忆系统调用
- 新增 get_dominant_emotion() 返回当前主导情绪标签

## 4. 数据库设计

### 4.1 新增表

```sql
-- 语义记忆（consolidation后的长期记忆）
CREATE TABLE IF NOT EXISTS semantic_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_memory_id INTEGER,
    content TEXT NOT NULL,
    embedding_id INTEGER DEFAULT -1,
    cluster_id INTEGER DEFAULT -1,
    salience REAL DEFAULT 0.5,
    access_count INTEGER DEFAULT 0,
    last_accessed REAL DEFAULT 0,
    created_at REAL NOT NULL,
    emotion_label TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}'
);
CREATE INDEX idx_semantic_cluster ON semantic_memories(cluster_id);
CREATE INDEX idx_semantic_salience ON semantic_memories(salience);

-- 记忆连接图
CREATE TABLE IF NOT EXISTS memory_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    weight REAL DEFAULT 0.5,
    edge_type TEXT NOT NULL DEFAULT 'similar',
    activation_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    last_activated REAL DEFAULT 0
);
CREATE INDEX idx_conn_source ON memory_connections(source_id);
CREATE INDEX idx_conn_target ON memory_connections(target_id);
CREATE INDEX idx_conn_type ON memory_connections(edge_type);

-- 桥接记忆
CREATE TABLE IF NOT EXISTS bridge_memories (
    id TEXT PRIMARY KEY,
    source_memory_id INTEGER NOT NULL,
    target_memory_id INTEGER NOT NULL,
    weight REAL NOT NULL,
    bridge_type TEXT DEFAULT 'semantic',
    source_session_id TEXT DEFAULT '',
    target_session_id TEXT DEFAULT '',
    cross_session INTEGER DEFAULT 0,
    discovered_at REAL NOT NULL,
    discovery_reason TEXT DEFAULT 'rem_bridge'
);
CREATE INDEX idx_bridge_source ON bridge_memories(source_memory_id);
CREATE INDEX idx_bridge_target ON bridge_memories(target_memory_id);

-- 冲突修订链
CREATE TABLE IF NOT EXISTS memory_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_memory_id INTEGER NOT NULL,
    new_memory_id INTEGER NOT NULL,
    conflict_type TEXT DEFAULT 'numeric_token',
    revision_chain TEXT DEFAULT '[]',
    created_at REAL NOT NULL
);
CREATE INDEX idx_revisions_old ON memory_revisions(old_memory_id);

-- 偏好模式
CREATE TABLE IF NOT EXISTS preference_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_text TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    source_sessions TEXT DEFAULT '[]',
    salience REAL DEFAULT 2.0,
    created_at REAL NOT NULL,
    last_matched REAL DEFAULT 0,
    match_count INTEGER DEFAULT 0
);
CREATE INDEX idx_preference_salience ON preference_patterns(salience);
```

### 4.2 episodic_memories 表新增字段

```sql
ALTER TABLE episodic_memories ADD COLUMN salience REAL DEFAULT 0.5;
ALTER TABLE episodic_memories ADD COLUMN last_accessed REAL DEFAULT 0;
ALTER TABLE episodic_memories ADD COLUMN status TEXT DEFAULT 'active';
-- status: active | consolidated | superseded | archived
```

## 5. 实施阶段

| 阶段 | 模块 | 交付物 |
|------|------|--------|
| P1 | DB表 + SalienceScorer + CognitiveMemory | 基础设施层 |
| P2 | HopfieldLayer + SpreadingActivation + BridgeMemory | 核心算法层 |
| P3 | DreamEngineV2 + ConflictSupersession | 梦境引擎层 |
| P4 | PreferenceDiscovery + 情绪耦合 + MetaCognition | 高级能力层 |
| P5 | MemoryManager集成 + 端到端测试 | 集成交付层 |

## 6. 关键参数

| 参数 | 值 | 来源 |
|------|-----|------|
| Episodic capacity | 10000 | mazemaker memory.h |
| Semantic max_clusters | 256 | mazemaker memory.h |
| Hopfield beta | 20.0 | mazemaker hopfield.h |
| Hopfield capacity | 1024 | mazemaker hopfield.h |
| Hopfield max_iterations | 10 | mazemaker hopfield.cpp |
| Salience threshold | 0.3 | mazemaker consolidation.cpp |
| Connection threshold | 0.5 | mazemaker consolidation.cpp |
| Bridge sim range | [0.3, 0.95) | mazemaker dream_engine.py |
| Bridge weight factor | 0.3 | mazemaker dream_engine.py |
| NREM strengthen | +0.05 | mazemaker dream-engine.md |
| NREM weaken | -0.01 | mazemaker dream-engine.md |
| Prune threshold | 0.05 | mazemaker dream-engine.md |
| Supersedes sim | 0.85 | mazemaker dream_engine.py |
| Insight min_community | 4 | mazemaker dream-engine.md |
| Sample recent_pct | 0.5 | mazemaker dream-engine.md |
| Sample random_pct | 0.3 | mazemaker dream-engine.md |
| Sample low_sal_pct | 0.2 | mazemaker dream-engine.md |
| Spread decay | 0.85 | mazemaker graph.h |
| Spread threshold | 0.01 | mazemaker graph.h |
| Spread max_depth | 5 | mazemaker graph.h |
| Preference salience | 2.0 | mazemaker dream_engine.py |
| Stage S cluster cos | 0.85 | mazemaker dream_engine.py |
| Salience weights | 0.4/0.3/0.3 | xiaoda扩展 (mazemaker原版0.6/0.4) |

## 7. 不采纳的部分

- **ColBERT rerank**: 需要独立模型服务，部署成本高，后续可选集成
- **GPU PPR加速**: xiaoda运行在ARM设备上，无CUDA，用BFS替代
- **DAE全语料扫描**: 计算量大，改为按需更新(仅检索时计算邻居均值)
- **C++ SIMD加速**: Python用numpy向量化替代
