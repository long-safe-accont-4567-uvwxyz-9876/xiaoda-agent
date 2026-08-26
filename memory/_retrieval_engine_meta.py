"""RetrievalEngine 的元数据/状态方法组 —— 拆分自 _retrieval_engine.py。

Mixin 组合：RetrievalEngine 继承 MemoryMetadataMixin 获得记忆计数缓存、
档位判断与检索审计。仅依赖 self._mm 组件。
"""
from __future__ import annotations

import time

from loguru import logger


class MemoryMetadataMixin:
    """记忆计数缓存/档位判断/检索审计方法组。"""

    async def _get_memory_count(self) -> int:
        """获取用户私有记忆总数 (带 60s TTL 缓存, 避免频繁 COUNT)."""
        now = time.time()
        if self._mm._memory_count_cache is not None and (now - self._mm._memory_count_ts) < 60:
            return self._mm._memory_count_cache
        try:
            count = await self._mm.memory.get_episodic_count()
        except (OSError, TypeError):
            count = 0
        self._mm._memory_count_cache = count
        self._mm._memory_count_ts = now
        return count

    def invalidate_memory_count_cache(self) -> None:
        """写入新记忆后主动失效缓存, 下次检索立即感知."""
        self._mm._memory_count_cache = None
        self._mm._memory_count_ts = 0

    async def get_memory_tier(self) -> str:
        """判断当前用户记忆档位: "cold" / "warm" / "hot".

        cold (0~COLD_MAX):       纯 FTS, 向量检索完全关闭
        warm (COLD_MAX+1~WARM_MAX): 向量低权重参与, 以关键词为主
        hot  (>WARM_MAX):        BM25+向量均衡融合
        """
        try:
            import config as _cfg
            cold_max = getattr(_cfg, "MEMORY_COLD_MAX", 0)
            warm_max = getattr(_cfg, "MEMORY_WARM_MAX", 10)
        except (ImportError, AttributeError):
            cold_max, warm_max = 0, 10
        count = await self._mm._get_memory_count()
        if count <= cold_max:
            return "cold"
        if count <= warm_max:
            return "warm"
        return "hot"

    async def audit_retrieval(self, response_id: str,
                                memories: list[dict] | None) -> int:
        """ContextNest A2: 审计一次检索消费了哪些记忆版本。

        由调用方 (message_processor) 在 retrieve_memories 返回后显式调用,
        记录 (response_id, memory_id, content_hash, version, score, source) 到
        context_audit_log, 支持 point-in-time 重建。
        """
        if not self._mm._governance or not memories:
            return 0
        try:
            return await self._mm._governance.audit_context_consumption(
                response_id, memories, auto_commit=True,
            )
        except Exception as e:
            logger.debug("memory.audit_retrieval_failed", error=str(e))
            return 0
