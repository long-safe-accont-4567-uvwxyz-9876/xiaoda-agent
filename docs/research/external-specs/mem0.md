# xiaoda-agent 记忆系统优化 Spec — 基于 mem0 核心机制

> **版本**: v1.0 | **日期**: 2026-07-11 | **基线**: xiaoda-agent v0.5.03 | **参考**: mem0ai/mem0 (60K⭐, YC S24)
> **目标**: 对齐mem0 6大核心机制, 将xiaoda-agent记忆系统的检索准确率从~65%提升至90%+, P95延迟<200ms

---

## 目录

1. [现状诊断](#1-现状诊断)
2. [mem0核心机制深度分析](#2-mem0核心机制深度分析)
3. [差距对比表](#3-差距对比表)
4. [优化方案](#4-优化方案)
5. [实施步骤](#5-实施步骤)
6. [预期效果](#6-预期效果)
7. [不采纳的部分及原因](#7-不采纳的部分及原因)

---

## 1. 现状诊断

### 1.1 记忆架构总览

xiaoda-agent v0.5.03记忆系统由7个核心模块构成:

```
memory/
├── memory_manager.py      # 主入口: jieba分词实体提取 + 时间词解析 + 话题停用词
├── fluid_memory.py         # 流体记忆: Ebbinghaus衰减 + 访问频率提升
├── vector_store.py         # 向量存储: sqlite-vec + OpenAI Embedding + LRU EmbedCache
├── emotional_memory.py     # Stanislavski情感记忆: Anchoring/Recalling/Bounding/Enacting
├── knowledge_graph.py      # 知识图谱: LLM实体关系提取→JSON→图谱存储
├── memory_distiller.py     # 记忆蒸馏
├── reranker.py             # 重排序
├── query_cache.py          # 查询缓存
├── recall_scheduler.py     # 召回调度
└── context_compressor.py   # 上下文压缩
```

辅助系统:
- `emotion/pad_model.py`: PAD(Pleasure-Arousal-Dominance)情绪模型
- `core/dream_consolidation.py`: 梦境整合(Ebbinghaus衰减 + 重要性重评估 + 相似记忆合并)
- `core/meta_cognition.py`: 元认知(confidence/fatigue/error_rate/memory_pressure)

### 1.2 六大问题诊断

#### 问题1: 实体链接缺失 — 记忆是孤岛

**现象**: 用户说"我昨天和妈妈去了西湖",jieba提取出"妈妈""西湖"两个实体,但这两个实体与任何已有记忆无关联。当用户后续问"上次和妈妈的旅行",系统只能靠语义相似度搜索,无法通过"妈妈"这个实体锚点精确召回。

**根因**: `knowledge_graph.py`中的实体提取只在存储时提取关系三元组,但:
- 实体没有独立存储和嵌入,无法做实体→记忆的反向链接
- `memory_manager.py`的jieba分词只做关键词提取,不做实体去重/链接
- 没有类似mem0的`entity_store`(独立向量集合),实体只是metadata字段

**代码证据**:
```python
# memory/memory_manager.py 当前实现
def extract_entities(self, text: str) -> list[str]:
    """jieba分词提取关键词,返回词列表"""
    words = jieba.cut(text)
    # 只返回过滤后的词列表,无实体类型/链接/嵌入
    return [w for w in words if w not in self.stop_words and len(w) > 1]
```

#### 问题2: 检索信号单一 — 只有语义向量

**现象**: 用户搜索"上个星期吃的火锅店",系统只做向量相似度搜索,返回了"喜欢吃辣"而非"上周去了海底捞"。因为"上周去了海底捞"的语义向量与查询的余弦相似度不如"喜欢吃辣"高。

**根因**: `vector_store.py`只支持语义检索一路信号:
```python
# memory/vector_store.py 当前实现
def search(self, query: str, top_k: int = 5) -> list[MemoryItem]:
    query_embedding = self.embed(query)
    results = self.vec_conn.search(query_embedding, top_k=top_k)
    # 只有语义相似度,无BM25关键词匹配,无实体boost
    return results
```

虽然已有FTS5全文索引,但未被召回流程使用。`recall_scheduler.py`和`reranker.py`只处理语义检索结果,FTS5形同虚设。

#### 问题3: UPDATE逻辑导致记忆丢失

**现象**: 用户先说"我喜欢吃辣",后说"我最近在减肥,不太吃辣了"。当前系统用UPDATE逻辑,将"我喜欢吃辣"更新为"在减肥,不太吃辣",**原始偏好被覆盖,丢失了用户曾经喜欢辣的事实**。这对情感陪伴场景尤其致命——陪伴者应该记住"用户曾经喜欢辣"这个时间线变化。

**根因**: `memory_distiller.py`的蒸馏逻辑和`fluid_memory.py`的衰减公式都假设记忆可以被覆盖:
```python
# memory/fluid_memory.py
def update_memory(self, memory_id: str, new_content: str):
    """直接覆盖记忆内容"""
    self.memories[memory_id].content = new_content
    self.memories[memory_id].updated_at = now()
```

#### 问题4: 无多级记忆隔离

**现象**: 所有记忆混在一个pool里,无法区分:
- User级: "用户叫小明"(跨session持久)
- Session级: "这次对话讨论了旅行计划"(当前会话)
- Agent级: "xiaoda应该温柔地回应"(人格指令)

**根因**: `vector_store.py`的表结构无`scope`字段:
```sql
-- 当前表结构
CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    content TEXT,
    embedding BLOB,
    importance REAL,
    created_at DATETIME,
    updated_at DATETIME
    -- 缺少: scope, session_id, agent_id
);
```

#### 问题5: 时间感知检索弱

**现象**: 用户问"我上周说了什么",系统无法将"上周"映射到具体日期范围并检索。`memory_manager.py`有`parse_time_words()`解析时间词,但解析结果只用于metadata标注,不参与检索排序。

**根因**: 时间词解析与检索脱节:
```python
# memory/memory_manager.py
def parse_time_words(self, text: str) -> dict:
    """解析'昨天'→日期,但结果只存metadata,不参与召回排序"""
    time_map = {"昨天": today - 1, "上周": today - 7, ...}
    return {word: date for word, date in time_map.items() if word in text}
```

#### 问题6: 记忆提取依赖单次完整对话

**现象**: `memory_distiller.py`需要一轮完整对话结束后才做蒸馏提取,无法在对话进行中实时提取事实。用户说了5句话,只有到第5句后才提取,中间的临时上下文丢失。

**根因**: 蒸馏是批处理模式,不是增量模式:
```python
# memory/memory_distiller.py
def distill(self, conversation: list[dict]) -> list[dict]:
    """整个对话结束后一次性蒸馏"""
    full_text = "\n".join(m["content"] for m in conversation)
    return self.llm_extract(full_text)
```

---

## 2. mem0核心机制深度分析

### 2.1 机制一: ADD-only积累式记忆提取

**核心思想**: 一次LLM调用提取事实,只做ADD不做UPDATE/DELETE,让记忆像日志一样积累式生长。

**代码路径**: `mem0/memory/main.py::_add_to_vector_store()` → Phase 2

```python
# mem0 V3 Pipeline (简化)
def _add_to_vector_store(self, messages, metadata, filters, infer):
    # Phase 0: 上下文收集
    last_messages = self.db.get_last_messages(session_scope, limit=10)
    
    # Phase 1: 已有记忆检索(防重复)
    existing_results = self.vector_store.search(query, vectors, top_k=10, filters=search_filters)
    existing_memories = [{"id": str(idx), "text": mem.payload.get("data", "")} for idx, mem in enumerate(existing_results)]
    
    # Phase 2: 单次LLM提取 — 只产出ADD操作
    system_prompt = ADDITIVE_EXTRACTION_PROMPT  # "Your sole operation is ADD"
    user_prompt = generate_additive_extraction_prompt(
        existing_memories=existing_memories,  # 传给LLM防重复
        new_messages=parsed_messages,
        last_k_messages=last_messages,
    )
    response = self.llm.generate_response(
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        response_format={"type": "json_object"},
    )
    extracted = json.loads(response).get("memory", [])
    # 每条记忆只有: {"id": "0", "text": "...", "attributed_to": "user", "linked_memory_ids": [...]}
    # 没有 "event": "UPDATE" 或 "DELETE"
    
    # Phase 3-5: 批量嵌入 + Hash去重
    mem_texts = [m.get("text") for m in extracted if m.get("text")]
    mem_embeddings = self.embedding_model.embed_batch(mem_texts, "add")
    
    existing_hashes = {mem.payload.get("hash") for mem in existing_results}
    for mem in extracted:
        mem_hash = hashlib.md5(text.encode()).hexdigest()
        if mem_hash in existing_hashes:  # 精确去重
            continue
    
    # Phase 6: 批量持久化
    self.vector_store.insert(vectors=all_vectors, ids=all_ids, payloads=all_payloads)
```

**关键设计决策**:
1. **不UPDATE**: "用户曾喜欢辣"和"用户现在减肥不吃辣"是两条独立记忆,通过`linked_memory_ids`关联
2. **Hash去重**: `hashlib.md5(text.encode()).hexdigest()`精确去重,避免语义相似但事实相同的重复
3. **单次LLM调用**: V3 Pipeline将旧版的两步(extract→update)合并为一步,减少50% Token消耗
4. **`linked_memory_ids`**: 新记忆可链接到已有记忆,形成记忆图谱边,为实体链接提供基础

**ADDITIVE_EXTRACTION_PROMPT核心指令** (33K字符,极长):
```
Your sole operation is ADD: identify every piece of memorable information and produce 
self-contained, contextually rich factual statements.

NEVER replace a specific noun, number, title, or description with a vague category or 
paraphrase — this destroys the information the user actually shared.

When extracting a new memory, check if it relates to any Existing Memory. 
Add related Existing Memory IDs to "linked_memory_ids".
```

### 2.2 机制二: 实体链接(Entity Linking)

**核心思想**: 从记忆文本中提取实体→嵌入→存储到独立entity_store→建立实体→记忆的双向链接→检索时通过实体boost提升相关记忆分数。

**代码路径**: `mem0/utils/entity_extraction.py` → `mem0/memory/main.py::_upsert_entity()` / `_compute_entity_boosts_async()`

**提取阶段** — 使用spaCy NLP提取4类实体:

```python
# mem0/utils/entity_extraction.py
def extract_entities(text: str) -> list[tuple[str, str]]:
    """提取4类实体: (entity_type, entity_text)"""
    # PROPER: 人名/地名/品牌 — spaCy NER + 大写连续序列
    # QUOTED: 引号内文本 — 正则提取
    # TOPIC: 名词复合短语 — spaCy noun_chunks + 过滤
    # IDENTIFIER: 技术标识符 — 如 "com.example.Service"
    nlp = get_nlp_full()
    doc = nlp(text)
    candidates = []
    _add_ner_candidates(doc, candidates)              # spaCy NER
    _add_technical_identifier_candidates(tokens, candidates)  # 正则
    _add_proper_name_candidates(tokens, candidates)   # 大写连续
    _add_quoted_candidates(text, candidates)           # 引号文本
    _add_topic_phrase_candidates(doc, candidates)      # 名词复合
    return _resolve_candidates(candidates)  # 去重+优先级排序
```

**存储阶段** — 实体存入独立向量集合:

```python
# mem0/memory/main.py
def _upsert_entity(self, entity_text, entity_type, memory_id, filters):
    entity_embedding = self.embedding_model.embed(entity_text, "add")
    
    # 先精确匹配(文本归一化)
    exact_match = self._existing_entities_by_text(filters).get(
        self._normalize_entity_text(entity_text)
    )
    
    # 再语义匹配(余弦相似度≥0.95)
    if exact_match is None:
        existing = self.entity_store.search(query=entity_text, vectors=entity_embedding, top_k=1)
        semantic_match = existing[0] if existing and existing[0].score >= 0.95 else None
    
    match = exact_match or semantic_match
    if match:
        # 链接已有实体: 在linked_memory_ids中追加当前memory_id
        linked_ids = payload.get("linked_memory_ids", [])
        if memory_id not in linked_ids:
            linked_ids.append(memory_id)
            payload["linked_memory_ids"] = linked_ids
            self.entity_store.update(vector_id=match.id, vector=None, payload=payload)
    else:
        # 新建实体
        entity_id = str(uuid.uuid4())
        entity_payload = {
            "data": entity_text,
            "entity_type": entity_type,
            "linked_memory_ids": [memory_id],  # 关键: 反向链接到记忆
        }
        self.entity_store.insert(vectors=[entity_embedding], ids=[entity_id], payloads=[entity_payload])
```

**检索阶段** — 实体boost增强:

```python
# mem0/memory/main.py::_compute_entity_boosts_async()
async def _compute_entity_boosts_async(self, query_entities, filters):
    memory_boosts = {}
    for entity_type, entity_text in query_entities:
        entity_embedding = self.embedding_model.embed(entity_text, "search")
        matches = self.entity_store.search(query=entity_text, vectors=entity_embedding, top_k=500)
        
        for match in matches:
            similarity = match.score
            if similarity < 0.5: continue
            
            linked_memory_ids = match.payload.get("linked_memory_ids", [])
            # 链接记忆数越多,单条记忆的boost越小(避免热门实体主导)
            memory_count_weight = 1.0 / (1.0 + 0.001 * ((len(linked_memory_ids) - 1) ** 2))
            boost = similarity * ENTITY_BOOST_WEIGHT * memory_count_weight  # 0.5
            
            for memory_id in linked_memory_ids:
                memory_boosts[memory_id] = max(memory_boosts.get(memory_id, 0.0), boost)
    
    return memory_boosts
```

**设计亮点**:
- 实体有独立向量空间(`entity_store`),与记忆向量空间分离
- `linked_memory_ids`实现实体→记忆的反向索引,无需全表扫描
- `memory_count_weight`惩罚链接数过多的实体(如"用户"这种泛实体),防止boost被稀释

### 2.3 机制三: 多信号检索融合(三路RRF)

**核心思想**: 语义检索 + BM25关键词检索 + 实体匹配boost,三路信号融合排序。

**代码路径**: `mem0/memory/main.py::_search_vector_store()` → `mem0/utils/scoring.py::score_and_rank()`

```python
# mem0/memory/main.py::_search_vector_store()
async def _search_vector_store(self, query, filters, limit, threshold=0.1):
    # Step 1: 预处理查询
    query_lemmatized = lemmatize_for_bm25(query)  # spaCy词形还原
    query_entities = extract_entities(query)        # 实体提取
    
    # Step 2: 嵌入查询
    embeddings = self.embedding_model.embed(query, "search")
    
    # Step 3: 语义检索(over-fetch: 4倍top_k)
    internal_limit = max(limit * 4, 60)
    semantic_results = self.vector_store.search(query=query, vectors=embeddings, top_k=internal_limit)
    
    # Step 4: BM25关键词检索(如果向量库支持)
    keyword_results = self.vector_store.keyword_search(query=query_lemmatized, top_k=internal_limit)
    
    # Step 5: BM25分数归一化(逻辑sigmoid)
    bm25_scores = {}
    midpoint, steepness = get_bm25_params(query, lemmatized=query_lemmatized)
    for mem in keyword_results:
        bm25_scores[mem_id] = normalize_bm25(raw_score, midpoint, steepness)
    
    # Step 6: 实体boost计算
    entity_boosts = {}
    if query_entities:
        entity_boosts = await self._compute_entity_boosts_async(query_entities, filters)
    
    # Step 7-8: 构建候选集 → 加性融合排序
    scored_results = score_and_rank(
        semantic_results=candidates,
        bm25_scores=bm25_scores,
        entity_boosts=entity_boosts,
        threshold=threshold,
        top_k=limit,
    )
```

**融合公式** — `scoring.py::score_and_rank()`:

```python
# mem0/utils/scoring.py
ENTITY_BOOST_WEIGHT = 0.5

def score_and_rank(semantic_results, bm25_scores, entity_boosts, threshold, top_k):
    max_possible = 1.0  # 语义
    if has_bm25: max_possible += 1.0           # BM25
    if has_entity: max_possible += 0.5         # 实体boost
    
    for result in semantic_results:
        semantic_score = result["score"]
        if semantic_score < threshold: continue  # 语义阈值前置过滤!
        
        bm25_score = bm25_scores.get(mem_id, 0.0)
        entity_boost = entity_boosts.get(mem_id, 0.0)
        
        raw_combined = semantic_score + bm25_score + entity_boost
        combined = min(raw_combined / max_possible, 1.0)
    
    return sorted(scored, key=lambda x: x["score"], reverse=True)[:top_k]
```

**BM25归一化** — 自适应sigmoid:
```python
def get_bm25_params(query, lemmatized=None):
    """根据查询词数自适应sigmoid参数"""
    num_terms = len(lemmatized.split())
    if num_terms <= 3: return 5.0, 0.7   # 短查询: 陡峭sigmoid
    elif num_terms <= 6: return 7.0, 0.6
    elif num_terms <= 9: return 9.0, 0.5
    else: return 12.0, 0.5               # 长查询: 平缓sigmoid

def normalize_bm25(raw_score, midpoint, steepness):
    """逻辑sigmoid归一化到[0,1]"""
    return 1.0 / (1.0 + math.exp(-steepness * (raw_score - midpoint)))
```

**关键设计**:
- 语义阈值前置过滤: 语义分<threshold直接淘汰,避免BM25/entity boost把低语义结果抬上来
- 自适应BM25: 短查询(1-3词)用更陡的sigmoid(0.7),避免短查询BM25分数被压平
- 除以max_possible: 不同信号数量下分数可比,有BM25时除2.5,没有时除1.0

### 2.4 机制四: User/Session/Agent三级记忆隔离

**核心思想**: 通过metadata中的`user_id`/`run_id`/`agent_id`实现三级记忆隔离,查询时必须指定scope。

**代码路径**: `mem0/memory/main.py::_build_filters_and_metadata()`

```python
# mem0/memory/main.py
def _build_filters_and_metadata(*, user_id=None, agent_id=None, run_id=None, ...):
    base_metadata_template = {}
    effective_query_filters = {}
    
    # 至少一个scope ID必须提供
    if user_id:
        base_metadata_template["user_id"] = user_id
        effective_query_filters["user_id"] = user_id
    if agent_id:
        base_metadata_template["agent_id"] = agent_id
        effective_query_filters["agent_id"] = agent_id
    if run_id:
        base_metadata_template["run_id"] = run_id
        effective_query_filters["run_id"] = run_id
    
    if not any([user_id, agent_id, run_id]):
        raise ValidationError("At least one of 'user_id', 'agent_id', or 'run_id' must be provided.")
    
    return base_metadata_template, effective_query_filters
```

**三级隔离语义**:
- **user_id**: 跨session持久记忆 — "用户叫小明,喜欢猫"
- **run_id**: 当前session临时记忆 — "这次对话在讨论旅行"
- **agent_id**: Agent自身记忆 — "xiaoda应该用温柔语气"

**session_scope** 用于消息历史隔离:
```python
def _build_session_scope(filters):
    """构建确定性session标识: 'agent_id=xiao&run_id=abc&user_id=ming'"""
    parts = []
    for key in sorted(["user_id", "agent_id", "run_id"]):
        val = filters.get(key)
        if val: parts.append(f"{key}={val}")
    return "&".join(parts)
```

### 2.5 机制五: 时间感知检索

**核心思想**: 记忆存储时记录精确的`created_at`/`updated_at`,提取时注入当前日期到prompt(避免LLM产生错误的时间推断),检索时支持`expiration_date`过期过滤。

**代码路径**: 
- 存储时: `_create_memory()` 自动填充 `created_at` / `updated_at`
- 提取时: `ADDITIVE_EXTRACTION_PROMPT` 中注入 `Observation Date` / `Current Date`
- 检索时: `_payload_is_expired()` 过滤过期记忆

```python
# 存储时自动时间戳
def _create_memory(self, data, existing_embeddings, metadata=None):
    new_metadata["created_at"] = datetime.now(timezone.utc).isoformat()
    new_metadata["updated_at"] = new_metadata["created_at"]
    new_metadata["text_lemmatized"] = lemmatize_for_bm25(data)  # BM25用词形还原

# 提取时注入时间上下文(在generate_additive_extraction_prompt中)
sections.append(f"## Observation Date\n{observation_date}")
sections.append(f"## Current Date\n{current_date}")
# LLM基于这些日期生成时间感知的记忆,如"User adopted a puppy around March 1-2, 2025"

# 检索时过滤过期记忆
def _payload_is_expired(payload):
    expiration_date = payload.get("expiration_date")
    if not expiration_date: return False
    return date.fromisoformat(str(expiration_date)) < datetime.now(timezone.utc).date()
```

### 2.6 机制六: V3批量Pipeline与性能优化

**核心思想**: 将6个Phase串行执行,最大化批处理效率,减少LLM/嵌入调用次数。

**代码路径**: `mem0/memory/main.py::_add_to_vector_store()` Phase 0-7

```
Phase 0: 上下文收集 (DB查询last 10 messages)
Phase 1: 已有记忆检索 (向量搜索top 10, 获取existing_memories + hashes)
Phase 2: 单次LLM提取 (system_prompt + user_prompt → json)
Phase 3: 批量嵌入 (embed_batch, 一次API调用嵌入所有记忆文本)
Phase 4: 每条记忆CPU处理 (构建payload)
Phase 5: Hash去重 (MD5精确去重, 跳过重复)
Phase 6: 批量持久化 (vector_store.insert批量写入)
Phase 7: 批量实体链接 (extract_entities_batch → embed_batch → batch search → batch insert/update)
```

**关键性能优化**:
1. **embed_batch**: 多个文本一次嵌入API调用(vs 逐个调用),减少N倍API开销
2. **Hash去重**: MD5 O(1)判定重复,避免语义相似度搜索
3. **Entity batch**: 提取、嵌入、搜索、插入全流程批处理
4. **UUID→整数映射**: 给LLM看`{"id": "0", "text": "..."}`而非UUID,防LLM幻觉生成不存在的ID

---

## 3. 差距对比表

| 维度 | xiaoda-agent v0.5.03 | mem0 V3 | 差距评级 |
|------|----------------------|---------|----------|
| **记忆提取策略** | UPDATE逻辑(覆盖式) | ADD-only积累式 + Hash去重 | 🔴 严重 |
| **实体链接** | jieba关键词提取,无链接 | spaCy 4类实体 + 独立entity_store + 双向链接 | 🔴 严重 |
| **检索信号** | 仅语义向量(1路) | 语义 + BM25 + 实体boost(3路融合) | 🔴 严重 |
| **BM25关键词检索** | FTS5已建索引但未使用 | lemmatize_for_bm25 + 自适应sigmoid归一化 | 🟡 中等 |
| **多级记忆隔离** | 无scope区分 | User/Session/Agent三级 + 强制scope | 🟡 中等 |
| **时间感知** | parse_time_words仅标注 | 注入日期到prompt + expiration_date + 时间推理 | 🟡 中等 |
| **去重机制** | 无(允许语义重复记忆) | MD5 Hash精确去重 + LLM上下文防重复 | 🔴 严重 |
| **批量处理** | 逐条嵌入/写入 | embed_batch + vector_store.insert批量 + entity batch | 🟢 轻微 |
| **情感记忆** | ✅ Stanislavski 4阶段 | ❌ 无 | 🟢 xiaoda优势 |
| **遗忘曲线** | ✅ Ebbinghaus + 访问频率boost | ❌ 无(托管版有decay) | 🟢 xiaoda优势 |
| **元认知** | ✅ confidence/fatigue/error_rate | ❌ 无 | 🟢 xiaoda优势 |
| **梦境整合** | ✅ Ebbinghaus衰减 + 相似合并 | ❌ 无 | 🟢 xiaoda优势 |
| **PAD情绪模型** | ✅ Pleasure-Arousal-Dominance | ❌ 无 | 🟢 xiaoda优势 |
| **存储后端** | SQLite + sqlite-vec | Chroma/Qdrant/Postgres/pgvector等20+ | 🟢 设计差异 |
| **API设计** | 本地Python API | 同步+异步双API + REST + CLI | 🟢 设计差异 |

**总结**: xiaoda-agent在**情感/遗忘/元认知**等AI陪伴特色功能上有显著优势,但在**记忆提取、实体链接、多信号检索**这三个核心记忆基础设施上与mem0存在严重差距。

---

## 4. 优化方案

### 4.1 引入实体链接机制

**目标**: 让每条记忆可被其中包含的实体精确召回,从"语义模糊匹配"升级为"实体锚定召回"。

**复用现有组件**: jieba分词(替换为更精准的方案) + sqlite-vec(独立entity集合)

#### 4.1.1 新建 `memory/entity_store.py`

```python
# memory/entity_store.py

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger

from memory.vector_store import VectorStore  # 复用现有sqlite-vec封装


@dataclass
class Entity:
    """实体记录 — 独立于记忆存储在entity_vec集合中"""
    id: str
    text: str                    # 实体文本: "西湖", "妈妈"
    entity_type: str             # PERSON/LOCATION/TOPIC/IDENTIFIER
    linked_memory_ids: list[str] = field(default_factory=list)
    embedding: Optional[list[float]] = None


class EntityStore:
    """实体存储 — 基于sqlite-vec的独立向量集合

    设计参考: mem0 entity_store (独立collection_name="{collection}_entities")
    实现: 复用现有VectorStore, 新建entity_vec表

    实体生命周期:
    1. 记忆写入时, extract_entities_from_text()提取实体
    2. 对每个实体:
       a. 精确匹配: 归一化文本在已有实体中查找
       b. 语义匹配: 余弦相似度≥0.95视为同一实体
       c. 已有实体: 追加memory_id到linked_memory_ids
       d. 新实体: 嵌入→写入entity_vec
    3. 检索时: 查询提取实体→在entity_vec搜索→获取linked_memory_ids→boost
    """

    SIMILARITY_THRESHOLD = 0.95  # 与mem0一致

    def __init__(self, vector_store: VectorStore, embed_fn):
        self._vec_store = vector_store
        self._embed_fn = embed_fn
        self._ensure_entity_table()

    def _ensure_entity_table(self):
        """创建entity_vec表(如果不存在)"""
        self._vec_store.create_collection("entity_vec", dimension=self._vec_store.dimension)

    @staticmethod
    def normalize(text: str) -> str:
        """归一化实体文本: 小写+去多余空格"""
        return " ".join(text.strip().lower().split())

    def upsert_entity(self, entity_text: str, entity_type: str, memory_id: str, scope: dict):
        """写入/更新实体, 建立实体→记忆链接

        对齐mem0: _upsert_entity()
        """
        entity_embedding = self._embed_fn(entity_text)
        normalized = self.normalize(entity_text)

        # Step 1: 精确匹配(文本归一化后完全相等)
        exact_match = self._find_exact_match(normalized, scope)
        if exact_match:
            self._link_memory_to_entity(exact_match.id, memory_id)
            return

        # Step 2: 语义匹配(余弦≥0.95)
        semantic_matches = self._vec_store.search(
            collection="entity_vec",
            query_vector=entity_embedding,
            top_k=1,
            filters=scope,
        )
        if semantic_matches and semantic_matches[0].score >= self.SIMILARITY_THRESHOLD:
            self._link_memory_to_entity(semantic_matches[0].id, memory_id)
            return

        # Step 3: 新建实体
        entity_id = str(uuid.uuid4())
        payload = {
            "data": entity_text,
            "entity_type": entity_type,
            "linked_memory_ids": [memory_id],
            **scope,
        }
        self._vec_store.insert(
            collection="entity_vec",
            vectors=[entity_embedding],
            ids=[entity_id],
            payloads=[payload],
        )
        logger.debug(f"Entity created: {entity_text} ({entity_type}) → memory {memory_id[:8]}")

    def compute_entity_boosts(self, query_entities: list[tuple[str, str]], scope: dict) -> dict[str, float]:
        """计算实体boost — 查询实体→链接记忆→boost分数

        对齐mem0: _compute_entity_boosts_async()
        公式: boost = similarity × ENTITY_BOOST_WEIGHT × memory_count_weight
        memory_count_weight = 1.0 / (1.0 + 0.001 × (num_linked - 1)²)
        """
        ENTITY_BOOST_WEIGHT = 0.5  # 与mem0一致
        memory_boosts: dict[str, float] = {}

        for entity_type, entity_text in query_entities:
            entity_embedding = self._embed_fn(entity_text)
            matches = self._vec_store.search(
                collection="entity_vec",
                query_vector=entity_embedding,
                top_k=500,
                filters=scope,
            )

            for match in matches:
                if match.score < 0.5:
                    continue
                linked_ids = match.payload.get("linked_memory_ids", [])
                if not linked_ids:
                    continue

                # 链接记忆数越多,单条记忆boost越小
                num_linked = max(len(linked_ids), 1)
                memory_count_weight = 1.0 / (1.0 + 0.001 * ((num_linked - 1) ** 2))
                boost = match.score * ENTITY_BOOST_WEIGHT * memory_count_weight

                for mid in linked_ids:
                    memory_boosts[mid] = max(memory_boosts.get(mid, 0.0), boost)

        return memory_boosts

    def remove_memory_from_entities(self, memory_id: str, scope: dict):
        """删除记忆时,从实体中移除链接"""
        entities = self._vec_store.list(collection="entity_vec", filters=scope)
        for entity in entities:
            linked = entity.payload.get("linked_memory_ids", [])
            if memory_id in linked:
                linked.remove(memory_id)
                if not linked:
                    self._vec_store.delete(collection="entity_vec", vector_id=entity.id)
                else:
                    entity.payload["linked_memory_ids"] = linked
                    self._vec_store.update(collection="entity_vec", vector_id=entity.id, payload=entity.payload)

    def _find_exact_match(self, normalized_text: str, scope: dict) -> Optional:
        """精确匹配已有实体(文本归一化后完全相等)"""
        entities = self._vec_store.list(collection="entity_vec", filters=scope)
        for entity in entities:
            if self.normalize(entity.payload.get("data", "")) == normalized_text:
                return entity
        return None

    def _link_memory_to_entity(self, entity_id: str, memory_id: str):
        """在实体的linked_memory_ids中追加memory_id"""
        entity = self._vec_store.get(collection="entity_vec", vector_id=entity_id)
        if entity is None:
            return
        linked = entity.payload.get("linked_memory_ids", [])
        if memory_id not in linked:
            linked.append(memory_id)
            entity.payload["linked_memory_ids"] = linked
            self._vec_store.update(collection="entity_vec", vector_id=entity_id, payload=entity.payload)
```

#### 4.1.2 中文实体提取 `memory/entity_extractor.py`

```python
# memory/entity_extractor.py

import re
from typing import Optional
from loguru import logger


class ChineseEntityExtractor:
    """中文实体提取器 — 替代jieba关键词提取

    mem0使用spaCy(英文NLP),但对中文支持弱。
    xiaoda-agent方案: jieba词性标注 + 规则增强, 比spaCy中文更准确。

    提取4类实体(对齐mem0分类):
    - PERSON: 人名 (jieba nr标签 + 中文姓名库)
    - LOCATION: 地名 (jieba ns标签 + 中国地名库)
    - TOPIC: 话题名词短语 (jieba nz/nw标签 + 复合名词)
    - IDENTIFIER: 技术标识符 (正则: 英文.英文格式)
    """

    # 中文姓名常见姓氏(百家姓前100)
    COMMON_SURNAMES = set("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄".replace(" ", ""))

    def __init__(self):
        import jieba
        import jieba.posseg as pseg
        self._pseg = pseg
        # 加载用户自定义词典(如果有)
        try:
            jieba.load_userdict("data/user_dict.txt")
        except Exception:
            pass

    def extract(self, text: str) -> list[tuple[str, str]]:
        """提取实体 → [(entity_type, entity_text), ...]

        对齐mem0: extract_entities() 返回格式
        """
        candidates = []

        # 1. jieba词性标注提取
        for word, flag in self._pseg.cut(text):
            if flag == "nr":  # 人名
                candidates.append(("PERSON", word))
            elif flag == "ns":  # 地名
                candidates.append(("LOCATION", word))
            elif flag in ("nz", "nw", "nt"):  # 专有名词/网络词/机构团体
                candidates.append(("TOPIC", word))
            elif flag == "eng" and re.match(r"[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)+", word):
                candidates.append(("IDENTIFIER", word))

        # 2. 中文姓名增强(jieba对人名识别不够准确)
        candidates.extend(self._extract_chinese_names(text))

        # 3. 引号内文本(与mem0一致)
        candidates.extend(self._extract_quoted(text))

        # 4. 复合名词短语(形+名, 如"火锅店"/"猫屎咖啡")
        candidates.extend(self._extract_compound_nouns(text))

        # 去重 + 过滤
        return self._deduplicate(candidates, text)

    def _extract_chinese_names(self, text: str) -> list[tuple[str, str]]:
        """中文姓名增强: 姓氏+1-2字名"""
        results = []
        # 匹配2-3字中文姓名
        for m in re.finditer(r'[\u4e00-\u9fff]{2,4}', text):
            word = m.group()
            if len(word) >= 2 and word[0] in self.COMMON_SURNAMES:
                # 排除常见非人名词汇
                non_name_suffixes = {"家", "人", "国", "省", "市", "区", "县", "路", "街"}
                if word[-1] not in non_name_suffixes:
                    results.append(("PERSON", word))
        return results

    def _extract_quoted(self, text: str) -> list[tuple[str, str]]:
        """提取引号内文本"""
        results = []
        # 中文引号
        for m in re.finditer(r'[\u201c\u300c"\'"]([^\u201d\u300d"\'"]{2,})[\u201d\u300d"\'"]', text):
            results.append(("TOPIC", m.group(1)))
        return results

    def _extract_compound_nouns(self, text: str) -> list[tuple[str, str]]:
        """提取复合名词短语"""
        results = []
        for word, flag in self._pseg.cut(text):
            if flag == "n" and len(word) >= 3:  # 3字以上的名词
                results.append(("TOPIC", word))
        return results

    def _deduplicate(self, candidates: list[tuple[str, str]], text: str) -> list[tuple[str, str]]:
        """去重: 文本归一化后去重, 优先级 PERSON > LOCATION > TOPIC > IDENTIFIER"""
        type_priority = {"PERSON": 0, "LOCATION": 1, "IDENTIFIER": 2, "TOPIC": 3}
        seen = {}
        for etype, etext in candidates:
            if len(etext) <= 1:
                continue
            key = etext.lower().strip()
            if key not in seen or type_priority.get(etype, 99) < type_priority.get(seen[key][0], 99):
                seen[key] = (etype, etext)
        return list(seen.values())
```

### 4.2 多信号检索融合

**目标**: 语义 + FTS5 + 实体匹配, 三路RRF(Reciprocal Rank Fusion)融合排序。

**复用现有组件**: sqlite-vec语义检索 + FTS5全文索引(已建但未用) + entity_store实体匹配

#### 4.2.1 新建 `memory/hybrid_retriever.py`

```python
# memory/hybrid_retriever.py

import math
from typing import Optional
from dataclasses import dataclass
from loguru import logger

from memory.vector_store import VectorStore
from memory.entity_store import EntityStore
from memory.entity_extractor import ChineseEntityExtractor


@dataclass
class HybridResult:
    """融合检索结果"""
    id: str
    content: str
    score: float
    score_details: dict  # 语义/BM25/实体 各路分数明细
    payload: dict


class HybridRetriever:
    """三路检索融合器

    对齐mem0: _search_vector_store() + score_and_rank()
    但xiaoda-agent使用RRF(Reciprocal Rank Fusion)而非加性融合,
    因为RRF不需要分数归一化,对异构信号更鲁棒。

    三路信号:
    1. 语义检索: sqlite-vec余弦相似度
    2. BM25关键词: FTS5全文搜索(已有,启用)
    3. 实体匹配: EntityStore.compute_entity_boosts()

    融合策略: RRF
    RRF(d) = Σ 1/(k + rank_i(d))  where k=60 (标准参数)
    """

    RRF_K = 60  # RRF常数,标准值

    def __init__(
        self,
        vector_store: VectorStore,
        entity_store: EntityStore,
        entity_extractor: ChineseEntityExtractor,
    ):
        self._vec = vector_store
        self._entity_store = entity_store
        self._entity_extractor = entity_extractor

    def search(
        self,
        query: str,
        top_k: int = 10,
        threshold: float = 0.1,
        scope: Optional[dict] = None,
        explain: bool = False,
    ) -> list[HybridResult]:
        """三路检索融合

        Pipeline:
        1. 语义检索 (over-fetch: 4×top_k)
        2. FTS5 BM25检索 (over-fetch: 4×top_k)
        3. 查询实体提取 → entity_store → 实体boost
        4. RRF融合排序
        5. 阈值过滤 + top_k截断
        """
        scope = scope or {}
        over_fetch = max(top_k * 4, 60)

        # === Signal 1: 语义检索 ===
        query_embedding = self._vec.embed(query)
        semantic_results = self._vec.search(
            collection="memories",
            query_vector=query_embedding,
            top_k=over_fetch,
            filters=scope,
        )
        semantic_rank = {str(r.id): rank + 1 for rank, r in enumerate(semantic_results)}

        # === Signal 2: FTS5 BM25检索 ===
        bm25_results = self._vec.fts_search(
            collection="memories",
            query=query,
            top_k=over_fetch,
            filters=scope,
        )
        bm25_rank = {str(r.id): rank + 1 for rank, r in enumerate(bm25_results)}

        # === Signal 3: 实体boost ===
        query_entities = self._entity_extractor.extract(query)
        entity_boosts = {}
        if query_entities:
            entity_boosts = self._entity_store.compute_entity_boosts(query_entities, scope)

        # === RRF融合 ===
        all_ids = set(semantic_rank.keys()) | set(bm25_rank.keys()) | set(entity_boosts.keys())
        candidates = []

        for mem_id in all_ids:
            rrf_score = 0.0
            details = {}

            # 语义RRF分量
            if mem_id in semantic_rank:
                rrf_score += 1.0 / (self.RRF_K + semantic_rank[mem_id])
                details["semantic_rank"] = semantic_rank[mem_id]
                # 获取原始语义分数
                for r in semantic_results:
                    if str(r.id) == mem_id:
                        details["semantic_score"] = r.score
                        if r.score < threshold:
                            # 语义阈值前置过滤(与mem0一致)
                            rrf_score = 0.0
                            break
            else:
                details["semantic_rank"] = None
                details["semantic_score"] = 0.0

            # BM25 RRF分量
            if mem_id in bm25_rank:
                rrf_score += 1.0 / (self.RRF_K + bm25_rank[mem_id])
                details["bm25_rank"] = bm25_rank[mem_id]
            else:
                details["bm25_rank"] = None

            # 实体boost分量(加性,非RRF)
            details["entity_boost"] = entity_boosts.get(mem_id, 0.0)
            rrf_score += details["entity_boost"] * 0.1  # 缩放到RRF量级

            details["rrf_score"] = rrf_score

            # 获取payload
            payload = {}
            for r in semantic_results:
                if str(r.id) == mem_id:
                    payload = r.payload
                    break
            if not payload:
                for r in bm25_results:
                    if str(r.id) == mem_id:
                        payload = r.payload
                        break

            candidates.append(HybridResult(
                id=mem_id,
                content=payload.get("data", ""),
                score=rrf_score,
                score_details=details if explain else {},
                payload=payload,
            ))

        # 排序 + 截断
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:top_k]
```

#### 4.2.2 修改 `memory/vector_store.py` — 启用FTS5

```python
# memory/vector_store.py 新增方法

def fts_search(self, collection: str, query: str, top_k: int = 10, filters: dict = None) -> list:
    """FTS5全文检索 — 启用已有但未使用的FTS5索引

    mem0使用向量库的keyword_search(), xiaoda-agent用SQLite FTS5原生能力
    """
    # 中文分词后的查询(jieba分词 + OR连接)
    import jieba
    tokens = list(jieba.cut(query))
    fts_query = " OR ".join(f'"{t}"' for t in tokens if len(t) > 1)

    # 构建scope过滤SQL
    where_parts = []
    params = []
    if filters:
        for k, v in filters.items():
            where_parts.append(f"m.{k} = ?")
            params.append(v)

    scope_sql = f"AND {' AND '.join(where_parts)}" if where_parts else ""

    sql = f"""
        SELECT m.id, m.content, m.importance, m.created_at, rank
        FROM {collection}_fts f
        JOIN {collection} m ON m.id = f.rowid
        WHERE {collection}_fts MATCH ? {scope_sql}
        ORDER BY rank
        LIMIT ?
    """
    params.insert(0, fts_query)
    params.append(top_k)

    results = self._conn.execute(sql, params).fetchall()
    return [MemoryItem(id=r[0], content=r[1], score=-r[4], payload={...}) for r in results]
```

### 4.3 ADD-only记忆积累策略

**目标**: 从UPDATE覆盖式改为ADD积累式,保留记忆的时间线完整性。

**核心改动**: 修改`memory_distiller.py`和`fluid_memory.py`的提取/存储逻辑

#### 4.3.1 修改 `memory/memory_distiller.py`

```python
# memory/memory_distiller.py — 从UPDATE逻辑改为ADD-only

class MemoryDistiller:
    """记忆蒸馏器 — V2: ADD-only积累式

    旧逻辑: 提取事实 → 与已有记忆对比 → ADD/UPDATE/DELETE
    新逻辑: 提取事实 → Hash去重 → 全部ADD → linked_memory_ids关联
    对齐mem0: _add_to_vector_store() Phase 2-6
    """

    ADD_EXTRACTION_PROMPT = """你是一个记忆提取器。从对话中提取用户的事实、偏好、计划和情感状态。

核心规则:
1. 只做ADD操作 — 每条新信息都是一条独立记忆,不覆盖旧记忆
2. 保留时间信息 — "用户曾喜欢辣"和"用户最近在减肥"是两条独立记忆
3. 自包含 — 每条记忆应包含足够上下文,无需查看原始对话即可理解
4. 中文提取 — 用用户使用的语言记录事实
5. 不要泛化 — 保留具体细节(数字、名称、地点),不要用模糊词汇替代

输出格式(JSON):
{"memory": [{"id": "0", "text": "...", "attributed_to": "user", "linked_memory_ids": []}]}

如果无有价值的事实, 返回: {"memory": []}
"""

    def distill_incremental(self, new_messages: list[dict], existing_memories: list[dict], scope: dict) -> list[dict]:
        """增量蒸馏 — 对齐mem0 V3 Pipeline

        Args:
            new_messages: 新增消息 [{"role": "user", "content": "..."}, ...]
            existing_memories: 已有记忆 [{"id": "0", "text": "..."}, ...]
            scope: 记忆范围 {"user_id": "xxx"}

        Returns:
            新增记忆列表(已去重)
        """
        # Phase 1: 构建LLM提示
        existing_text = json.dumps(existing_memories[:10], ensure_ascii=False)  # 最多传10条防Token溢出
        messages_text = json.dumps(new_messages, ensure_ascii=False)

        user_prompt = f"""## 已有记忆
{existing_text}

## 新对话
{messages_text}

## 当前日期
{datetime.now().strftime("%Y-%m-%d")}

提取新的记忆事实,已有记忆中的信息不要重复提取。如果新对话中提及的实体与已有记忆相关,在linked_memory_ids中填写对应ID。"""

        # Phase 2: 单次LLM提取
        response = self.llm.generate_response(
            messages=[
                {"role": "system", "content": self.ADD_EXTRACTION_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )

        # Phase 3: 解析+Hash去重
        extracted = json.loads(response).get("memory", [])
        existing_hashes = {hashlib.md5(m["text"].encode()).hexdigest() for m in existing_memories}

        new_memories = []
        seen_hashes = set()
        for mem in extracted:
            text = mem.get("text", "")
            if not text:
                continue
            mem_hash = hashlib.md5(text.encode()).hexdigest()
            if mem_hash in existing_hashes or mem_hash in seen_hashes:
                continue  # Hash精确去重
            seen_hashes.add(mem_hash)
            new_memories.append(mem)

        return new_memories
```

#### 4.3.2 修改 `memory/fluid_memory.py` — 兼容ADD-only

```python
# memory/fluid_memory.py — 修改存储逻辑

class FluidMemory:
    """流体记忆 — V2: 兼容ADD-only策略

    旧逻辑: update_memory()直接覆盖content
    新逻辑: add_memory()总是新增, 通过linked_memory_ids关联相关记忆
    遗忘曲线仍然有效: score = similarity × e^(-λ×days) + min(α×ln(1+access_count), MAX_BOOST)
    """

    def add_memory(self, content: str, metadata: dict = None, linked_ids: list[str] = None) -> str:
        """添加新记忆(永远不覆盖)

        Args:
            content: 记忆内容
            metadata: 元数据(含scope, attributed_to等)
            linked_ids: 关联的已有记忆ID列表

        Returns:
            新记忆ID
        """
        memory_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        payload = {
            "data": content,
            "hash": hashlib.md5(content.encode()).hexdigest(),
            "created_at": now,
            "updated_at": now,
            "linked_memory_ids": linked_ids or [],
            **(metadata or {}),
        }

        # 向量嵌入
        embedding = self._embed_fn(content)
        self._vec_store.insert(
            collection="memories",
            vectors=[embedding],
            ids=[memory_id],
            payloads=[payload],
        )

        # 实体链接
        entities = self._entity_extractor.extract(content)
        for entity_type, entity_text in entities:
            self._entity_store.upsert_entity(
                entity_text=entity_text,
                entity_type=entity_type,
                memory_id=memory_id,
                scope={k: v for k, v in (metadata or {}).items() if k in ("user_id", "session_id", "agent_id")},
            )

        return memory_id

    # 废弃update_memory() — 保留方法签名但改为add
    def update_memory(self, memory_id: str, new_content: str):
        """已废弃: 改用add_memory + linked_memory_ids"""
        logger.warning("update_memory() is deprecated. Use add_memory() with linked_memory_ids instead.")
        self.add_memory(new_content, metadata={"supersedes": memory_id}, linked_ids=[memory_id])
```

### 4.4 User/Session/Agent三级记忆隔离

**目标**: 在SQLite表结构中增加scope字段,所有查询必须带scope过滤。

#### 4.4.1 数据库Schema变更

```sql
-- 新增scope相关字段
ALTER TABLE memories ADD COLUMN user_id TEXT;
ALTER TABLE memories ADD COLUMN session_id TEXT;  -- 对齐mem0 run_id
ALTER TABLE memories ADD COLUMN agent_id TEXT;

-- scope联合索引(高频查询)
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(user_id, session_id, agent_id);

-- FTS5也加入scope过滤
-- (需要重建FTS5索引, 在迁移脚本中处理)
```

#### 4.4.2 修改 `memory/memory_manager.py`

```python
# memory/memory_manager.py — 增加scope参数

class MemoryManager:
    """记忆管理器 — V2: 三级scope隔离

    对齐mem0: _build_filters_and_metadata() 强制scope
    """

    def add(self, content: str, *, user_id: str = None, session_id: str = None,
            agent_id: str = None, metadata: dict = None) -> dict:
        """添加记忆 — 强制scope参数"""
        # 至少一个scope必须提供(与mem0一致)
        if not any([user_id, session_id, agent_id]):
            raise ValueError("At least one of user_id, session_id, or agent_id must be provided")

        scope = {}
        if user_id: scope["user_id"] = user_id
        if session_id: scope["session_id"] = session_id
        if agent_id: scope["agent_id"] = agent_id

        # 调用ADD-only蒸馏
        new_memories = self.distiller.distill_incremental(
            new_messages=[{"role": "user", "content": content}],
            existing_memories=self._get_existing_memories(scope),
            scope=scope,
        )

        results = []
        for mem in new_memories:
            mem_id = self.fluid_memory.add_memory(
                content=mem["text"],
                metadata={**scope, "attributed_to": mem.get("attributed_to", "user")},
                linked_ids=mem.get("linked_memory_ids", []),
            )
            results.append({"id": mem_id, "memory": mem["text"], "event": "ADD"})

        return {"results": results}

    def search(self, query: str, *, user_id: str = None, session_id: str = None,
               agent_id: str = None, top_k: int = 10) -> list[dict]:
        """搜索记忆 — 强制scope参数"""
        if not any([user_id, session_id, agent_id]):
            raise ValueError("At least one scope parameter is required")

        scope = {}
        if user_id: scope["user_id"] = user_id
        if session_id: scope["session_id"] = session_id
        if agent_id: scope["agent_id"] = agent_id

        # 使用HybridRetriever三路检索
        return self.hybrid_retriever.search(query=query, top_k=top_k, scope=scope)
```

### 4.5 时间感知检索增强

**目标**: 将时间词解析结果融入检索排序,支持"上次"/"昨天"/"上周"等自然语言时间查询。

#### 4.5.1 修改 `memory/memory_manager.py` — 时间词融入检索

```python
# memory/memory_manager.py — 时间感知增强

class MemoryManager:
    # ... (前面的代码)

    def search_with_time(self, query: str, *, user_id: str = None, session_id: str = None,
                         agent_id: str = None, top_k: int = 10) -> list[dict]:
        """时间感知搜索

        对齐mem0: ADDITIVE_EXTRACTION_PROMPT中的Observation Date注入
        增强: 解析中文时间词→日期范围→检索时加权

        Pipeline:
        1. 解析查询中的时间词("昨天"→2026-07-10)
        2. 如果有时间词,对候选记忆做时间加权: 越接近目标日期的权重越高
        3. 与RRF融合分数相乘(而非替代)
        """
        scope = self._build_scope(user_id, session_id, agent_id)

        # Step 1: 解析时间词
        time_context = self.parse_time_words(query)
        # time_context = {"target_date": "2026-07-10", "range_days": 1, "time_word": "昨天"}

        # Step 2: 常规三路检索
        results = self.hybrid_retriever.search(query=query, top_k=top_k * 2, scope=scope)

        # Step 3: 时间加权(如果有时间词)
        if time_context and time_context.get("target_date"):
            target_date = datetime.fromisoformat(time_context["target_date"])
            range_days = time_context.get("range_days", 7)

            for r in results:
                created_at = r.payload.get("created_at")
                if created_at:
                    mem_date = datetime.fromisoformat(created_at)
                    days_diff = abs((mem_date - target_date).days)
                    # 高斯加权: 距离目标日期越近, 权重越高
                    time_weight = math.exp(-0.5 * (days_diff / range_days) ** 2)
                    r.score *= (1.0 + 0.3 * time_weight)  # 最多提升30%
                    if r.score_details is not None:
                        r.score_details["time_weight"] = time_weight
                        r.score_details["target_date"] = time_context["target_date"]

            results.sort(key=lambda x: x.score, reverse=True)

        return results[:top_k]

    def parse_time_words(self, text: str) -> dict:
        """解析中文时间词 → 精确日期

        对齐mem0: prompt中注入Current Date + Observation Date
        xiaoda-agent增强: 直接解析为日期范围, 融入检索排序
        """
        from datetime import timedelta

        today = datetime.now().date()
        time_words = {
            "今天": (today, 0),
            "昨天": (today - timedelta(days=1), 1),
            "前天": (today - timedelta(days=2), 2),
            "大前天": (today - timedelta(days=3), 3),
            "上周": (today - timedelta(weeks=1), 7),
            "上个月": (today - timedelta(days=30), 30),
            "去年": (today - timedelta(days=365), 365),
            "刚才": (today, 0),
            "早上": (today, 0),
            "晚上": (today, 0),
            "前阵子": (today - timedelta(days=14), 14),
            "最近": (today - timedelta(days=7), 7),
        }

        for word, (target, range_days) in time_words.items():
            if word in text:
                return {
                    "target_date": target.isoformat(),
                    "range_days": range_days,
                    "time_word": word,
                }

        return {}
```

---

## 5. 实施步骤

### Phase 1: 实体链接 + 多信号检索 (5天, P0最高优先)

> **依赖**: 无外部依赖, 可独立开发
> **验收标准**: 包含实体词的查询召回率从65%提升至85%

| 天 | 任务 | 产出 | 验收 |
|----|------|------|------|
| D1 | 新建`entity_extractor.py` — 中文4类实体提取 | ChineseEntityExtractor类, 单元测试通过 | "我和妈妈去西湖" → [(PERSON,"妈妈"),(LOCATION,"西湖")] |
| D2 | 新建`entity_store.py` — 独立entity_vec集合 | EntityStore类, upsert/boost/remove方法 | 实体→记忆双向链接可查询 |
| D3 | 新建`hybrid_retriever.py` — 三路RRF融合 | HybridRetriever.search() | 三路信号融合, RRF排序 |
| D4 | 启用FTS5检索 — vector_store.py新增fts_search() | fts_search()方法 | FTS5查询返回BM25排序结果 |
| D5 | 集成测试 — 端到端验证 | 10条测试对话 → 实体链接 + 三路检索 | P95延迟<200ms, 召回率>85% |

### Phase 2: ADD-only记忆策略 (3天, P0)

> **依赖**: Phase 1 (entity_store需要在add时同步写入)
> **验收标准**: 记忆不被覆盖, 时间线完整性100%

| 天 | 任务 | 产出 | 验收 |
|----|------|------|------|
| D1 | 修改`memory_distiller.py` — ADD-only Prompt + Hash去重 | distill_incremental() | 提取结果只有ADD事件, 无UPDATE/DELETE |
| D2 | 修改`fluid_memory.py` — add_memory()替代update_memory() | add_memory() + linked_memory_ids | 新旧偏好共存, 不覆盖 |
| D3 | 迁移现有记忆 — 旧UPDATE记忆拆分为多条ADD | 迁移脚本 + 回归测试 | 旧记忆数据无损迁移 |

### Phase 3: 三级记忆隔离 + 时间感知 (3天, P1)

> **依赖**: Phase 1 (scope参数需在所有查询中传递)
> **验收标准**: User/Session/Agent记忆严格隔离, 时间词查询加权生效

| 天 | 任务 | 产出 | 验收 |
|----|------|------|------|
| D1 | Schema变更 + scope字段 + 索引 | ALTER TABLE + CREATE INDEX | scope过滤走索引 |
| D2 | 修改`memory_manager.py` — 强制scope参数 | add()/search()增加scope | 无scope报错, 有scope正确过滤 |
| D3 | 时间感知检索增强 — parse_time_words融入搜索 | search_with_time() | "昨天"查询正确加权 |

### Phase 4: 性能优化 + 回归测试 (2天, P1)

> **依赖**: Phase 1-3
> **验收标准**: P95延迟<200ms, 批量嵌入减少50% Token消耗

| 天 | 任务 | 产出 | 验收 |
|----|------|------|------|
| D1 | 批量嵌入优化 — embed_batch减少API调用 | embed_batch() | 10条记忆1次API调用(vs 10次) |
| D2 | 全量回归测试 + 性能基准 | 测试报告 | P95<200ms, 召回率>90% |

---

## 6. 预期效果

### 6.1 量化预期

| 指标 | 当前(v0.5.03) | Phase 1后 | Phase 4后 | 提升幅度 |
|------|---------------|-----------|-----------|----------|
| **检索准确率** | ~65% | ~85% | ~90%+ | +25% |
| **实体相关查询召回率** | ~40% | ~85% | ~90% | +50% |
| **P95检索延迟** | ~150ms | ~180ms | ~150ms | 保持 |
| **记忆写入延迟** | ~200ms | ~250ms | ~200ms | 保持 |
| **Token消耗(add)** | ~2000/条 | ~1500/条 | ~800/条 | -60% |
| **重复记忆率** | ~15% | ~3% | ~1% | -14% |
| **时间相关查询准确率** | ~30% | ~50% | ~80% | +50% |

### 6.2 质量预期

| 场景 | 当前行为 | 优化后行为 |
|------|----------|------------|
| 用户问"上次和妈妈的旅行" | 返回"喜欢吃辣"(语义最相似) | 返回"和妈妈去了西湖"(实体"妈妈"精确召回) |
| 用户说"最近在减肥"后问"我吃什么" | 返回"喜欢吃辣"(被UPDATE覆盖了) | 同时返回"曾喜欢辣"和"最近在减肥"(两条独立记忆) |
| 用户问"昨天说了什么" | 返回所有语义相似记忆(无时间过滤) | 返回昨天创建的记忆(时间加权) |
| Session A的记忆出现在Session B | 会(无隔离) | 不会(scope过滤) |

### 6.3 延迟影响分析

- **实体链接**: 新增~30ms/条(提取+嵌入+搜索), 但只在add时执行,不影响检索
- **三路检索**: 并行执行时~150ms(语义~50ms + FTS5~10ms + 实体~30ms + RRF~5ms), 与当前单路检索持平
- **ADD-only**: 减少LLM调用次数(1次vs 2次), 写入延迟实际下降

---

## 7. 不采纳的部分及原因

### 7.1 不采纳: 托管服务(mem0 Platform)

**mem0设计**: 云端API服务, 记忆存储在mem0服务器, 支持多租户/团队协作/自动扩展
**不采纳原因**: xiaoda-agent是本地部署的情感陪伴AI(类似Hermes), 所有数据必须在本地SQLite, 不能将用户情感数据上传第三方服务器

### 7.2 不采纳: Chroma/Qdrant/Postgres等外部向量库

**mem0设计**: 支持20+向量库后端(Chroma/Qdrant/Postgres/pgvector/Milvus/Pinecone等)
**不采纳原因**: 
1. xiaoda-agent本地部署, SQLite + sqlite-vec是最佳选择(零依赖, 单文件, 嵌入式)
2. 引入Chroma/Qdrant等需要额外进程/容器, 违反本地部署轻量原则
3. sqlite-vec性能足以支撑(10万级记忆, <50ms检索)

### 7.3 不采纳: spaCy英文NLP

**mem0设计**: 使用spaCy做实体提取(`entity_extraction.py`)和词形还原(`lemmatization.py`)
**不采纳原因**: 
1. spaCy中文模型(`zh_core_web_sm`)远不如jieba准确
2. spaCy模型体积大(~50MB), 加载慢(~2s)
3. jieba对中文分词/词性标注更成熟, 配合自定义规则可达到更好效果
4. 替代方案: jieba词性标注 + 规则增强(见4.1.2节)

### 7.4 不采纳: mem0的UPDATE/DELETE旧版逻辑

**mem0设计**: V3之前使用ADD/UPDATE/DELETE三操作模式(`DEFAULT_UPDATE_MEMORY_PROMPT`)
**不采纳原因**: 
1. mem0 V3已自己放弃了UPDATE/DELETE, 改为ADD-only
2. 情感陪伴场景更需要时间线完整性, UPDATE会丢失"用户曾经喜欢X"的信息
3. xiaoda-agent的Ebbinghaus遗忘曲线已天然处理了"旧记忆逐渐淡出"

### 7.5 不采纳: mem0的CLI/REST API层

**mem0设计**: 完整的CLI工具(`mem0_cli`)和REST API服务
**不采纳原因**: xiaoda-agent是Python库, 通过`MemoryManager`类直接调用, 不需要独立进程的API服务

### 7.6 不采纳: Procedural Memory

**mem0设计**: 从Agent执行历史中提取过程性记忆(如"如何完成某任务")
**不采纳原因**: 
1. 情感陪伴场景无需记录"如何执行任务"
2. xiaoda-agent已有`knowledge_graph.py`记录工具使用模式
3. 过程性记忆的LLM Prompt极长(~5K Token), 性价比低

### 7.7 部分采纳: BM25归一化

**mem0设计**: 自适应sigmoid归一化(`normalize_bm25`), 根据查询长度调整sigmoid参数
**调整方案**: RRF不需要分数归一化(按排名融合), 但保留查询长度自适应的思想——短查询给BM25更高权重, 长查询给语义更高权重。在`HybridRetriever`中通过权重参数实现:

```python
# 查询长度自适应权重
num_terms = len(jieba.cut(query))
if num_terms <= 3:
    bm25_weight, semantic_weight = 0.6, 0.4  # 短查询: BM25更重要
elif num_terms <= 6:
    bm25_weight, semantic_weight = 0.5, 0.5
else:
    bm25_weight, semantic_weight = 0.3, 0.7  # 长查询: 语义更重要
```

### 7.8 部分采纳: V3批量Pipeline

**mem0设计**: Phase 0-7串行Pipeline, 全流程批处理
**调整方案**: 保留批量嵌入(embed_batch)和Hash去重的核心思想, 但Pipeline简化为:
- Phase 1: 实体提取 + 嵌入(复用jieba, 不需要spaCy)
- Phase 2: LLM提取(单次调用, ADD-only)
- Phase 3: Hash去重 + 批量写入
- Phase 4: 实体链接(批量upsert)

省略了mem0的Phase 0(last_messages)和UUID映射(不需要, 因为xiaoda-agent不传UUID给LLM)

---

## 附录A: xiaoda-agent独有优势(不应对齐mem0的部分)

| 特性 | xiaoda-agent | mem0 | 结论 |
|------|-------------|------|------|
| Stanislavski情感记忆 | ✅ Anchoring/Recalling/Bounding/Enacting | ❌ | 保持 |
| Ebbinghaus遗忘曲线 | ✅ FluidMemory自适应衰减 | ❌(托管版有decay) | 保持 |
| PAD情绪模型 | ✅ Pleasure-Arousal-Dominance | ❌ | 保持 |
| 梦境整合 | ✅ 夜周期记忆重评估+合并 | ❌ | 保持 |
| 元认知 | ✅ confidence/fatigue/error_rate | ❌ | 保持 |
| 记忆蒸馏 | ✅ 对话后压缩提炼 | 部分(只提取) | 保持+增强 |

## 附录B: 关键文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `memory/entity_store.py` | **新建** | 实体存储, 独立entity_vec集合 |
| `memory/entity_extractor.py` | **新建** | 中文实体提取, 替代jieba关键词 |
| `memory/hybrid_retriever.py` | **新建** | 三路RRF融合检索器 |
| `memory/memory_distiller.py` | **修改** | ADD-only逻辑 + Hash去重 |
| `memory/fluid_memory.py` | **修改** | add_memory()替代update_memory() |
| `memory/memory_manager.py` | **修改** | scope参数 + 时间感知检索 |
| `memory/vector_store.py` | **修改** | 新增fts_search() + scope过滤 |
| `data/migrations/v0.5.04_add_scope.sql` | **新建** | Schema变更脚本 |

## 附录C: 依赖关系图

```
Phase 1 (实体链接+多信号检索) ← 无依赖, 可立即开始
    ↓
Phase 2 (ADD-only策略) ← 依赖Phase 1的entity_store
    ↓
Phase 3 (三级隔离+时间感知) ← 依赖Phase 1的scope参数
    ↓
Phase 4 (性能优化+回归测试) ← 依赖Phase 1-3全部完成
```

---

> 📌 **本Spec共5项核心优化**, 对齐mem0 6大机制, 保留xiaoda-agent情感陪伴特色:
> - 🔗 **实体链接**: 中文4类实体 + 独立entity_vec + 双向链接 + 检索boost
> - 🔍 **多信号检索**: 语义+FTS5+实体三路RRF融合 + 查询长度自适应权重
> - 📝 **ADD-only策略**: 积累式记忆 + Hash去重 + linked_memory_ids关联
> - 🏷️ **三级隔离**: User/Session/Agent scope + 强制scope参数 + 索引优化
> - ⏰ **时间感知**: 中文时间词解析 + 高斯时间加权 + 与RRF融合
>
> 预期: 检索准确率 65% → 90%+, Token消耗 -60%, 重复记忆率 15% → 1%
