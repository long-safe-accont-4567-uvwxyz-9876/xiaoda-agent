# reranker.py — bge-reranker-v2-m3 交叉编码器重排序（SiliconFlow）
from typing import ClassVar
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

    SUPPORTED_MODELS: ClassVar[dict[str, dict[str, str | int]]] = {
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
                 max_length: int = 512, batch_size: int = 8) -> None:
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
            # Q0-2: 失败返回空列表，让调用方走 RRF 降级，避免假数据污染分数
            return []