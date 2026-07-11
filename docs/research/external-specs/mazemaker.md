# xiaoda-agent 认知架构优化 Spec

> 基于 mazemaker (itsXactlY/mazemaker) 核心机制深度分析  
> 目标版本: xiaoda-agent v0.6.0  
> 文档版本: 2026-07-11  
> 状态: Draft

---

## 目录

1. [现状诊断](#1-现状诊断)
2. [mazemaker 核心机制深度分析](#2-mazemaker-核心机制深度分析)
3. [差距对比表](#3-差距对比表)
4. [优化方案](#4-优化方案)
5. [实施步骤](#5-实施步骤)
6. [预期效果](#6-预期效果)
7. [不采纳的部分](#7-不采纳的部分)

---

## 1. 现状诊断

### 1.1 xiaoda-agent v0.5.03 认知架构总览

| 模块 | 路径 | 能力 | 缺陷 |
|------|------|------|------|
| PAD三维情绪 | `emotion/pad_model.py` | Pleasure-Arousal-Dominance量化情绪 | 情绪不参与记忆检索权重 |
| LLM深度情绪分析 | `emotion/emotion_llm.py` | LLM提取情感标签 | 分析结果未反馈到记忆系统 |
| 情绪状态机 | `emotion/emotion_state.py` | 情绪状态转移 | 无情绪-记忆耦合机制 |
| Stanislavski情感记忆 | `emotion/emotional_memory.py` | 情感记忆标签 | 缺乏情绪加权检索 |
| 重聚反思 | `emotion/reunion_reflection.py` | 跨会话重聚感知 | 不产生桥接记忆 |
| 记忆系统 | `memory/` | 短期/长期记忆+遗忘曲线 | 无认知整合，无Episodic→Semantic转化 |
| 元认知 | `core/meta_cognition.py` | confidence/fatigue/error_rate/memory_pressure | 元认知不驱动consolidation |
| 梦境 | `core/dream_consolidation.py` | Ebbinghaus衰减+归档 | 仅衰减归档，无NREM/REM/Insight三阶段 |

### 1.2 核心缺失能力

1. **桥接记忆 (Bridge Memory)**: 无法跨会话连接语义相关但时间分散的记忆，导致孤岛化
2. **偏好结构发现 (Preference Discovery)**: 无法从交互历史中推断用户潜在偏好（如"喜欢简洁回复"、"偏好晚间长谈"）
3. **冲突超驱 (Conflict Supersession)**: 用户纠正后旧知识不标记废弃，新旧矛盾并存
4. **Hopfield联想回忆 (Associative Recall)**: 给定一条记忆无法自动扩展关联记忆上下文
5. **认知整合 (Cognitive Consolidation)**: 缺乏 Episodic → Semantic → Abstraction 三阶段转化
6. **Salience评分**: 当前仅使用简单importance打分，未融合recency×frequency×emotion
7. **情绪加权认知**: 情感系统与记忆系统并行运行但无交互，情绪不调制记忆检索

### 1.3 现有遗忘曲线的局限

当前 `dream_consolidation.py` 实现了简单的 Ebbinghaus 衰减：

```
R(t) = R0 × exp(-t/S)
```

但存在三个关键缺陷：
- **单向衰减**: 只有遗忘，没有"想起来"时的重新强化（mazemaker的NREM strengthening）
- **无结构化整合**: 衰减后归档，但不产生更高级别的语义抽象
- **无图结构**: 记忆之间无连接关系，无法做spreading activation

---

## 2. mazemaker 核心机制深度分析

### 2.1 认知图七层结构

mazemaker将原始对话转化为七层认知结构：

```
原始对话 (Raw Conversation)
    ↓ Layer 1: Sponge Ingestion — 全量吸收
原子事实 (Atomic Facts)
    ↓ Layer 2: AFE — 事实提取
语义链接 (Semantic Links)
    ↓ Layer 3: Embedding + ColBERT + DAE — 多通道编码
超驰链 (Supersession Chains)
    ↓ Layer 4: Conflict Detection — 冲突检测与替换
合成抽象 (Synthesized Abstractions)
    ↓ Layer 5: Stage S Synthesis — 聚类抽象
桥接记忆 (Bridge Memories)
    ↓ Layer 6: REM Bridge Discovery — 跨域桥接
偏好结构 + 时间轨迹 (Preference Structures + Temporal Trajectories)
    ↓ Layer 7: Preference Extraction — 偏好推断
```

**对xiaoda-agent的启示**: 情感陪伴Agent比工具型Agent更需要偏好结构和时间轨迹——因为用户情感偏好（如"难过时需要安静陪伴而非建议"）是隐性的，只有通过跨会话的模式发现才能捕捉。

### 2.2 三层记忆架构

mazemaker的核心数据结构是三层递进式记忆：

#### EpisodicMemory（情景记忆）

```cpp
// 源自 include/mazemaker/memory.h
struct MemoryEntry {
    uint64_t id;
    vector<float> embedding;   // 语义向量 (1024-d BGE-M3)
    string label;              // 语义标签
    string content;            // 原始内容
    string source;             // "perception" | "inference" | "consolidated"
    uint64_t timestamp;        // 创建时间 (微秒)
    uint64_t last_accessed;    // 最后访问时间
    uint64_t access_count;     // 总检索次数
    float salience;            // 重要性评分
    float decay_factor;        // 指数衰减因子
    vector<uint64_t> linked;   // 连接的记忆ID
};
```

关键设计决策：
- **FIFO淘汰**: deque存储，容量满时自动淘汰最旧条目（非最不重要——因为重要记忆已consolidation到Semantic层）
- **余弦搜索**: 暴力余弦相似度（C++ SIMD加速），不做HNSW索引——因为Episodic层是热缓冲
- **容量**: 默认10000条，用于短期"工作记忆"

#### SemanticMemory（语义记忆）

```cpp
struct Cluster {
    uint64_t id;
    vector<float> centroid;        // 簇中心向量
    vector<uint64_t> member_ids;   // 成员ID列表
    float coherence;               // 簇内平均相似度
    uint64_t created;
    uint64_t last_updated;
};
```

关键设计决策：
- **聚类组织**: 语义记忆按cluster分组，新记忆自动分配到最近cluster
- **持久化**: 存储在SQLite，不参与FIFO淘汰
- **合并**: 高相似度记忆可合并为单条（embedding取平均）
- **衰减**: 所有salience乘以decay_factor（默认0.999），但永不删除

#### HopfieldLayer（联想记忆）

```cpp
// 源自 include/mazemaker/hopfield.h
struct HopfieldConfig {
    size_t dimensions = 512;    // 模式向量维度
    size_t capacity = 1024;     // 最大存储模式数
    float beta = 20.0f;         // 逆温度（锐化注意力）
    float learning_rate = 0.01f;
    float decay_rate = 0.999f;  // salience衰减率
};
```

核心算法——Modern Hopfield (Transformer Attention)：

```
存储: patterns.append({data: embedding, salience: 1.0})

检索 (迭代注意力):
    x_0 = cue (查询向量)
    for iter = 1..max_iterations:
        weights = softmax(beta × cosine_sim(x_{n-1}, patterns))
        x_n = sum_j weights[j] × patterns[j]
        if ||x_n - x_{n-1}|| < epsilon: converged
    
    return {pattern: x_n, confidence: max_cosine_to_stored}
```

这是 mazemaker 最精巧的设计：**不需要训练权重矩阵**，而是用存储模式的注意力加权求和实现联想回忆。beta=20使注意力分布极尖锐（近似one-hot），确保检索的确定性。

### 2.3 Consolidation引擎

#### 指数衰减函数

```cpp
// 源自 src/memory/consolidation.cpp
float exponential_decay(float initial_value, float time_seconds, float decay_rate = 0.001f) {
    return initial_value * exp(-decay_rate * time_seconds);
}
```

#### Salience计算

```cpp
float compute_salience(const MemoryEntry& entry, uint64_t now) {
    double recency = entry.recency_seconds(now);
    
    // 时近性评分: 从最后访问时间起指数衰减 (1小时半衰期)
    float recency_score = exp(-recency / 3600.0);
    
    // 频率评分: 对数归一化访问次数
    float freq_score = log1p(access_count) / 10.0f;
    freq_score = min(freq_score, 1.0f);
    
    // 加权组合
    return 0.6f * recency_score + 0.4f * freq_score;
}
```

**对xiaoda-agent的扩展**: mazemaker的salience是纯认知维度的（recency+frequency），对于情感陪伴Agent需要加入emotion维度。

#### 连接强度计算

```cpp
float connection_strength_internal(const MemoryEntry& a, const MemoryEntry& b, size_t dim) {
    if (a.embedding.empty() || b.embedding.empty()) return 0.0f;
    
    float sim = cosine_similarity(a.embedding, b.embedding);
    
    // 时间邻近性加成 (1分钟衰减)
    double time_diff = abs(a.timestamp - b.timestamp) / 1e6;
    float temporal_boost = exp(-time_diff / 60.0);
    
    // 共享链接加成
    float link_boost = 0.0f;
    unordered_set<uint64_t> a_links(a.linked.begin(), a.linked.end());
    for (uint64_t lid : b.linked) {
        if (a_links.count(lid)) link_boost += 0.1f;
    }
    link_boost = min(link_boost, 0.3f);
    
    return max(0.0f, sim * 0.5f + temporal_boost * 0.3f + link_boost);
}
```

#### 自注意力扫描 (Self-Attention Sweep)

```cpp
// 在consolidation过程中发现记忆间关联
auto self_attention_sweep(candidates, dimensions, threshold=0.5f) {
    connections = []
    for i, j in pairwise(candidates):
        strength = connection_strength(mem[i], mem[j])
        if strength >= threshold:
            connections.append((mem[i].id, mem[j].id, strength))
    sort by strength descending
    return connections
```

#### 整合流程 (Consolidation Pipeline)

```
1. candidates = episodic.candidates_for_consolidation(batch_size=64)
2. connections = self_attention_sweep(candidates)  // 发现关联
3. for each candidate:
     if candidate.salience > 0.3 OR candidate.access_count >= 3:
         transfer to semantic_memory       // 提升为长期记忆
         store in hopfield_layer           // 存入联想记忆
4. update connection graph from sweep results
5. remove transferred entries from episodic
6. rebuild semantic clusters
```

**关键阈值**:
- salience > 0.3 或 access_count >= 3 才提升（过滤噪声）
- 连接强度 >= 0.5 才建立连接（避免弱关联污染图结构）

### 2.4 梦境整合引擎

mazemaker的dream engine实现6阶段周期：

#### NREM（强化+修剪）

```
1. sample_for_dream(limit=2000, recent_pct=0.5, random_old_pct=0.3, low_salience_pct=0.2)
2. for each seed memory:
     activated = spreading_activation(seed)  // PPR或BFS
     for each connection inside activated cluster:
         connection.weight += 0.05  // Hebbian强化
     for each connection outside:
         connection.weight -= 0.01  // 被动衰减
3. prune connections where weight < 0.05
```

**三切片采样** 是对抗"表层陷阱"的关键：如果只采样最近记忆，旧记忆永远不被重放，最终衰减到prune阈值以下被永久遗忘。30%随机采样确保每个记忆每个周期都有非零重放概率。

#### SUPERSEDES（冲突超驱）

```python
# 源自 python/dream_engine.py _phase_supersedes
for i, j in pairwise(numeric_memories):
    if cosine_sim(mem[i], mem[j]) >= 0.85:       # 语义高度相似
        if numeric_tokens[i] != numeric_tokens[j]: # 但数值不同
            older, newer = order_by_timestamp(i, j)
            add_edge(older, newer, type="supersedes")  # 有向边
```

**核心洞察**: 仅靠语义相似度不够——两条记忆可能讨论同一话题但信息相同。mazemaker引入**数值token差异**作为冲突判据：只有当两条记忆语义相近但包含不同的数值/金额/度量时，才判定为超驱关系。

#### REM（桥接发现）

```
1. isolated = get_isolated_memories(max_connections=3, limit=800)
2. for each orphan:
     similar = recall_batch(orphans, k=5)  // 批量检索
     for each hit not already connected:
         add_bridge(orphan, hit, weight=similarity * 0.3)
```

REM是打破孤立簇的关键阶段。没有它，知识图谱退化为不相交的会话森林。

#### Insight（社区物化）

```
1. communities = louvain_community_detection(graph)
2. for each community (size >= 4):
     centroid = compute_centroid(members)
     representative = most_central_member(community)
     create derived:cluster memory(representative.content, centroid_embedding)
```

社区物化使"隐含知识"变为"显式记忆"——一条cluster摘要记忆本身可被检索，为后续recall提供入口。

### 2.5 知识图谱与扩散激活

```cpp
// 源自 include/mazemaker/graph.h
struct Node {
    uint64_t id;
    NodeType type;       // Entity, Event, Concept, Memory, Procedure
    vector<float> embedding;
    float centrality;    // 度/PageRank中心性
    float activation;    // 当前扩散激活值
};

struct Edge {
    uint64_t source_id, target_id;
    EdgeType type;       // Similar, Causal, Temporal, Associative, Semantic, Inferred
    float weight;        // [0, 1]
    uint32_t activation_count;  // 遍历次数（Hebbian信号）
};
```

扩散激活算法：

```
activation[seed] = 1.0
priority_queue.push((1.0, seed))

while queue not empty:
    (act, current) = queue.pop()
    if act < threshold: continue
    if depth[current] >= max_depth: continue
    
    for edge in adjacency[current]:
        propagated = act * edge.weight * decay_factor
        if propagated > activation[neighbor]:
            activation[neighbor] = propagated
            queue.push((propagated, neighbor))
```

链接预测（三种方法融合）：

```
score = 0.3 × common_neighbors(a, b) 
      + 0.4 × adamic_adar(a, b)
      + 0.3 × embedding_similarity(a, b)
```

### 2.6 偏好结构发现

mazemaker通过Stage S Synthesis实现偏好发现：

```
1. Stage C: 从交互中提取用户状态事实 (LLM one-shot)
   输入: session_content
   输出: ["user prefers X", "user owns Y", "user X is Z"]

2. Stage S: 聚类 + LLM蒸馏
   for each cluster of Stage C outputs (cos >= 0.85):
     pattern = LLM_distill(cluster_members)  // ~10% yield by design
     store as high-confidence pattern memory at salience=2.0
```

**关键设计**: 10%的低产出率是有意为之——更高的产出率会导致规范记忆覆盖原始事实但不覆盖其session ID，反而降低recall质量。

### 2.7 基准数据

mazemaker在LongMemEval-oracle上的表现：

| 指标 | 分数 |
|------|------|
| R@5 | **0.8426** |
| R@10 | **0.9000** |

关键消融实验：
- Hop-2 graph reasoning: 0.00 → 1.00（证明图遍历的必要性）
- Post-dream synthesis: 0.00 → 0.43（证明梦境整合的必要性）
- Conflict supersession: 0.03 → 0.33（证明冲突超驱的必要性）
- Cross-session continuity: 0.06 → 0.62（证明桥接记忆的必要性）

---

## 3. 差距对比表

| 能力维度 | mazemaker | xiaoda-agent v0.5.03 | 差距等级 |
|----------|-----------|---------------------|----------|
| **记忆分层** | Episodic → Semantic → Hopfield 三层递进 | 短期/长期两层，无转化机制 | 🔴 严重 |
| **Salience评分** | 0.6×recency + 0.4×frequency（代码级） | 简单importance打分 | 🔴 严重 |
| **认知整合** | 三阶段consolidation（代码级） | 仅Ebbinghaus衰减+归档 | 🔴 严重 |
| **冲突超驱** | SUPERSEDES有向边 + 数值token差异判据 | 无 | 🔴 严重 |
| **桥接记忆** | REM阶段发现孤立记忆并桥接 | 无 | 🔴 严重 |
| **联想回忆** | Modern Hopfield (beta=20迭代注意力) | 无 | 🟡 中等 |
| **偏好发现** | Stage C + Stage S LLM蒸馏 | 无 | 🟡 中等 |
| **扩散激活** | KnowledgeGraph.spread_activation | 无 | 🟡 中等 |
| **情绪-记忆耦合** | 无（纯认知系统） | PAD情绪模型已有，但未与记忆耦合 | 🟢 可超越 |
| **梦境整合** | NREM/REM/Insight/SUPERSEDES/AFE/StageS/DAE 七阶段 | 简单衰减归档 | 🔴 严重 |
| **Hebbian强化** | 共激活边权重+0.05 | 无 | 🟡 中等 |
| **三切片采样** | recent 50% + random 30% + low_salience 20% | 无 | 🟡 中等 |

---

## 4. 优化方案

### 4.1 桥接记忆机制

#### 设计目标

跨会话连接语义相关但时间分散的记忆，解决记忆孤岛化问题。

#### 数据模型

```python
# memory/bridge_memory.py

@dataclass
class BridgeMemory:
    """桥接记忆：连接两条跨会话的语义相关记忆"""
    id: str                          # UUID
    source_memory_id: str            # 记忆A的ID
    target_memory_id: str            # 记忆B的ID
    weight: float                    # 桥接强度 [0, 1]
    bridge_type: str                 # "semantic" | "temporal" | "emotional"
    discovered_at: float             # 发现时间戳
    discovery_reason: str            # "rem_bridge" | "emotion_bridge" | "temporal_bridge"
    source_session_id: str           # 记忆A所属会话
    target_session_id: str           # 记忆B所属会话
    cross_session: bool              # 是否跨会话
```

#### 桥接发现算法

```python
def discover_bridges(
    isolated_memories: List[MemoryEntry],
    all_memories: List[MemoryEntry],
    sim_threshold: float = 0.3,   # 桥接的语义相似度下界（比consolidation低）
    sim_high: float = 0.95,        # 桥接的语义相似度上界（过高=重复，不是桥接）
    bridge_weight_factor: float = 0.3,
    max_connections: int = 3,      # "孤立"定义：连接数 < max_connections
) -> List[BridgeMemory]:
    """
    REM桥接发现算法，源自mazemaker dream_engine.py
    
    核心思想：
    - 在0.3~0.95的相似度区间内发现桥接
    - 低于0.3：语义不相关，不是桥接
    - 高于0.95：语义重复，应走consolidation合并而非桥接
    - 中间区间：真正的桥接——相关但不重复
    """
    bridges = []
    orphans = [m for m in isolated_memories if len(m.linked) < max_connections]
    
    for orphan in orphans:
        # 余弦搜索找相似记忆
        candidates = cosine_search(orphan.embedding, all_memories, k=10)
        
        for candidate_id, similarity in candidates:
            if sim_threshold <= similarity < sim_high:
                # 检查是否已连接
                if not already_connected(orphan.id, candidate_id):
                    bridge = BridgeMemory(
                        source_memory_id=orphan.id,
                        target_memory_id=candidate_id,
                        weight=similarity * bridge_weight_factor,
                        bridge_type="semantic",
                        discovery_reason="rem_bridge",
                        cross_session=(orphan.session_id != get_session(candidate_id)),
                    )
                    bridges.append(bridge)
    
    return bridges
```

#### 情感桥接扩展

mazemaker的桥接仅基于语义相似度。xiaoda-agent可利用PAD情绪做**情感桥接**：

```python
def discover_emotional_bridges(
    memories: List[MemoryEntry],
    pad_model: PADModel,
    emotion_weight: float = 0.3,   # 情绪维度权重
    semantic_weight: float = 0.7,  # 语义维度权重
) -> List[BridgeMemory]:
    """
    情感桥接：将情绪PAD空间相似的跨会话记忆桥接。
    例如：用户在多次会话中表达悲伤时提到的不同话题，
    这些记忆在语义上可能不相似，但情绪上高度相关。
    """
    bridges = []
    
    for i, mem_a in enumerate(memories):
        pad_a = pad_model.analyze(mem_a.content)  # (P, A, D)
        for mem_b in memories[i+1:]:
            if mem_a.session_id == mem_b.session_id:
                continue  # 跳过同会话（已由语义桥接覆盖）
            
            pad_b = pad_model.analyze(mem_b.content)
            
            # 情绪距离：PAD三维欧氏距离
            emotion_dist = sqrt(
                (pad_a.pleasure - pad_b.pleasure)**2 +
                (pad_a.arousal - pad_b.arousal)**2 +
                (pad_a.dominance - pad_b.dominance)**2
            )
            emotion_sim = exp(-emotion_dist / 2.0)  # 高斯核
            
            # 语义相似度
            semantic_sim = cosine_similarity(mem_a.embedding, mem_b.embedding)
            
            # 融合
            combined = semantic_weight * semantic_sim + emotion_weight * emotion_sim
            
            if combined >= 0.4:  # 情感桥接阈值较低
                bridges.append(BridgeMemory(
                    source_memory_id=mem_a.id,
                    target_memory_id=mem_b.id,
                    weight=combined * 0.3,
                    bridge_type="emotional",
                    discovery_reason="emotion_bridge",
                    cross_session=True,
                ))
    
    return bridges
```

#### 存储与持久化

```python
# SQLite 表结构
"""
CREATE TABLE IF NOT EXISTS bridge_memories (
    id TEXT PRIMARY KEY,
    source_memory_id TEXT NOT NULL,
    target_memory_id TEXT NOT NULL,
    weight REAL NOT NULL,
    bridge_type TEXT NOT NULL DEFAULT 'semantic',
    discovered_at REAL NOT NULL,
    discovery_reason TEXT,
    source_session_id TEXT,
    target_session_id TEXT,
    cross_session INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (source_memory_id) REFERENCES memories(id),
    FOREIGN KEY (target_memory_id) REFERENCES memories(id)
);
CREATE INDEX idx_bridge_source ON bridge_memories(source_memory_id);
CREATE INDEX idx_bridge_target ON bridge_memories(target_memory_id);
CREATE INDEX idx_bridge_cross_session ON bridge_memories(cross_session);
"""
```

---

### 4.2 偏好结构发现

#### 设计目标

从情感记忆+交互历史中推断用户潜在偏好，构建"用户画像"的深层结构。

#### 数据模型

```python
# memory/preference_discovery.py

@dataclass
class Preference:
    """用户偏好结构"""
    id: str                           # UUID
    category: str                     # "communication" | "emotion" | "topic" | "timing" | "style"
    content: str                      # 偏好描述，如 "user prefers concise replies when sad"
    confidence: float                 # 置信度 [0, 1]
    source_count: int                 # 支撑此偏好的源记忆数量
    source_memory_ids: List[str]      # 支撑此偏好的记忆ID列表
    emotional_context: Optional[str]  # 偏好激活的情绪上下文，如 "sadness"
    created_at: float
    updated_at: float
    superseded_by: Optional[str]      # 被更新的偏好ID取代
```

#### 偏好发现流程

```python
class PreferenceDiscoveryEngine:
    """
    偏好结构发现引擎
    
    灵感源自mazemaker Stage C + Stage S，但针对情感陪伴场景深度定制：
    - Stage C: 从会话中提取偏好候选（LLM one-shot）
    - Stage D (Discovery): 偏好聚类 + 情绪关联
    - Stage V (Validation): 偏好验证 + 冲突检测
    """
    
    def discover_preferences(
        self,
        session_memories: List[MemoryEntry],
        emotional_memories: List[EmotionalMemory],
        existing_preferences: List[Preference],
        llm_client: LLMClient,
    ) -> List[Preference]:
        """
        三阶段偏好发现：
        
        Stage C (Candidate Extraction):
            从会话记忆中提取偏好候选
            
        Stage D (Discovery & Clustering):  
            将候选聚类，发现跨会话的模式
            
        Stage V (Validation):
            与已有偏好对比，处理冲突和更新
        """
        # Stage C: 提取偏好候选
        candidates = self._extract_preference_candidates(
            session_memories, emotional_memories, llm_client
        )
        
        # Stage D: 聚类发现
        patterns = self._cluster_and_discover(candidates)
        
        # Stage V: 验证和整合
        new_preferences = self._validate_and_integrate(
            patterns, existing_preferences, llm_client
        )
        
        return new_preferences
    
    def _extract_preference_candidates(
        self,
        session_memories: List[MemoryEntry],
        emotional_memories: List[EmotionalMemory],
        llm_client: LLMClient,
    ) -> List[PreferenceCandidate]:
        """
        Stage C: LLM提取偏好候选
        
        使用sub-1B模型 + 精心设计的prompt，从交互中提取用户偏好。
        这是mazemaker AFE Stage C在情感维度的扩展。
        """
        prompt = """Analyze the following conversation segment and extract 
user preferences. Focus on:
- Communication style preferences (concise vs detailed, formal vs casual)
- Emotional response preferences (comfort vs advice, presence vs solutions)  
- Topic preferences (what the user enjoys discussing)
- Timing patterns (when the user is most engaged)

Format each preference as:
PREFERENCE: <category>|<description>|<emotional_context>

Only extract HIGH-CONFIDENCE preferences that are clearly indicated.
Do NOT infer preferences from single interactions.
Yield rate target: ~10% (deliberately selective)."""

        candidates = []
        # 批量处理，每批10条记忆
        for batch in chunk(session_memories, 10):
            context = format_batch(batch, emotional_memories)
            response = llm_client.generate(prompt + context)
            for line in response.split("\n"):
                if line.startswith("PREFERENCE:"):
                    candidate = parse_preference_line(line)
                    candidates.append(candidate)
        
        return candidates
    
    def _cluster_and_discover(
        self,
        candidates: List[PreferenceCandidate],
        similarity_threshold: float = 0.85,
    ) -> List[Preference]:
        """
        Stage D: 偏好聚类
        
        源自mazemaker Stage S的聚类逻辑：
        - 将embedding相似度 >= 0.85的候选聚类
        - 每个聚类产生一个偏好（~10% yield）
        - 合并同类偏好，提升confidence
        """
        if not candidates:
            return []
        
        # 为每个候选生成embedding
        embeddings = [self._embed_candidate(c) for c in candidates]
        
        # 简单凝聚聚类
        clusters = self._agglomerative_cluster(
            candidates, embeddings, threshold=similarity_threshold
        )
        
        patterns = []
        for cluster in clusters:
            if len(cluster) < 2:  # 至少2个独立来源才构成偏好
                continue
            
            # 合并同类候选
            merged = self._merge_cluster(cluster)
            merged.confidence = min(1.0, 0.3 + 0.1 * len(cluster))
            merged.source_count = len(cluster)
            patterns.append(merged)
        
        return patterns
    
    def _validate_and_integrate(
        self,
        new_patterns: List[Preference],
        existing: List[Preference],
        llm_client: LLMClient,
    ) -> List[Preference]:
        """
        Stage V: 偏好验证 + 冲突检测
        
        新偏好可能与旧偏好冲突：
        - 旧："用户喜欢详细解释" → 新："用户觉得解释太啰嗦"
        → 冲突超驱：旧偏好标记superseded_by
        """
        results = []
        for pattern in new_patterns:
            conflicts = self._find_conflicts(pattern, existing)
            
            if conflicts:
                # 让LLM判断是否为真冲突
                for conflict in conflicts:
                    is_conflict = self._verify_conflict(
                        pattern, conflict, llm_client
                    )
                    if is_conflict:
                        conflict.superseded_by = pattern.id
                        pattern.content = f"[UPDATED] {pattern.content}"
            
            results.append(pattern)
        
        return results
```

#### 情绪上下文关联

xiaoda-agent的独有优势——偏好可以绑定情绪上下文：

```python
@dataclass
class ContextualPreference:
    """带情绪上下文的偏好"""
    preference: Preference
    active_emotions: List[str]  # ["sadness", "anxiety"]
    
    def is_active(self, current_pad: PADState) -> bool:
        """判断当前情绪是否激活此偏好"""
        if not self.active_emotions:
            return True  # 全局偏好
        
        current_emotion = classify_emotion(current_pad)
        return current_emotion in self.active_emotions
```

示例偏好：

```
{category: "communication", content: "user prefers concise replies", 
 confidence: 0.8, emotional_context: "sadness"}
→ 用户悲伤时偏好简洁回复

{category: "emotion", content: "user prefers silent companionship over advice", 
 confidence: 0.7, emotional_context: "anxiety"}  
→ 用户焦虑时偏好安静陪伴而非建议
```

---

### 4.3 冲突超驱

#### 设计目标

用户纠正后旧知识标记废弃，新知识插入，同时保留完整溯源链。

#### 数据模型

```python
# memory/conflict_supersession.py

@dataclass
class SupersessionRecord:
    """超驱记录"""
    id: str
    older_memory_id: str       # 被取代的记忆
    newer_memory_id: str       # 取代的新记忆
    reason: str                # "user_correction" | "numeric_update" | "preference_change"
    detected_at: float
    detection_method: str      # "ingest_time" | "dream_supersedes" | "explicit_correction"
    similarity_at_detection: float  # 检测时的语义相似度

@dataclass  
class MemoryRevision:
    """记忆版本链"""
    memory_id: str
    revision_number: int
    prev_content: str
    new_content: str
    reason: str
    created_at: float
```

#### 冲突检测算法

```python
class ConflictDetector:
    """
    冲突检测器，基于mazemaker _detect_conflicts和_phase_supersedes
    
    两层检测：
    1. 写入时即时检测（ingest-time）
    2. 梦境周期批量检测（dream-time）
    """
    
    # 数值token正则（源自mazemaker dream_engine.py）
    NUMERIC_TOKEN_RE = re.compile(
        r'\$\d[\d,]*(?:\.\d+)?[KkMmBb]?'
        r'|\b\d[\d,]*(?:\.\d+)?'
        r'(?:%\s*(?:GB|MB|KB|TB|GHz|MHz|kg|km|cm|lbs|hrs?|mins?))?'
        r'\b',
        re.IGNORECASE,
    )
    
    def detect_ingest_conflict(
        self,
        new_content: str,
        new_embedding: List[float],
        recent_memories: List[MemoryEntry],
        sim_threshold: float = 0.85,
    ) -> Optional[SupersessionRecord]:
        """
        写入时即时冲突检测
        
        源自mazemaker Memory.remember()的detect_conflicts逻辑：
        - 新记忆写入时与近期记忆做余弦比较
        - sim >= 0.85 且有数值差异 → 冲突超驱
        - sim >= 0.95 → 内容融合（重复）
        """
        for mem in recent_memories:
            sim = cosine_similarity(new_embedding, mem.embedding)
            
            if sim >= 0.95:
                # 极高相似度 → 内容融合而非超驱
                return self._fuse_memories(mem, new_content, sim)
            
            if sim >= sim_threshold:
                # 高相似度 → 检查是否有数值差异
                old_numerics = set(self.NUMERIC_TOKEN_RE.findall(mem.content))
                new_numerics = set(self.NUMERIC_TOKEN_RE.findall(new_content))
                
                if old_numerics and new_numerics and old_numerics != new_numerics:
                    # 数值不同 → 冲突超驱
                    return SupersessionRecord(
                        older_memory_id=mem.id,
                        newer_memory_id="",  # 待写入后填充
                        reason="numeric_update",
                        detection_method="ingest_time",
                        similarity_at_detection=sim,
                    )
                
                # 语义相似但无数值差异 → 检查是否为用户纠正
                if self._is_user_correction(new_content, mem.content):
                    return SupersessionRecord(
                        older_memory_id=mem.id,
                        newer_memory_id="",
                        reason="user_correction",
                        detection_method="ingest_time",
                        similarity_at_detection=sim,
                    )
        
        return None
    
    def detect_dream_conflicts(
        self,
        all_memories: List[MemoryEntry],
        batch_size: int = 200,
        sim_threshold: float = 0.85,
    ) -> List[SupersessionRecord]:
        """
        梦境周期批量冲突检测
        
        源自mazemaker _phase_supersedes:
        - 三切片采样避免表层陷阱
        - 向量化N×N相似度矩阵（numpy matmul）
        - 仅对含数值token的记忆做冲突判定
        """
        # 三切片采样
        sample = self._three_slice_sample(
            all_memories,
            recent_pct=0.5,
            random_old_pct=0.3,
            low_salience_pct=0.2,
            limit=batch_size,
        )
        
        # 筛选含数值token的记忆
        numeric_memories = [
            m for m in sample 
            if self.NUMERIC_TOKEN_RE.search(m.content)
        ]
        
        if len(numeric_memories) < 2:
            return []
        
        # 向量化相似度矩阵
        import numpy as np
        stacked = np.array([m.embedding for m in numeric_memories], dtype=np.float32)
        norms = np.linalg.norm(stacked, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        stacked = stacked / norms
        sim_matrix = stacked @ stacked.T  # N×N cosine sim
        
        # 扫描冲突对
        conflicts = []
        n = len(numeric_memories)
        for i in range(n):
            tokens_i = set(self.NUMERIC_TOKEN_RE.findall(numeric_memories[i].content))
            for j in range(i + 1, n):
                sim = float(sim_matrix[i, j])
                if sim < sim_threshold:
                    continue
                
                tokens_j = set(self.NUMERIC_TOKEN_RE.findall(numeric_memories[j].content))
                if tokens_i == tokens_j:
                    continue  # 数值相同 → 非冲突
                
                # 确定新旧方向
                if numeric_memories[i].timestamp <= numeric_memories[j].timestamp:
                    older, newer = numeric_memories[i], numeric_memories[j]
                else:
                    older, newer = numeric_memories[j], numeric_memories[i]
                
                conflicts.append(SupersessionRecord(
                    older_memory_id=older.id,
                    newer_memory_id=newer.id,
                    reason="numeric_update",
                    detection_method="dream_supersedes",
                    similarity_at_detection=sim,
                ))
        
        return conflicts
    
    def _is_user_correction(self, new_content: str, old_content: str) -> bool:
        """
        检测用户纠正的启发式方法
        
        情感陪伴场景特有的纠正模式：
        - "不对，其实是..." / "不是的，应该是..."
        - "我更..." / "其实我喜欢的是..."
        """
        correction_patterns = [
            r"不对[，,]?",
            r"不是[的]?[，,]?",
            r"其实[是]?",
            r"我更(喜欢|偏好|想)",
            r"应该是",
            r"纠正一下",
        ]
        for pattern in correction_patterns:
            if re.search(pattern, new_content):
                return True
        return False
    
    def apply_supersession(
        self,
        record: SupersessionRecord,
        memory_store: MemoryStore,
    ) -> None:
        """
        应用超驱：旧记忆标记废弃，新记忆插入，溯源保留
        
        关键原则（源自mazemaker）：
        - 旧记忆不删除，只标记 [SUPERSEDED]
        - 超驱关系记录在 memory_revisions 表中
        - 检索时过滤SUPERSEDED记忆，但可通过revision链回溯
        """
        # 标记旧记忆
        old_mem = memory_store.get(record.older_memory_id)
        old_mem.label = f"[SUPERSEDED] {old_mem.label}"
        old_mem.metadata["superseded_by"] = record.newer_memory_id
        old_mem.metadata["superseded_at"] = record.detected_at
        old_mem.metadata["superseded_reason"] = record.reason
        memory_store.update(old_mem)
        
        # 记录版本链
        memory_store.add_revision(MemoryRevision(
            memory_id=record.older_memory_id,
            revision_number=self._next_revision_number(record.older_memory_id),
            prev_content=old_mem.content,
            new_content=memory_store.get(record.newer_memory_id).content,
            reason=record.reason,
            created_at=record.detected_at,
        ))
```

#### 存储扩展

```sql
-- 冲突超驱相关表
CREATE TABLE IF NOT EXISTS memory_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    prev_content TEXT,
    new_content TEXT,
    reason TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memories(id)
);
CREATE INDEX idx_revisions_memory ON memory_revisions(memory_id);

CREATE TABLE IF NOT EXISTS supersession_records (
    id TEXT PRIMARY KEY,
    older_memory_id TEXT NOT NULL,
    newer_memory_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    detected_at REAL NOT NULL,
    detection_method TEXT NOT NULL,
    similarity_at_detection REAL
);
CREATE INDEX idx_supersession_older ON supersession_records(older_memory_id);
CREATE INDEX idx_supersession_newer ON supersession_records(newer_memory_id);
```

---

### 4.4 Hopfield联想回忆（简化版）

#### 设计目标

给定一条记忆，自动扩展关联记忆上下文，实现"想到A就想到B"的联想能力。

#### 为什么不用完整Hopfield网络

mazemaker的HopfieldLayer用C++ SIMD实现，迭代注意力更新pattern vector。对于xiaoda-agent的Python实现，完整Hopfield有以下问题：

1. **计算成本**: 每次迭代需要全量模式矩阵的注意力计算
2. **容量限制**: 经典Hopfield容量 ≈ 0.14N（N为维度），modern Hopfield虽无此限制但需大量存储
3. **模式表示**: 需要将自然语言记忆映射为固定维度二值/实值pattern

#### 简化版设计：Attention-Based Associative Recall

核心思想：**不存储独立的pattern矩阵，复用已有的embedding + connection graph实现联想**。

```python
# memory/associative_recall.py

@dataclass
class AssociativeRecallResult:
    """联想回忆结果"""
    seed_memory_id: str
    recalled_memories: List[RecalledMemory]  # 关联回忆列表
    total_explored: int
    convergence_iteration: int

@dataclass
class RecalledMemory:
    memory_id: str
    content: str
    association_score: float     # 联想强度
    association_path: List[str]  # 联想路径 (seed → ... → this)
    hop_depth: int               # 跳数

class SimplifiedHopfieldRecall:
    """
    简化版Hopfield联想回忆
    
    不实现完整的迭代pattern completion，而是用两跳扩散激活
    模拟联想效果。在效果上等价于beta极大的Hopfield检索
    （近似one-hot），但实现简单得多。
    
    算法：
    1. 给定seed记忆的embedding
    2. 第一跳：embedding余弦搜索 top-K
    3. 第二跳：沿connection graph扩展
    4. 融合两跳结果，按联想强度排序
    """
    
    def recall_associative(
        self,
        seed_memory_id: str,
        memory_store: MemoryStore,
        bridge_store: BridgeMemoryStore,
        k: int = 5,
        max_hops: int = 2,
        decay: float = 0.85,
        min_activation: float = 0.1,
    ) -> AssociativeRecallResult:
        """
        联想回忆主函数
        
        类似mazemaker的HopfieldLayer.retrieve()，但用
        embedding搜索+图遍历替代迭代注意力。
        """
        seed = memory_store.get(seed_memory_id)
        if not seed:
            return AssociativeRecallResult(
                seed_memory_id=seed_memory_id,
                recalled_memories=[],
                total_explored=0,
                convergence_iteration=0,
            )
        
        recalled = {}  # memory_id → RecalledMemory
        explored = 0
        
        # 第一跳：embedding余弦搜索（等价于Hopfield的top-k lookup）
        hop1_results = self._embedding_search(
            seed.embedding, memory_store, k=k*3
        )
        explored += len(hop1_results)
        
        for mem_id, sim in hop1_results:
            if mem_id == seed_memory_id:
                continue
            recalled[mem_id] = RecalledMemory(
                memory_id=mem_id,
                content=memory_store.get(mem_id).content,
                association_score=sim,
                association_path=[seed_memory_id, mem_id],
                hop_depth=1,
            )
        
        # 第二跳：沿connection graph扩展（等价于Hopfield的迭代更新）
        if max_hops >= 2:
            hop1_ids = [r for r in hop1_results[:k]]
            hop2_frontier = {}
            
            for mem_id, sim in hop1_ids:
                # 直接连接
                connections = memory_store.get_connections(mem_id)
                for conn in connections:
                    if conn.target_id == seed_memory_id:
                        continue
                    if conn.target_id in recalled:
                        continue
                    
                    propagated = sim * conn.weight * decay
                    if propagated < min_activation:
                        continue
                    
                    if conn.target_id not in hop2_frontier or \
                       propagated > hop2_frontier[conn.target_id][0]:
                        hop2_frontier[conn.target_id] = (
                            propagated, mem_id
                        )
                
                # 桥接连接
                bridges = bridge_store.get_bridges(mem_id)
                for bridge in bridges:
                    other_id = (bridge.target_memory_id 
                               if bridge.source_memory_id == mem_id 
                               else bridge.source_memory_id)
                    if other_id == seed_memory_id or other_id in recalled:
                        continue
                    
                    propagated = sim * bridge.weight * decay
                    if propagated < min_activation:
                        continue
                    
                    if other_id not in hop2_frontier or \
                       propagated > hop2_frontier[other_id][0]:
                        hop2_frontier[other_id] = (propagated, mem_id)
            
            explored += len(hop2_frontier)
            
            for other_id, (score, via_id) in hop2_frontier.items():
                mem = memory_store.get(other_id)
                if mem and not mem.label.startswith("[SUPERSEDED]"):
                    recalled[other_id] = RecalledMemory(
                        memory_id=other_id,
                        content=mem.content,
                        association_score=score,
                        association_path=[seed_memory_id, via_id, other_id],
                        hop_depth=2,
                    )
        
        # 按联想强度排序
        sorted_recall = sorted(
            recalled.values(), 
            key=lambda r: r.association_score, 
            reverse=True
        )[:k]
        
        return AssociativeRecallResult(
            seed_memory_id=seed_memory_id,
            recalled_memories=sorted_recall,
            total_explored=explored,
            convergence_iteration=max_hops,
        )
    
    def _embedding_search(
        self, 
        query_embedding: List[float], 
        memory_store: MemoryStore, 
        k: int = 15,
    ) -> List[Tuple[str, float]]:
        """余弦相似度搜索"""
        results = []
        for mem in memory_store.iter_all():
            if mem.embedding:
                sim = cosine_similarity(query_embedding, mem.embedding)
                results.append((mem.id, sim))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:k]
```

#### 与mazemaker Hopfield的对应关系

| mazemaker Hopfield | xiaoda-agent 简化版 |
|---------------------|---------------------|
| `store(pattern)` | 复用已有的memory_store embedding |
| `retrieve(cue, max_iter=10)` | `recall_associative(seed, max_hops=2)` |
| 迭代注意力更新pattern vector | 两跳扩散激活（embedding搜索+图遍历） |
| beta=20 锐化注意力 | 余弦搜索天然锐化（top-k = 隐式softmax） |
| confidence = max_cosine_to_stored | association_score = 衰减传播强度 |
| C++ SIMD 加速 | Python numpy 向量化 |

---

### 4.5 认知整合流程

#### 设计目标

将xiaoda-agent的记忆系统从"两层存储"升级为"三阶段转化"，类似mazemaker的consolidation。

#### 三阶段整合

```python
# core/cognitive_consolidation.py

class CognitiveConsolidationEngine:
    """
    认知整合引擎
    
    三阶段转化：
    Stage 1: Episodic → Semantic (情景记忆 → 语义记忆)
    Stage 2: Semantic → Abstraction (语义记忆 → 抽象知识)
    Stage 3: Abstraction → Preference (抽象知识 → 偏好结构)
    """
    
    def consolidate(
        self,
        episodic_store: EpisodicMemoryStore,
        semantic_store: SemanticMemoryStore,
        preference_engine: PreferenceDiscoveryEngine,
        emotional_state: Optional[PADState] = None,
        batch_size: int = 64,
    ) -> ConsolidationResult:
        """
        执行一轮认知整合
        
        等价于mazemaker MemoryManager.consolidate()的完整Python重写，
        加入了情感维度。
        """
        result = ConsolidationResult()
        
        # Stage 1: Episodic → Semantic
        stage1 = self._stage_episodic_to_semantic(
            episodic_store, semantic_store, emotional_state, batch_size
        )
        result.transferred = stage1.transferred
        result.connections_found = stage1.connections_found
        
        # Stage 2: Semantic → Abstraction
        stage2 = self._stage_semantic_to_abstraction(
            semantic_store, batch_size=20
        )
        result.abstractions_created = stage2.abstractions_created
        
        # Stage 3: Abstraction → Preference
        stage3 = self._stage_abstraction_to_preference(
            semantic_store, preference_engine
        )
        result.preferences_discovered = stage3.preferences_discovered
        
        return result
    
    def _stage_episodic_to_semantic(
        self,
        episodic: EpisodicMemoryStore,
        semantic: SemanticMemoryStore,
        emotional_state: Optional[PADState],
        batch_size: int,
    ) -> Stage1Result:
        """
        Stage 1: 情景记忆 → 语义记忆
        
        源自mazemaker consolidation.cpp的MemoryManager::consolidate():
        
        1. 获取consolidation候选（按access_count降序、timestamp升序）
        2. 自注意力扫描发现关联
        3. 高salience或高access_count的记忆提升到semantic层
        4. 更新连接图
        5. 从episodic层移除已提升的记忆
        6. 重建semantic clusters
        """
        # 1. 获取候选
        candidates = episodic.candidates_for_consolidation(batch_size)
        if not candidates:
            return Stage1Result(transferred=0, connections_found=0)
        
        # 2. 自注意力扫描（发现记忆间关联）
        connections = self._self_attention_sweep(candidates)
        
        # 3. 提升高价值记忆
        transferred = 0
        transferred_ids = []
        now = time.time()
        
        for entry in candidates:
            # 计算salience（含情绪权重）
            salience = compute_salience_with_emotion(
                entry, now, emotional_state
            )
            
            # 提升条件：salience > 0.3 或 access_count >= 3
            if salience > 0.3 or entry.access_count >= 3:
                # 存入semantic层
                semantic_entry = SemanticMemoryEntry(
                    id=generate_id(),
                    embedding=entry.embedding,
                    label=entry.label,
                    content=entry.content,
                    source="consolidated",
                    salience=salience,
                    decay_factor=1.0,
                    original_episodic_id=entry.id,
                    consolidated_at=now,
                )
                semantic.store(semantic_entry)
                transferred_ids.append(entry.id)
                transferred += 1
        
        # 4. 更新连接图
        for id_a, id_b, strength in connections:
            semantic.add_connection(id_a, id_b, strength)
        
        # 5. 从episodic层移除
        for mid in transferred_ids:
            episodic.remove(mid)
        
        # 6. 重建semantic clusters
        if transferred > 0:
            semantic.rebuild_clusters()
        
        return Stage1Result(
            transferred=transferred,
            connections_found=len(connections),
        )
    
    def _self_attention_sweep(
        self,
        candidates: List[MemoryEntry],
        strength_threshold: float = 0.5,
    ) -> List[Tuple[str, str, float]]:
        """
        自注意力扫描：发现记忆间关联
        
        源自mazemaker consolidation.cpp的self_attention_sweep():
        
        对候选记忆做两两connection_strength计算，
        返回强度超过阈值的关联对。
        """
        import numpy as np
        
        connections = []
        n = len(candidates)
        
        if n < 2:
            return connections
        
        # 向量化相似度计算
        embeddings = np.array([c.embedding for c in candidates])
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        embeddings = embeddings / norms
        sim_matrix = embeddings @ embeddings.T
        
        for i in range(n):
            for j in range(i + 1, n):
                # connection_strength = 0.5*cos_sim + 0.3*temporal_boost + link_boost
                cos_sim = float(sim_matrix[i, j])
                
                # 时间邻近性加成（1分钟衰减，源自mazemaker）
                time_diff = abs(candidates[i].timestamp - candidates[j].timestamp)
                temporal_boost = math.exp(-time_diff / 60.0)
                
                # 共享链接加成
                link_boost = self._compute_link_boost(
                    candidates[i], candidates[j]
                )
                
                strength = max(0.0, 
                    cos_sim * 0.5 + temporal_boost * 0.3 + link_boost
                )
                
                if strength >= strength_threshold:
                    connections.append(
                        (candidates[i].id, candidates[j].id, strength)
                    )
        
        # 按强度降序排列
        connections.sort(key=lambda x: x[2], reverse=True)
        return connections
    
    def _stage_semantic_to_abstraction(
        self,
        semantic: SemanticMemoryStore,
        batch_size: int = 20,
        min_cluster_size: int = 3,
    ) -> Stage2Result:
        """
        Stage 2: 语义记忆 → 抽象知识
        
        源自mazemaker Insight阶段的community detection：
        
        1. 对semantic clusters做社区检测
        2. 对每个足够大的社区，提取代表内容
        3. 生成抽象知识记忆（derived:abstraction）
        """
        clusters = semantic.get_clusters()
        abstractions = 0
        
        for cluster in clusters:
            if len(cluster.member_ids) < min_cluster_size:
                continue
            
            # 提取最中心成员作为代表
            representative = self._extract_central_member(
                cluster, semantic
            )
            
            # 生成抽象知识
            abstraction = SemanticMemoryEntry(
                id=generate_id(),
                embedding=cluster.centroid,
                label=f"derived:abstraction",
                content=representative.content[:240],
                source="abstraction",
                salience=max(m.salience for m in cluster.members),
                decay_factor=1.0,
            )
            semantic.store(abstraction)
            abstractions += 1
        
        return Stage2Result(abstractions_created=abstractions)
    
    def _stage_abstraction_to_preference(
        self,
        semantic: SemanticMemoryStore,
        preference_engine: PreferenceDiscoveryEngine,
    ) -> Stage3Result:
        """
        Stage 3: 抽象知识 → 偏好结构
        
        xiaoda-agent独有阶段：将抽象知识与情感记忆交叉，
        推断用户的潜在偏好。
        """
        abstractions = semantic.get_by_source("abstraction")
        emotional_memories = semantic.get_emotional_memories()
        existing_prefs = preference_engine.get_all_preferences()
        
        new_prefs = preference_engine.discover_preferences(
            session_memories=abstractions,
            emotional_memories=emotional_memories,
            existing_preferences=existing_prefs,
        )
        
        return Stage3Result(preferences_discovered=len(new_prefs))
```

---

### 4.6 Salience评分替代简单Importance

#### 当前问题

xiaoda-agent v0.5.03使用简单的importance打分（通常是LLM生成的1-5整数评分），存在：
- 主观性强，不同LLM打分不一致
- 无时间衰减（一条5分记忆永远不会"变旧"）
- 无访问频次信号（从未被检索的记忆和频繁检索的记忆同权）

#### 新Salience公式

```
salience = α × recency_score 
         + β × frequency_score 
         + γ × emotion_weight

其中:
  α = 0.5    (时近性权重，比mazemaker的0.6略低，为emotion让出空间)
  β = 0.3    (频次权重，比mazemaker的0.4略低)
  γ = 0.2    (情绪权重，xiaoda-agent独有)
  
  recency_score = exp(-recency_seconds / τ)
                  τ = 3600秒 (1小时半衰期，与mazemaker一致)
                  
  frequency_score = min(1.0, log1p(access_count) / 10)
                    (对数归一化，与mazemaker一致)
                    
  emotion_weight  = pad_intensity × emotion_recency
                    pad_intensity = sqrt(P² + A² + D²) / sqrt(3)
                    emotion_recency = exp(-emotion_age / 7200)
```

#### 伪代码

```python
def compute_salience_with_emotion(
    entry: MemoryEntry,
    now: float,
    current_emotional_state: Optional[PADState] = None,
    alpha: float = 0.5,
    beta: float = 0.3,
    gamma: float = 0.2,
    recency_tau: float = 3600.0,
    emotion_tau: float = 7200.0,
) -> float:
    """
    带情绪权重的Salience计算
    
    源自mazemaker compute_salience()，扩展了emotion维度。
    
    mazemaker原版:
        salience = 0.6 * recency_score + 0.4 * frequency_score
    
    xiaoda-agent扩展:
        salience = 0.5 * recency_score + 0.3 * frequency_score + 0.2 * emotion_weight
    
    emotion_weight的设计逻辑：
    - 情绪强烈时创建的记忆更重要（pad_intensity高 → 记忆深刻）
    - 近期有情绪波动的记忆更重要（emotion_recency衰减）
    - 当前情绪与记忆情绪共振时增强（可选：current_emotional_state匹配）
    """
    # 时近性评分
    recency_seconds = now - entry.last_accessed
    recency_score = math.exp(-recency_seconds / recency_tau)
    
    # 频次评分
    frequency_score = min(1.0, math.log1p(entry.access_count) / 10.0)
    
    # 情绪权重
    emotion_weight = 0.0
    if hasattr(entry, 'emotion_pad') and entry.emotion_pad is not None:
        p, a, d = entry.emotion_pad
        # PAD强度：三维向量的模，归一化到[0,1]
        pad_intensity = math.sqrt(p**2 + a**2 + d**2) / math.sqrt(3.0)
        
        # 情绪时近性：情绪记忆随时间衰减（2小时半衰期，比认知慢）
        emotion_age = now - getattr(entry, 'emotion_timestamp', entry.timestamp)
        emotion_recency = math.exp(-emotion_age / emotion_tau)
        
        emotion_weight = pad_intensity * emotion_recency
        
        # 可选：当前情绪共振加成
        if current_emotional_state is not None:
            resonance = _compute_emotion_resonance(
                entry.emotion_pad, current_emotional_state
            )
            emotion_weight *= (1.0 + 0.3 * resonance)  # 最多+30%
    
    return alpha * recency_score + beta * frequency_score + gamma * emotion_weight


def _compute_emotion_resonance(
    memory_pad: Tuple[float, float, float],
    current_pad: PADState,
) -> float:
    """
    计算记忆情绪与当前情绪的共振度
    
    共振 = 1 - normalized_distance
    当记忆的情绪PAD与当前情绪PAD越接近，共振越强。
    """
    mp = np.array(memory_pad)
    cp = np.array([current_pad.pleasure, current_pad.arousal, current_pad.dominance])
    distance = np.linalg.norm(mp - cp)
    max_distance = 2 * math.sqrt(3)  # PAD各维度[-1,1]，最大距离
    return 1.0 - distance / max_distance
```

#### Salience衰减（梦境周期中）

```python
def apply_salience_decay(
    semantic_store: SemanticMemoryStore,
    decay_factor: float = 0.999,  # 与mazemaker一致
) -> int:
    """
    对所有语义记忆的salience应用指数衰减
    
    源自mazemaker SemanticMemory::decay_all():
    每个dream cycle对所有记忆的salience乘以0.999
    → 约1000个cycle后salience降至1/e ≈ 0.37
    → 在默认~5min/cycle下约83小时（3.5天）
    
    但被recall的记忆会被"想起来"：
    - 每次recall → access_count += 1
    - access_count增加 → frequency_score增加
    - → salience回升
    
    这是"用进废退"的数学表达。
    """
    count = 0
    for entry in semantic_store.iter_all():
        entry.salience *= decay_factor
        entry.decay_factor *= decay_factor
        semantic_store.update(entry)
        count += 1
    return count
```

---

### 4.7 情绪加权认知

#### 设计目标

让Agent在用户悲伤时更容易回忆悲伤相关记忆——情绪调制的记忆检索。

#### 核心算法：情绪加权检索

```python
# emotion/emotion_weighted_recall.py

class EmotionWeightedRecall:
    """
    情绪加权检索
    
    核心思想：
    mazemaker的检索是多通道融合（semantic + BM25 + entity + temporal + PPR）
    xiaoda-agent在语义通道上增加情绪调制：
    
    final_score = semantic_score × (1 + λ × emotion_affinity)
    
    其中 emotion_affinity 是记忆情绪与当前情绪的共振度。
    """
    
    def __init__(
        self,
        emotion_weight_lambda: float = 0.3,  # 情绪调制强度
        pad_model: PADModel = None,
    ):
        self.lambda_ = emotion_weight_lambda
        self.pad_model = pad_model
    
    def recall(
        self,
        query: str,
        query_embedding: List[float],
        current_pad: PADState,
        memory_store: SemanticMemoryStore,
        bridge_store: BridgeMemoryStore,
        k: int = 5,
        use_bridges: bool = True,
    ) -> List[WeightedRecallResult]:
        """
        情绪加权检索主函数
        
        步骤：
        1. 标准语义检索
        2. 情绪加权调制
        3. 桥接记忆扩展
        4. 联想回忆扩展
        5. 去重排序
        """
        # 1. 标准语义检索
        semantic_results = self._semantic_search(
            query_embedding, memory_store, k=k*3
        )
        
        # 2. 情绪加权调制
        weighted_results = []
        for mem_id, semantic_score in semantic_results:
            mem = memory_store.get(mem_id)
            
            # 计算情绪亲和度
            emotion_affinity = self._compute_emotion_affinity(
                mem, current_pad
            )
            
            # 情绪调制
            # 当用户悲伤时，悲伤记忆的得分提升最高30%
            # 当用户开心时，开心记忆的得分提升
            # 情绪不相关的记忆得分不变
            final_score = semantic_score * (1.0 + self.lambda_ * emotion_affinity)
            
            weighted_results.append(WeightedRecallResult(
                memory_id=mem_id,
                content=mem.content,
                semantic_score=semantic_score,
                emotion_affinity=emotion_affinity,
                final_score=final_score,
                source="semantic",
            ))
        
        # 3. 桥接记忆扩展
        if use_bridges:
            bridge_results = self._bridge_expansion(
                weighted_results[:k], bridge_store, memory_store, current_pad
            )
            weighted_results.extend(bridge_results)
        
        # 4. 去重排序
        deduped = self._deduplicate(weighted_results)
        deduped.sort(key=lambda r: r.final_score, reverse=True)
        
        return deduped[:k]
    
    def _compute_emotion_affinity(
        self,
        memory: MemoryEntry,
        current_pad: PADState,
    ) -> float:
        """
        计算记忆与当前情绪的亲和度
        
        亲和度 ∈ [-0.5, 1.0]:
        - 1.0: 记忆情绪与当前情绪完全匹配（强共振）
        - 0.0: 记忆无情绪标签或情绪中性
        - -0.5: 记忆情绪与当前情绪完全对立（如记忆是开心的但用户悲伤）
        """
        if not hasattr(memory, 'emotion_pad') or memory.emotion_pad is None:
            return 0.0  # 无情绪标签的记忆不受调制
        
        mem_p, mem_a, mem_d = memory.emotion_pad
        cur_p, cur_a, cur_d = current_pad.pleasure, current_pad.arousal, current_pad.dominance
        
        # Pleasure维度共振（最重要：快乐-悲伤对立）
        pleasure_affinity = 1.0 - abs(mem_p - cur_p) / 2.0  # [0, 1]
        
        # Arousal维度共振（次重要：兴奋-平静协调）
        arousal_affinity = 1.0 - abs(mem_a - cur_a) / 2.0   # [0, 1]
        
        # Dominance维度（参考）
        dominance_affinity = 1.0 - abs(mem_d - cur_d) / 2.0
        
        # 加权平均（pleasure权重最高，因为是情感陪伴核心维度）
        affinity = (0.5 * pleasure_affinity + 
                    0.3 * arousal_affinity + 
                    0.2 * dominance_affinity)
        
        # 映射到 [-0.5, 1.0]：完全对立 = -0.5，完全匹配 = 1.0
        return affinity * 1.5 - 0.5
    
    def _bridge_expansion(
        self,
        top_results: List[WeightedRecallResult],
        bridge_store: BridgeMemoryStore,
        memory_store: SemanticMemoryStore,
        current_pad: PADState,
    ) -> List[WeightedRecallResult]:
        """
        通过桥接记忆扩展检索结果
        
        对于top结果中每条记忆，查找其桥接记忆，
        并加入结果集（带衰减）。
        """
        bridge_results = []
        seen_ids = {r.memory_id for r in top_results}
        
        for result in top_results:
            bridges = bridge_store.get_bridges(result.memory_id)
            for bridge in bridges:
                other_id = (bridge.target_memory_id 
                           if bridge.source_memory_id == result.memory_id 
                           else bridge.source_memory_id)
                
                if other_id in seen_ids:
                    continue
                seen_ids.add(other_id)
                
                mem = memory_store.get(other_id)
                if not mem or mem.label.startswith("[SUPERSEDED]"):
                    continue
                
                # 桥接记忆的得分 = 源记忆得分 × 桥接权重 × 情绪调制
                emotion_affinity = self._compute_emotion_affinity(mem, current_pad)
                bridge_score = result.final_score * bridge.weight * 0.5
                
                # 如果桥接是情感桥接，额外加权
                if bridge.bridge_type == "emotional":
                    bridge_score *= 1.2  # 情感桥接更可信
                
                bridge_results.append(WeightedRecallResult(
                    memory_id=other_id,
                    content=mem.content,
                    semantic_score=bridge_score,
                    emotion_affinity=emotion_affinity,
                    final_score=bridge_score * (1.0 + self.lambda_ * emotion_affinity),
                    source=f"bridge:{bridge.bridge_type}",
                ))
        
        return bridge_results
```

#### 情绪-记忆写入耦合

```python
def remember_with_emotion(
    content: str,
    current_pad: PADState,
    memory_store: EpisodicMemoryStore,
    embedder: Embedder,
) -> str:
    """
    带情绪标签的记忆写入
    
    每条记忆在写入时记录当前PAD状态，
    作为后续情绪加权检索的依据。
    """
    embedding = embedder.embed(content)
    
    # 量化情绪强度
    pad_intensity = math.sqrt(
        current_pad.pleasure**2 + 
        current_pad.arousal**2 + 
        current_pad.dominance**2
    ) / math.sqrt(3.0)
    
    entry = MemoryEntry(
        id=generate_id(),
        embedding=embedding,
        content=content,
        emotion_pad=(current_pad.pleasure, current_pad.arousal, current_pad.dominance),
        emotion_timestamp=time.time(),
        emotion_intensity=pad_intensity,
        salience=compute_salience_with_emotion(
            entry, time.time(), current_emotional_state=current_pad
        ),
    )
    
    return memory_store.write(entry)
```

---

## 5. 实施步骤

### Phase 1: 基础设施（2周）

**目标**: 建立三层记忆架构和salience评分

| 任务 | 文件 | 依赖 |
|------|------|------|
| 定义三层记忆数据模型 | `memory/entry_types.py` | - |
| 实现EpisodicMemoryStore (FIFO) | `memory/episodic_store.py` | entry_types |
| 实现SemanticMemoryStore (持久+聚类) | `memory/semantic_store.py` | entry_types |
| 实现salience评分（含emotion权重） | `memory/salience.py` | entry_types, pad_model |
| SQLite schema migration | `migrations/001_cognitive.sql` | - |

**验收标准**:
- [ ] 新记忆写入Episodic层，FIFO淘汰正常
- [ ] salience公式输出在[0,1]区间，情绪权重生效
- [ ] Semantic层支持cluster重建

### Phase 2: 认知整合（2周）

**目标**: 实现Episodic → Semantic → Abstraction三阶段转化

| 任务 | 文件 | 依赖 |
|------|------|------|
| 实现self_attention_sweep | `core/cognitive_consolidation.py` | Phase 1 |
| 实现Stage 1: Episodic → Semantic | `core/cognitive_consolidation.py` | Phase 1 |
| 实现Stage 2: Semantic → Abstraction | `core/cognitive_consolidation.py` | Phase 1 |
| 整合到dream_consolidation | `core/dream_consolidation.py` | cognitive_consolidation |
| 三切片采样 | `core/dream_sampling.py` | - |

**验收标准**:
- [ ] episodic层高salience记忆正确转移到semantic层
- [ ] 自注意力扫描发现关联并建立连接
- [ ] dream cycle完成后episodic层已清理已转移记忆

### Phase 3: 冲突超驱（1.5周）

**目标**: 用户纠正和新信息更新时旧知识标记废弃

| 任务 | 文件 | 依赖 |
|------|------|------|
| 实现ConflictDetector (ingest-time) | `memory/conflict_supersession.py` | Phase 1 |
| 实现ConflictDetector (dream-time) | `memory/conflict_supersession.py` | Phase 2 |
| 实现SupersessionRecord存储 | `memory/conflict_supersession.py` | - |
| Memory revision chain | `memory/revision_chain.py` | - |
| SQLite schema for revisions | `migrations/002_supersession.sql` | - |
| 检索时过滤SUPERSEDED记忆 | `memory/semantic_store.py` | - |

**验收标准**:
- [ ] 写入时检测到数值冲突并标记SUPERSEDED
- [ ] 用户纠正短语触发超驱
- [ ] 梦境周期批量发现跨会话冲突
- [ ] 版本链可追溯

### Phase 4: 桥接记忆（1.5周）

**目标**: 跨会话连接语义/情绪相关记忆

| 任务 | 文件 | 依赖 |
|------|------|------|
| BridgeMemory数据模型 | `memory/bridge_memory.py` | Phase 1 |
| REM桥接发现算法 | `memory/bridge_memory.py` | Phase 2 |
| 情感桥接扩展 | `memory/bridge_memory.py` | pad_model |
| SQLite schema for bridges | `migrations/003_bridges.sql` | - |
| 桥接记忆参与检索 | `emotion/emotion_weighted_recall.py` | Phase 6 |

**验收标准**:
- [ ] 孤立记忆通过桥接连接到相关记忆
- [ ] 跨会话桥接正确标记cross_session=True
- [ ] 情感桥接在语义相似度低时仍能发现关联

### Phase 5: 联想回忆（1周）

**目标**: 给定一条记忆自动扩展关联上下文

| 任务 | 文件 | 依赖 |
|------|------|------|
| SimplifiedHopfieldRecall实现 | `memory/associative_recall.py` | Phase 1, 4 |
| 两跳扩散激活 | `memory/associative_recall.py` | Phase 4 |
| 整合到recall流程 | `memory/recall.py` | - |

**验收标准**:
- [ ] 给定seed记忆，返回5条关联记忆
- [ ] 两跳扩展正常工作
- [ ] 桥接记忆参与第二跳扩展

### Phase 6: 情绪加权认知（1.5周）

**目标**: 情绪系统与记忆系统深度耦合

| 任务 | 文件 | 依赖 |
|------|------|------|
| EmotionWeightedRecall实现 | `emotion/emotion_weighted_recall.py` | Phase 1, 5 |
| 情绪-记忆写入耦合 | `emotion/emotion_weighted_recall.py` | pad_model |
| 桥接扩展+情绪调制 | `emotion/emotion_weighted_recall.py` | Phase 4 |
| 情绪共振计算 | `emotion/emotion_weighted_recall.py` | pad_model |

**验收标准**:
- [ ] 悲伤状态下悲伤相关记忆检索排名提升
- [ ] 情绪写入标签正确持久化
- [ ] 情绪共振加成不超过30%

### Phase 7: 偏好发现（2周）

**目标**: 从交互历史中推断用户潜在偏好

| 任务 | 文件 | 依赖 |
|------|------|------|
| Preference数据模型 | `memory/preference_discovery.py` | Phase 1 |
| Stage C: 偏好候选提取 (LLM) | `memory/preference_discovery.py` | llm_client |
| Stage D: 偏好聚类发现 | `memory/preference_discovery.py` | Phase 2 |
| Stage V: 偏好验证+冲突检测 | `memory/preference_discovery.py` | Phase 3 |
| 情绪上下文关联 | `memory/preference_discovery.py` | Phase 6 |
| 偏好驱动回复风格调整 | `emotion/preference_adapter.py` | - |

**验收标准**:
- [ ] 从3+次类似交互中提取出偏好
- [ ] 偏好带情绪上下文标签
- [ ] 冲突偏好被正确超驱
- [ ] 偏好影响回复风格（如"悲伤时简洁回复"）

### 总时间线

```
Week 1-2:  Phase 1 (基础设施)
Week 3-4:  Phase 2 (认知整合)
Week 5-6:  Phase 3 (冲突超驱) + Phase 4 (桥接记忆)
Week 7:    Phase 5 (联想回忆)
Week 8-9:  Phase 6 (情绪加权) + Phase 7 (偏好发现)
```

---

## 6. 预期效果

### 6.1 定量预期

| 指标 | v0.5.03 | v0.6.0 预期 | 提升来源 |
|------|---------|-------------|----------|
| 跨会话记忆连接率 | ~0% | >60% | 桥接记忆 |
| 冲突记忆混淆率 | 高 | <5% | 冲突超驱 |
| 情境相关检索精度 | 基线 | +20~30% | 情绪加权+salience |
| 偏好识别数 | 0 | 5-15个/月 | 偏好发现 |
| 记忆生命周期 | 单一衰减 | 三阶段转化 | 认知整合 |
| 联想扩展率 | 0% | 3-5条/种子 | 联想回忆 |

### 6.2 定性预期

1. **"记住你是谁"**: 通过桥接记忆，Agent能跨会话保持对用户的连贯认知
2. **"知道你变了"**: 冲突超驱让Agent跟随用户观点变化，而非固守旧知识
3. **"懂你心情"**: 情绪加权让Agent的回复与用户情绪状态共振
4. **"知道你喜欢什么"**: 偏好发现让Agent从被动响应到主动适配
5. **"想到你就想到了"**: 联想回忆让Agent的对话有深度而非浅层检索

### 6.3 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 情绪加权过度，导致"情绪气泡" | 中 | 检索多样性下降 | lambda=0.3硬上限 + 多样性采样 |
| 偏好发现LLM幻觉 | 中 | 错误偏好影响回复 | 10% yield + 2+来源确认 |
| 桥接发现计算成本 | 低 | dream cycle变长 | 批量处理 + 限制孤立记忆数 |
| 冲突超驱误判 | 低 | 有效记忆被标记SUPERSEDED | 版本链可回溯 + sim阈值0.85 |
| 三层记忆存储膨胀 | 中 | 存储压力 | FIFO淘汰 + cluster合并 + 定期prune |

---

## 7. 不采纳的部分

### 7.1 C++ SIMD引擎

**mazemaker实现**: `src/simd/simd_engine.cpp` 使用AVX2/SSE指令加速余弦相似度和向量运算。

**不采纳原因**:
- xiaoda-agent是纯Python项目，引入C++构建链会显著增加部署复杂度
- Python numpy向量化操作对10k级记忆规模已足够（<100ms）
- 若后续性能不足，可考虑PyArrow或Cython而非完整C++重写

**替代方案**: numpy `matmul` 做批量余弦计算，与mazemaker的向量化思路一致但用Python生态。

### 7.2 完整Hopfield网络

**mazemaker实现**: `src/memory/hopfield.cpp` 实现了完整的Modern Hopfield Network，含：
- 迭代注意力更新（最多10轮）
- 模式补全（pattern completion）
- 批量检索（多线程并行）
- salience衰减和淘汰

**不采纳原因**:
- 情感陪伴Agent的记忆规模远小于mazemaker的193k（通常<10k）
- 迭代注意力收敛需要5-10轮全量扫描，Python实现太慢
- 两跳扩散激活已覆盖"联想到相关记忆"的核心需求

**替代方案**: SimplifiedHopfieldRecall（见4.4节），用embedding搜索+图遍历替代迭代注意力。

### 7.3 知识图谱文件系统接口

**mazemaker实现**: Agent可以"走"图而不只是"搜"图——通过spreading activation和Personalized PageRank在知识图谱上做图遍历。

**不采纳原因**:
- 情感陪伴Agent的核心场景是检索而非图探索
- PPR需要GPU加速（`torch.sparse.mm`），xiaoda-agent本地部署无GPU保证
- BFS扩散激活已足够覆盖情感陪伴的联想需求

**替代方案**: SimplifiedHopfieldRecall中的两跳BFS扩散，轻量且确定性。

### 7.4 LSTM访问模式预测器

**mazemaker实现**: 2层LSTM预测下次访问的embedding方向，用于kNN重排序。

**不采纳原因**:
- 训练数据不足：情感陪伴Agent的交互频率远低于开发工具Agent
- LSTM权重需要持续训练和持久化，增加系统复杂度
- 收益不确定：mazemaker自己也在lean模式中弃用了多通道融合

**替代方案**: salience评分中的recency+frequency维度已隐式捕捉访问模式。

### 7.5 ColBERT重排序

**mazemaker实现**: ColBERT@1.5 late-interaction reranking，每条记忆缓存32个token embedding。

**不采纳原因**:
- 需要额外的ColBERT模型推理（~200ms/query）
- 情感陪伴场景的query通常较短，ColBERT的细粒度匹配优势不明显
- 增加每条记忆64KB存储（32 tokens × 1024 dim × 4 bytes）

**替代方案**: 语义搜索 + 情绪加权已足够，如需精度提升可后接轻量LLM rerank。

### 7.6 GPU Recall Engine

**mazemaker实现**: `gpu_recall.py` 将所有embedding加载到CUDA tensor，用`torch.matmul`做批量余弦。

**不采纳原因**:
- xiaoda-agent定位本地部署，不保证GPU
- 记忆规模(<10k)下CPU numpy已够快

**替代方案**: numpy matmul，必要时可用FAISS CPU索引。

---

## 附录A: mazemaker核心公式速查

| 公式 | 来源 | 用途 |
|------|------|------|
| `R(t) = R₀ × exp(-t/τ)` | consolidation.cpp | 指数遗忘 |
| `salience = 0.6×recency + 0.4×frequency` | consolidation.cpp | 记忆重要性 |
| `recency_score = exp(-recency_sec / 3600)` | consolidation.cpp | 时近性评分 |
| `frequency_score = log1p(access_count) / 10` | consolidation.cpp | 频次评分 |
| `conn_strength = 0.5×cos_sim + 0.3×temporal + link_boost` | consolidation.cpp | 连接强度 |
| `temporal_boost = exp(-time_diff / 60)` | consolidation.cpp | 时间邻近性 |
| `link_boost = min(0.3, shared_links × 0.1)` | consolidation.cpp | 共享链接加成 |
| `xi_new = Σⱼ softmax(β × sim(xi, xⱼ)) × xⱼ` | hopfield.cpp | Hopfield迭代更新 |
| `bridge_weight = similarity × 0.3` | dream_engine.py | 桥接权重 |
| `pred_score = 0.3×CN + 0.4×AA + 0.3×emb_sim` | knowledge_graph.cpp | 链接预测 |

## 附录B: xiaoda-agent扩展公式

| 公式 | 用途 | 与mazemaker差异 |
|------|------|-----------------|
| `salience = 0.5×recency + 0.3×frequency + 0.2×emotion` | 情绪Salience | +emotion维度 |
| `emotion_weight = pad_intensity × emotion_recency` | 情绪权重 | 新增 |
| `final_score = semantic × (1 + λ × affinity)` | 情绪加权检索 | 新增 |
| `affinity = 0.5×pleasure + 0.3×arousal + 0.2×dominance` | 情绪亲和度 | 新增 |
| `bridge_combined = 0.7×semantic + 0.3×emotion` | 情感桥接融合 | 新增 |

---

*文档结束*
