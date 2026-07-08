"""查询语义缓存层：基于嵌入向量余弦相似度匹配，LRU + TTL 淘汰。"""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from loguru import logger

# numpy 为可选依赖：不可用时降级为纯 Python 向量运算，保证 agent 可启动
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False


class QueryCache:
    """查询语义缓存：基于嵌入向量余弦相似度匹配。

    对查询生成嵌入向量，与缓存中已有条目计算余弦相似度，
    相似度 >= threshold 则视为命中，直接返回缓存的检索结果，
    避免重复执行昂贵的混合检索 + Reranker 流水线。

    嵌入函数不可用时自动降级为禁用缓存（get 返回 None，put 不存）。
    """

    def __init__(
        self,
        embed_func: Callable[[str], Awaitable[list[float]]] | None = None,
        threshold: float = 0.88,
        max_size: int = 256,
        ttl: int = 300,
    ) -> None:
        """
        Args:
            embed_func: 异步嵌入函数 (text) -> vector，None 时禁用缓存
            threshold: 余弦相似度阈值，>= 此值视为命中
            max_size: LRU 最大条目数
            ttl: 缓存过期时间（秒）
        """
        self._embed_func = embed_func
        self._threshold = float(threshold)
        self._max_size = int(max_size)
        self._ttl = int(ttl)
        # 每个条目: {"vec": list[float], "results": list[dict], "ts": float}
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._lock = asyncio.Lock()
        # 统计指标
        self.hits: int = 0
        self.misses: int = 0
        self.total_queries: int = 0

    async def _embed(self, text: str) -> list[float] | None:
        """生成文本嵌入向量；不可用或失败返回 None。"""
        if not self._embed_func:
            return None
        try:
            vec = await self._embed_func(text)
        except Exception as e:
            logger.debug("query_cache.embed_failed", error=str(e))
            return None
        if not vec:
            return None
        if _HAS_NUMPY:
            return np.asarray(vec, dtype=np.float32).tolist()
        return [float(x) for x in vec]

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算两个向量的余弦相似度，零向量或长度不匹配返回 0.0。"""
        if len(a) != len(b):
            # 嵌入模型升级导致向量维度变化时不崩溃
            return 0.0
        if _HAS_NUMPY:
            try:
                na = float(np.linalg.norm(a))
                nb = float(np.linalg.norm(b))
                if na == 0.0 or nb == 0.0:
                    return 0.0
                return float(np.dot(a, b) / (na * nb))
            except (ValueError, TypeError):
                return 0.0
        # 纯 Python 降级实现
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)

    async def get(self, query: str) -> list[dict] | None:
        """查询缓存。

        对 query 生成嵌入向量，遍历缓存条目计算余弦相似度，
        >= threshold 且未过 TTL 则返回缓存的 results（LRU 命中时更新访问顺序）。
        嵌入函数不可用、向量生成失败、无命中均返回 None。
        顺便清理已过期的条目。
        """
        self.total_queries += 1
        if not self._embed_func:
            self.misses += 1
            return None

        query_vec = await self._embed(query)
        if query_vec is None:
            self.misses += 1
            return None

        now = time.time()
        best_key: str | None = None
        best_score: float = 0.0
        expired_keys: list[str] = []

        async with self._lock:
            for key, entry in self._cache.items():
                # TTL 过期检查
                if (now - entry["ts"]) > self._ttl:
                    expired_keys.append(key)
                    continue
                score = self._cosine_similarity(query_vec, entry["vec"])
                if score >= self._threshold and score > best_score:
                    best_score = score
                    best_key = key

            # 清理过期条目（惰性淘汰）
            for k in expired_keys:
                self._cache.pop(k, None)

            if best_key is not None:
                # LRU: 命中时移到末尾（最久未访问在头部）
                self._cache.move_to_end(best_key)
                self.hits += 1
                logger.debug("query_cache.hit", score=round(best_score, 4),
                             size=len(self._cache))
                # 返回列表浅拷贝，避免调用方修改影响缓存内部条目
                return list(self._cache[best_key]["results"])

            self.misses += 1
            return None

    async def put(self, query: str, results: list[dict]) -> None:
        """写入缓存。

        嵌入函数不可用、results 为空、向量生成失败时不存。
        缓存已满时淘汰最久未访问的条目（LRU，头部出队）。
        同一 query 重复写入时更新并刷新访问顺序。
        """
        if not self._embed_func or not results:
            return

        query_vec = await self._embed(query)
        if query_vec is None:
            return

        async with self._lock:
            key = query
            if key in self._cache:
                # 已存在：先移到末尾，再覆盖（保持末尾位置）
                self._cache.move_to_end(key)
            self._cache[key] = {
                "vec": query_vec,
                "results": results,
                "ts": time.time(),
            }
            # LRU 淘汰：超出容量时从头部弹出最久未访问
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def invalidate(self) -> None:
        """全量失效：清空所有缓存条目。"""
        self._cache.clear()

    def invalidate_by_entity(self, entity: str) -> None:
        """按实体失效。

        简单实现：缓存条目未按实体索引，无法精确按实体定位，
        为保证正确性执行全量失效。
        """
        # entity 参数保留以兼容接口契约；精确按实体失效需建立倒排索引，
        # 当前规模下全量失效成本可接受。
        self._cache.clear()

    @property
    def stats(self) -> dict:
        """返回缓存统计指标：hits / misses / total_queries / hit_rate / size。"""
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "total_queries": self.total_queries,
            "hit_rate": round(self.hits / total, 3) if total > 0 else 0.0,
            "size": len(self._cache),
        }
