# xiaoda-agent 知识图谱优化设计：基于 graphiti 核心机制

> **版本**: v2.0  
> **项目**: xiaoda-agent v0.5.03  
> **分支**: feat/memory-cognition-v06  
> **参考**: getzep/graphiti (28K⭐, Zep 开源时序知识图谱引擎) + arXiv:2501.13956 论文  
> **日期**: 2026-07-10

---

## 目录

1. [现状诊断](#1-现状诊断)
2. [设计目标与范围](#2-设计目标与范围)
3. [架构设计](#3-架构设计)
4. [Schema 扩展](#4-schema-扩展)
5. [事实超驰逻辑](#5-事实超驰逻辑)
6. [实体演化](#6-实体演化)
7. [混合检索](#7-混合检索)
8. [社区发现](#8-社区发现)
9. [Episode 溯源](#9-episode-溯源)
10. [文件组织与集成](#10-文件组织与集成)
11. [数据迁移](#11-数据迁移)
12. [测试策略](#12-测试策略)
13. [不采纳的部分](#13-不采纳的部分)

---

## 1. 现状诊断

### 1.1 当前知识图谱架构

| 文件 | 职责 |
|------|------|
| `memory/knowledge_graph.py` | LLM实体关系提取、JSON解析、合并逻辑、检索增强 |
| `db/db_knowledge.py` | SQLite持久化（aiosqlite）、CRUD、FTS5搜索、BFS图遍历 |
| `memory/vector_store.py` | sqlite-vec 向量存储，支持 embed/upsert/search（BAAI/bge-m3） |
| `db/db_temporal_memory.py` | bi-temporal facts/preferences 超驰（独立于KG） |

### 1.2 当前 Schema

```sql
CREATE TABLE knowledge_entities (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE,
    kind TEXT DEFAULT '',
    observations TEXT DEFAULT '[]',  -- JSON数组，追加式，无版本
    updated_at REAL NOT NULL
);

CREATE TABLE knowledge_relations (
    id TEXT PRIMARY KEY,
    from_entity TEXT,
    relation_type TEXT,
    to_entity TEXT,
    created_at REAL DEFAULT 0,
    updated_at REAL NOT NULL,
    valid_from REAL DEFAULT 0,  -- 已有但未使用
    valid_to REAL DEFAULT 0,    -- 已有但未使用
    confidence REAL DEFAULT 1.0
);
```

### 1.3 六大缺口

| 编号 | 缺口 | 严重度 | 描述 |
|------|------|--------|------|
| P1 | 无时序追踪 | 🔴 严重 | 关系无 `valid_at`/`invalid_at`，无法区分过去/当前事实 |
| P2 | 无事实超驰 | 🔴 严重 | 矛盾事实并存，无自动失效机制 |
| P3 | 无向量检索 | 🔴 严重 | 仅 FTS5+LIKE，无 embedding 字段，无语义检索 |
| P4 | 无实体演化 | 🟡 中等 | observations 追加列表，无压缩无摘要 |
| P5 | 无 Episode 溯源 | 🟡 中等 | 关系无法追溯到原始对话 |
| P6 | 无社区发现 | 🟠 低优先 | 无实体聚类，无高层语义摘要 |

### 1.4 已有基础

- `VectorStore` 已集成 sqlite-vec，支持 BAAI/bge-m3 嵌入（1024维），有 LRU 缓存和并发嵌入限制
- `db_temporal_memory.py` 已实现 bi-temporal 的 supersede 逻辑（`valid_from`/`valid_to`/`expired_at`/`superseded_by`）
- `knowledge_relations` 表已有 `valid_from`/`valid_to`/`confidence` 列但代码未使用
- 数据库迁移系统使用 `schema_version` 表 + `_migrate_vN()` 方法，当前到 v13

---

## 2. 设计目标与范围

### 2.1 目标

完整对齐 graphiti 6项核心机制，在 SQLite + aiosqlite 环境下实现最优效果。

### 2.2 设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 优化范围 | 全部6项 | 完整对齐 graphiti |
| 向量方案 | sqlite-vec | 项目已集成，支持 KNN |
| 架构策略 | 并行新表+迁移 | 安全可回滚，遵循项目迁移模式 |
| LLM 调用 | 免费模型优先 | 不占用主模型配额，与现有实现一致 |

### 2.3 验收标准

1. 新事实输入时，自动检测并标记与旧事实的矛盾（`invalid_at` 设置正确）
2. 实体 summary 随 episode 演化重写，observations 不再无限膨胀
3. 检索结果融合语义+全文+图三路，按 RRF 排序，P95 < 500ms
4. 每条关系可追溯到派生它的 Episode
5. 社区发现自动聚类实体，生成社区摘要
6. 旧数据完整迁移，旧表保留可回滚

---

## 3. 架构设计

### 3.1 整体架构

```
Episode 摄入
    │
    ▼
┌──────────────────────────────────────┐
│ KnowledgeGraphV2                     │
│  ├── add_facts_from_episode()        │
│  │   ├── 保存 Episode                │
│  │   ├── LLM 提取实体+关系           │
│  │   ├── merge_entities_v2() ────────┼──→ 实体演化（summary重写）
│  │   └── merge_relation_v2() ────────┼──→ 事实超驰（矛盾检测+失效）
│  │                                   │
│  ├── search() ───────────────────────┼──→ 混合检索
│  │   ├── _semantic_search()          │      （语义+全文+图）
│  │   ├── _fulltext_search()          │
│  │   ├── _graph_search()             │
│  │   └── _rrf_fuse()                 │
│  │                                   │
│  └── detect_communities() ───────────┼──→ 社区发现
│      ├── _label_propagation()        │      （标签传播+摘要）
│      └── _build_community_summary()  │
└──────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────┐
│ DB层                                  │
│  ├── KnowledgeDBV2 (db_kg_v2.py)     │
│  │   ├── kg_entities_v2              │
│  │   ├── kg_relations_v2             │
│  │   ├── kg_episodes                 │
│  │   ├── kg_communities              │
│  │   └── kg_edge_episode_refs        │
│  │                                   │
│  └── VectorStore (扩展)              │
│      ├── kg_entities_vec             │
│      └── kg_relations_vec            │
└──────────────────────────────────────┘
```

### 3.2 与现有系统的关系

- **旧 KG（v1）**：`knowledge_entities`/`knowledge_relations` + `KnowledgeGraph` + `KnowledgeDB` 保留不动，通过功能开关切换
- **temporal_memory**：`memory_facts`/`memory_preferences` 独立保留，服务偏好/事实查询场景；KG v2 服务图遍历+混合检索场景
- **VectorStore**：扩展而非替换，新增 KG 专用向量表

---

## 4. Schema 扩展

### 4.1 kg_episodes — Episode 溯源表

```sql
CREATE TABLE IF NOT EXISTS kg_episodes (
    id TEXT PRIMARY KEY,                -- EP-前缀UUID
    content TEXT NOT NULL,              -- 原始对话摘要内容
    source_type TEXT DEFAULT 'summary', -- summary | message | text
    source_description TEXT DEFAULT '',
    valid_at REAL NOT NULL,             -- Episode参考时间（对话发生时间）
    created_at REAL NOT NULL,           -- 系统录入时间
    group_id TEXT DEFAULT 'default'
);
CREATE INDEX IF NOT EXISTS idx_kg_episode_valid_at ON kg_episodes(valid_at);
```

### 4.2 kg_entities_v2 — 实体表（含摘要演化 + 向量）

```sql
CREATE TABLE IF NOT EXISTS kg_entities_v2 (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE,
    kind TEXT DEFAULT '',
    observations TEXT DEFAULT '[]',      -- 保留兼容
    summary TEXT DEFAULT '',             -- 替换式摘要（核心演化字段）
    summary_version INTEGER DEFAULT 0,   -- 摘要版本号
    name_embedding TEXT DEFAULT NULL,    -- JSON数组（同步到vec表）
    updated_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kg_entity_v2_name ON kg_entities_v2(name);
```

### 4.3 kg_relations_v2 — 关系表（含时序窗口 + 溯源 + 事实向量）

```sql
CREATE TABLE IF NOT EXISTS kg_relations_v2 (
    id TEXT PRIMARY KEY,
    from_entity TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    to_entity TEXT NOT NULL,
    fact TEXT DEFAULT '',               -- 自然语言事实陈述
    fact_embedding TEXT DEFAULT NULL,   -- JSON数组（同步到vec表）
    episode_ids TEXT DEFAULT '[]',      -- Episode ID列表（JSON数组）
    valid_at REAL DEFAULT NULL,         -- 事实生效时间（Unix时间戳）
    invalid_at REAL DEFAULT NULL,       -- 事实失效时间
    expired_at REAL DEFAULT NULL,       -- 系统标记过期时间
    is_current INTEGER DEFAULT 1,       -- 是否当前有效（1=有效, 0=已失效）
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_from ON kg_relations_v2(from_entity);
CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_to ON kg_relations_v2(to_entity);
CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_current ON kg_relations_v2(is_current);
CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_valid_at ON kg_relations_v2(valid_at);
CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_invalid_at ON kg_relations_v2(invalid_at);
```

### 4.4 kg_communities — 社区表

```sql
CREATE TABLE IF NOT EXISTS kg_communities (
    id TEXT PRIMARY KEY,                -- COM-前缀UUID
    name TEXT NOT NULL,
    summary TEXT DEFAULT '',
    member_entities TEXT DEFAULT '[]',  -- 成员实体名列表（JSON数组）
    name_embedding TEXT DEFAULT NULL,   -- 名称向量
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
```

### 4.5 kg_edge_episode_refs — 双向溯源关联表

```sql
CREATE TABLE IF NOT EXISTS kg_edge_episode_refs (
    edge_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    PRIMARY KEY (edge_id, episode_id)
);
CREATE INDEX IF NOT EXISTS idx_kg_eer_episode ON kg_edge_episode_refs(episode_id);
CREATE INDEX IF NOT EXISTS idx_kg_eer_edge ON kg_edge_episode_refs(edge_id);
```

### 4.6 向量虚拟表（在 VectorStore.init() 中创建）

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS kg_entities_vec USING vec0(embedding float[1024]);
CREATE VIRTUAL TABLE IF NOT EXISTS kg_relations_vec USING vec0(embedding float[1024]);
```

### 4.7 FTS5 全文索引

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS kg_entities_v2_fts USING fts5(
    id UNINDEXED,
    name_summary  -- name + summary 拼接
);
CREATE VIRTUAL TABLE IF NOT EXISTS kg_relations_v2_fts USING fts5(
    id UNINDEXED,
    fact
);
```

FTS5 触发器同步实体和关系的全文索引（与现有 `knowledge_entities_fts` 模式一致）。

---

## 5. 事实超驰逻辑

### 5.1 核心流程

```python
async def add_facts_from_episode(
    self,
    episode_content: str,
    episode_time: float,
    source_type: str = "summary",
) -> dict:
    """
    从Episode提取并合并事实，完整流程：
    1. 保存Episode记录
    2. LLM提取实体和关系
    3. 对每条新关系执行超驰检查
    4. 更新实体摘要
    """
    # Step 1: 保存Episode
    episode_id = f"EP-{uuid.uuid4().hex[:12]}"
    now = time.time()
    await self.db.insert_episode(episode_id, episode_content, source_type, episode_time, now)

    # Step 2: LLM提取
    extracted = await self.extract_from_summary(episode_content)
    if not extracted.get("entities") and not extracted.get("relations"):
        return {"episode_id": episode_id, "new_facts": 0, "invalidated": 0}

    # Step 3: 合并实体（含摘要演化）
    await self.merge_entities_v2(extracted["entities"], episode_content, episode_time)

    # Step 4: 合并关系（含事实超驰）
    invalidated_count = 0
    new_facts_count = 0
    for rel in extracted.get("relations", []):
        is_new, invalidated = await self.merge_relation_v2(rel, episode_id, episode_time)
        new_facts_count += int(is_new)
        invalidated_count += len(invalidated)

    return {"episode_id": episode_id, "new_facts": new_facts_count, "invalidated": invalidated_count}
```

### 5.2 事实超驰核心逻辑

```python
async def merge_relation_v2(
    self,
    relation: dict,
    episode_id: str,
    episode_time: float,
) -> tuple[bool, list[dict]]:
    """
    合并新关系到知识图谱，自动处理超驰。
    Returns: (is_new, invalidated_relations)
    """
    from_entity = relation.get("from_entity", "")
    relation_type = relation.get("relation_type", "")
    to_entity = relation.get("to_entity", "")
    fact = relation.get("fact", f"{from_entity} {relation_type} {to_entity}")

    if not from_entity or not relation_type or not to_entity:
        return False, []

    # 1. 查找同端点的当前有效关系
    existing = await self.db.get_active_relations_between(from_entity, to_entity)

    # 2. 过滤同类型的冲突候选
    conflict_candidates = [
        r for r in existing
        if r["relation_type"] == relation_type and r.get("is_current", 1) == 1
    ]

    invalidated = []
    is_duplicate = False

    if conflict_candidates:
        # 3a. 精确匹配检查
        for candidate in conflict_candidates:
            if candidate.get("fact", "") == fact:
                is_duplicate = True
                await self.db.append_episode_ref(candidate["id"], episode_id)
                break

        # 3b. LLM矛盾检测
        if not is_duplicate:
            contradictions = await self._detect_contradictions(
                new_fact=fact,
                existing_facts=[r.get("fact", "") for r in conflict_candidates],
            )
            for idx in contradictions:
                candidate = conflict_candidates[idx]
                # 时间窗口冲突解析
                if self._resolve_contradiction(candidate, episode_time):
                    invalidated.append(candidate)

    # 4. 插入新关系
    if not is_duplicate:
        rel_id = f"REL-{uuid.uuid4().hex[:12]}"
        await self.db.insert_relation_v2(
            rel_id, from_entity, relation_type, to_entity, fact,
            episode_id, episode_time
        )
        # 异步嵌入事实向量
        await self._embed_and_store_fact(rel_id, fact)

    return not is_duplicate, invalidated
```

### 5.3 矛盾检测 Prompt

```python
CONTRADICTION_PROMPT = """判断新事实是否与已有事实矛盾。

新事实: {new_fact}
已有事实: {existing_facts_list}

规则:
1. 如果新事实与已有事实表达相同含义，不算矛盾
2. 如果新事实使已有事实不再成立，算矛盾
3. 输出JSON: {{"contradicted_indices": [索引列表]}}

输出JSON:"""
```

### 5.4 时间窗口冲突解析

直接复用 graphiti 的 `resolve_edge_contradictions` 算法：

```python
def _resolve_contradiction(self, old_relation: dict, new_valid_at: float) -> bool:
    """解析时间窗口冲突，返回是否标记了旧关系失效"""
    old_valid_at = old_relation.get("valid_at") or 0
    old_invalid_at = old_relation.get("invalid_at")

    # 旧事实已失效 → 不冲突
    if old_invalid_at and old_invalid_at <= new_valid_at:
        return False

    # 旧事实生效更早 → 标记失效
    if old_valid_at < new_valid_at:
        old_relation["invalid_at"] = new_valid_at
        old_relation["expired_at"] = time.time()
        old_relation["is_current"] = 0
        return True

    return False
```

---

## 6. 实体演化

### 6.1 替换式 Summary 重写

```python
async def merge_entities_v2(
    self,
    entities: list[dict],
    episode_content: str,
    episode_time: float,
) -> None:
    for ent in entities[:5]:
        existing = await self.db.get_entity_v2(ent["name"])
        if existing:
            # 拼接旧summary + 新observations
            old_summary = existing.get("summary", "")
            new_obs = ent.get("observations", [])

            if old_summary and len(old_summary) + len(str(new_obs)) < 200:
                # 内容较短，直接拼接
                new_summary = f"{old_summary}; {'; '.join(new_obs)}"
            elif old_summary:
                # LLM重写summary
                new_summary = await self._rewrite_summary(old_summary, new_obs, ent["name"])
            else:
                # 首次生成summary
                new_summary = "; ".join(new_obs) if new_obs else ""

            # 替换式更新，version+1
            await self.db.update_entity_summary_v2(
                ent["name"], new_summary,
                summary_version=existing.get("summary_version", 0) + 1
            )

            # 异步嵌入 name + summary
            await self._embed_and_store_entity(ent["name"], new_summary)
        else:
            # 新实体
            entity_id = f"ENT-{uuid.uuid4().hex[:12]}"
            summary = "; ".join(ent.get("observations", []))
            await self.db.insert_entity_v2(entity_id, ent["name"], ent.get("kind", ""), ent.get("observations", []), summary)
            await self._embed_and_store_entity(ent["name"], summary)
```

### 6.2 Summary 重写 Prompt

```python
SUMMARY_REWRITE_PROMPT = """你是知识压缩助手。将旧摘要和新信息融合为一条精简摘要。

旧摘要: {old_summary}
新信息: {new_observations}
实体名: {entity_name}

要求:
1. 保留所有关键事实
2. 去除冗余和重复
3. 不超过200字
4. 直接输出摘要文本，不要加任何标记"""
```

### 6.3 向量同步

每次 summary 更新后，将 `name + ": " + summary` 拼接文本嵌入到 `kg_entities_vec`。使用 VectorStore 的 embed 方法（带 LRU 缓存），异步执行不阻塞主流程。

---

## 7. 混合检索

### 7.1 三路并发搜索

```python
async def search(self, query: str, top_k: int = 10, as_of: float | None = None) -> list[dict]:
    """
    混合检索：语义 + 全文 + 图遍历，RRF融合。
    支持时序过滤：as_of=None 只返回当前有效事实，as_of=时间戳 返回历史快照。
    """
    results = await asyncio.gather(
        self._semantic_search(query, top_k * 2),
        self._fulltext_search(query, top_k * 2),
        self._graph_search(query, top_k * 2),
    )
    fused = self._rrf_fuse(results, k=60)
    # 时序过滤
    if as_of is None:
        fused = [r for r in fused if r.get("is_current", 1) == 1]
    else:
        fused = [r for r in fused
                 if (r.get("valid_at") or 0) <= as_of
                 and (r.get("invalid_at") is None or r["invalid_at"] > as_of)]
    return fused[:top_k]
```

### 7.2 语义搜索（sqlite-vec）

```python
async def _semantic_search(self, query: str, k: int) -> list[dict]:
    vec = await self._vector_store.embed(query)
    if not vec:
        return []
    vec_json = json.dumps(vec)
    # 搜索实体向量
    entity_rows = await self._vec_conn.execute(
        "SELECT rowid, distance FROM kg_entities_vec "
        "WHERE embedding MATCH vec_f32(?) AND k=? ORDER BY distance",
        [vec_json, k]
    )
    # 搜索事实向量
    fact_rows = await self._vec_conn.execute(
        "SELECT rowid, distance FROM kg_relations_vec "
        "WHERE embedding MATCH vec_f32(?) AND k=? ORDER BY distance",
        [vec_json, k]
    )
    # 统一格式返回
    return [...]
```

### 7.3 全文搜索（FTS5 BM25）

```python
async def _fulltext_search(self, query: str, k: int) -> list[dict]:
    from db.fts_utils import _build_fts_query
    fts_query = _build_fts_query(query)
    if not fts_query:
        return []
    # 实体: name + summary
    entity_hits = await self._conn.execute(
        "SELECT id FROM kg_entities_v2_fts WHERE name_summary MATCH ? ORDER BY rank LIMIT ?",
        [fts_query, k]
    )
    # 事实: fact
    fact_hits = await self._conn.execute(
        "SELECT id FROM kg_relations_v2_fts WHERE fact MATCH ? ORDER BY rank LIMIT ?",
        [fts_query, k]
    )
    return [...]
```

### 7.4 图遍历搜索（递归 CTE）

```python
async def _graph_search(self, query: str, k: int) -> list[dict]:
    # 先从 query 提取实体名
    entities = await self.get_query_entities(query)
    if not entities:
        return []
    results = []
    for seed in list(entities)[:3]:
        cursor = await self._conn.execute("""
            WITH RECURSIVE bfs(entity, depth) AS (
                SELECT ?, 0
                UNION ALL
                SELECT CASE WHEN r.from_entity = b.entity THEN r.to_entity
                            ELSE r.from_entity END, b.depth + 1
                FROM kg_relations_v2 r JOIN bfs b
                  ON (r.from_entity = b.entity OR r.to_entity = b.entity)
                WHERE b.depth < 2 AND r.is_current = 1
            )
            SELECT DISTINCT entity, MIN(depth) as min_depth FROM bfs
            GROUP BY entity ORDER BY min_depth LIMIT ?
        """, [seed, k])
        rows = await cursor.fetchall()
        results.extend([{"type": "entity", "id": r[0], "graph_distance": r[1]} for r in rows])
    return results
```

### 7.5 RRF 融合

```python
def _rrf_fuse(self, ranked_lists: list[list[dict]], k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion: score = Σ 1/(k + rank)"""
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked):
            key = f"{item['type']}:{item['id']}"
            scores[key] = scores.get(key, 0) + 1.0 / (k + rank)
            items[key] = item
    sorted_keys = sorted(scores.keys(), key=lambda x: -scores[x])
    return [{**items[key], "rrf_score": scores[key]} for key in sorted_keys]
```

### 7.6 性能目标

- P95 延迟 < 500ms（graphiti 的 300ms 目标在 SQLite 下放宽）
- 检索热路径零 LLM 调用（实体提取在 query 预处理阶段完成）

---

## 8. 社区发现

### 8.1 标签传播算法

纯 Python 内存计算，从 SQLite 加载图投影：

```python
async def detect_communities(self) -> list[list[str]]:
    # 1. 加载邻接表
    cursor = await self._conn.execute("""
        SELECT from_entity, to_entity, COUNT(*) as edge_count
        FROM kg_relations_v2
        WHERE is_current = 1
        GROUP BY from_entity, to_entity
    """)
    rows = await cursor.fetchall()

    # 2. 构建邻接表
    adjacency: dict[str, list[tuple[str, int]]] = {}
    for row in rows:
        adjacency.setdefault(row[0], []).append((row[1], row[2]))
        adjacency.setdefault(row[1], []).append((row[0], row[2]))

    # 3. 运行标签传播
    clusters = self._label_propagation(adjacency, max_iter=10)

    # 4. 为每个社区生成摘要
    for cluster in clusters:
        if len(cluster) > 1:
            await self._build_community_summary(cluster)

    return clusters
```

### 8.2 标签传播实现

```python
def _label_propagation(
    self,
    adjacency: dict[str, list[tuple[str, int]]],
    max_iter: int = 10,
) -> list[list[str]]:
    labels = {node: i for i, node in enumerate(adjacency)}

    for _ in range(max_iter):
        no_change = True
        for node in adjacency:
            neighbor_labels: dict[int, int] = {}
            for neighbor, edge_count in adjacency[node]:
                lbl = labels[neighbor]
                neighbor_labels[lbl] = neighbor_labels.get(lbl, 0) + edge_count

            if not neighbor_labels:
                continue

            best_label = max(neighbor_labels, key=neighbor_labels.get)
            # 要求多于一条连接才切换社区
            if neighbor_labels[best_label] > 1 and labels[node] != best_label:
                labels[node] = best_label
                no_change = False

        if no_change:
            break

    communities: dict[int, list[str]] = {}
    for node, lbl in labels.items():
        communities.setdefault(lbl, []).append(node)
    return list(communities.values())
```

### 8.3 社区摘要生成

```python
async def _build_community_summary(self, member_names: list[str]) -> None:
    placeholders = ",".join("?" * len(member_names))
    cursor = await self._conn.execute(
        f"SELECT name, summary FROM kg_entities_v2 WHERE name IN ({placeholders}) AND summary != ''",
        member_names
    )
    rows = await cursor.fetchall()

    if not rows:
        return

    summaries = [r[1] for r in rows]
    if len(summaries) <= 4:
        combined = "; ".join(summaries)
    else:
        combined = await self._hierarchical_summarize(summaries)

    community_id = f"COM-{uuid.uuid4().hex[:12]}"
    name = await self._generate_community_name(combined)
    await self.db.insert_community(community_id, name, combined, member_names)
    await self._embed_and_store_community(community_id, name)
```

### 8.4 增量更新

```python
async def update_community_for_entity(self, entity_name: str) -> None:
    """新增实体后，查邻居社区归属，取众数归入"""
    cursor = await self._conn.execute("""
        SELECT r.from_entity, r.to_entity FROM kg_relations_v2 r
        WHERE r.is_current = 1 AND (r.from_entity = ? OR r.to_entity = ?)
    """, [entity_name, entity_name])
    rows = await cursor.fetchall()

    neighbor_names = set()
    for row in rows:
        neighbor_names.add(row[0] if row[1] == entity_name else row[1])

    # 查邻居所属社区
    community_votes: dict[str, int] = {}
    for neighbor in neighbor_names:
        comm = await self.db.get_entity_community(neighbor)
        if comm:
            community_votes[comm] = community_votes.get(comm, 0) + 1

    if community_votes:
        best = max(community_votes, key=community_votes.get)
        await self.db.add_entity_to_community(entity_name, best)
```

### 8.5 触发时机

社区发现在后台任务中周期性运行：
- 每 100 次 episode 摄入触发一次增量更新
- 每天一次完整重建（通过 `cron_last_run` 表调度）
- 不阻塞主流程，异步执行

---

## 9. Episode 溯源

### 9.1 双向索引

通过 `kg_edge_episode_refs` 关联表实现双向查询：

```python
# 前向: episode → facts
async def get_facts_from_episode(self, episode_id: str) -> list[dict]:
    cursor = await self._conn.execute("""
        SELECT r.* FROM kg_relations_v2 r
        JOIN kg_edge_episode_refs ref ON ref.edge_id = r.id
        WHERE ref.episode_id = ?
    """, [episode_id])
    return [dict(row) for row in await cursor.fetchall()]

# 反向: fact → episodes
async def get_episodes_for_fact(self, edge_id: str) -> list[dict]:
    cursor = await self._conn.execute("""
        SELECT e.* FROM kg_episodes e
        JOIN kg_edge_episode_refs ref ON ref.episode_id = e.id
        WHERE ref.edge_id = ?
    """, [edge_id])
    return [dict(row) for row in await cursor.fetchall()]
```

### 9.2 溯源链路

```
EpisodicNode (kg_episodes)
    ↓ kg_edge_episode_refs
EntityEdge (kg_relations_v2)
    ↓ from_entity / to_entity
EntityNode (kg_entities_v2)
```

查询一条知识的完整溯源：关系 → episode → 原始对话摘要。

---

## 10. 文件组织与集成

### 10.1 新增文件

| 文件 | 职责 |
|------|------|
| `db/db_kg_v2.py` | v2 表的 CRUD：实体、关系、episode、社区的持久化操作 |
| `memory/knowledge_graph_v2.py` | KG v2 核心：episode摄入、事实超驰、实体演化、社区发现 |
| `memory/kg_search.py` | 混合检索引擎：语义+全文+图遍历+RRF融合 |
| `db/migrations/kg_v14_migration.py` | v14 迁移：创建v2表+导入旧数据 |

### 10.2 修改文件

| 文件 | 改动 |
|------|------|
| `db/database.py` | 新增 `_migrate_v14()` |
| `memory/vector_store.py` | `init()` 新增 kg_entities_vec/kg_relations_vec 创建 + upsert/search 方法 |
| `memory/knowledge_graph.py` | `auto_extract_and_merge()` 增加 v2 分支（功能开关） |
| `memory/memory_manager.py` | 检索路径增加 KG v2 混合检索参与召回 |

### 10.3 功能开关

```python
# config.py
KG_V2_ENABLED = os.getenv("KG_V2_ENABLED", "true").lower() == "true"
```

- 开启时：`auto_extract_and_merge()` 调用 `KnowledgeGraphV2.add_facts_from_episode()`
- 关闭时：降级到旧 `KnowledgeGraph` 逻辑

### 10.4 向后兼容

- 旧 `KnowledgeGraph` 类和 `KnowledgeDB` 类保留不动
- 旧表 `knowledge_entities`/`knowledge_relations` 保留不删
- Web UI 的知识图谱管理页面同时支持 v1 和 v2 数据展示

---

## 11. 数据迁移

### 11.1 迁移脚本

```python
async def _migrate_v14(self) -> None:
    """v14: 知识图谱 v2 — 时序事实、实体演化、Episode溯源、社区发现。"""
    # 1. 创建所有 v2 表（IF NOT EXISTS 保证幂等）
    await self._conn.executescript(V2_SCHEMA_SQL)

    # 2. 迁移 entities: knowledge_entities → kg_entities_v2
    await self._conn.execute("""
        INSERT OR IGNORE INTO kg_entities_v2 (id, name, kind, observations, summary, summary_version, updated_at, created_at)
        SELECT id, name, kind, observations,
               observations AS summary,  -- 兼容性转换: observations JSON → summary 文本
               0,
               updated_at,
               updated_at
        FROM knowledge_entities
        WHERE name NOT IN (SELECT name FROM kg_entities_v2)
    """)

    # 3. 迁移 relations: knowledge_relations → kg_relations_v2
    await self._conn.execute("""
        INSERT OR IGNORE INTO kg_relations_v2 (id, from_entity, relation_type, to_entity, fact, episode_ids, valid_at, invalid_at, expired_at, is_current, created_at, updated_at)
        SELECT id, from_entity, relation_type, to_entity,
               from_entity || ' ' || relation_type || ' ' || to_entity AS fact,
               '[]',
               created_at AS valid_at,  -- 无法推断时用录入时间
               NULL,                    -- 旧数据视为永久有效
               NULL,
               1,                       -- is_current = 1
               created_at,
               updated_at
        FROM knowledge_relations
        WHERE id NOT IN (SELECT id FROM kg_relations_v2)
    """)

    # 4. 保留旧表不删除（回滚安全）
    await self._conn.execute("INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (14, ?)", (time.time(),))
```

### 11.2 迁移安全

- 所有 CREATE TABLE 使用 `IF NOT EXISTS`
- 所有 INSERT 使用 `INSERT OR IGNORE` / `WHERE NOT EXISTS` 防止重复
- 旧表保留不删除
- 迁移可多次执行（幂等）

---

## 12. 测试策略

### 12.1 单元测试

| 模块 | 测试内容 |
|------|----------|
| 事实超驰 | 矛盾检测、时间窗口冲突解析、重复事实合并 |
| 实体演化 | summary 重写、版本递增、向量同步 |
| 混合检索 | RRF 融合、语义/全文/图各路搜索正确性 |
| 社区发现 | 标签传播收敛、社区摘要生成 |
| 时序过滤 | 当前有效查询、历史快照查询 |

### 12.2 集成测试

端到端流程：
```
Episode 摄入 → LLM 提取 → 实体演化 → 事实超驰 → 混合检索验证
```

测试场景：
1. 用户说"喜欢篮球" → 提取关系 → 后说"改打网球" → 旧关系自动失效
2. 多次对话后实体 summary 压缩正确
3. 检索结果按 RRF 排序，时序过滤正确
4. 社区发现聚类合理

### 12.3 回归测试

- v1 数据迁移后完整性验证（实体数、关系数一致）
- 功能开关切换：v2 关闭时降级到 v1 正常工作

### 12.4 性能测试

- 混合检索 P95 < 500ms
- Episode 摄入（含 LLM 调用）P95 < 3s
- 社区发现（100 实体规模）< 5s

---

## 13. 不采纳的部分

| graphiti 特性 | 不采纳原因 |
|---------------|-----------|
| Neo4j/FalkorDB/Kuzu 图数据库 | 项目使用 SQLite，不引入新依赖 |
| Cross-Encoder 重排 | 引入额外模型依赖，RRF 已足够 |
| MMR 多样性重排 | 复杂度高，YAGNI |
| Saga 长篇叙事链 | 场景不匹配，情感陪伴 Agent 不需要 |
| 自定义 Pydantic 本体 | 硬编码5种 kind 足够，YAGNI |
| GraphDriver 抽象层 | 仅一个后端（SQLite），不需要抽象 |
| Attribute Capping (250字截断) | LLM 输出由 prompt 控制长度，不需要额外截断 |
| MinHash + LSH 模糊去重 | 精确匹配 + LLM 判断已足够，YAGNI |
