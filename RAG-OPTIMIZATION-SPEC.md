# Nahida Agent RAG 优化 Spec

> 基于 SiliconFlow BAAI/bge-reranker-v2-m3 的全链路检索增强方案  
> 版本: v1.1 | 日期: 2026-06-17 | 作者: 纳西妲 🌿

---

## 一、现状诊断

### 1.1 当前 RAG 管线

```
用户查询
  ├→ FTS5 BM25 全文检索 (jieba 分词, limit=k*2)
  ├→ bge-m3 向量检索 (硅流 API, sqlite-vec, top_k=k*2)
  └→ RRF 双路融合 → 重要性+时效性加权 → top-k 结果注入上下文
```

**涉及核心文件**:
- `vector_store.py` — 向量存储（bge-m3 嵌入 + sqlite-vec）
- `memory_manager.py` — 混合检索 + RRF 融合
- `db_memory.py` — FTS5 检索 + 情景记忆管理
- `knowledge_graph.py` — 实体关系提取（对话摘要 → LLM → JSON）
- `context_compressor.py` — 上下文压缩（CCR 缓存机制）

### 1.2 已识别的 7 个瓶颈

| # | 瓶颈 | 严重程度 | 影响 |
|---|------|---------|------|
| B1 | **无重排序**：RRF 融合后直接取 top-k，无交叉编码器精排 | 🔴 高 | "伪相关"记忆被注入上下文，浪费 token 且误导 LLM |
| B2 | **单一嵌入模型**：仅 bge-m3 稠密向量，缺稀疏检索（BM25 只对 FTS 索引，不对向量索引） | 🟡 中 | 语义相似但词汇不匹配的文档召回差 |
| B3 | **粗粒度索引**：以整条 episodic_memory 为检索单元，未做分块 | 🟡 中 | 长摘要中只有部分相关时，整条被召回浪费上下文窗口 |
| B4 | **嵌入缓存过小**：EmbedCache 仅 128 条，LRU 淘汰频繁 | 🟢 低 | 重复查询/相似文本重复调 API，增加延迟和成本 |
| B5 | **知识图谱不参与排序**：KG 仅做"后置增强"（inject kg_context），不影响检索排序 | 🟡 中 | 用户说"我喜欢吃枣椰蜜糖"时，KG 知道偏好但检索排序没用到 |
| B6 | **无查询变换**：原始用户查询直接送检索，未做扩展/改写/分解 | 🟡 中 | 口语化/模糊查询召回率低（如"那个东西"） |
| B7 | **RRF 无分数校准**：FTS BM25 分数和向量 cosine distance 量纲不同，RRF 只看排名不看分数 | 🟢 低 | 一路质量显著优于另一路时融合效果退化 |

---

## 二、优化目标

| 指标 | 当前基线 | 目标值 | 衡量方式 |
|------|---------|-------|---------|
| 检索 Precision@5 | ~0.60（估算） | ≥ 0.80 | 人工标注 50 条查询，评估 top-5 相关率 |
| 检索 Recall@10 | ~0.70 | ≥ 0.85 | 同上 |
| 端到端延迟（检索+重排） | ~800ms | ≤ 1500ms | P95 监控 |
| API 成本增量 | 0 | ≤ ¥0/天 | SiliconFlow bge-reranker-v2-m3 免费 |
| 上下文窗口利用率 | ~40% 有效信息 | ≥ 70% | 相关记忆 token / 总注入 token |

---

## 三、优化方案设计

### Phase 1: 引入 Reranker（核心改动，优先级 P0）

#### 3.1.1 新增 `reranker.py` 模块

```python
# reranker.py — bge-reranker-v2-m3 交叉编码器重排序（SiliconFlow）
import asyncio
import time
import httpx
from loguru import logger


class Reranker:
    """基于 SiliconFlow BAAI/bge-reranker-v2-m3 的交叉编码器重排序
    
    特点:
    - 硅基流动免费常驻模型，非限时促销，稳定性有保障
    - 与项目现有 bge-m3 嵌入模型同系列，语义空间天然对齐
    - 使用 /v1/rerank 端点，Jina 兼容格式
    """

    SUPPORTED_MODELS = {
        "BAAI/bge-reranker-v2-m3": {
            "max_length": 8192,
            "provider": "siliconflow",
            "price": "免费",
        },
        "netease-youdao/bce-reranker-base_v1": {
            "max_length": 512,
            "provider": "siliconflow",
            "price": "免费",
        },
    }

    def __init__(self, api_key: str = "", base_url: str = "",
                 model: str = "BAAI/bge-reranker-v2-m3",
                 max_length: int = 512, batch_size: int = 8):
        self._api_key = api_key
        self._base_url = base_url or "https://api.siliconflow.cn/v1"
        self._model = model
        self._max_length = max_length
        self._batch_size = batch_size
        self._stats = {
            "total_queries": 0,
            "total_documents": 0,
            "avg_latency_ms": 0,
        }

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int = 5,
        return_documents: bool = True,
    ) -> list[dict]:
        """
        对 documents 相对 query 做相关性重排序。

        返回格式:
        [
            {
                "index": 原始索引,
                "relevance_score": float,
                "document": str (可选)
            },
            ...
        ]
        """
        if not self._api_key or not documents:
            return []

        start = time.time()
        self._stats["total_queries"] += 1
        self._stats["total_documents"] += len(documents)

        try:
            payload = {
                "model": self._model,
                "query": query,
                "documents": documents,
                "top_n": top_n,
                "return_documents": return_documents,
                "max_chunks_per_doc": 1,
            }

            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self._base_url}/rerank",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                data = response.json()

            results = []
            for item in data.get("results", []):
                result = {
                    "index": item.get("index", 0),
                    "relevance_score": item.get("relevance_score", 0.0),
                }
                if return_documents:
                    doc = item.get("document", {})
                    result["document"] = doc if isinstance(doc, dict) else {"text": str(doc)}
                results.append(result)

            latency = (time.time() - start) * 1000
            self._stats["avg_latency_ms"] = (
                self._stats["avg_latency_ms"] * 0.9 + latency * 0.1
            )

            logger.debug("reranker.done",
                         query_len=len(query),
                         docs=len(documents),
                         top_n=top_n,
                         latency_ms=f"{latency:.0f}")

            return results

        except Exception as e:
            logger.warning("reranker.failed", error=str(e))
            return [
                {
                    "index": i,
                    "relevance_score": 1.0 - i * 0.1,
                    "document": {"text": doc} if return_documents else None,
                }
                for i, doc in enumerate(documents[:top_n])
            ]
```

#### 3.1.2 修改 `memory_manager.py` — 在 RRF 之后插入 Reranker

**当前流程**:
```
FTS → RRF 融合 → 重要性加权 → top-k
VEC ↗
```

**优化后流程**:
```
FTS → RRF 融合 → top-k*3 (过采样) → Reranker 精排 → top-k
VEC ↗
```

**关键修改点**:

```python
# memory_manager.py — retrieve_memories_hybrid 新增 rerank 步骤

async def retrieve_memories_hybrid(self, query: str, k: int = 5) -> list[dict]:
    """FTS + 向量 RRF 混合检索 + Reranker 精排"""
    
    # ... 现有 FTS + 向量检索 + RRF 融合逻辑不变 ...
    
    # ★ 新增：过采样 3x 供 Reranker 筛选
    oversample_k = k * 3
    fused = reciprocal_rank_fusion([fts_ids, vec_ids], limit=oversample_k)
    
    # ★ 新增：Reranker 精排
    if self._reranker and self._reranker.available and len(fused) > k:
        docs = []
        idx_map = {}
        for i, (item_id, rrf_score) in enumerate(fused):
            if item_id in all_items:
                docs.append(all_items[item_id].get("summary", ""))
                idx_map[i] = item_id
        
        if docs:
            reranked = await self._reranker.rerank(
                query=query,
                documents=docs,
                top_n=k,
            )
            results = []
            for item in reranked:
                orig_idx = item["index"]
                item_id = idx_map.get(orig_idx)
                if item_id and item_id in all_items:
                    mem = all_items[item_id]
                    mem["rerank_score"] = item["relevance_score"]
                    mem["rrf_score"] = dict(fused).get(item_id, 0)
                    results.append(mem)
            return results[:k]
    
    # 降级：无 Reranker 时走原 RRF 逻辑
    # ... 现有逻辑 ...
```

#### 3.1.3 配置项

在 `config.py` 中新增:

```python
# Reranker 配置（SiliconFlow 免费常驻模型）
RERANKER_API_KEY = os.getenv("RERANKER_API_KEY", "")  # 留空则复用 SILICONFLOW_API_KEY
RERANKER_BASE_URL = os.getenv("RERANKER_BASE_URL", "https://api.siliconflow.cn/v1")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "true").lower() in ("1", "true", "yes")
RERANKER_OVERSAMPLE_RATIO = int(os.getenv("RERANKER_OVERSAMPLE_RATIO", "3"))
RERANKER_TOP_N = int(os.getenv("RERANKER_TOP_N", "5"))
```

在 `.env.example` 中新增:
```env
# Reranker (SiliconFlow 免费常驻)
RERANKER_API_KEY=          # 留空则复用 SILICONFLOW_API_KEY
RERANKER_BASE_URL=https://api.siliconflow.cn/v1
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
RERANKER_ENABLED=true
```

---

### Phase 2: 查询变换（优先级 P1）

#### 3.2.1 新增 `query_transform.py`

```python
# query_transform.py — 查询改写与扩展

class QueryTransformer:
    """查询变换器：改写/扩展/分解用户原始查询"""

    def __init__(self, router=None):
        self._router = router

    async def rewrite_query(self, original_query: str, context: str = "") -> str:
        """将口语化查询改写为更适合检索的形式"""
        if not self._router:
            return original_query

        prompt = f"""将以下用户查询改写为更适合文档检索的关键词查询。
保持语义不变，去除口语化表达，补充必要的上下文信息。
只输出改写后的查询，不要解释。

原始查询: {original_query}
对话上下文: {context[-200:] if context else '无'}

改写后的查询:"""

        result = await self._router.route(
            "chat_flash",
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=100,
        )
        return result.strip() if result else original_query

    async def expand_query(self, query: str, n: int = 3) -> list[str]:
        """生成 n 个不同视角的查询扩展"""
        if not self._router:
            return [query]

        prompt = f"""为以下查询生成 {n} 个不同视角的搜索查询，用于提高检索召回率。
每行一个查询，不要编号，不要解释。

原始查询: {query}"""

        result = await self._router.route(
            "chat_flash",
            [{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=150,
        )
        if result:
            expanded = [line.strip() for line in result.strip().split("\n") if line.strip()]
            return [query] + expanded[:n]
        return [query]
```

#### 3.2.2 集成到检索流程

```
用户查询
  ├→ QueryTransformer.rewrite → 改写查询（用于 FTS+向量）
  ├→ QueryTransformer.expand → 多查询扩展（每路独立检索）
  └→ 多路结果合并 → Reranker 精排 → top-k
```

---

### Phase 3: 知识图谱增强排序（优先级 P2）

#### 3.3.1 KG-Rerank 联合评分

```python
# knowledge_graph.py — 新增方法

async def get_relevance_boost(self, query: str, memory_summaries: list[str]) -> list[float]:
    """基于知识图谱的检索增强评分"""
    boosts = []
    
    query_entities = set()
    entities = await self.extract_from_summary(query)
    for ent in entities.get("entities", []):
        query_entities.add(ent.get("name", ""))
    
    for summary in memory_summaries:
        boost = 0.0
        summary_entities = set()
        entities = await self.extract_from_summary(summary)
        for ent in entities.get("entities", []):
            summary_entities.add(ent.get("name", ""))
        
        overlap = query_entities & summary_entities
        if overlap:
            boost += len(overlap) * 0.15
        
        for qe in list(query_entities)[:3]:
            for se in list(summary_entities)[:3]:
                relations = await self.knowledge_db.get_knowledge_relations(qe)
                for rel in relations[:5]:
                    if rel.get("to_entity") == se or rel.get("from_entity") == se:
                        boost += 0.05
                        break
        
        boosts.append(min(boost, 0.5))
    
    return boosts
```

#### 3.3.2 融合评分公式

```
final_score = α × rerank_score + β × kg_boost + γ × (importance × time_decay)

推荐权重: α=0.65, β=0.15, γ=0.20
```

---

### Phase 4: 分块索引（优先级 P3，长期）

#### 3.4.1 分块策略

新增 `memory_chunks` 表，对长摘要做语义分块：

```sql
CREATE TABLE IF NOT EXISTS memory_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    token_count INTEGER DEFAULT 0,
    created_at REAL DEFAULT 0,
    FOREIGN KEY (memory_id) REFERENCES episodic_memories(id)
);
```

**分块规则**:
- 短摘要（< 200 字）: 不分块，整条索引
- 中等摘要（200-500 字）: 按句子边界切 2-3 块
- 长摘要（> 500 字）: 按语义段落切，每块 150-300 字，重叠 50 字

#### 3.4.2 检索时合并

```
chunk 级检索 → top-k chunks → 合并到所属 memory → 去重 → Reranker
```

---

### Phase 5: 嵌入模型升级 + 缓存优化（优先级 P3）

#### 3.5.1 稠密+稀疏混合嵌入

**当前**: 仅 bge-m3 稠密向量（1024 维）  
**优化**: bge-m3 原生支持稠密+稀疏（ColBERT）双输出

```python
async def embed_dense_sparse(self, text: str) -> dict:
    """同时获取稠密和稀疏嵌入"""
    response = await self._embed_client.embeddings.create(
        model=self._embed_model,
        input=text,
        extra_body={"return_sparse": True},
    )
    dense_vec = response.data[0].embedding
    sparse_vec = getattr(response.data[0], "sparse_embedding", None)
    return {"dense": dense_vec, "sparse": sparse_vec}
```

#### 3.5.2 嵌入缓存扩容

```python
self._cache = EmbedCache(max_size=2048)  # 原来 128
```

增加磁盘持久化，进程重启后缓存不丢失。

---

## 四、配置文件变更汇总

### `.env.example` 新增

```env
# ── Reranker (SiliconFlow 免费常驻) ──
RERANKER_API_KEY=
RERANKER_BASE_URL=https://api.siliconflow.cn/v1
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
RERANKER_ENABLED=true
RERANKER_OVERSAMPLE_RATIO=3

# ── Query Transform ──
QUERY_TRANSFORM_ENABLED=true
QUERY_EXPAND_COUNT=2

# ── RAG Fusion Weights ──
RAG_RERANK_WEIGHT=0.65
RAG_KG_WEIGHT=0.15
RAG_IMPORTANCE_WEIGHT=0.20
```

### `config.py` 新增

```python
# Reranker（SiliconFlow 免费常驻）
RERANKER_API_KEY = os.getenv("RERANKER_API_KEY", "")
RERANKER_BASE_URL = os.getenv("RERANKER_BASE_URL", "https://api.siliconflow.cn/v1")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "true").lower() in ("1", "true", "yes")
RERANKER_OVERSAMPLE_RATIO = int(os.getenv("RERANKER_OVERSAMPLE_RATIO", "3"))

# Query Transform
QUERY_TRANSFORM_ENABLED = os.getenv("QUERY_TRANSFORM_ENABLED", "true").lower() in ("1", "true", "yes")
QUERY_EXPAND_COUNT = int(os.getenv("QUERY_EXPAND_COUNT", "2"))

# RAG Fusion Weights
RAG_RERANK_WEIGHT = float(os.getenv("RAG_RERANK_WEIGHT", "0.65"))
RAG_KG_WEIGHT = float(os.getenv("RAG_KG_WEIGHT", "0.15"))
RAG_IMPORTANCE_WEIGHT = float(os.getenv("RAG_IMPORTANCE_WEIGHT", "0.20"))
```

---

## 五、数据流总览（优化后）

```
用户消息: "上次我们讨论的那个PLC编程的方案怎么样了"
  │
  ├─ 1. QueryTransformer
  │     ├→ rewrite: "PLC编程方案讨论结果"  (去口语化)
  │     └→ expand: ["PLC编程助手项目进展", "PLC AI编程方案"]
  │
  ├─ 2. 多路并行检索
  │     ├→ FTS5 BM25 (3 查询 × k*2)
  │     ├→ bge-m3 向量 (3 查询 × top_k*2)
  │     └→ 合并去重
  │
  ├─ 3. RRF 双路融合
  │     └→ 过采样 top-k*3 = 15 条候选
  │
  ├─ 4. ★ Reranker 精排 (SiliconFlow BAAI/bge-reranker-v2-m3)
  │     ├→ 15 条候选 × 原始查询 → 交叉编码器打分
  │     └→ 取 relevance_score top-5
  │
  ├─ 5. ★ KG 增强
  │     ├→ 查询实体: ["PLC", "编程方案"]
  │     ├→ 记忆实体: 重叠检测 + 关系路径
  │     └→ boost_score 叠加
  │
  ├─ 6. 综合评分
  │     final = 0.65×rerank + 0.15×kg_boost + 0.20×(importance×decay)
  │     → top-5 注入上下文
  │
  └─ 7. 注入 system prompt
        "[相关记忆] ..."
```

---

## 六、实施路线图

| Phase | 改动 | 预估工作量 | 风险 | 依赖 |
|-------|------|----------|------|------|
| **P0** Reranker | 新增 `reranker.py`，修改 `memory_manager.py` | 1 天 | 低 | SiliconFlow rerank 端点免费常驻 |
| **P1** 查询变换 | 新增 `query_transform.py`，修改 `memory_manager.py` | 0.5 天 | 低 | MiMo chat_flash 路由可用 |
| **P2** KG 增强 | 修改 `knowledge_graph.py` + `memory_manager.py` | 1 天 | 中 | KG 实体量需 > 50 条才有效果 |
| **P3** 分块索引 | 新增 `memory_chunks` 表，修改 `vector_store.py` + `memory_manager.py` | 2 天 | 中 | 需要数据迁移，影响写入链路 |
| **P3** 嵌入升级 | 修改 `vector_store.py`，扩容缓存 | 0.5 天 | 低 | 硅流 API sparse embedding 支持度 |

**推荐顺序**: P0 → P1 → P2 → P3（分块）→ P3（嵌入）

---

## 七、SiliconFlow Rerank 端点验证清单

- [x] SiliconFlow 提供 `/v1/rerank` 端点 ✅（Jina 兼容格式）
- [x] `BAAI/bge-reranker-v2-m3` 模型免费常驻 ✅（非限时促销）
- [x] 请求格式：Jina 兼容（model, query, documents, top_n, return_documents）
- [ ] 实测 Rerank API 调用是否正常（需用硅流 API Key 测试）
- [ ] QPS 限制确认
- [ ] 降级方案（Reranker 不可用时回退到 RRF 原始排序，代码已内置）

---

## 八、监控埋点

```python
metrics.inc("reranker.queries")
metrics.observe("reranker.latency_ms", t)
metrics.observe("reranker.doc_count", n)
metrics.inc("query_transform.rewrites")
metrics.inc("query_transform.expands")
metrics.observe("rag.total_latency_ms", t)
metrics.gauge("rag.cache_hit_rate", r)
```

---

## 九、测试方案

### 9.1 单元测试

```python
async def test_reranker_basic():
    reranker = Reranker(api_key="sk-xxx", model="BAAI/bge-reranker-v2-m3")
    results = await reranker.rerank(
        query="PLC编程方案",
        documents=[
            "今天天气不错",
            "我们讨论了AI智能PLC编程助手的技术方案",
            "昨天吃了枣椰蜜糖",
            "PLC编程项目需要3人团队",
            "线性代数期末考试复习",
        ],
        top_n=3,
    )
    assert results[0]["index"] in [1, 3]

async def test_reranker_fallback():
    reranker = Reranker(api_key="invalid", model="BAAI/bge-reranker-v2-m3")
    results = await reranker.rerank(
        query="test",
        documents=["doc1", "doc2"],
        top_n=2,
    )
    assert len(results) == 2
```

### 9.2 端到端测试

```python
RAG_TEST_CASES = [
    {
        "query": "我之前说喜欢什么来着",
        "expected_in_top3": ["枣椰蜜糖"],
    },
    {
        "query": "PLC项目进展",
        "expected_in_top3": ["PLC", "编程", "AI"],
    },
    {
        "query": "那个编程的东西",
        "expected_in_top3": ["PLC", "编程"],
    },
]
```

---

## 十、学术参考

1. **RAG-Fusion** (IJNLC 2024): 多查询生成 + RRF 融合 + Reranker  
   DOI: [10.5121/ijnlc.2024.13103](https://doi.org/10.5121/ijnlc.2024.13103)

2. **Cross-Encoder Reranking 对比研究** (Zenodo 2026): Reranking 以 0.827 综合分第一  
   DOI: [10.5281/zenodo.19774692](https://doi.org/10.5281/zenodo.19774692)

3. **BGE Reranker 系列** (BAAI 2024): bge-reranker-v2-m3 MTEB/CMTEB 持续领先

4. **Reciprocal Rank Fusion** (SIGIR 2009): RRF 原始论文  
   DOI: [10.1145/1571941.1572114](https://doi.org/10.1145/1571941.1572114)
