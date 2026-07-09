# 父子 Chunk RAG 优化 + Contextual Retrieval 设计文档

> 日期: 2026-07-09 | 基于 Coze SPEC + Anthropic Contextual Retrieval
> 范围: Phase 1-3（核心切分 + enrichment 子chunk化 + 重叠窗口）

## 一、背景

当前 RAG 检索端已完善（Reranker + HyDE + CRAG + QueryCache + 三路并行检索），但切分端存在核心短板：
- 摘要粒度固定（6轮×150字→500字），无层级
- 检索=生成同源，无法同时兼顾精准匹配和完整上下文
- enrichment 提取的实体/决策仅存 metadata，不参与独立检索
- 相邻 summary 无重叠，跨 chunk 边界信息断裂

## 二、架构设计

### 2.1 核心原理

```
子块（小粒度）→ 负责检索：语义聚焦，精准匹配用户问题
父块（大粒度）→ 负责生成：保留完整上下文，避免断章取义
```

### 2.2 Contextual Retrieval 集成

每个子 chunk 嵌入时注入父摘要上下文前缀（零额外 LLM 调用）：
```
embed_content = "[上下文: {parent_summary[:80]}] {child_content}"
```

Anthropic 研究表明上下文前缀可降低 35% 检索失败率，复用已生成的 parent_summary 实现零成本。

## 三、数据模型

### 3.1 新增表

```sql
CREATE TABLE IF NOT EXISTS memory_child_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    embed_content TEXT DEFAULT '',
    chunk_type TEXT NOT NULL DEFAULT 'segment',
    importance REAL DEFAULT 0.5,
    overlap_hash TEXT DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES episodic_memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_child_parent ON memory_child_chunks(parent_id);
CREATE INDEX IF NOT EXISTS idx_child_type ON memory_child_chunks(chunk_type);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_child_chunks_fts
    USING fts5(content, tokenize='unicode61');
```

### 3.2 子 chunk 向量表

在 `VectorStore.init()` 中新增 `memories_child_vec` 虚拟表（同结构，独立表名）。

### 3.3 chunk_type 定义

| type | 来源 | 粒度 |
|------|------|------|
| segment | 对话中每轮用户/助手消息 | 单轮 |
| entity | enrichment提取的实体 | 关键词/短语 |
| decision | enrichment提取的决策 | 1句话 |
| topic | enrichment提取的话题 | 1-3词 |

## 四、编码管线改造

### 4.1 encode_memory() 改造

在现有 `mem_id` 写入后新增：
1. `_split_into_children(exchanges, mem_id, parent_summary)` 切分子 chunk
2. 每个子 chunk 生成 `embed_content`（含 Contextual Retrieval 前缀）
3. 批量写入 `memory_child_chunks` 表 + FTS 索引
4. 批量嵌入到 `memories_child_vec`

### 4.2 _split_into_children() 切分逻辑

- 取最近 8 轮对话
- 每轮作为一个 segment 子 chunk，最多 200 字
- 重叠窗口：与前一个子 chunk 尾部 30 字重叠
- 用户消息权重 1.0，助手消息权重 0.8
- 最多 10 个子 chunk per parent

## 五、检索管线改造

### 5.1 retrieve_memories_hybrid() 改造

```
query → [child_FTS + child_Vec] 并行  ← 子chunk精准检索
      → RRF 融合 → 子chunk→父chunk映射（去重）
      → 合并直接父chunk检索结果（FTS+Vec+KG）
      → Reranker 在父chunk层面精排
      → top-k
```

### 5.2 关键设计

- 子 chunk 只参与检索，不参与生成
- 向后兼容：子 chunk 检索 0 结果时退化为纯父 chunk 检索
- 子 chunk 和父 chunk 检索并行执行（asyncio.gather）

## 六、Enrichment 子 chunk 化

`_enrich_memory_async()` 提取的实体/决策/话题 → 写入子 chunk：
- chunk_type = 'entity' — 每个实体一个子 chunk
- chunk_type = 'decision' — 决策语句
- chunk_type = 'topic' — 话题关键词

## 七、重叠窗口

- overlap_chars = 30（可配置）
- overlap_hash = SHA256[:8] of 重叠区域
- 检索去重：同一父 chunk 的多个子 chunk 被召回时合并

## 八、配置项

```python
PARENT_CHILD_CHUNK_ENABLED = true（默认开启）
CONTEXTUAL_RETRIEVAL_ENABLED = true（默认开启）
CHILD_CHUNK_OVERLAP_CHARS = 30
CHILD_CHUNK_MAX_PER_PARENT = 10
CHILD_CHUNK_SEGMENT_MAX_LEN = 200
CHILD_VEC_TABLE = "memories_child_vec"
```

## 九、向后兼容

| 场景 | 处理 |
|------|------|
| 旧记忆无子 chunk | 退化为纯父 chunk 检索 |
| 配置关闭 | 跳过所有子 chunk 逻辑 |
| 向量表分离 | memories_vec 和 memories_child_vec 独立 |

## 十、涉及文件

| 文件 | 改动 |
|------|------|
| db/schema.sql | 新增 memory_child_chunks 表 + FTS |
| db/db_memory.py | 新增 child chunk CRUD 方法 |
| memory/vector_store.py | 新增 memories_child_vec 表 + batch_upsert + EmbedCache 512 |
| memory/memory_manager.py | 改造 encode/retrieve/enrich + 新增 _split_into_children |
| config.py | 新增配置项 |
| .env.example | 新增配置示例 |
| tests/test_parent_child_chunk.py | 新增测试 |

## 十一、预期效果

| 指标 | 当前 | 优化后 | 提升 |
|------|------|--------|------|
| 向量检索召回率 | ~57.5% | ~75%+ | +30% |
| context_recall | ~0.625 | ~0.875 | +0.250 |
| context_precision | ~0.65 | ~0.85 | +0.200 |
| 指代不明错误率 | 基线 | 降低57.1% | — |
