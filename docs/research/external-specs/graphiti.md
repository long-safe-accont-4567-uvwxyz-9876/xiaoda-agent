# xiaoda-agent 知识图谱优化 Spec：基于 graphiti 核心机制

> **版本**: v1.0  
> **项目**: xiaoda-agent v0.5.03  
> **参考**: getzep/graphiti (28K⭐, Zep 开源时序知识图谱引擎)  
> **日期**: 2025-07

---

## 目录

1. [现状诊断](#1-现状诊断)
2. [graphiti 核心机制深度分析](#2-graphiti-核心机制深度分析)
3. [差距对比表](#3-差距对比表)
4. [优化方案](#4-优化方案)
5. [实施步骤](#5-实施步骤)
6. [预期效果](#6-预期效果)
7. [不采纳的部分](#7-不采纳的部分)

---

## 1. 现状诊断

### 1.1 架构概览

xiaoda-agent 当前知识图谱由两个核心文件构成：

| 文件 | 职责 |
|------|------|
| `memory/knowledge_graph.py` | LLM实体关系提取、JSON解析、合并逻辑、检索增强 |
| `db/db_knowledge.py` | SQLite持久化（aiosqlite）、CRUD、FTS5搜索、图遍历 |

### 1.2 当前 Schema

```sql
-- 现有表结构（无时序、无超驰、无溯源）
CREATE TABLE knowledge_entities (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE,
    kind TEXT DEFAULT '',           -- 人物/游戏/地点/概念/物品
    observations TEXT DEFAULT '[]', -- JSON数组，无版本
    updated_at REAL NOT NULL        -- 单一时间戳，覆盖更新
);

CREATE TABLE knowledge_relations (
    id TEXT PRIMARY KEY,
    from_entity TEXT,
    relation_type TEXT,
    to_entity TEXT,
    created_at REAL DEFAULT 0,
    updated_at REAL NOT NULL        -- 无valid_at/invalid_at
);
```

### 1.3 核心问题

#### P1: 无时序追踪 — 事实无有效期窗口

当前 `knowledge_relations` 没有 `valid_at` / `invalid_at` 字段。系统无法区分：
- "小明喜欢篮球"（过去式，已不再成立）
- "小明喜欢篮球"（现在式，仍然成立）

`observations` 同理——追加式合并（`merge_entity` 仅做 `if obs not in merged: merged.append(obs)`），无版本概念，旧观察永远存在。

**影响**：情感陪伴Agent无法感知用户偏好的时间演变，可能持续引用过时事实。

#### P2: 无事实超驰 — 观点变化时旧事实不更新

当用户说"我现在不喜欢篮球了，改打网球"，当前系统：
1. LLM提取出新关系 `小明 → 喜欢 → 网球`
2. 旧关系 `小明 → 喜欢 → 篮球` **依然保留**
3. 检索时两者同时出现，Agent无法判断哪个是当前状态

graphiti的做法：新事实自动 `invalidat` 旧事实，设置 `invalid_at = 新事实.valid_at`，保留完整历史但标记失效。

#### P3: 无图遍历检索 — 仅BFS邻接展开

当前 `get_related_knowledge` 实现了 depth=1/2 的BFS邻接展开，但：
- 无语义向量检索（无 embedding 字段）
- 无混合检索（hybrid: 语义 + 关键词 + 图距离）
- 无重排序（cross-encoder reranking）
- 检索结果仅按 `updated_at` 排序，无相关度评分

#### P4: 实体不演化 — 摘要无版本无更新

`EntityNode.summary` 在 graphiti 中是核心字段——每次 `add_episode` 都会重新生成实体摘要，融合新事实。xiaoda-agent 的 `observations` 是简单追加列表：
```python
# 当前 merge_entity 逻辑
merged = list(old_obs)
for obs in new_obs:
    if obs not in merged:
        merged.append(obs)
```
无压缩、无摘要、无去重、无语义归并。

#### P5: 无 Episode 溯源 — 事实无法追溯到原始对话

graphiti 中每条 `EntityEdge` 携带 `episodes: list[str]`，指向派生它的原始 `EpisodicNode`。xiaoda-agent 的 `knowledge_relations` 无此字段，无法回答"这条知识是从哪次对话中提取的"。

#### P6: 无社区发现 — 实体聚类缺失

graphiti 使用标签传播算法 (`label_propagation`) 自动发现实体社区，生成 `CommunityNode` 和 `CommunityEdge`。这为高层语义检索和图结构理解提供基础。xiaoda-agent 完全缺失此能力。

---

## 2. graphiti 核心机制深度分析

### 2.1 时序上下文图 (Temporal Context Graph)

graphiti 的核心创新：每条事实（`EntityEdge`）携带时间窗口：

```python
class EntityEdge(Edge):
    name: str                          # 关系名称
    fact: str                          # 自然语言事实陈述
    fact_embedding: list[float] | None # 事实向量
    episodes: list[str]                # 派生此事实的Episode UUID列表
    valid_at: datetime | None          # 事实生效时间
    invalid_at: datetime | None        # 事实失效时间
    expired_at: datetime | None        # 系统标记过期时间
    reference_time: datetime | None    # 产生此事实的Episode的参考时间
```

**检索时自动过滤**：search 方法支持 `SearchFilters` 可按时间窗口过滤，只返回当前有效事实。

**关键设计**：
- `valid_at` 来自Episode的参考时间（用户说这句话的时间）
- `invalid_at` 由事实超驰逻辑自动设置
- `expired_at` 是系统时间戳，标记何时被系统判定为过期
- `reference_time` 是原始Episode的时间，用于溯源

### 2.2 事实超驰 (Fact Invalidation)

graphiti 的 `resolve_extracted_edge` 是核心超驰逻辑：

```
新事实提取 → 搜索已有同端点边(related_edges) → 搜索全局可能冲突边(existing_edges)
    ↓
LLM判断：
  1. 是否是重复事实(duplicate) → 合并episode引用
  2. 是否是矛盾事实(contradicted) → 标记旧边invalid_at=新边.valid_at
    ↓
resolve_edge_contradictions():
  - 如果旧边.invalid_at ≤ 新边.valid_at → 不冲突（旧事实已失效）
  - 如果新边.invalid_at ≤ 旧边.valid_at → 不冲突（新事实已失效）
  - 否则 → 旧边.invalid_at = 新边.valid_at，旧边.expired_at = now()
```

**关键代码路径**（`edge_operations.py`）：
```python
def resolve_edge_contradictions(resolved_edge, invalidation_candidates):
    for edge in invalidation_candidates:
        if (edge_invalid_at ≤ resolved_edge_valid_at) or \
           (resolved_edge_invalid_at ≤ edge_valid_at):
            continue  # 时间窗口不重叠，不冲突
        elif edge_valid_at < resolved_edge_valid_at:
            edge.invalid_at = resolved_edge.valid_at  # 旧事实失效
            edge.expired_at = edge.expired_at or utc_now()
            invalidated_edges.append(edge)
    return invalidated_edges
```

### 2.3 实体演化 (Entity Evolution)

graphiti 的 `EntityNode` 携带 `summary` 字段，每次 `add_episode` 后重新生成：

```
add_episode → extract_nodes → resolve_extracted_nodes → extract_attributes_from_nodes
    ↓
_extract_entity_summaries_batch():
  - 如果 summary + edge_facts ≤ 阈值 → 直接拼接（无LLM调用）
  - 否则 → 批量LLM调用重写summary
    ↓
每30个实体一批(batch)，并行处理
summary 截断到 MAX_SUMMARY_CHARS
```

**关键**：summary 是**替换式**而非追加式——每次融合新信息后重写整个摘要，避免 observations 无限膨胀。

### 2.4 Episode 溯源 (Episode Provenance)

graphiti 的溯源链路：

```
EpisodicNode (原始对话/文本)
    ↓ MENTIONS边
EntityNode (提取的实体)
    ↓ RELATES_TO边 (携带 episodes: list[str])
EntityEdge (提取的事实)
```

每条 `EntityEdge.episodes` 记录了所有派生它的 `EpisodicNode.uuid`。当同一事实在多个Episode中出现时，追加引用：
```python
if episode is not None and episode.uuid not in resolved.episodes:
    resolved.episodes.append(episode.uuid)
```

还支持 Saga 机制（长篇叙事链）：
- `SagaNode` → `HAS_EPISODE` → 多个 `EpisodicNode`
- `EpisodicNode` → `NEXT_EPISODE` → 下一个 `EpisodicNode`（时序链）
- `summarize_saga()` 增量式汇总长篇叙事

### 2.5 社区发现 (Community Detection)

graphiti 使用**标签传播算法** (`label_propagation`)：

```
1. 初始化：每个节点一个社区
2. 迭代：每个节点取邻居中边数最多的社区
3. 平局：取最大社区
4. 终止：无变化时停止
```

社区发现后：
1. 对每个社区内的实体summary做**层级汇总**（两两配对 → LLM合并 → 递归直到一个summary）
2. 生成 `CommunityNode`（含 name + summary）
3. 生成 `CommunityEdge`（HAS_MEMBER 边连接社区到成员实体）
4. 社区node也生成embedding用于检索

**增量更新**：`update_community()` 对新增实体判断归属社区，仅更新受影响的社区summary。

### 2.6 混合检索 (Hybrid Retrieval)

graphiti 的 search 架构：

```
SearchConfig 定义三层：
  EdgeSearchConfig:
    - method: cosine_similarity | fulltext_search | bfs | rrf
    - reranker: none | cross_encoder | node_distance | episode_mentions
  
  NodeSearchConfig:
    - method: cosine_similarity | fulltext_search | bfs
    - reranker: none | cross_encoder | mmr
  
  CommunitySearchConfig:
    - method: cosine_similarity | fulltext_search
    - reranker: none | cross_encoder
```

**预置配方**：
- `EDGE_HYBRID_SEARCH_RRF`: 语义+全文 → RRF融合
- `EDGE_HYBRID_SEARCH_NODE_DISTANCE`: 加图距离重排
- `COMBINED_HYBRID_SEARCH_CROSS_ENCODER`: 全层 + Cross-Encoder重排

---

## 3. 差距对比表

| 能力维度 | xiaoda-agent v0.5.03 | graphiti | 差距等级 |
|---------|---------------------|----------|---------|
| **时序追踪** | 无 `valid_at`/`invalid_at`，关系和观察无有效期 | 每条事实有 `valid_at`/`invalid_at` 时间窗口 | 🔴 严重 |
| **事实超驰** | 无，旧事实与新事实并存，无矛盾检测 | `resolve_edge_contradictions` 自动标记旧事实失效 | 🔴 严重 |
| **实体演化** | `observations` 追加列表，无压缩无摘要 | `summary` 每次episode后重写，批量LLM汇总 | 🟡 中等 |
| **Episode溯源** | 无，关系无法追溯到原始对话 | `episodes: list[str]` + MENTIONS边完整溯源链 | 🟡 中等 |
| **图遍历检索** | BFS depth=1/2邻接展开，无语义检索 | BFS + 语义 + 全文 + RRF融合 + Cross-Encoder重排 | 🟡 中等 |
| **社区发现** | 无 | 标签传播 + 层级LLM汇总 + 增量更新 | 🟠 低优先 |
| **向量检索** | 无embedding字段，仅FTS5+LIKE | name_embedding + fact_embedding 全覆盖 | 🔴 严重 |
| **去重** | `INSERT OR IGNORE` 按名称去重 | 语义相似度 + LLM判定双重去重 | 🟡 中等 |
| **数据后端** | SQLite (aiosqlite) | Neo4j/FalkorDB/Kuzu/Neptune | ✅ 无需对齐 |
| **自定义本体** | 硬编码5种kind | Pydantic模型定义entity和edge类型 | 🟠 低优先 |

---

## 4. 优化方案

### 4.1 时序事实表 Schema 扩展

#### 4.1.1 新 Schema 设计

```sql
-- 新增：Episode溯源表（替代直接从对话摘要提取的逻辑）
CREATE TABLE IF NOT EXISTS kg_episodes (
    id TEXT PRIMARY KEY,                -- EP-前缀UUID
    content TEXT NOT NULL,              -- 原始对话摘要内容
    source_type TEXT DEFAULT 'summary', -- summary | message | text
    source_description TEXT DEFAULT '', -- 来源描述
    valid_at REAL NOT NULL,             -- Episode参考时间（对话发生时间）
    created_at REAL NOT NULL,           -- 系统录入时间
    group_id TEXT DEFAULT 'default'     -- 分组ID（预留）
);

-- 扩展：实体表增加摘要和版本
CREATE TABLE IF NOT EXISTS knowledge_entities_v2 (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE,
    kind TEXT DEFAULT '',
    observations TEXT DEFAULT '[]',     -- 保留兼容
    summary TEXT DEFAULT '',            -- 🆕 实体摘要（替换式更新）
    summary_version INTEGER DEFAULT 0,  -- 🆕 摘要版本号
    name_embedding TEXT DEFAULT NULL,   -- 🆕 名称向量（JSON数组）
    updated_at REAL NOT NULL,
    created_at REAL NOT NULL
);

-- 扩展：关系表增加时序窗口和溯源
CREATE TABLE IF NOT EXISTS knowledge_relations_v2 (
    id TEXT PRIMARY KEY,
    from_entity TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    to_entity TEXT NOT NULL,
    fact TEXT DEFAULT '',               -- 🆕 自然语言事实陈述
    fact_embedding TEXT DEFAULT NULL,   -- 🆕 事实向量（JSON数组）
    episode_ids TEXT DEFAULT '[]',      -- 🆕 派生此关系的Episode ID列表
    valid_at REAL DEFAULT NULL,         -- 🆕 事实生效时间（Unix时间戳）
    invalid_at REAL DEFAULT NULL,       -- 🆕 事实失效时间（Unix时间戳）
    expired_at REAL DEFAULT NULL,       -- 🆕 系统标记过期时间
    is_current INTEGER DEFAULT 1,       -- 🆕 是否当前有效（1=有效, 0=已失效）
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- 🆕 社区表（简化版）
CREATE TABLE IF NOT EXISTS kg_communities (
    id TEXT PRIMARY KEY,                -- COM-前缀UUID
    name TEXT NOT NULL,                 -- 社区名称
    summary TEXT DEFAULT '',            -- 社区摘要
    member_entities TEXT DEFAULT '[]',  -- 成员实体名列表（JSON数组）
    name_embedding TEXT DEFAULT NULL,   -- 名称向量
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- 🆕 索引
CREATE INDEX IF NOT EXISTS idx_rel_v2_from ON knowledge_relations_v2(from_entity);
CREATE INDEX IF NOT EXISTS idx_rel_v2_to ON knowledge_relations_v2(to_entity);
CREATE INDEX IF NOT EXISTS idx_rel_v2_current ON knowledge_relations_v2(is_current);
CREATE INDEX IF NOT EXISTS idx_rel_v2_valid_at ON knowledge_relations_v2(valid_at);
CREATE INDEX IF NOT EXISTS idx_rel_v2_invalid_at ON knowledge_relations_v2(invalid_at);
CREATE INDEX IF NOT EXISTS idx_episode_valid_at ON kg_episodes(valid_at);
CREATE INDEX IF NOT EXISTS idx_entity_v2_name ON knowledge_entities_v2(name);
```

#### 4.1.2 数据迁移策略

```python
async def migrate_v1_to_v2(conn: aiosqlite.Connection) -> None:
    """从v1表迁移数据到v2表，保留旧数据完整"""
    # 1. 创建v2表（IF NOT EXISTS保证幂等）
    # 2. 迁移entities: 从knowledge_entities → knowledge_entities_v2
    #    summary = observations的join（兼容性转换）
    #    summary_version = 0
    # 3. 迁移relations: 从knowledge_relations → knowledge_relations_v2
    #    valid_at = created_at（无法推断时用录入时间）
    #    invalid_at = NULL（旧数据视为永久有效）
    #    is_current = 1
    #    fact = "{from_entity} {relation_type} {to_entity}"
    # 4. 保留旧表不删除（回滚安全）
```

### 4.2 事实超驰逻辑

#### 4.2.1 核心流程

```python
class KnowledgeGraphV2:
    """时序知识图谱，支持事实超驰"""

    async def add_facts_from_episode(
        self,
        episode_content: str,
        episode_time: float,       # time.time() 对应的对话时间
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
        await self.knowledge_db.insert_episode(
            episode_id, episode_content, source_type, episode_time, now
        )

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
            new, invalidated = await self.merge_relation_v2(
                rel, episode_id, episode_time
            )
            new_facts_count += int(new)
            invalidated_count += len(invalidated)

        return {
            "episode_id": episode_id,
            "new_facts": new_facts_count,
            "invalidated": invalidated_count,
        }
```

#### 4.2.2 事实超驰核心逻辑

```python
async def merge_relation_v2(
    self,
    relation: dict,
    episode_id: str,
    episode_time: float,
) -> tuple[bool, list[dict]]:
    """
    合并新关系到知识图谱，自动处理超驰。

    Returns:
        (is_new, invalidated_relations)
    """
    from_entity = relation.get("from_entity", "")
    relation_type = relation.get("relation_type", "")
    to_entity = relation.get("to_entity", "")
    fact = relation.get("fact", f"{from_entity} {relation_type} {to_entity}")

    if not from_entity or not relation_type or not to_entity:
        return False, []

    # 1. 查找同端点的已有关系
    existing = await self.knowledge_db.get_active_relations_between(
        from_entity, to_entity
    )

    # 2. 查找可能冲突的关系（同类型 + 同端点方向）
    conflict_candidates = [
        r for r in existing
        if r["relation_type"] == relation_type
        and r.get("is_current", 1) == 1
    ]

    # 3. LLM判断事实冲突（仅在存在候选时调用）
    invalidated = []
    is_duplicate = False

    if conflict_candidates:
        # 3a. 先检查完全重复
        for candidate in conflict_candidates:
            if candidate.get("fact", "") == fact:
                is_duplicate = True
                # 追加episode引用
                await self._append_episode_ref(candidate["id"], episode_id)
                break

        # 3b. 不重复则用LLM判断是否矛盾
        if not is_duplicate:
            contradictions = await self._detect_contradictions(
                new_fact=fact,
                existing_facts=[r.get("fact", "") for r in conflict_candidates],
                episode_time=episode_time,
            )
            for idx in contradictions:
                candidate = conflict_candidates[idx]
                # 标记旧事实失效
                await self.knowledge_db.invalidate_relation(
                    relation_id=candidate["id"],
                    invalid_at=episode_time,  # 旧事实在此时刻失效
                )
                invalidated.append(candidate)

    # 4. 插入新关系
    if not is_duplicate:
        rel_id = f"REL-{uuid.uuid4().hex[:12]}"
        await self.knowledge_db.insert_relation_v2(
            relation_id=rel_id,
            from_entity=from_entity,
            relation_type=relation_type,
            to_entity=to_entity,
            fact=fact,
            episode_ids=[episode_id],
            valid_at=episode_time,
            invalid_at=None,
            is_current=1,
        )
        return True, invalidated

    return False, invalidated


async def _detect_contradictions(
    self,
    new_fact: str,
    existing_facts: list[str],
    episode_time: float,
) -> list[int]:
    """
    使用LLM判断新事实与哪些旧事实矛盾。

    返回矛盾的旧事实索引列表。
    对标graphiti的EdgeDuplicate.contradicted_facts机制，
    但简化为单次LLM调用。
    """
    if not existing_facts:
        return []

    prompt = f"""判断以下新事实是否与已有事实矛盾。

新事实：{new_fact}

已有事实：
{chr(10).join(f"[{i}] {f}" for i, f in enumerate(existing_facts))}

规则：
1. 只有直接矛盾才算（如"喜欢"vs"不喜欢"、"在A地"vs"在B地"）
2. 互补信息不算矛盾（如"喜欢篮球"和"也喜欢网球"）
3. 返回矛盾的已有事实编号列表

严格输出JSON：{{"contradicted": [0, 2]}}"""

    result = await self._call_free_model(
        [
            {"role": "system", "content": "你是事实冲突检测助手，只输出纯JSON。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=200,
    )

    if result:
        try:
            parsed = json.loads(_clean_json_response(result))
            return [i for i in parsed.get("contradicted", []) if 0 <= i < len(existing_facts)]
        except (json.JSONDecodeError, TypeError):
            pass

    return []
```

#### 4.2.3 KnowledgeDB 超驰方法

```python
# db/db_knowledge.py 新增方法

async def insert_episode(self, episode_id: str, content: str,
                          source_type: str, valid_at: float,
                          created_at: float) -> None:
    await self._conn.execute(
        """INSERT INTO kg_episodes (id, content, source_type, valid_at, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (episode_id, content, source_type, valid_at, created_at),
    )
    await self._conn.commit()

async def insert_relation_v2(self, relation_id: str, from_entity: str,
                              relation_type: str, to_entity: str,
                              fact: str, episode_ids: list[str],
                              valid_at: float | None, invalid_at: float | None,
                              is_current: int = 1) -> None:
    ep_json = json.dumps(episode_ids, ensure_ascii=False)
    now = time.time()
    await self._conn.execute(
        """INSERT INTO knowledge_relations_v2
           (id, from_entity, relation_type, to_entity, fact, episode_ids,
            valid_at, invalid_at, is_current, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (relation_id, from_entity, relation_type, to_entity, fact,
         ep_json, valid_at, invalid_at, is_current, now, now),
    )
    await self._conn.commit()

async def invalidate_relation(self, relation_id: str, invalid_at: float) -> None:
    """标记关系失效——事实超驰的核心操作"""
    now = time.time()
    await self._conn.execute(
        """UPDATE knowledge_relations_v2
           SET invalid_at=?, expired_at=?, is_current=0, updated_at=?
           WHERE id=?""",
        (invalid_at, now, now, relation_id),
    )
    await self._conn.commit()

async def get_active_relations_between(self, from_entity: str,
                                        to_entity: str) -> list[dict]:
    """获取两个实体间当前有效的关系"""
    cursor = await self._conn.execute(
        """SELECT * FROM knowledge_relations_v2
           WHERE from_entity=? AND to_entity=? AND is_current=1
           ORDER BY valid_at DESC""",
        (from_entity, to_entity),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]

async def _append_episode_ref(self, relation_id: str, episode_id: str) -> None:
    """追加Episode引用到已有关系"""
    cursor = await self._conn.execute(
        "SELECT episode_ids FROM knowledge_relations_v2 WHERE id=?",
        (relation_id,),
    )
    row = await cursor.fetchone()
    if row:
        eps = json.loads(row["episode_ids"]) if row["episode_ids"] else []
        if episode_id not in eps:
            eps.append(episode_id)
            await self._conn.execute(
                "UPDATE knowledge_relations_v2 SET episode_ids=?, updated_at=? WHERE id=?",
                (json.dumps(eps, ensure_ascii=False), time.time(), relation_id),
            )
            await self._conn.commit()
```

### 4.3 实体摘要演化

#### 4.3.1 替换式摘要更新

```python
async def merge_entities_v2(
    self,
    entities: list[dict],
    episode_content: str,
    episode_time: float,
) -> None:
    """合并实体，含摘要演化"""
    for ent in entities[:5]:
        name = ent.get("name", "")
        if not name:
            continue
        kind = ent.get("kind", "")
        new_obs = ent.get("observations", [])

        existing = await self.knowledge_db.get_entity_v2(name)
        if existing:
            # 摘要演化：融合新旧信息后重写
            old_summary = existing.get("summary", "")
            old_obs = existing.get("observations", "[]")
            if isinstance(old_obs, str):
                try:
                    old_obs = json.loads(old_obs)
                except (json.JSONDecodeError, TypeError):
                    old_obs = []

            # 合并observations（保留兼容）
            merged_obs = list(old_obs)
            for obs in new_obs:
                if obs not in merged_obs:
                    merged_obs.append(obs)

            # 判断是否需要LLM重写摘要
            current_summary = old_summary or "；".join(str(o) for o in merged_obs[:5])
            new_info = "；".join(str(o) for o in new_obs)

            if new_info and len(current_summary) < 2000:
                # 摘要未超限，尝试LLM融合
                updated_summary = await self._evolve_summary(
                    current_summary, new_info, episode_content
                )
            else:
                # 摘要过长，直接截断
                updated_summary = current_summary[:1500]

            await self.knowledge_db.update_entity_v2(
                name=name,
                kind=kind or existing.get("kind", ""),
                observations=merged_obs,
                summary=updated_summary,
                summary_version=existing.get("summary_version", 0) + 1,
            )
        else:
            # 新实体
            entity_id = f"ENT-{uuid.uuid4().hex[:12]}"
            initial_summary = "；".join(str(o) for o in new_obs[:3]) if new_obs else ""
            await self.knowledge_db.insert_entity_v2(
                entity_id, name, kind, new_obs, initial_summary
            )


async def _evolve_summary(
    self,
    current_summary: str,
    new_info: str,
    episode_content: str,
) -> str:
    """用LLM融合新信息到实体摘要中（替换式，非追加式）"""
    prompt = f"""将新信息融合到实体摘要中，生成更新后的摘要。

当前摘要：{current_summary[:500]}
新信息：{new_info[:200]}
上下文：{episode_content[:300]}

规则：
1. 保留当前摘要中的重要信息
2. 融合新信息，如果与旧信息矛盾则替换旧信息
3. 摘要应简洁（200字以内），聚焦关键事实
4. 使用第三人称描述

直接输出更新后的摘要文本，不要输出其他内容。"""

    result = await self._call_free_model(
        [
            {"role": "system", "content": "你是信息整合助手，输出简洁的实体摘要。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=400,
    )

    if result and result.strip():
        return result.strip()[:1500]  # 安全截断

    # LLM失败时退化为拼接
    return f"{current_summary}；{new_info}"[:1500]
```

### 4.4 SQLite 图遍历查询

#### 4.4.1 CTE递归查询方案

由于SQLite不支持Cypher/GQL等图查询语言，使用 **递归CTE (Common Table Expression)** 实现图遍历：

```sql
-- 方案1：BFS多跳遍历（对标graphiti的get_related_knowledge）
-- 从指定实体出发，沿关系图遍历N跳，返回所有关联实体和关系

WITH RECURSIVE graph_traverse AS (
    -- 基础查询：起始实体
    SELECT
        e.name AS entity_name,
        e.kind AS entity_kind,
        e.summary AS entity_summary,
        0 AS hop_depth,
        e.name AS path  -- 路径追踪
    FROM knowledge_entities_v2 e
    WHERE e.name IN ({start_placeholders})

    UNION ALL

    -- 递归查询：沿关系扩展
    SELECT
        CASE
            WHEN r.from_entity = gt.entity_name THEN te.name
            ELSE fe.name
        END AS entity_name,
        CASE
            WHEN r.from_entity = gt.entity_name THEN te.kind
            ELSE fe.kind
        END AS entity_kind,
        CASE
            WHEN r.from_entity = gt.entity_name THEN te.summary
            ELSE fe.summary
        END AS entity_summary,
        gt.hop_depth + 1 AS hop_depth,
        gt.path || ' -> ' ||
        CASE WHEN r.from_entity = gt.entity_name THEN te.name ELSE fe.name END AS path
    FROM graph_traverse gt
    JOIN knowledge_relations_v2 r
        ON (r.from_entity = gt.entity_name OR r.to_entity = gt.entity_name)
        AND r.is_current = 1  -- 🆕 只遍历当前有效的关系
    JOIN knowledge_entities_v2 fe ON fe.name = r.from_entity
    JOIN knowledge_entities_v2 te ON te.name = r.to_entity
    WHERE gt.hop_depth < ?   -- 最大深度
      AND gt.path NOT LIKE '%' || 
          CASE WHEN r.from_entity = gt.entity_name THEN te.name ELSE fe.name END || '%'  -- 防环
)
SELECT DISTINCT entity_name, entity_kind, entity_summary, MIN(hop_depth) AS min_depth
FROM graph_traverse
WHERE hop_depth > 0  -- 排除起始实体
GROUP BY entity_name
ORDER BY min_depth
LIMIT ?;
```

#### 4.4.2 时序过滤的图遍历

```sql
-- 方案2：时序感知的图遍历
-- 只遍历在指定时间点有效的关系（核心创新）

WITH RECURSIVE temporal_traverse AS (
    -- 基础查询
    SELECT e.name AS entity_name, 0 AS hop_depth
    FROM knowledge_entities_v2 e
    WHERE e.name IN ({start_placeholders})

    UNION ALL

    -- 递归：只沿"在query_time时有效"的关系扩展
    SELECT
        CASE
            WHEN r.from_entity = tt.entity_name THEN r.to_entity
            ELSE r.from_entity
        END AS entity_name,
        tt.hop_depth + 1 AS hop_depth
    FROM temporal_traverse tt
    JOIN knowledge_relations_v2 r
        ON (r.from_entity = tt.entity_name OR r.to_entity = tt.entity_name)
        AND r.is_current = 1
        AND r.valid_at <= ?           -- 事实在查询时间前生效
        AND (r.invalid_at IS NULL     -- 事实尚未失效
             OR r.invalid_at > ?)     -- 或在查询时间后才失效
    WHERE tt.hop_depth < ?
)
SELECT DISTINCT entity_name, MIN(hop_depth) AS min_depth
FROM temporal_traverse
WHERE hop_depth > 0
GROUP BY entity_name
ORDER BY min_depth
LIMIT ?;
```

#### 4.4.3 防环优化

```sql
-- 方案3：高效防环的CTE遍历（使用visited集合）
-- SQLite的CTE不支持ARRAY类型，用路径字符串防环

WITH RECURSIVE safe_traverse AS (
    SELECT
        e.name AS entity_name,
        '|' || e.name || '|' AS visited,  -- 管道符分隔的已访问列表
        0 AS hop_depth
    FROM knowledge_entities_v2 e
    WHERE e.name IN ({start_placeholders})

    UNION ALL

    SELECT
        next_entity.name AS entity_name,
        st.visited || next_entity.name || '|' AS visited,
        st.hop_depth + 1 AS hop_depth
    FROM safe_traverse st
    JOIN knowledge_relations_v2 r
        ON (r.from_entity = st.entity_name OR r.to_entity = st.entity_name)
        AND r.is_current = 1
    CROSS JOIN (
        SELECT CASE
            WHEN r.from_entity = st.entity_name THEN r.to_entity
            ELSE r.from_entity
        END AS name
    ) next_entity
    JOIN knowledge_entities_v2 ne ON ne.name = next_entity.name
    WHERE st.hop_depth < ?
      AND st.visited NOT LIKE '%|' || next_entity.name || '|%'  -- 防环
)
SELECT DISTINCT entity_name, MIN(hop_depth) AS min_depth
FROM safe_traverse
WHERE hop_depth > 0
GROUP BY entity_name
ORDER BY min_depth
LIMIT ?;
```

> **注意**：SQLite CTE递归默认限制1000层（`SQLITE_MAX_RECURSION`），对于情感陪伴Agent的知识图谱（通常<500实体），depth=2-3 的遍历完全在安全范围内。生产环境应在代码中校验depth参数。

#### 4.4.4 Python封装

```python
async def graph_traverse(
    self,
    entity_names: list[str],
    max_depth: int = 2,
    query_time: float | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    CTE递归图遍历（对标graphiti的BFS search）。

    Args:
        entity_names: 起始实体名列表
        max_depth: 最大遍历深度（1-3）
        query_time: 时序过滤时间点，None=不限时间
        limit: 返回结果上限
    """
    if max_depth < 1:
        max_depth = 1
    if max_depth > 3:
        max_depth = 3  # 安全限制

    placeholders = ",".join("?" * len(entity_names))

    if query_time is not None:
        # 时序感知遍历
        sql = f"""
        WITH RECURSIVE temporal_traverse AS (
            SELECT e.name AS entity_name, e.summary, e.kind, 0 AS hop_depth,
                   '|' || e.name || '|' AS visited
            FROM knowledge_entities_v2 e
            WHERE e.name IN ({placeholders})

            UNION ALL

            SELECT
                CASE WHEN r.from_entity = tt.entity_name THEN r.to_entity
                     ELSE r.from_entity END AS entity_name,
                CASE WHEN r.from_entity = tt.entity_name
                     THEN (SELECT summary FROM knowledge_entities_v2 WHERE name = r.to_entity)
                     ELSE (SELECT summary FROM knowledge_entities_v2 WHERE name = r.from_entity)
                END AS summary,
                CASE WHEN r.from_entity = tt.entity_name
                     THEN (SELECT kind FROM knowledge_entities_v2 WHERE name = r.to_entity)
                     ELSE (SELECT kind FROM knowledge_entities_v2 WHERE name = r.from_entity)
                END AS kind,
                tt.hop_depth + 1 AS hop_depth,
                tt.visited ||
                CASE WHEN r.from_entity = tt.entity_name THEN r.to_entity
                     ELSE r.from_entity END || '|' AS visited
            FROM temporal_traverse tt
            JOIN knowledge_relations_v2 r
                ON (r.from_entity = tt.entity_name OR r.to_entity = tt.entity_name)
                AND r.is_current = 1
                AND r.valid_at <= ?
                AND (r.invalid_at IS NULL OR r.invalid_at > ?)
            WHERE tt.hop_depth < ?
              AND tt.visited NOT LIKE '%|' ||
                  CASE WHEN r.from_entity = tt.entity_name THEN r.to_entity
                       ELSE r.from_entity END || '|%'
        )
        SELECT DISTINCT entity_name, summary, kind, MIN(hop_depth) AS min_depth
        FROM temporal_traverse
        WHERE hop_depth > 0
        GROUP BY entity_name
        ORDER BY min_depth
        LIMIT ?
        """
        params = entity_names + [query_time, query_time, max_depth, limit]
    else:
        # 无时序过滤遍历
        sql = f"""
        WITH RECURSIVE graph_traverse AS (
            SELECT e.name AS entity_name, e.summary, e.kind, 0 AS hop_depth,
                   '|' || e.name || '|' AS visited
            FROM knowledge_entities_v2 e
            WHERE e.name IN ({placeholders})

            UNION ALL

            SELECT
                CASE WHEN r.from_entity = gt.entity_name THEN r.to_entity
                     ELSE r.from_entity END AS entity_name,
                CASE WHEN r.from_entity = gt.entity_name
                     THEN (SELECT summary FROM knowledge_entities_v2 WHERE name = r.to_entity)
                     ELSE (SELECT summary FROM knowledge_entities_v2 WHERE name = r.from_entity)
                END AS summary,
                CASE WHEN r.from_entity = gt.entity_name
                     THEN (SELECT kind FROM knowledge_entities_v2 WHERE name = r.to_entity)
                     ELSE (SELECT kind FROM knowledge_entities_v2 WHERE name = r.from_entity)
                END AS kind,
                gt.hop_depth + 1 AS hop_depth,
                gt.visited ||
                CASE WHEN r.from_entity = gt.entity_name THEN r.to_entity
                     ELSE r.from_entity END || '|' AS visited
            FROM graph_traverse gt
            JOIN knowledge_relations_v2 r
                ON (r.from_entity = gt.entity_name OR r.to_entity = gt.entity_name)
                AND r.is_current = 1
            WHERE gt.hop_depth < ?
              AND gt.visited NOT LIKE '%|' ||
                  CASE WHEN r.from_entity = gt.entity_name THEN r.to_entity
                       ELSE r.from_entity END || '|%'
        )
        SELECT DISTINCT entity_name, summary, kind, MIN(hop_depth) AS min_depth
        FROM graph_traverse
        WHERE hop_depth > 0
        GROUP BY entity_name
        ORDER BY min_depth
        LIMIT ?
        """
        params = entity_names + [max_depth, limit]

    cursor = await self._conn.execute(sql, params)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]
```

### 4.5 社区发现简化版

#### 4.5.1 基于连接度的标签传播

对标graphiti的 `label_propagation`，但用纯SQL+Python在SQLite上实现：

```python
async def build_communities(self) -> int:
    """
    简化版社区发现：基于连接度的标签传播算法。

    对标graphiti的label_propagation + build_community，
    但省略LLM汇总（社区摘要用成员实体名拼接）。
    """
    # Step 1: 构建邻接投影（对标graphiti的projection构建）
    entities = await self.knowledge_db.get_all_entities_v2()
    if len(entities) < 3:
        return 0

    # 构建邻接表 {entity_name: [(neighbor_name, edge_count)]}
    projection: dict[str, dict[str, int]] = {}
    for ent in entities:
        name = ent["name"]
        relations = await self.knowledge_db.get_active_relations(name)
        neighbor_counts: dict[str, int] = {}
        for rel in relations:
            neighbor = rel["to_entity"] if rel["from_entity"] == name else rel["from_entity"]
            neighbor_counts[neighbor] = neighbor_counts.get(neighbor, 0) + 1
        if neighbor_counts:  # 只加入有连接的实体
            projection[name] = neighbor_counts

    if len(projection) < 3:
        return 0

    # Step 2: 标签传播（直接移植graphiti的label_propagation逻辑）
    community_map = {name: i for i, name in enumerate(projection.keys())}

    for _ in range(20):  # 最大迭代20轮
        no_change = True
        new_community_map = {}

        for name, neighbors in projection.items():
            curr_community = community_map[name]

            # 统计邻居社区投票（按边数加权）
            community_candidates: dict[int, int] = {}
            for neighbor, edge_count in neighbors.items():
                if neighbor in community_map:
                    nc = community_map[neighbor]
                    community_candidates[nc] = community_candidates.get(nc, 0) + edge_count

            if not community_candidates:
                new_community_map[name] = curr_community
                continue

            # 选边数最多的邻居社区
            sorted_candidates = sorted(
                community_candidates.items(), key=lambda x: x[1], reverse=True
            )
            best_community, best_count = sorted_candidates[0]

            if best_count > 1:
                new_community = best_community
            else:
                new_community = max(best_community, curr_community)

            new_community_map[name] = new_community
            if new_community != curr_community:
                no_change = False

        community_map = new_community_map
        if no_change:
            break

    # Step 3: 收集社区
    community_clusters: dict[int, list[str]] = {}
    for name, comm_id in community_map.items():
        community_clusters.setdefault(comm_id, []).append(name)

    # 过滤单实体社区
    valid_clusters = {
        k: v for k, v in community_clusters.items() if len(v) >= 2
    }

    # Step 4: 清空旧社区，写入新社区
    await self.knowledge_db.clear_communities()
    now = time.time()
    for comm_id, members in valid_clusters.items():
        community_id = f"COM-{uuid.uuid4().hex[:12]}"
        # 简化摘要：成员实体名拼接（省略LLM调用）
        summary = "、".join(members[:5])
        if len(members) > 5:
            summary += f"等{len(members)}个实体"
        name = f"社区-{comm_id}"
        await self.knowledge_db.insert_community(
            community_id, name, summary, members, now
        )

    return len(valid_clusters)
```

#### 4.5.2 社区感知检索

```python
async def recall_by_community(self, query_entities: set[str], limit: int = 10) -> list[str]:
    """社区感知召回：query实体 → 所属社区 → 社区内全部成员实体"""
    if not query_entities:
        return []

    communities = await self.knowledge_db.get_communities_by_entities(
        list(query_entities)
    )
    related_entities: set[str] = set()
    for comm in communities:
        members = comm.get("member_entities", "[]")
        if isinstance(members, str):
            try:
                members = json.loads(members)
            except (json.JSONDecodeError, TypeError):
                members = []
        related_entities.update(members)

    # 排除query自身实体
    return list(related_entities - query_entities)[:limit]
```

### 4.6 Episode 溯源链

#### 4.6.1 完整溯源查询

```python
async def trace_fact_provenance(self, relation_id: str) -> dict | None:
    """
    溯源查询：从事实追溯到原始Episode。

    对标graphiti的 EntityEdge.episodes → EpisodicNode 溯源链。

    Returns:
        {
            "relation": {...},
            "episodes": [{id, content, valid_at, source_type}, ...],
            "entities": [{name, kind, summary}, ...]
        }
    """
    rel = await self.knowledge_db.get_relation_v2(relation_id)
    if not rel:
        return None

    episode_ids = rel.get("episode_ids", "[]")
    if isinstance(episode_ids, str):
        try:
            episode_ids = json.loads(episode_ids)
        except (json.JSONDecodeError, TypeError):
            episode_ids = []

    episodes = []
    for ep_id in episode_ids:
        ep = await self.knowledge_db.get_episode(ep_id)
        if ep:
            episodes.append(ep)

    from_ent = await self.knowledge_db.get_entity_v2(rel["from_entity"])
    to_ent = await self.knowledge_db.get_entity_v2(rel["to_entity"])

    return {
        "relation": rel,
        "episodes": episodes,
        "entities": [e for e in [from_ent, to_ent] if e],
    }
```

#### 4.6.2 时间线查询

```python
async def get_entity_timeline(self, entity_name: str) -> list[dict]:
    """
    获取实体相关的所有事实的时间线（含已失效事实）。

    这是graphiti时序窗口的查询端体现：
    用户可以看到完整的观点演变历史。
    """
    cursor = await self._conn.execute(
        """SELECT * FROM knowledge_relations_v2
           WHERE (from_entity=? OR to_entity=?)
           ORDER BY valid_at ASC""",
        (entity_name, entity_name),
    )
    rows = await cursor.fetchall()
    timeline = []
    for r in rows:
        row = dict(r)
        # 解析JSON字段
        if isinstance(row.get("episode_ids"), str):
            try:
                row["episode_ids"] = json.loads(row["episode_ids"])
            except (json.JSONDecodeError, TypeError):
                row["episode_ids"] = []
        timeline.append(row)
    return timeline
```

### 4.7 时序感知检索增强

#### 4.7.1 当前有效事实优先

```python
async def get_relevance_boost_v2(
    self,
    query_entities: set[str],
    memory_entities_list: list[list[str]],
    query_time: float | None = None,
) -> list[float]:
    """
    时序感知的KG检索增强评分。

    对标graphiti的时序过滤search，但在评分层面实现。
    """
    boosts: list[float] = []
    for mem_entities in memory_entities_list:
        boost = 0.0
        mem_set = set(mem_entities)
        overlap = query_entities & mem_set
        if overlap:
            boost += len(overlap) * 0.15

        # 关系增强：通过CTE图遍历发现间接关联
        if self.knowledge_db and query_entities and mem_set:
            try:
                # 使用新的图遍历接口
                related = await self.knowledge_db.graph_traverse(
                    list(query_entities)[:2], max_depth=2,
                    query_time=query_time, limit=5,
                )
                related_names = {r["entity_name"] for r in related}
                indirect_overlap = related_names & mem_set
                if indirect_overlap:
                    boost += len(indirect_overlap) * 0.08
            except Exception:
                logger.debug("kg.graph_traverse_boost_failed", exc_info=True)

        boosts.append(min(boost, 0.5))
    return boosts
```

#### 4.7.2 时序过滤的召回接口

```python
async def recall_by_entities_v2(
    self,
    query_entities: set[str],
    query_time: float | None = None,
    limit: int = 10,
) -> list[str]:
    """
    时序感知的KG召回：只返回当前有效的关联实体。

    对标graphiti的search with SearchFilters的时间过滤能力。
    """
    if not query_entities or not self.knowledge_db:
        return []

    related = await self.knowledge_db.graph_traverse(
        list(query_entities)[:3],
        max_depth=2,
        query_time=query_time,
        limit=limit,
    )
    related_names = {r["entity_name"] for r in related}
    return list(related_names - query_entities)[:limit]
```

---

## 5. 实施步骤

### Phase 1: Schema扩展与数据迁移（3天）

| 天数 | 任务 | 交付物 |
|------|------|--------|
| D1 | 创建v2表（kg_episodes, knowledge_entities_v2, knowledge_relations_v2, kg_communities）；编写迁移脚本 | `db/migrations/002_kg_v2_schema.py` |
| D2 | 在 `db/db_knowledge.py` 中实现 v2 的 CRUD 方法（insert_episode, insert_relation_v2, invalidate_relation, get_active_relations_between, graph_traverse） | `db/db_knowledge_v2.py`（新文件，从db_knowledge继承扩展） |
| D3 | 迁移验证：v1→v2数据迁移 + 单元测试 + 回滚测试 | `tests/test_kg_v2_migration.py` |

**关键约束**：
- v2表与v1表并行存在，通过配置开关切换
- 迁移脚本幂等（`IF NOT EXISTS`）
- 保留v1表不删除

### Phase 2: 事实超驰与实体演化（5天）

| 天数 | 任务 | 交付物 |
|------|------|--------|
| D1 | 实现 `_detect_contradictions` LLM矛盾检测 | `memory/knowledge_graph_v2.py` |
| D2 | 实现 `merge_relation_v2` 事实超驰逻辑 | `memory/knowledge_graph_v2.py` |
| D3 | 实现 `merge_entities_v2` 实体摘要演化（`_evolve_summary`） | `memory/knowledge_graph_v2.py` |
| D4 | 实现 `add_facts_from_episode` 完整流程，串接 Episode→提取→超驰→演化 | `memory/knowledge_graph_v2.py` |
| D5 | 集成测试：模拟用户偏好变化场景（喜欢篮球→改打网球） | `tests/test_fact_invalidation.py` |

**关键约束**：
- LLM矛盾检测使用免费模型（硅基流动），不占主模型配额
- 超驰时旧事实标记 `invalid_at` 而非删除，保留完整历史
- 摘要演化失败时退化为拼接（graceful degradation）

### Phase 3: 图遍历与时序检索（3天）

| 天数 | 任务 | 交付物 |
|------|------|--------|
| D1 | 实现 `graph_traverse` CTE递归查询（含时序过滤变体） | `db/db_knowledge_v2.py` |
| D2 | 实现 `recall_by_entities_v2` 时序感知召回 + `get_relevance_boost_v2` | `memory/knowledge_graph_v2.py` |
| D3 | 性能测试：不同depth/entity数量的查询延迟 | `tests/benchmark_graph_traverse.py` |

**关键约束**：
- CTE递归depth上限=3（安全限制）
- query_time参数默认为None（不限制时间），按需传入
- 防环使用管道符分隔的visited字符串

### Phase 4: Episode溯源与时间线（2天）

| 天数 | 任务 | 交付物 |
|------|------|--------|
| D1 | 实现 `trace_fact_provenance` 溯源查询 + `get_entity_timeline` 时间线 | `memory/knowledge_graph_v2.py` |
| D2 | 集成到对话上下文：Agent可展示"我知道你喜欢篮球是因为你上次说的" | `memory/context_builder.py` 修改 |

### Phase 5: 社区发现（3天）

| 天数 | 任务 | 交付物 |
|------|------|--------|
| D1 | 实现 `build_communities` 标签传播算法 | `memory/knowledge_graph_v2.py` |
| D2 | 实现 `recall_by_community` 社区感知召回 | `memory/knowledge_graph_v2.py` |
| D3 | 集成：定时任务触发社区重建 + 端到端测试 | `tasks/kg_maintenance.py` |

**关键约束**：
- 社区发现作为低优先级后台任务，不阻塞主流程
- 社区摘要用实体名拼接而非LLM汇总（节省token）
- 社区数量 < 20（小规模图谱的合理上限）

### Phase 6: 集成与灰度（3天）

| 天数 | 任务 | 交付物 |
|------|------|--------|
| D1 | 配置开关：`KG_V2_ENABLED=true/false` 控制新旧路径 | `config.py` |
| D2 | 在 `auto_extract_and_merge` 中调用v2路径；memory_manager 的召回通道切换 | `memory/knowledge_graph.py` |
| D3 | 灰度验证：v2路径全量回归 + 对比v1召回质量 | 灰度报告 |

---

## 6. 预期效果

### 6.1 定量指标

| 指标 | 当前 (v0.5.03) | 优化后 | 改善 |
|------|---------------|--------|------|
| 事实准确率（偏好类查询） | ~60%（新旧事实混杂） | ~90%（只返回当前有效事实） | +30% |
| 实体信息密度 | 低（observations无限膨胀） | 高（summary压缩式更新） | 3-5x |
| 图遍历召回率 | depth=1 BFS | depth=2 CTE + 时序过滤 | +40% |
| 事实可解释性 | 无法溯源 | 完整Episode溯源链 | 从无到有 |
| 社区发现 | 无 | 自动标签传播 | 从无到有 |

### 6.2 定性效果

1. **情感陪伴准确性**：Agent不再引用过时的用户偏好（"你现在应该不喜欢篮球了吧"），而是基于当前有效事实回应
2. **知识可解释性**：每条知识可追溯到原始对话，用户可验证Agent的记忆来源
3. **记忆演化**：实体摘要随对话自然演化，不会因observations膨胀而降低LLM上下文利用率
4. **图谱结构理解**：社区发现帮助Agent理解实体间的隐含关联（"你的朋友们似乎都喜欢运动"）
5. **时间线视角**：Agent可以展示用户偏好的变化历程，增强情感连接

### 6.3 性能影响

| 操作 | 额外耗时 | 缓解措施 |
|------|---------|---------|
| 事实超驰（LLM矛盾检测） | +200-500ms/次 | 仅在有冲突候选时触发；用免费模型 |
| 实体摘要演化 | +300-800ms/实体 | 批量处理；仅obs有变化时触发 |
| CTE图遍历 | +10-50ms/次 | depth≤3；SQLite缓存查询计划 |
| 社区发现 | +2-5s/次（全量） | 后台定时任务；N分钟一次 |
| Episode写入 | +1ms/次 | 可忽略 |

---

## 7. 不采纳的部分

### 7.1 Neo4j/FalkorDB/Kuzu 图数据库依赖

**graphiti实现**：支持Neo4j、FalkorDB、Kuzu、Neptune四种图数据库后端，使用Cypher查询语言。

**不采纳原因**：
- xiaoda-agent 是**本地部署的情感陪伴Agent**，单用户场景，SQLite已足够
- 引入Neo4j/FalkorDB增加部署复杂度（Docker、JVM、端口管理）
- SQLite CTE递归已能满足depth≤3的图遍历需求
- 向量检索可通过SQLite的JSON字段 + Python余弦相似度实现

**替代方案**：SQLite + CTE递归 + Python端向量计算。

### 7.2 Zep 托管服务

**graphiti实现**：Zep公司提供托管版graphiti服务（cloud API）。

**不采纳原因**：
- xiaoda-agent的核心价值是本地部署、数据隐私
- 托管服务引入网络延迟和可用性风险
- 情感陪伴数据不应上传第三方

**替代方案**：纯本地计算，LLM调用走免费模型或本地模型。

### 7.3 完整 GDS 社区发现

**graphiti实现**：Neo4j GDS (Graph Data Science) 库提供Louvain、Label Propagation等高级社区发现算法。

**不采纳原因**：
- 依赖Neo4j GDS插件
- xiaoda-agent的知识图谱规模小（<500实体），简单标签传播足够
- GDS的大规模优化对小图谱无显著优势

**替代方案**：纯Python实现的标签传播算法（直接移植graphiti的`label_propagation`函数）。

### 7.4 Cross-Encoder 重排序

**graphiti实现**：搜索结果使用Cross-Encoder模型（如BGE-reranker）重排序。

**不采纳原因**：
- 引入额外的模型推理开销（+100-300ms）
- 情感陪伴场景的检索精度需求低于企业级RAG
- 免费模型的延迟不稳定

**替代方案**：暂不实现重排序，通过时序过滤 + 图距离评分 + FTS5 BM25 排序。后续可根据召回质量数据决定是否引入。

### 7.5 Saga 长篇叙事机制

**graphiti实现**：SagaNode + NEXT_EPISODE边 + 增量式摘要。

**不采纳原因**：
- Saga是为长文本（播客转写、书籍）设计的，情感陪伴的对话粒度是短回合
- 增加了数据模型复杂度（额外3张表/2种边类型）
- 当前Episode表 + episode_ids引用已满足溯源需求

**替代方案**：kg_episodes表 + 关系的episode_ids字段，提供基本溯源能力。

### 7.6 自定义本体 (Custom Ontology)

**graphiti实现**：通过Pydantic模型定义entity和edge类型，LLM提取时按类型schema生成结构化属性。

**不采纳原因**：
- 当前5种kind（人物/游戏/地点/概念/物品）已覆盖情感陪伴场景
- 自定义本体需要用户配置，增加使用门槛
- LLM提取结构化属性的准确率不稳定

**替代方案**：保留现有kind分类，在fact字段中承载细粒度信息。

---

## 附录A: graphiti核心数据模型对照

```
graphiti                          →  xiaoda-agent适配
─────────────────────────────────────────────────────────
EpisodicNode                      →  kg_episodes表
  uuid                            →  id
  content                         →  content
  valid_at                        →  valid_at
  source (EpisodeType)            →  source_type
  entity_edges                    →  (通过knowledge_relations_v2.episode_ids反查)

EntityNode                        →  knowledge_entities_v2表
  uuid                            →  id
  name                            →  name
  name_embedding                  →  name_embedding
  summary                         →  summary
  labels                          →  kind (简化)
  attributes                      →  observations (保留兼容)

EntityEdge                        →  knowledge_relations_v2表
  uuid                            →  id
  source_node_uuid                →  from_entity (用name代替uuid)
  target_node_uuid                →  to_entity (用name代替uuid)
  name                            →  relation_type
  fact                            →  fact
  fact_embedding                  →  fact_embedding
  episodes                        →  episode_ids
  valid_at                        →  valid_at
  invalid_at                      →  invalid_at
  expired_at                      →  expired_at

CommunityNode                     →  kg_communities表
  uuid                            →  id
  name                            →  name
  summary                         →  summary

CommunityEdge                     →  kg_communities.member_entities (嵌入JSON)
EpisodicEdge (MENTIONS)          →  (隐含，通过episode_ids反查)
SagaNode                          →  ❌ 不采纳
NextEpisodeEdge                   →  ❌ 不采纳
HasEpisodeEdge                    →  ❌ 不采纳
```

## 附录B: 关键Prompt扩展

### B.1 增强版实体关系提取Prompt

```python
ENTITY_EXTRACT_PROMPT_V2 = """从以下对话摘要中提取关键实体和关系，包含时间信息。

严格输出JSON，不要添加任何其他文字。格式如下：
{{"entities": [{{"name": "实体名", "kind": "人物/游戏/地点/概念/物品", "observations": ["观察1"]}}], "relations": [{{"from_entity": "实体A", "relation_type": "关系类型", "to_entity": "实体B", "fact": "自然语言事实陈述", "is_change": false}}]}}

规则：
1. 只提取明确提及的实体，不要推测
2. observations 是关于实体的具体描述
3. relation_type 使用简洁的动词短语，如"喜欢"、"属于"、"住在"
4. fact 是完整的自然语言事实，如"小明喜欢打篮球"
5. is_change 标记此关系是否表示状态变化（如"不再喜欢"、"改成了"）
6. 如果没有明确的实体和关系，返回 {{"entities": [], "relations": []}}

对话时间：{episode_time}
对话摘要：
{summary}"""
```

### B.2 矛盾检测Prompt

```python
CONTRADICTION_DETECT_PROMPT = """判断以下新事实是否与已有事实矛盾。

新事实：{new_fact}

已有事实：
{existing_facts_formatted}

规则：
1. 只有直接矛盾才算（如"喜欢"vs"不喜欢"、"在A地"vs"在B地"）
2. 互补信息不算矛盾（如"喜欢篮球"和"也喜欢网球"）
3. 时序变化算矛盾（如"喜欢篮球"vs"改打网球了"）
4. 返回矛盾的已有事实编号列表

严格输出JSON：{{"contradicted": [索引列表]}}"""
```

### B.3 实体摘要演化Prompt

```python
ENTITY_SUMMARY_EVOLVE_PROMPT = """将新信息融合到实体摘要中，生成更新后的摘要。

当前摘要：{current_summary}
新信息：{new_info}
上下文：{episode_content}

规则：
1. 保留当前摘要中的重要信息
2. 融合新信息，如果与旧信息矛盾则替换旧信息
3. 摘要应简洁（200字以内），聚焦关键事实
4. 使用第三人称描述

直接输出更新后的摘要文本，不要输出其他内容。"""
```
