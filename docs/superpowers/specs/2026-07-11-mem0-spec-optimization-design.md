# mem0 SPEC 记忆系统优化设计文档

> 基于外部 mem0 SPEC (Coze: LLpy94BHkxs) 对 xiaoda-agent 记忆系统的对齐优化
> 版本：v1.0 | 编写日期：2026-07-11

---

## 1. 项目背景

### 1.1 优化来源

用户提供外部 mem0 SPEC（`https://www.coze.cn/s/LLpy94BHkxs/`），要求基于该 SPEC 对项目记忆系统进行优化，深入研究相关论文技术和 mem0 原仓库源码，保证效果最优。

### 1.2 研究发现：SPEC诊断与实际代码的差距

通过完整阅读 [memory_manager.py](file:///home/orangepi/ai-agent/memory/memory_manager.py)（1993行）和 [vector_store.py](file:///home/orangepi/ai-agent/memory/vector_store.py)（627行），发现 SPEC 中有 5 项诊断已过时：

| SPEC诊断 | 实际代码状态 |
|---------|------------|
| FTS5未使用 | 已在 `retrieve_memories_hybrid()` 并行使用 |
| 只有语义向量一路信号 | 已有 FTS+向量+KG+子chunk 四路 RRF 融合 |
| 无查询变换 | 已有 rewrite+expand 并行查询变换 |
| 无 reranker | 已有 bge-reranker-v2-m3 集成 |
| 嵌入缓存128条 | 已扩容到 512 条 |

**真正的差距**（SPEC诊断正确，4项）：
1. **实体提取简单** — `_extract_entities()` 仅用 jieba.cut，无类型/链接/嵌入
2. **无独立 entity_store** — 实体只是 metadata 字段，无反向链接
3. **无 ADD-only 策略** — `encode_memory()` 仍是覆盖式
4. **无 scope 隔离** — 表结构无 user_id/agent_id 字段（session_id 已有）

### 1.3 优化范围

只聚焦真正差距的 4 项：
- 实体链接机制（entity_store + entity_extractor）
- ADD-only 记忆积累策略
- User/Session/Agent 三级记忆隔离
- 时间感知检索增强（与新增组件兼容）

### 1.4 约束

- 保留并兼容 xiaoda-agent 独有功能（情感记忆/遗忘曲线/元认知/梦境整合/流体记忆/知识图谱/ContextNest）
- 不破坏现有线上稳定性（全量测试 1311 passed 不回归）
- 保持与现有代码风格一致（Python 3.11 + asyncio + aiosqlite + sqlite-vec）
- 所有时间相关函数使用 `ZoneInfo("Asia/Shanghai")`
- 不引入 Neo4j 等重依赖，仅用 sqlite-vec + FTS5

### 1.5 决策摘要

| 决策项 | 选择 |
|--------|------|
| 优化范围 | 只聚焦真正差距（实体链接/ADD-only/scope隔离/时间感知） |
| 独有功能 | 保留并兼容（情感记忆/遗忘曲线/元认知/梦境整合等） |
| scope隔离 | 完整三级（user_id + session_id + agent_id） |
| 实体提取 | 混合策略（jieba+规则快抽 → 低置信度调 LLM 精抽） |
| ADD-only粒度 | 混合架构（原始数据 append-only + 提炼知识 UPDATE/DELETE） |
| 技术方案 | 方案C混合最优（新建 entity_store + 复用 episodic_memories 区分 is_raw） |

---

## 2. 架构设计

### 2.1 新增组件

```
memory/
├── entity_store.py        # 实体存储管理（CRUD + 反向链接查询）
├── entity_extractor.py    # 混合实体提取（jieba+规则快抽 → LLM精抽）
```

### 2.2 修改组件

| 文件 | 修改内容 |
|------|---------|
| [memory_manager.py](file:///home/orangepi/ai-agent/memory/memory_manager.py) | 编码流程接入实体提取+ADD-only；检索流程接入 Entity Boost |
| [vector_store.py](file:///home/orangepi/ai-agent/memory/vector_store.py) | 无需修改（不新增实体向量表） |
| [db/db_memory.py](file:///home/orangepi/ai-agent/db/db_memory.py) | 新增 scope 过滤方法 + entity_store CRUD |
| [db/schema.sql](file:///home/orangepi/ai-agent/db/schema.sql) | episodic_memories 加3字段 + 新建3表 |
| [memory/memory_distiller.py](file:///home/orangepi/ai-agent/memory/memory_distiller.py) | 新增 `merge_knowledge()` 方法 |

### 2.3 数据流变化

**编码流程（写入记忆）：**
```
用户对话
  → RuleBasedMemoryExtractor 规则提取（已有）
  → MemoryDistiller LLM蒸馏（已有）
  → 【新增】EntityExtractor 混合实体提取
    ├─ jieba 词性标注 + 规则快抽（<10ms）
    └─ 低置信度 → LLM 精抽（异步，+200-500ms）
  → 【新增】写入 episodic_memories（is_raw=1，append-only，永不覆盖）
  → 【新增】EntityStore 实体链接（FTS5名称匹配 + 反向链接）
  → 【新增】异步蒸馏 → 写入 episodic_memories（is_raw=0，允许 UPDATE/DELETE）
  → VectorStore 向量写入（复用 memories_vec）
```

**检索流程（读取记忆）：**
```
用户查询
  → QueryCache 语义缓存（已有）
  → 【新增】EntityExtractor 提取查询实体（jieba快抽，<10ms）
  → 【已有】四路并行检索（FTS+向量+KG+子chunk）+ scope过滤
  → 【新增】EntityStore.recall_by_entities() 第5路召回
  → 【已有】RRF 融合（现在是五路：FTS+向量+KG+子chunk+实体）
  → 【新增】精排阶段计算 Entity Boost
  → 【已有】Reranker 精排 + 综合评分
  → 【已有】CRAG 评估 + 兜底
```

### 2.4 关键设计原则

- 所有新增组件都是**可选注入**，失败时降级到已有流程（不破坏现有功能）
- 实体提取、实体链接都是**异步**的，不阻塞主流程
- scope 过滤在 SQL 层实现，不影响已有检索逻辑
- 原始数据 append-only 保证可追溯，提炼知识 UPDATE/DELETE 保持简洁

---

## 3. 数据库 Schema 设计

### 3.1 技术约束

sqlite-vec 的 `vec0` 虚拟表只有 `rowid + embedding` 两列，不支持加列。因此不新建实体向量表，实体链接通过 FTS5 名称匹配 + `entity_memory_links` 反向查询实现。

### 3.2 episodic_memories 表新增3字段

通过迁移脚本执行（ALTER TABLE ADD COLUMN，SQLite 支持且不锁表）：

```sql
ALTER TABLE episodic_memories ADD COLUMN user_id TEXT DEFAULT 'default';
ALTER TABLE episodic_memories ADD COLUMN agent_id TEXT DEFAULT 'xiaoda';
ALTER TABLE episodic_memories ADD COLUMN is_raw INTEGER DEFAULT 0;  -- 0=提炼知识, 1=原始记录
-- 注: session_id 已存在（默认 'user'），复用为 scope 隔离的 session 级
```

### 3.3 新建 memory_entities 表

实体存储，与 KG 的 knowledge_entities 分离（职责不同）：

```sql
CREATE TABLE IF NOT EXISTS memory_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    entity_type TEXT DEFAULT 'TOPIC',   -- PROPER/QUOTED/TOPIC/IDENTIFIER
    kind TEXT DEFAULT '',               -- 人物/地点/组织/概念/技术
    observations TEXT DEFAULT '[]',     -- JSON 数组
    memory_count INTEGER DEFAULT 0,     -- 链接的记忆数
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    metadata_json TEXT DEFAULT '{}',
    UNIQUE(name, entity_type)
);
CREATE INDEX IF NOT EXISTS idx_memory_entities_name ON memory_entities(name);
CREATE INDEX IF NOT EXISTS idx_memory_entities_type ON memory_entities(entity_type);
```

### 3.4 新建 memory_entities_fts

实体名称全文索引（用于快速名称匹配）：

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS memory_entities_fts USING fts5(
    id UNINDEXED, name_index
);

CREATE TRIGGER IF NOT EXISTS memory_entities_fts_ai AFTER INSERT ON memory_entities BEGIN
    INSERT INTO memory_entities_fts(id, name_index) VALUES (new.id, new.name);
END;
CREATE TRIGGER IF NOT EXISTS memory_entities_fts_ad AFTER DELETE ON memory_entities BEGIN
    INSERT INTO memory_entities_fts(memory_entities_fts, id, name_index)
    VALUES ('delete', old.id, old.name);
END;
CREATE TRIGGER IF NOT EXISTS memory_entities_fts_au AFTER UPDATE ON memory_entities BEGIN
    INSERT INTO memory_entities_fts(memory_entities_fts, id, name_index)
    VALUES ('delete', old.id, old.name);
    INSERT INTO memory_entities_fts(id, name_index) VALUES (new.id, new.name);
END;
```

### 3.5 新建 entity_memory_links 表

实体↔记忆反向链接（检索时第5路召回的核心）：

```sql
CREATE TABLE IF NOT EXISTS entity_memory_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL,
    memory_id INTEGER NOT NULL,
    confidence REAL DEFAULT 1.0,
    created_at REAL NOT NULL,
    FOREIGN KEY (entity_id) REFERENCES memory_entities(id) ON DELETE CASCADE,
    FOREIGN KEY (memory_id) REFERENCES episodic_memories(id) ON DELETE CASCADE,
    UNIQUE(entity_id, memory_id)
);
CREATE INDEX IF NOT EXISTS idx_eml_entity ON entity_memory_links(entity_id);
CREATE INDEX IF NOT EXISTS idx_eml_memory ON entity_memory_links(memory_id);
```

### 3.6 新增复合索引

```sql
CREATE INDEX IF NOT EXISTS idx_episodic_scope
    ON episodic_memories(user_id, agent_id, is_raw, timestamp DESC);
```

### 3.7 不新建 memories_raw 表

复用 `episodic_memories` 通过 `is_raw` 字段区分：
- `is_raw=1`：append-only 原始记录（永不覆盖/删除）
- `is_raw=0`：提炼知识（允许 UPDATE/DELETE）

### 3.8 不新建实体向量表

实体链接通过 FTS5 名称匹配 + `entity_memory_links` 反向查询。初版不需要实体向量，未来如需语义链接可再加 `memory_entities_vec` 表。

### 3.9 数据迁移策略

```sql
-- 1. 为现有记忆回填 user_id 和 agent_id（默认值）
UPDATE episodic_memories SET user_id='default' WHERE user_id IS NULL OR user_id='';
UPDATE episodic_memories SET agent_id='xiaoda' WHERE agent_id IS NULL OR agent_id='';

-- 2. 现有记忆标记为 is_raw=0（视为提炼知识）
UPDATE episodic_memories SET is_raw=0 WHERE is_raw IS NULL;

-- 3. 现有记忆保持 session_id 不变（已有值如 'user'）

-- 4. 为现有记忆的 entities 字段解析并建立反向链接（异步迁移脚本）
```

### 3.10 迁移安全性

- 所有 ALTER TABLE 都是加列带 DEFAULT，SQLite 支持且不锁表
- 新表用 `IF NOT EXISTS`，重复执行安全
- 迁移脚本通过已有的 `schema_version` 表追踪版本
- 迁移失败不影响现有功能（新字段有默认值，新表不存在时代码降级）

---

## 4. 实体链接机制

### 4.1 EntityExtractor — 混合实体提取

**实体类型分类**（参考 mem0 原版，4类）：
- `PROPER`：专有名词（人名/地名/组织名）
- `QUOTED`：引号内容（用户强调的概念）
- `TOPIC`：主题关键词（jieba.extract_tags）
- `IDENTIFIER`：技术标识符（英文/代码符号）

**提取流程：**

```python
class EntityExtractor:
    """混合实体提取器：jieba+规则快抽 → 低置信度时 LLM 精抽"""

    async def extract(self, text: str, importance: float = 0.5) -> list[Entity]:
        # 第1层：jieba 词性标注 + 规则快抽（<10ms）
        entities = self._rule_based_extract(text)

        # 第2层：低置信度时触发 LLM 精抽
        if len(entities) < 2 or importance > 0.7:
            llm_entities = await self._llm_extract(text)
            entities = self._merge_entities(entities, llm_entities)

        return entities

    def _rule_based_extract(self, text: str) -> list[Entity]:
        """jieba 词性标注 + 正则规则"""
        # jieba.posseg.cut → nr/ns/nt/nz → PROPER
        # 引号匹配 → QUOTED
        # jieba.analyse.extract_tags → TOPIC
        # 英文标识符正则 → IDENTIFIER
```

**LLM 精抽触发条件：**
1. jieba 提取结果 < 2 个实体（可能遗漏）
2. 记忆 importance > 0.7（重要记忆值得精抽）

**LLM prompt 设计**（纯结构化，无 hardcoded 内容）：
```
提取以下文本中的实体，返回JSON数组。
每项格式：{"name":"实体名","type":"PROPER|QUOTED|TOPIC|IDENTIFIER","kind":"人物|地点|组织|概念|技术"}
文本：{text}
```

### 4.2 EntityStore — 实体存储与链接

**实体链接算法：**

```python
class EntityStore:
    """实体存储管理：CRUD + 反向链接查询"""

    async def link_entities(self, memory_id: int, entities: list[Entity]) -> int:
        """将提取的实体链接到记忆"""
        linked = 0
        for entity in entities:
            # 1. FTS5 精确匹配实体名
            existing = await self._find_by_name(entity.name, entity.entity_type)

            if existing:
                # 2. 找到匹配 → 建立反向链接
                await self._create_link(existing.id, memory_id, entity.confidence)
                await self._increment_memory_count(existing.id)
            else:
                # 3. 无匹配 → 创建新实体
                new_id = await self._create_entity(entity)
                await self._create_link(new_id, memory_id, entity.confidence)
            linked += 1
        return linked

    async def recall_by_entities(self, entity_names: list[str],
                                  scope: Scope) -> list[int]:
        """通过实体名反向查询关联的记忆ID（检索时第5路召回）"""
        # SELECT memory_id FROM entity_memory_links
        # JOIN memory_entities ON entity_id = memory_entities.id
        # JOIN episodic_memories ON memory_id = episodic_memories.id
        # WHERE memory_entities.name IN (?) AND scope过滤
```

### 4.3 Entity Boost 计算公式

参考 mem0 原版 `scoring.py`，Entity Boost 用于精排阶段加分：

```python
ENTITY_BOOST_WEIGHT = 0.5  # 与 mem0 原版一致

def compute_entity_boost(entity: dict, query_entities: set[str], now: float) -> float:
    """计算实体对查询的 boost 值"""
    # 1. 实体与查询实体的匹配度
    similarity = 1.0 if entity["name"] in query_entities else 0.0

    # 2. 记忆数权重：链接记忆越多越重要，但边际递减
    #    mem0 原版公式: 1/(1 + 0.001*(count-1)^2)
    memory_count_weight = 1.0 / (1.0 + 0.001 * (entity["memory_count"] - 1) ** 2)

    # 3. 时间衰减因子（天级）
    recency = 1.0 / (1.0 + max(0, now - entity["last_seen"]) / 86400)

    # 4. Entity Boost
    return similarity * ENTITY_BOOST_WEIGHT * memory_count_weight * recency
```

### 4.4 检索流程集成

```
用户查询
  → 【新增】EntityExtractor 提取查询实体（jieba快抽，<10ms）
  → 【已有】四路并行检索（FTS+向量+KG+子chunk）+ scope过滤
  → 【新增】EntityStore.recall_by_entities() 第5路召回
  → 【已有】RRF 融合（五路：FTS+向量+KG+子chunk+实体）
    五路 RRF 权重：[1.0, 1.0, 0.8, 0.6, 0.7]（FTS/向量等权，KG/子chunk/实体略低）
  → 【新增】精排阶段计算 Entity Boost
    final_score = rerank*0.45 + fluid*0.25 + kg*0.1 + recency*0.1 + entity_boost*0.1
    （原公式: rerank*0.5 + fluid*0.3 + kg*0.1 + recency*0.1）
  → 【已有】CRAG 评估 + 兜底
```

### 4.5 与已有代码的关系

| 已有代码 | 变更 |
|---------|------|
| `_extract_entities()` ([memory_manager.py:20](file:///home/orangepi/ai-agent/memory/memory_manager.py#L20)) | 保留为降级方案，`EntityExtractor` 为主入口 |
| `entities` 字段（episodic_memories 表） | 保留，存储 JSON 实体名列表；`entity_memory_links` 为反向链接 |
| `knowledge_graph.py` | 不变，KG 和实体链接职责分离 |

---

## 5. ADD-only 混合架构

### 5.1 核心原则

```
原始数据 append-only（永不覆盖/删除） → 可追溯
提炼知识 UPDATE/DELETE → 保持简洁
```

### 5.2 编码流程变更

**当前流程**（覆盖式）：
```python
async def encode_memory(self, summary, ...):
    if await self._has_duplicate(summary):
        return  # 重复则跳过
    memory_id = await self.memory.add_episodic_memory(summary, ...)
    await self.vec.upsert(memory_id, summary)
```

**新流程**（ADD-only + 蒸馏）：
```python
async def encode_memory(self, summary, scope: Scope, ...):
    # 1. 写入原始记忆（append-only，不去重，不覆盖）
    raw_id = await self.memory.add_episodic_memory(
        summary, is_raw=1,
        user_id=scope.user_id, session_id=scope.session_id, agent_id=scope.agent_id,
    )
    await self.vec.upsert(raw_id, summary)

    # 2. 异步触发蒸馏（生成 is_raw=0 的提炼知识）
    asyncio.create_task(self._distill_to_knowledge(raw_id, summary, scope, ...))

    # 3. 异步触发实体提取+链接
    asyncio.create_task(self._extract_and_link_entities(raw_id, summary))
```

### 5.3 蒸馏流程变更

**新增 `_distill_to_knowledge` 方法：**
```python
async def _distill_to_knowledge(self, raw_id: int, summary: str, scope: Scope, ...):
    """将原始记忆蒸馏为提炼知识（允许 UPDATE/DELETE）"""
    # 1. 调用已有 MemoryDistiller 蒸馏
    distilled = await self.distiller.distill(summary)
    if not distilled:
        return

    # 2. 检查是否已有相似的提炼知识（is_raw=0, 同scope）
    similar = await self._find_similar_knowledge(distilled, scope)

    if similar:
        # 3a. 有相似知识 → UPDATE（合并/增强）
        await self._update_knowledge(similar.id, distilled, raw_id)
    else:
        # 3b. 无相似知识 → 新建提炼知识（is_raw=0）
        knowledge_id = await self.memory.add_episodic_memory(
            distilled, is_raw=0, source_raw_ids=[raw_id], scope=scope, ...
        )
        await self.vec.upsert(knowledge_id, distilled)
```

### 5.4 提炼知识的 UPDATE/DELETE 策略

**UPDATE 场景**（合并相似知识）：
```python
async def _update_knowledge(self, knowledge_id: int, new_content: str, raw_id: int):
    """更新已有提炼知识（合并新信息）"""
    # 1. 获取已有知识
    existing = await self.memory.get_memory(knowledge_id)

    # 2. LLM 合并新旧知识（避免信息丢失）
    merged = await self.distiller.merge_knowledge(existing.summary, new_content)

    # 3. 更新记录（version+1，记录 source_raw_ids）
    await self.memory.update_episodic_memory(
        knowledge_id,
        summary=merged,
        version=existing.version + 1,
        source_raw_ids=existing.metadata.get("source_raw_ids", []) + [raw_id]
    )
    await self.vec.upsert(knowledge_id, merged)
```

**DELETE 场景**（定期清理冗余）：
- 不真正 DELETE，而是标记 `rag_status='excluded'`
- 保留记录用于溯源，但排除出检索结果
- 由已有的梦境整合/遗忘曲线机制触发

### 5.5 检索时的 is_raw 过滤

```python
async def retrieve_memories_hybrid(self, query, scope: Scope, ...):
    # 默认只检索提炼知识（is_raw=0），更简洁
    include_raw = kwargs.get("include_raw", False)

    # FTS/向量/KG/子chunk 四路检索都加 is_raw + scope 过滤
    if not include_raw:
        # WHERE is_raw = 0 AND user_id = ? AND agent_id = ?
    else:
        # 溯源模式：查所有记忆（含原始）
```

### 5.6 与已有机制的协调

| 已有机制 | 变更 |
|---------|------|
| `_has_duplicate()` ([memory_manager.py:322](file:///home/orangepi/ai-agent/memory/memory_manager.py#L322)) | 改为只对 `is_raw=0` 的提炼知识生效；原始记忆不去重 |
| `MemoryDistiller` ([memory_distiller.py](file:///home/orangepi/ai-agent/memory/memory_distiller.py)) | 新增 `merge_knowledge()` 方法用于合并相似知识 |
| 梦境整合/遗忘曲线 | 通过 `rag_status='excluded'` 标记删除，不真正 DELETE |
| `RuleBasedMemoryExtractor` | 不变，规则提取结果作为蒸馏输入 |

### 5.7 数据膨胀控制

- 原始记忆（is_raw=1）会持续增长，但通过已有的遗忘曲线/梦境整合定期清理
- 提炼知识（is_raw=0）通过 UPDATE 合并控制数量
- `content_hash` 字段用于快速检测完全重复的原始记忆（可选跳过）

---

## 6. Scope 三级隔离

### 6.1 三字段填充策略

| 字段 | 来源 | 默认值 | 说明 |
|------|------|--------|------|
| `user_id` | 用户标识 | `'default'` | 单用户桌面应用默认 'default'；未来多用户时区分 |
| `session_id` | 会话标识 | 已有字段，复用 | 当前对话会话 ID，用于会话级隔离 |
| `agent_id` | Agent 标识 | `'xiaoda'` | 当前 agent 名称（xiaoda/xiaoli/xiaolian/xiaoke） |

### 6.2 Scope 对象设计

```python
from dataclasses import dataclass

@dataclass
class Scope:
    """记忆隔离的三级 scope"""
    user_id: str = "default"
    session_id: str = "user"
    agent_id: str = "xiaoda"

    def to_sql_filter(self, table: str = "episodic_memories") -> str:
        """生成 SQL WHERE 子句"""
        return (
            f"{table}.user_id = '{self.user_id}' "
            f"AND {table}.agent_id = '{self.agent_id}'"
        )
```

### 6.3 检索时的 Scope 过滤逻辑

**默认检索**（跨会话，用户+agent 级）：
```python
async def retrieve_memories_hybrid(self, query, scope: Scope, ...):
    # 默认：检索当前 user_id + agent_id 的所有记忆（跨 session）
    # 这是最常见的场景：回忆用户的所有相关记忆
    where_clause = f"user_id = ? AND agent_id = ?"
    params = [scope.user_id, scope.agent_id]
```

**会话级检索**（可选，限定当前会话）：
```python
    if kwargs.get("session_only"):
        where_clause += " AND session_id = ?"
        params.append(scope.session_id)
```

**跨 agent 检索**（可选，用户全局）：
```python
    if kwargs.get("cross_agent"):
        # 只按 user_id 过滤，不限定 agent_id
        where_clause = "user_id = ?"
        params = [scope.user_id]
```

### 6.4 Scope 在各组件的传递

```
message_processor
  → 构建 Scope(user_id, session_id, agent_id)
  → 传入 MemoryManager.encode_memory(scope=...)
  → 传入 MemoryManager.retrieve_memories_hybrid(scope=...)
  → 传入 EntityStore.recall_by_entities(scope=...)
  → 传入 MemoryDistiller.distill(scope=...)
```

### 6.5 与已有 session_id 的兼容

| 已有代码 | 变更 |
|---------|------|
| `episodic_memories.session_id` | 已有字段，复用为 scope 的 session 级 |
| `idx_episodic_session` 索引 | 已有，继续使用 |
| 检索时不带 session_id 过滤 | 默认改为带 `user_id + agent_id` 过滤 |
| `_has_duplicate()` | 加入 scope 过滤，只在同 scope 内查重 |

### 6.6 多用户扩展性

未来支持多用户时：
- `user_id` 从 `'default'` 改为实际用户标识
- 所有检索自动按 user_id 隔离
- 无需重构表结构

---

## 7. 时间感知增强

### 7.1 已有实现（无需重写）

- [_parse_temporal_query()](file:///home/orangepi/ai-agent/memory/memory_manager.py#L47) 解析中文时间词（昨天/前天/上周等）
- `_try_temporal_search()` 按时间范围检索
- [FluidMemory](file:///home/orangepi/ai-agent/memory/fluid_memory.py) Ebbinghaus 衰减 + 访问频率 boost

### 7.2 增强点（与新增组件兼容）

```python
# 1. 时间范围过滤加入 scope 条件
async def _try_temporal_search(self, query, scope: Scope, ...):
    time_range = _parse_temporal_query(query)
    if time_range:
        where = (f"user_id=? AND agent_id=? "
                 f"AND timestamp BETWEEN ? AND ?")
        params = [scope.user_id, scope.agent_id, *time_range]

# 2. EntityStore 时间衰减（实体 last_seen 更新）
async def link_entities(self, memory_id, entities):
    for entity in entities:
        # 更新实体的 last_seen（时间感知）
        await self._update_last_seen(entity.id)

# 3. Entity Boost 加入时间衰减因子
def compute_entity_boost(entity, query_entities, now: float):
    recency = 1.0 / (1.0 + max(0, now - entity["last_seen"]) / 86400)  # 天级衰减
    return similarity * ENTITY_BOOST_WEIGHT * memory_count_weight * recency
```

### 7.3 保留 Ebbinghaus

爸爸已确认保留独有功能，FluidMemory 的 Ebbinghaus 衰减不变，不替换为 FSRS-5。

---

## 8. 错误处理和降级策略

| 组件 | 失败场景 | 降级策略 |
|------|---------|---------|
| EntityExtractor | jieba 不可用 | 降级到 n-gram 提取（已有逻辑） |
| EntityExtractor | LLM 精抽失败 | 使用 jieba 结果，记录日志 |
| EntityStore | 查询失败 | 跳过第5路召回，不影响已有四路 |
| EntityStore | 链接写入失败 | 记录日志，记忆仍正常写入 |
| ADD-only 蒸馏 | distiller 失败 | 原始记忆保留（is_raw=1），无提炼知识 |
| ADD-only 合并 | merge_knowledge 失败 | 新建提炼知识（不合并） |
| Scope 过滤 | 字段为空 | 使用默认值（user='default', agent='xiaoda'） |
| 迁移脚本 | ALTER TABLE 失败 | 记录日志，代码用 `try-except` 降级 |

---

## 9. 测试策略

### 9.1 单元测试（新增）

```
tests/
├── test_entity_extractor.py     # 实体提取（jieba/LLM/降级）
├── test_entity_store.py         # 实体 CRUD + 反向链接
├── test_scope_isolation.py      # 三级隔离过滤
├── test_add_only_architecture.py # ADD-only + 蒸馏 + UPDATE
└── test_entity_boost.py         # Entity Boost 计算
```

### 9.2 回归测试（保障已有功能）

- 现有 `test_memory_manager.py` 全部通过
- 现有 `test_vector_store.py` 全部通过
- 现有 `test_fluid_memory.py` 全部通过
- 检索准确率不下降（已有基准）

### 9.3 集成测试（新增）

```python
# 端到端：编码 → 提取实体 → 蒸馏 → 检索 → Entity Boost
async def test_end_to_end_memory_flow():
    # 1. 编码记忆
    await mm.encode_memory("我喜欢Python编程", scope=test_scope)
    # 2. 验证原始记忆写入（is_raw=1）
    # 3. 验证实体提取（"Python" → IDENTIFIER）
    # 4. 验证实体链接（entity_memory_links 有记录）
    # 5. 等待蒸馏完成
    # 6. 检索 "Python" 相关记忆
    # 7. 验证 Entity Boost 生效
```

---

## 10. 实施顺序

| 阶段 | 内容 | 依赖 | 预计工作量 |
|------|------|------|-----------|
| Phase 1 | Schema 迁移 + Scope 隔离 | 无 | 中 |
| Phase 2 | EntityExtractor + EntityStore | Phase 1 | 中 |
| Phase 3 | ADD-only 混合架构 + 蒸馏变更 | Phase 1 | 中 |
| Phase 4 | 检索五路 RRF + Entity Boost + 测试 | Phase 2, 3 | 中 |

---

## 11. 成功标准

- 全量测试通过（现有 1311 + 新增 ~50 = ~1361 passed）
- 检索准确率提升（Entity Boost 生效）
- 重复记忆率下降（ADD-only + 蒸馏合并）
- Scope 隔离生效（不同 agent 记忆不串）
- 性能无退化（检索延迟 < 现有 +10%）

---

## 12. 不采纳的 SPEC 建议

| SPEC建议 | 不采纳原因 |
|---------|-----------|
| mem0 托管服务 | 项目是本地桌面应用，不依赖外部托管 |
| 外部向量库（Pinecone/Qdrant） | 已用 sqlite-vec，无需引入重依赖 |
| spaCy 英文 NLP | 项目是中文场景，spaCy 中文 F1 仅 85-89% |
| UPDATE/DELETE 旧逻辑 | 已用 ADD-only 混合架构替代 |
| CLI/REST API | 项目已有 WebUI，不需要额外接口 |
| Procedural Memory | 超出当前优化范围，YAGNI |
| FSRS-5 替换 Ebbinghaus | 保留独有功能，Ebbinghaus 已满足需求 |

---

## 13. 参考资料

- mem0 原仓库：`https://github.com/mem0ai/mem0`
  - `mem0/memory/main.py` — Memory 类，实体链接核心逻辑
  - `mem0/utils/scoring.py` — BM25 归一化 + Entity Boost 计算
  - `mem0/utils/entity_extraction.py` — spaCy NER + 正则提取
- 外部 SPEC：`https://www.coze.cn/s/LLpy94BHkxs/`
- 项目已有 RAG 优化 SPEC：[RAG-OPTIMIZATION-SPEC.md](file:///home/orangepi/ai-agent/RAG-OPTIMIZATION-SPEC.md)
- 技术研究：
  - RRF (Reciprocal Rank Fusion)，k=60 标准参数
  - BM25 sigmoid 归一化：`1/(1+e^(-steepness*(x-midpoint)))`
  - Entity Boost：`boost = similarity × 0.5 × memory_count_weight`
  - ADD-only vs UPDATE/DELETE：混合架构是生产系统主流
  - sqlite-vec + FTS5 混合检索：ZeroClaw 系统 <3ms 延迟
