"""Tool Search — BM25 + Vector 混合检索按需加载工具定义

灵感: Anthropic Claude Tool Search (85% token 节省)
原理: 只向 LLM 暴露一个 search_tools 元工具,
LLM 按需搜索并加载完整工具定义, 避免上下文膨胀。

实现 (v2 混合检索, 2026-07 升级):
1. 工具注册时标记 defer_loading
2. BM25 检索 (词法匹配, 精确关键词强项)
3. Vector 检索 (语义匹配, 同义词/近义概念强项, 可选)
4. Reciprocal Rank Fusion (RRF, k=60) 融合两路排序
5. 只返回 top-k 工具的完整定义

论文支撑:
- Anthropic Claude Tool Search: 85% token 节省
- BM25 (Robertson 1994): 经典概率检索模型
- AnyTool (arXiv:2402.04253): 大规模 API 分层检索
- RRF (Cormack 2009): 多路排序融合, 无需分数标定

设计原则:
- 向后兼容: 无 embed_client 时降级到纯 BM25 (v1 行为)
- 优雅降级: embed 失败不影响 BM25 检索
- 懒初始化: VectorIndex 首次 search 时才初始化 embed_client
"""
import math
import re
import asyncio
import threading
from collections import Counter
from dataclasses import dataclass, field
from loguru import logger
from typing import Optional, Any


@dataclass
class ToolDef:
    """工具定义"""
    name: str
    description: str
    parameters: dict
    handler_path: str = ""        # 模块路径, 懒加载
    defer_loading: bool = True    # 默认延迟加载
    keywords: list[str] = field(default_factory=list)
    use_count: int = 0            # 热度统计
    last_used: float = 0          # 最近使用时间

    @property
    def token_estimate(self) -> int:
        """估算工具定义的 token 数"""
        import json
        text = json.dumps({"name": self.name, "description": self.description, "parameters": self.parameters})
        return len(text) // 4  # 粗略估算

    def searchable_text(self) -> str:
        """用于向量化的文本 (name + description + keywords)"""
        return f"{self.name} {self.description} {' '.join(self.keywords)}"


class BM25Index:
    """BM25 倒排索引 (词法匹配, 精确关键词强项)"""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._docs: list[list[str]] = []
        self._df: Counter = Counter()
        self._avg_len: float = 0
        self._tool_defs: list[ToolDef] = []

    def add_tool(self, tool: ToolDef) -> None:
        """添加工具到索引"""
        doc = self._tokenize(tool.searchable_text())
        self._docs.append(doc)
        self._tool_defs.append(tool)
        for term in set(doc):
            self._df[term] += 1
        self._avg_len = sum(len(d) for d in self._docs) / max(1, len(self._docs))

    def search(self, query: str, top_k: int = 5) -> list[tuple[ToolDef, float]]:
        """BM25 搜索, 返回 (tool, score) 列表 (按分数降序)"""
        if not self._docs:
            return []
        q_terms = self._tokenize(query)
        scores = []
        for i, doc in enumerate(self._docs):
            score = self._bm25_score(q_terms, doc, i)
            scores.append((score, i))
        scores.sort(reverse=True)
        return [(self._tool_defs[idx], score) for score, idx in scores[:top_k]]

    def ranked_names(self, query: str, top_k: int = 10) -> list[str]:
        """返回按 BM25 分数排序的工具名列表 (供 RRF 融合用)"""
        return [t.name for t, _ in self.search(query, top_k=top_k)]

    def _bm25_score(self, q_terms: list[str], doc: list[str], doc_idx: int) -> float:
        """计算 BM25 分数"""
        doc_len = len(doc)
        doc_counter = Counter(doc)
        score = 0.0
        N = len(self._docs)
        for term in q_terms:
            if term not in doc_counter:
                continue
            tf = doc_counter[term]
            df = self._df.get(term, 0)
            idf = math.log((N - df + 0.5) / (df + 0.5) + 1)
            norm = tf * (self._k1 + 1) / (tf + self._k1 * (1 - self._b + self._b * doc_len / max(1, self._avg_len)))
            score += idf * norm
        return score

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """简单分词 (英文按词, 中文按字)"""
        # 英文: [a-zA-Z_]\w*
        # 中文: 单字 (BM25 对中文用字粒度比词粒度更稳)
        en_tokens = re.findall(r'[a-zA-Z_]\w*', text.lower())
        zh_tokens = re.findall(r'[\u4e00-\u9fff]', text)
        return en_tokens + zh_tokens


class VectorIndex:
    """向量检索索引 (语义匹配, 同义词/近义概念强项)

    论文支撑: AnyTool (arXiv:2402.04253) 大规模 API 分层检索
    实现: 用 OpenAI 兼容 embedding API 生成向量, 余弦相似度搜索

    优雅降级:
      - 无 embed_client → 返回空 (BM25 兜底)
      - embed 失败 → 返回空 (BM25 兜底)
      - 向量维度不匹配 → 跳过该工具
    """

    def __init__(
        self,
        embed_client: Any = None,
        embed_model: str = "text-embedding-3-small",
    ) -> None:
        self._embed_client = embed_client
        self._embed_model = embed_model
        self._tool_defs: list[ToolDef] = []
        self._vectors: list[list[float]] = []
        self._query_cache: dict[str, list[float]] = {}  # query → vector (LRU)
        self._max_cache = 128
        self._enabled = embed_client is not None

    def add_tool(self, tool: ToolDef) -> None:
        """添加工具 (向量懒生成, 首次 search 时批量 embed)"""
        self._tool_defs.append(tool)
        self._vectors.append([])  # 占位, search 时填充

    def _ensure_vectors_sync(self) -> None:
        """同步批量生成所有工具的向量 (懒初始化)"""
        if not self._enabled or not self._tool_defs:
            return
        # 找出未生成向量的工具
        pending = [(i, t) for i, t in enumerate(self._tool_defs) if not self._vectors[i]]
        if not pending:
            return
        try:
            for i, tool in pending:
                vec = self._embed_sync(tool.searchable_text())
                if vec and self._vectors and (not self._vectors[0] or len(vec) == len(self._vectors[0])):
                    self._vectors[i] = vec
                else:
                    self._vectors[i] = vec  # 第一条作为维度基准
            logger.info(
                "tool_search.vector_index_ready",
                n_tools=len(self._tool_defs),
                n_vectors=sum(1 for v in self._vectors if v),
            )
        except Exception as e:
            logger.warning("tool_search.vector_init_failed", error=str(e))
            self._enabled = False  # 失败后禁用, 后续走 BM25

    def _embed_sync(self, text: str) -> list[float]:
        """同步生成 embedding (用线程隔离 loop 兼容嵌套场景)"""
        if not self._embed_client:
            return []

        async def _embed():
            try:
                response = await self._embed_client.embeddings.create(
                    model=self._embed_model,
                    input=text,
                )
                return response.data[0].embedding
            except Exception as e:
                logger.warning("tool_search.embed_failed", text_len=len(text), error=str(e))
                return []

        try:
            # 检测是否已有运行中的 event loop
            try:
                asyncio.get_running_loop()
                in_loop = True
            except RuntimeError:
                in_loop = False

            if not in_loop:
                return asyncio.run(_embed())

            # 已在 loop 中, 用线程隔离的新 loop
            result_holder: list = []
            def _run_in_new_loop():
                new_loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(new_loop)
                    result_holder.append(new_loop.run_until_complete(_embed()))
                finally:
                    new_loop.close()
            t = threading.Thread(target=_run_in_new_loop)
            t.start()
            t.join()
            return result_holder[0] if result_holder else []
        except Exception as e:
            logger.warning("tool_search.embed_sync_failed", error=str(e))
            return []

    def search(self, query: str, top_k: int = 5) -> list[tuple[ToolDef, float]]:
        """向量搜索, 返回 (tool, score) 列表 (按余弦相似度降序)"""
        if not self._enabled or not self._tool_defs:
            return []
        self._ensure_vectors_sync()
        if not any(self._vectors):
            return []
        # 查询向量 (带缓存)
        if query in self._query_cache:
            q_vec = self._query_cache[query]
        else:
            q_vec = self._embed_sync(query)
            if not q_vec:
                return []
            # LRU 缓存
            if len(self._query_cache) >= self._max_cache:
                # 简单 LRU: 删第一个 key
                self._query_cache.pop(next(iter(self._query_cache)))
            self._query_cache[query] = q_vec

        # 余弦相似度
        scores = []
        for i, tool_vec in enumerate(self._vectors):
            if not tool_vec or len(tool_vec) != len(q_vec):
                continue
            sim = self._cosine(q_vec, tool_vec)
            scores.append((sim, i))
        scores.sort(reverse=True)
        return [(self._tool_defs[idx], score) for score, idx in scores[:top_k]]

    def ranked_names(self, query: str, top_k: int = 10) -> list[str]:
        """返回按向量相似度排序的工具名列表 (供 RRF 融合用)"""
        return [t.name for t, _ in self.search(query, top_k=top_k)]

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        """余弦相似度"""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


def reciprocal_rank_fusion(
    ranked_lists: list[list[str]],
    k: int = 60,
    limit: int = 10,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: 多路排序融合算法 (Cormack 2009).

    无需分数标定, 只用排名, 适合融合 BM25 (词法) + Vector (语义).

    Args:
        ranked_lists: 多路排序结果 (每路是 tool name 列表, 按相关性降序)
        k: 平滑常数 (标准值 60), 防止排名 1 的项压倒一切
        limit: 返回前 N 个

    Returns:
        [(name, fused_score), ...] 按融合分数降序
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, name in enumerate(ranked, start=1):
            scores[name] = scores.get(name, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]


class ToolSearchEngine:
    """Tool Search 引擎 — BM25 + Vector 混合检索 (v2)

    v1: 纯 BM25 (词法匹配)
    v2: BM25 + Vector + RRF 融合 (词法 + 语义)
        - 无 embed_client 时降级到 v1 纯 BM25 (向后兼容)
        - 有 embed_client 时启用混合检索, 召回率提升
    """

    def __init__(self) -> None:
        self._index = BM25Index()
        self._vector_index: Optional[VectorIndex] = None  # 懒初始化
        self._always_loaded: list[ToolDef] = []  # 常驻工具 (不延迟加载)
        self._loaded_tools: dict[str, ToolDef] = {}  # 已加载的工具
        self._search_count = 0
        self._total_token_saved = 0
        self._hybrid_search_count = 0  # 混合检索次数 (统计用)

    def enable_vector_search(
        self,
        embed_client: Any,
        embed_model: str = "text-embedding-3-small",
    ) -> None:
        """启用向量检索 (混合模式).

        Args:
            embed_client: OpenAI 兼容的 AsyncOpenAI 客户端
            embed_model: embedding 模型名
        """
        self._vector_index = VectorIndex(embed_client, embed_model)
        # 把已注册的 defer_loading 工具加到向量索引
        for tool in self._index._tool_defs:
            self._vector_index.add_tool(tool)
        logger.info("tool_search.vector_enabled", embed_model=embed_model)

    def register(self, tool: ToolDef, always_loaded: bool = False) -> None:
        """注册工具"""
        if always_loaded:
            tool.defer_loading = False
            self._always_loaded.append(tool)
            self._loaded_tools[tool.name] = tool
        else:
            self._index.add_tool(tool)
            if self._vector_index is not None:
                self._vector_index.add_tool(tool)
        logger.debug(f"ToolSearch: 注册工具 {tool.name} (defer={tool.defer_loading})")

    def search(self, query: str, top_k: int = 5) -> list[ToolDef]:
        """搜索工具 (v2 混合检索).

        流程:
          1. BM25 检索 (词法匹配)
          2. 向量检索 (语义匹配, 如启用)
          3. RRF 融合两路排序
          4. 返回 top-k 工具

        向后兼容: 无向量索引时退化为纯 BM25
        """
        # 1. BM25 检索 (扩 top_k 到 2x, 给 RRF 更多候选)
        bm25_results = self._index.search(query, top_k=top_k * 2)
        bm25_names = [t.name for t, _ in bm25_results]

        # 2. 向量检索 (如启用)
        vector_names: list[str] = []
        if self._vector_index is not None:
            vector_results = self._vector_index.search(query, top_k=top_k * 2)
            vector_names = [t.name for t, _ in vector_results]
            if vector_names:
                self._hybrid_search_count += 1

        # 3. 融合 (有向量结果时 RRF, 否则纯 BM25)
        if vector_names:
            fused = reciprocal_rank_fusion([bm25_names, vector_names], limit=top_k)
            # name → ToolDef 映射
            name_to_tool = {t.name: t for t, _ in bm25_results}
            name_to_tool.update({t.name: t for t, _ in vector_results})
            return [name_to_tool[name] for name, _ in fused if name in name_to_tool]
        else:
            # 纯 BM25 (v1 行为)
            return [t for t, _ in bm25_results[:top_k]]

    def get_tools_for_llm(self, query: str = "", top_k: int = 5) -> list[dict]:
        """获取要发送给 LLM 的工具定义"""
        tools = list(self._always_loaded)
        if query:
            searched = self.search(query, top_k=top_k)
            tools.extend(searched)
            self._search_count += 1
            saved = sum(t.token_estimate for t in self._index._tool_defs) - sum(t.token_estimate for t in searched)
            self._total_token_saved += saved
            mode = "hybrid" if self._vector_index is not None else "bm25"
            logger.info(f"ToolSearch: 搜索'{query}' → {len(searched)}个工具, 节省~{saved} tokens, mode={mode}")
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
            }
            for t in tools
        ]

    def get_search_tool_def(self) -> dict:
        """返回 search_tools 元工具定义 (暴露给 LLM)"""
        return {
            "type": "function",
            "function": {
                "name": "search_tools",
                "description": "Search for available tools by keyword. Use this when you need a tool that isn't already loaded.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Keywords describing the tool you need"},
                        "top_k": {"type": "integer", "description": "Max tools to return", "default": 5}
                    },
                    "required": ["query"]
                }
            }
        }

    def get_stats(self) -> dict:
        """获取统计信息"""
        total_tools = len(self._always_loaded) + len(self._index._tool_defs)
        return {
            "total_tools": total_tools,
            "always_loaded": len(self._always_loaded),
            "deferred": len(self._index._tool_defs),
            "search_count": self._search_count,
            "hybrid_search_count": self._hybrid_search_count,
            "vector_enabled": self._vector_index is not None and self._vector_index._enabled,
            "total_token_saved": self._total_token_saved,
            "avg_token_saved": self._total_token_saved / max(1, self._search_count),
        }


# 全局单例
_engine = ToolSearchEngine()


def get_tool_search_engine() -> ToolSearchEngine:
    return _engine


def register_tool(name: str, description: str, parameters: dict,
                  handler_path: str = "", keywords: list[str] | None = None,
                  always_loaded: bool = False) -> None:
    """便捷注册工具"""
    tool = ToolDef(
        name=name,
        description=description,
        parameters=parameters,
        handler_path=handler_path,
        keywords=keywords or [],
    )
    _engine.register(tool, always_loaded=always_loaded)
