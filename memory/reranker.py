# reranker.py — bge-reranker-v2-m3 交叉编码器重排序（SiliconFlow）
import hashlib
import time
from collections import OrderedDict
from typing import ClassVar

import httpx
from loguru import logger

from utils.http_pool import get_shared_client


class Reranker:
    """基于 SiliconFlow BAAI/bge-reranker-v2-m3 的交叉编码器重排序

    特点:
    - 硅基流动免费常驻模型，非限时促销，稳定性有保障
    - 与项目现有 bge-m3 嵌入模型同系列，语义空间天然对齐
    - 使用 /v1/rerank 端点，Jina 兼容格式
    """

    SUPPORTED_MODELS: ClassVar[dict[str, dict[str, str | int]]] = {
        "BAAI/bge-reranker-v2-m3": {
            "max_length": 8192,
            "provider": "siliconflow",
            "price": "免费",
        },
        "netease-youdao/bce-reranker-base_v1": {
            "max_length": 5120,
            "provider": "siliconflow",
            "price": "免费",
        },
    }

    # G14: LRU 缓存配置 — (query_hash, doc_hash) → score，避免重复调外部 API
    RERANK_CACHE_MAXSIZE = 1000

    def __init__(self, api_key: str = "", base_url: str = "",
                 model: str = "BAAI/bge-reranker-v2-m3",
                 max_length: int = 8192, batch_size: int = 8) -> None:
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
        # G14: (query_hash, doc_hash) → score 缓存，避免重复调外部 API
        self._score_cache: OrderedDict[tuple[str, str], float] = OrderedDict()

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def clear_cache(self) -> None:
        """G14: 清空 rerank 缓存。"""
        self._score_cache.clear()

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
                "document": {"text": str} (可选)
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
            # G14: 缓存命中检查 — 对每个 doc 计算 (query_hash, doc_hash)
            query_hash = hashlib.md5(query.encode("utf-8")).hexdigest()
            cached_results: list[dict] = []
            uncached_indices: list[int] = []
            uncached_documents: list[str] = []

            for idx, doc in enumerate(documents):
                doc_hash = hashlib.md5(doc.encode("utf-8")).hexdigest()
                cache_key = (query_hash, doc_hash)
                if cache_key in self._score_cache:
                    score = self._score_cache[cache_key]
                    # move_to_end 维护 LRU 顺序（最近访问移到尾部）
                    self._score_cache.move_to_end(cache_key)
                    cached_results.append({
                        "index": idx,
                        "relevance_score": score,
                        **({"document": {"text": doc}} if return_documents else {}),
                    })
                else:
                    uncached_indices.append(idx)
                    uncached_documents.append(doc)

            # G14: 全部命中缓存则跳过 API 调用；否则只对未命中的 doc 调 API
            if uncached_documents:
                api_results = await self._call_rerank_api(
                    query,
                    uncached_documents,
                    top_n=len(uncached_documents),
                    return_documents=False,
                )
                # 把 API 返回的分数写入缓存 + 构造结果
                for item in api_results:
                    uncached_idx_in_batch = item.get("index", 0)
                    original_idx = uncached_indices[uncached_idx_in_batch]
                    score = item.get("relevance_score", 0.0)
                    doc_text = uncached_documents[uncached_idx_in_batch]
                    doc_hash = hashlib.md5(doc_text.encode("utf-8")).hexdigest()
                    cache_key = (query_hash, doc_hash)
                    self._score_cache[cache_key] = score
                    # 维护 maxsize：超过时淘汰最旧（ OrderedDict 头部）
                    if len(self._score_cache) > self.RERANK_CACHE_MAXSIZE:
                        self._score_cache.popitem(last=False)
                    cached_results.append({
                        "index": original_idx,
                        "relevance_score": score,
                        **({"document": {"text": doc_text}} if return_documents else {}),
                    })

            # 按 relevance_score 降序排序，取 top_n
            cached_results.sort(
                key=lambda x: x["relevance_score"], reverse=True
            )
            final_results = cached_results[:top_n]

            latency = (time.time() - start) * 1000
            self._stats["avg_latency_ms"] = (
                self._stats["avg_latency_ms"] * 0.9 + latency * 0.1
            )

            logger.debug("reranker.done",
                         query_len=len(query),
                         docs=len(documents),
                         top_n=top_n,
                         latency_ms=f"{latency:.0f}")

            return final_results

        except Exception as e:
            logger.warning("reranker.failed", error=str(e))
            # Q0-2: 失败返回空列表，让调用方走 RRF 降级，避免假数据污染分数
            return []

    async def _call_rerank_api(
        self,
        query: str,
        documents: list[str],
        top_n: int,
        return_documents: bool,
    ) -> list[dict]:
        """调 SiliconFlow /rerank API（内部方法，不含缓存逻辑）.

        G14: 由 rerank() 调用，仅处理未命中缓存的文档。
        """
        payload = {
            "model": self._model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
            "return_documents": return_documents,
            "max_chunks_per_doc": 1,
        }

        # G4: 共享 httpx.AsyncClient（连接池复用 + HTTP/2），单次请求级别覆盖 timeout
        client = get_shared_client()
        response = await client.post(
            f"{self._base_url}/rerank",
            json=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0),
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
        return results
