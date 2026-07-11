# 扩散激活记忆系统优化设计：基于 mind 概念图机制

> **版本**: v1.0
> **日期**: 2026-07-10
> **目标**: xiaoda-agent 记忆系统引入扩散激活检索 + 概念图 + confirm/correct
> **参考**: [Da7-Tech/mind](https://github.com/Da7-Tech/mind) v6.2.8
> **范围**: Phase 1 (概念图) + Phase 2 (扩散激活) + Phase 3 (confirm/correct + Boost)

---

## 1. 设计决策

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 实施范围 | Phase 1+2+3 | 最高 ROI：联想召回 + 核心记忆保护 |
| 嵌入策略 | 保留现有模型嵌入 | sqlite-vec + OpenAI 兼容嵌入语义精度优于 mind 的 hash 嵌入 |
| 存储方式 | SQLite 表 | 与现有架构一致，SQLite 事务保证并发安全 |
| 迁移策略 | 双写+懒迁移 | 新记忆同时写入两表，旧记忆按需迁移，平滑过渡 |
| 检索融合 | 扩散激活作为第五路 RRF 通道 | 保留现有 FTS/Vector/KG/ChildChunk 能力，增加联想能力 |

---

## 2. 架构概览

### 2.1 现有架构

```
Query → [FTS5] + [Vector] + [KG] + [ChildChunk] → RRF融合 → Reranker → 结果
```

### 2.2 优化后架构

```
Query → [FTS5] + [Vector] + [KG] + [ChildChunk] + [扩散激活] → RRF融合 → Reranker → 结果
                                                    ↑
                    concept_nodes + concept_edges (SQLite)
                    ├── 直接命中通道 (IDF + key重叠 + weight_bias)
                    ├── 扩散激活通道 (沿边传播, 3跳)
                    └── RRF融合 → 模式补全 → 语义重排 → 模式分离

  remember → episodic_memories (保留) + concept_nodes (双写)
  confirm  → concept_nodes.weight += 0.15, concept_edges.weight += 0.25
  correct  → 旧节点 valid_to设置, 新节点创建, supersedes边, 连接迁移
```

### 2.3 新增/修改模块

| 模块 | 类型 | 职责 |
|------|------|------|
| `db/db_concept.py` | 新增 | concept_nodes/edges CRUD |
| `memory/concept_graph.py` | 新增 | 概念图管理（Hippocampus），节点/边管理 + auto_link |
| `memory/spreading_activation.py` | 新增 | 扩散激活检索引擎 |
| `memory/confirm_correct.py` | 新增 | confirm/correct 机制 |
| `memory/key_extractor.py` | 新增 | Key 提取器（jieba + 停用词 + 同义词归一化） |
| `memory/fluid_memory.py` | 修改 | 移除 MAX_BOOST 硬上限，改用增量式 Ebbinghaus 模型 |
| `memory/memory_manager.py` | 修改 | 集成扩散激活第五路通道 |
| `tools/memory_tool.py` | 修改 | 暴露 confirm/correct 为 Agent 工具 |
| `db/schema.sql` | 修改 | 新增 concept_nodes/edges/meta 表 DDL |
| `db/database.py` | 修改 | 新增表创建 + 迁移逻辑 |

---

## 3. 概念图设计 (Phase 1)

### 3.1 数据库 Schema

```sql
-- concept_nodes: 概念节点表
CREATE TABLE IF NOT EXISTS concept_nodes (
    id            TEXT PRIMARY KEY,        -- md5(cleaned_text)[:12]
    text          TEXT NOT NULL,           -- 事实文本
    weight        REAL NOT NULL DEFAULT 1.0,   -- 显著性权重 [0,1]
    peak_weight   REAL NOT NULL DEFAULT 1.0,   -- 历史最高权重（衰减基准）
    confidence    REAL NOT NULL DEFAULT 1.0,   -- 置信度 [0,1]
    access_count  INTEGER NOT NULL DEFAULT 0,  -- 确认次数
    keys          TEXT NOT NULL DEFAULT '[]',  -- JSON: 索引关键词列表
    layer         TEXT NOT NULL DEFAULT 'hippocampus',  -- hippocampus|cortex
    created       TEXT NOT NULL,            -- ISO timestamp
    last_accessed TEXT NOT NULL,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,                    -- NULL=仍有效
    superseded_by TEXT,                    -- 被替代者 ID
    history       TEXT NOT NULL DEFAULT '[]', -- JSON: 历史版本
    origin        TEXT NOT NULL DEFAULT '{}', -- JSON: 来源信息
    source_mem_id INTEGER,                 -- 关联 episodic_memories.id（双写映射）
    embedding     BLOB                     -- 向量嵌入（兼容 sqlite-vec）
);

-- concept_edges: 概念边表（双向存储）
CREATE TABLE IF NOT EXISTS concept_edges (
    source_id  TEXT NOT NULL,
    target_id  TEXT NOT NULL,
    relation   TEXT NOT NULL DEFAULT 'related',  -- related|co-occurrence|supersedes|superseded-by|possible-conflict
    weight     REAL NOT NULL DEFAULT 1.0,
    created    TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id)
);

-- concept_meta: 元数据（梦境衰减标记等）
CREATE TABLE IF NOT EXISTS concept_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_concept_node_keys ON concept_nodes(keys);
CREATE INDEX IF NOT EXISTS idx_concept_node_layer ON concept_nodes(layer);
CREATE INDEX IF NOT EXISTS idx_concept_node_weight ON concept_nodes(weight);
CREATE INDEX IF NOT EXISTS idx_concept_node_valid ON concept_nodes(valid_to);
CREATE INDEX IF NOT EXISTS idx_concept_edge_source ON concept_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_concept_edge_target ON concept_edges(target_id);
```

### 3.2 Key 提取器

```python
# memory/key_extractor.py
class KeyExtractor:
    """关键词提取器 — jieba 分词 + 停用词 + 同义词归一化"""

    MAX_KEYS = 24  # 与 mind 一致

    # 复用现有 _TOPIC_STOPWORDS
    # 新增同义词归一化映射
    NORMALIZE = {
        "postgre": "postgresql",
        "postgres": "postgresql",
        "redis缓存": "redis",
        "前端": "frontend",
        "后端": "backend",
    }

    def extract(self, text: str, is_query: bool = False) -> list[str]:
        """
        提取索引关键词：
        1. jieba 分词
        2. 停用词过滤
        3. 同义词归一化
        4. 保留 len >= 2 的词
        5. 截断到 MAX_KEYS
        """
        # 查询时：加入身份 facet key（代词查询时）
        # 写入时：只保留内容关键词
```

**不使用 mind 的 CONCEPT_SEED**（编程领域专用），但保留 `NORMALIZE` 映射作为扩展点，可后续添加情感陪伴领域的概念种子。

### 3.3 Auto-linking 策略

**基于 mind 源码研究的关键发现**：mind 的 `remember()` 不会自动创建图边。自动关联通过 shared keys + IDF 在召回时实现。

我们采用相似策略，但稍作增强（中文场景 key 重叠更有意义）：

- `remember()` 只创建节点 + 提取 keys，不自动建边
- 图边在以下情况创建：
  1. **共享 ≥3 个 keys** 的节点自动建边（co-occurrence, weight=1.0）
     - 比 mind 更积极（mind 完全不自动建边），因为中文场景下 key 重叠更少
  2. `correct()` — supersedes/superseded-by 边 (weight=0.5)
  3. `link()` — 手动创建（Agent 工具）
  4. REM 矛盾检测 — possible-conflict 边 (weight=0.5)

### 3.4 双写+懒迁移

```python
# remember 流程
async def remember(text, ...):
    # 1. 写入 episodic_memories（保留现有逻辑）
    mem_id = await memory.insert_episodic_memory(...)
    # 2. 同时写入 concept_nodes
    node_id = md5(cleaned_text)[:12]
    keys = key_extractor.extract(text)
    await concept_db.insert_node(
        id=node_id, text=text, keys=keys,
        source_mem_id=mem_id, ...
    )
    # 3. auto-link：与共享 ≥3 keys 的节点建边
    await concept_db.auto_link(node_id, keys)

# 懒迁移流程
async def lazy_migrate():
    """检索时发现 concept_nodes 数 < episodic_memories 数时触发"""
    ep_count = await memory.get_episodic_count()
    node_count = await concept_db.get_node_count()
    if node_count < ep_count:
        # 取最旧的未迁移记忆，批量写入 concept_nodes
        unmigrated = await memory.get_unmigrated_memories(limit=50)
        for mem in unmigrated:
            keys = key_extractor.extract(mem['summary'])
            await concept_db.insert_node(...)
```

---

## 4. 扩散激活检索 (Phase 2)

### 4.1 引擎架构

```python
# memory/spreading_activation.py
class SpreadingActivationEngine:
    """扩散激活检索引擎 — mind 风格的三通道融合"""

    # 参数（与 mind 一致）
    RECALL_RADIUS = 3           # 最大扩散跳数
    ACTIVATION_DECAY = 0.5      # 每跳衰减50%
    SPREADING_THRESHOLD = 0.05  # 低于不传播
    RRF_K = 60                  # RRF 平滑参数
    FUZZY_ACTIVATION = 0.5     # 模糊匹配系数
    SEPARATION_SIM = 0.92      # 去重相似度阈值

    def __init__(self, concept_db, vector_store, key_extractor):
        self.db = concept_db
        self.vec = vector_store     # 复用现有 VectorStore
        self.key_extractor = key_extractor

    async def recall(self, query: str, top_k: int = 5) -> list[dict]:
        """
        扩散激活检索主入口

        Returns:
            [{id, text, score, weight, keys, ...}, ...]
        """
        # Step 1: Key 提取
        query_keys = set(self.key_extractor.extract(query, is_query=True))
        if not query_keys:
            return []

        # Step 2: 获取存活节点
        alive_nodes = await self.db.get_alive_nodes()
        if not alive_nodes:
            return []

        # Step 3: IDF 计算
        idf = self._compute_idf(query_keys, alive_nodes)

        # Step 4: 直接命中通道
        direct = self._direct_channel(query_keys, idf, alive_nodes, query)

        # Step 5: 模式补全（无直接命中时用向量模糊匹配）
        if not direct:
            direct = await self._pattern_completion(query, alive_nodes)
        if not direct:
            return []

        # Step 6: 扩散激活通道
        spread = await self._spreading_channel(direct, alive_nodes)

        # Step 7: RRF 融合
        fused = self._rrf_fusion(direct, spread)

        # Step 8: 语义重排
        fused = await self._semantic_rerank(query, fused, top_k)

        # Step 9: 模式分离（去重）
        results = self._pattern_separation(fused, top_k)

        return results
```

### 4.2 直接命中通道

```python
def _direct_channel(self, keys, idf, alive_nodes, query):
    """IDF 加权 key 重叠 + 子串包含"""
    direct = {}
    q_lower = query.lower()
    for nid, node in alive_nodes.items():
        node_keys = set(json.loads(node.get("keys", "[]")))
        # weight_bias floor 0.35：衰减但精确匹配的节点仍能胜过新噪声
        w_bias = 0.35 + 0.65 * node.get("weight", 1.0)

        shared = keys & node_keys
        if shared:
            idf_score = sum(idf.get(k, 0) for k in shared)
            direct[nid] = direct.get(nid, 0) + idf_score * w_bias

        # 子串包含（len >= 4 才计）
        n_text = node["text"].lower()
        substr = sum(1 for w in keys if len(w) >= 4 and w in n_text)
        reverse = sum(1 for k in node_keys if len(k) >= 4 and k in q_lower)
        if substr + reverse:
            direct[nid] = direct.get(nid, 0) + (substr + reverse) * 0.6 * w_bias

    return direct
```

### 4.3 扩散激活通道

```python
async def _spreading_channel(self, direct, alive_nodes):
    """从种子节点沿边传播激活值，3跳"""
    spread = defaultdict(float)
    wave = dict(direct)

    for hop in range(self.RECALL_RADIUS + 1):
        nxt = defaultdict(float)
        for nid, act in wave.items():
            spread[nid] += act  # 累积激活
            if hop < self.RECALL_RADIUS and act > self.SPREADING_THRESHOLD:
                edges = await self.db.get_edges(nid)
                for neighbor_id, edge in edges.items():
                    if neighbor_id not in alive_nodes:
                        continue  # closed 事实不中继
                    propagated = (act * self.ACTIVATION_DECAY
                                  * edge["weight"] / (hop + 1))
                    nxt[neighbor_id] += propagated
        wave = nxt
        if not wave:
            break

    return dict(spread)
```

### 4.4 模式补全

```python
async def _pattern_completion(self, query, alive_nodes):
    """无直接命中时，用现有 VectorStore 做模糊匹配

    复用 VectorStore.search() 的向量检索能力，
    将结果映射到 concept_nodes（通过 source_mem_id 或 text 匹配）。
    """
    direct = {}
    if not self.vec or not self.vec.enabled:
        return direct
    # 复用现有向量检索（已在 _hybrid_vec_search 中验证可靠）
    vec_results = await self.vec.search(query, top_k=20)
    if not vec_results:
        return direct
    # 将向量结果映射到 concept_nodes
    for row_id, distance in vec_results:
        # 通过 source_mem_id 找到对应的 concept_node
        node = await self.db.get_node_by_source_mem(row_id)
        if node and node["id"] in alive_nodes:
            sim = max(0.0, 1.0 - distance)
            if sim >= 0.25:
                direct[node["id"]] = (sim * self.FUZZY_ACTIVATION
                                       * node.get("weight", 1.0))
    return direct
```

### 4.5 RRF 融合

```python
def _rrf_fusion(self, direct, spread):
    """Reciprocal Rank Fusion: 双通道排名融合"""
    dr = {n: i for i, (n, _) in enumerate(
        sorted(direct.items(), key=lambda x: (-x[1], x[0])))}
    sr = {n: i for i, (n, _) in enumerate(
        sorted(spread.items(), key=lambda x: (-x[1], x[0])))}
    dr_default = len(dr) + 1
    sr_default = len(sr) + 1

    fused = {}
    for nid in set(direct) | set(spread):
        fused[nid] = (1.0 / (self.RRF_K + dr.get(nid, dr_default)) +
                      1.0 / (self.RRF_K + sr.get(nid, sr_default)))
    return fused
```

### 4.6 与现有检索的集成

```python
# memory_manager.py - retrieve_memories_hybrid 修改
async def retrieve_memories_hybrid(self, query, k, ...):
    fts_items, vec_items, kg_items, child_items, spread_items = await asyncio.gather(
        self._hybrid_fts_search(query, recall_limit),
        self._hybrid_vec_search(query, recall_limit, ...),
        _kg_recall(),
        _child_recall(),
        self._spreading_recall(query, recall_limit),  # 新增第五路
    )

    # RRF 融合：五路
    ranked_lists = [fts_ids, vec_ids]
    weights = [fts_weight, vec_weight]
    if kg_items:
        ranked_lists.append(kg_ids)
        weights.append(0.8)
    if child_items:
        ranked_lists.append(child_ids)
        weights.append(0.9)
    if spread_items:
        ranked_lists.append(spread_ids)
        weights.append(0.85)  # 扩散激活权重略低于直接匹配

    fused = reciprocal_rank_fusion(ranked_lists, weights=weights, limit=oversample_k)
```

---

## 5. Confirm/Correct + Boost 优化 (Phase 3)

### 5.1 FluidMemory 修改

```python
# memory/fluid_memory.py — 修改后
class FluidMemory:
    """流体记忆 — mind 风格 Ebbinghaus 增量模型"""

    # 移除旧参数
    # LAMBDA_DECAY = 0.05    # 删除
    # ALPHA_BOOST = 0.2      # 删除
    # MAX_BOOST = 0.3         # 删除

    # 新参数（与 mind 一致）
    STABILITY_BASE_DAYS = 3.0       # 未确认记忆 3 天半衰期
    STABILITY_PER_ACCESS = 14.0     # 每次确认买 14 天稳定性
    BOOST_PER_ACCESS = 0.15        # 每次确认权重增量
    GRACE_DAYS = 45                # 宽限期
    WEIGHT_THRESHOLD = 0.1         # 修剪阈值
    FORGET_THRESHOLD = 0.05        # 过滤阈值（保留）
    DREAM_THRESHOLD = 0.15         # 归档阈值（保留）

    def score(self, similarity: float, created_at: float,
              access_count: int = 0, peak_weight: float = 1.0) -> float:
        """
        mind 风格: R = e^(-t/S), S = 3 + 14×access_count

        公式: score = similarity × peak_weight × e^(-days / stability)
        stability = STABILITY_BASE_DAYS + access_count × STABILITY_PER_ACCESS

        与旧公式的区别：
        - 旧: similarity × e^(-λ×days) + min(α×ln(1+access), 0.3)
        - 新: similarity × peak_weight × e^(-days / (3 + 14×access))
        - 核心变化：确认次数影响稳定性（半衰期），而非加法 boost
        - 效果：10次确认的记忆 30 天后保留率 81%，远超旧的 ~30%
        """
        days = max(0, (time.time() - created_at) / 86400.0)
        stability = self.STABILITY_BASE_DAYS + access_count * self.STABILITY_PER_ACCESS
        retention = math.exp(-days / stability)
        weight = peak_weight * retention
        return similarity * weight

    def should_filter(self, score: float) -> bool:
        return score < self.FORGET_THRESHOLD

    def should_archive(self, score: float) -> bool:
        return score < self.DREAM_THRESHOLD
```

### 5.2 Confirm 机制

```python
# memory/confirm_correct.py
class ConfirmCorrect:
    """confirm: 确认强化 / correct: 纠正超驰"""

    BOOST_PER_ACCESS = 0.15    # 每次确认的节点权重增量
    EDGE_BOOST = 0.25          # 确认时边权重增量

    def __init__(self, concept_db, spreading_engine, memory_db):
        self.db = concept_db
        self.engine = spreading_engine
        self.memory = memory_db

    async def confirm(self, node_ids: list[str]) -> dict:
        """
        确认强化：
        1. access_count += 1
        2. weight = min(1.0, weight + 0.15)
        3. peak_weight = max(peak_weight, weight)
        4. last_accessed = now
        5. 所有关联边 weight += 0.25 (双向同步)
        6. 同步 episodic_memories.access_count
        """
        now = datetime.now().isoformat()
        reinforced = 0
        unknown = 0

        for nid in node_ids:
            node = await self.db.get_node(nid)
            if node is None:
                unknown += 1
                continue

            new_access = node["access_count"] + 1
            new_weight = min(1.0, node["weight"] + self.BOOST_PER_ACCESS)
            new_peak = max(node["peak_weight"], new_weight)

            await self.db.update_node(nid, access_count=new_access,
                                       weight=new_weight, peak_weight=new_peak,
                                       last_accessed=now)

            # 强化所有关联边（双向同步）
            edges = await self.db.get_edges(nid)
            for target_id, edge in edges.items():
                new_edge_w = min(1.0, edge["weight"] + self.EDGE_BOOST)
                await self.db.update_edge(nid, target_id, weight=new_edge_w)
                await self.db.update_edge(target_id, nid, weight=new_edge_w)

            # 同步 episodic_memories
            if node.get("source_mem_id"):
                await self.memory.increment_access_count(node["source_mem_id"])

            reinforced += 1

        return {"reinforced": reinforced, "unknown": unknown}
```

### 5.3 Correct 机制

```python
    async def correct(self, old_hint: str, new_text: str) -> dict:
        """
        纠正超驰（融合而非擦除）：
        1. recall 找到最匹配旧记忆
        2. 验证匹配质量（共享 ≥2 token 或覆盖 ≥50%）
        3. 创建新节点（继承权重, confidence×0.7）
        4. 迁移旧节点的知识边到新节点（不迁移 supersedes 边）
        5. 建立双向 supersedes/superseded-by 边 (weight=0.5)
        6. 关闭旧节点 (valid_to = now, superseded_by = new_id)
        7. 保留 history 溯源链
        """
        # 1. 找到旧记忆
        results = await self.engine.recall(old_hint, top_k=1)
        if not results:
            return {"error": "no match"}

        old_id = results[0]["id"]
        old_node = results[0]
        old_text = old_node["text"]

        # 2. 验证匹配质量
        hint_tokens = set(self.key_extractor.extract(old_hint))
        node_tokens = set(self.key_extractor.extract(old_text))
        shared = hint_tokens & node_tokens
        if not (len(shared) >= 2 or
                (hint_tokens and len(shared) / len(hint_tokens) >= 0.5)):
            return {"error": "insufficient match quality"}

        # 3. 创建新节点
        now = datetime.now().isoformat()
        new_id = hashlib.md5(
            self._clean_text(new_text).encode('utf-8')
        ).hexdigest()[:12]
        lowered_conf = round(old_node.get("confidence", 1.0) * 0.7, 3)

        history = json.loads(old_node.get("history", "[]"))
        history.append({"text": old_text, "replaced": now})

        new_keys = self.key_extractor.extract(new_text, is_query=False)

        await self.db.insert_node(
            id=new_id, text=self._clean_text(new_text),
            weight=old_node.get("weight", 1.0),
            peak_weight=old_node.get("peak_weight", 1.0),
            confidence=lowered_conf, access_count=0,
            keys=json.dumps(new_keys), layer="hippocampus",
            created=now, last_accessed=now,
            valid_from=now, valid_to=None,
            superseded_by=None, history=json.dumps(history),
            origin=json.dumps({"via": "correct"}),
        )

        # 4. 迁移旧节点的知识边（不迁移 supersedes 边）
        old_edges = await self.db.get_edges(old_id)
        for target_id, edge in old_edges.items():
            if edge["relation"] in ("supersedes", "superseded-by"):
                continue
            if target_id == new_id:
                continue
            await self.db.create_edge(new_id, target_id,
                                       edge["relation"], edge["weight"], now)
            await self.db.create_edge(target_id, new_id,
                                       edge["relation"], edge["weight"], now)

        # 5. supersedes 双向边
        await self.db.create_edge(new_id, old_id, "supersedes", 0.5, now)
        await self.db.create_edge(old_id, new_id, "superseded-by", 0.5, now)

        # 6. 关闭旧节点
        await self.db.update_node(old_id, valid_to=now,
                                   superseded_by=new_id)

        return {
            "old_text": old_text, "new_text": new_text,
            "old_id": old_id, "new_id": new_id,
        }
```

### 5.4 Agent 集成

```python
# tools/memory_tool.py — 新增工具
# confirm_memory: 当用户确认记忆正确时调用
# correct_memory: 当用户纠正错误记忆时调用

# Agent 通过以下方式自动触发：
# - confirm: 用户说"对/没错/就是这样"等确认词
# - correct: 用户说"不对/应该是/搞错了"等纠正词
# - 也可通过 slash command 手动触发
```

### 5.5 向后兼容

- `FluidMemory.score()` 新增 `peak_weight` 参数（默认 1.0），向后兼容
- `DreamConsolidator` 使用更新后的 `FluidMemory` 评分公式
- 现有 `episodic_memories.access_count` 与 `concept_nodes.access_count` 保持同步
- 现有检索流程（不涉及 concept_nodes）继续正常工作

---

## 6. 关键参数速查表

| 参数 | 值 | 来源 | 说明 |
|------|-----|------|------|
| `BOOST_PER_ACCESS` | 0.15 | mind | 每次 confirm 的节点权重增量 |
| `EDGE_BOOST` | 0.25 | mind | 每次 confirm 的边权重增量 |
| `STABILITY_BASE_DAYS` | 3.0 | mind | 未确认记忆的稳定性天数 |
| `STABILITY_PER_ACCESS` | 14.0 | mind | 每次确认增加的稳定性天数 |
| `GRACE_DAYS` | 45 | mind | 记忆修剪宽限期 |
| `WEIGHT_THRESHOLD` | 0.1 | mind | 节点修剪权重阈值 |
| `RECALL_RADIUS` | 3 | mind | 扩散激活最大跳数 |
| `ACTIVATION_DECAY` | 0.5 | mind | 每跳激活衰减系数 |
| `SPREADING_THRESHOLD` | 0.05 | mind | 扩散传播最低激活值 |
| `RRF_K` | 60 | mind | RRF 平滑参数 |
| `FUZZY_ACTIVATION` | 0.5 | mind | 模糊匹配激活系数 |
| `SEPARATION_SIM` | 0.92 | mind | 去重相似度阈值 |
| `MAX_KEYS` | 24 | mind | 每个节点最大 key 数 |
| 扩散激活 RRF 权重 | 0.85 | 自定义 | 略低于直接匹配的 FTS/Vector |

---

## 7. 测试策略

### 7.1 单元测试

- `test_key_extractor.py`: jieba 分词 + 停用词 + 归一化
- `test_concept_graph.py`: 节点/边 CRUD + auto_link
- `test_spreading_activation.py`: 直接命中 + 扩散 + RRF + 模式分离
- `test_confirm_correct.py`: confirm 强化 + correct 超驰 + 溯源链
- `test_fluid_memory.py`: 新 Ebbinghaus 公式 + 稳定性计算

### 7.2 集成测试

- 扩散激活作为第五路通道的 RRF 融合
- 双写一致性：episodic_memories ↔ concept_nodes
- 懒迁移：旧记忆迁移后检索结果一致
- confirm/correct 后检索权重变化

### 7.3 回归测试

- 现有 `test_fluid_memory.py` 更新为新公式
- 现有 `retrieve_memories_hybrid` 测试通过（扩散激活通道为空时不影响结果）

---

## 8. 不在本期范围

以下功能推迟到后续迭代：

- **三阶段梦境周期** (Phase 4): Light→Deep→REM，边衰减，聚类提升，矛盾检测
- **Cortex 长期巩固层** (Phase 4): 聚类提升后的永久存储
- **Working Memory 自动注入** (Phase 4): 从 Hippocampus 取 top-N 热记忆注入上下文
- **provenance journal** (Phase 3 扩展): journal.jsonl 永久溯源日志
- **RelatedTerms 同现矩阵** (Phase 2 扩展): 2-hop PageRank 术语关联发现
- **自动梦境触发** (Phase 4): 信号≥10 或 >24h 自动触发
