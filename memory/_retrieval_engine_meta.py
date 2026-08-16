"""RetrievalEngine 的元数据/状态方法组 —— 拆分自 _retrieval_engine.py。

Mixin 组合：RetrievalEngine 继承 MemoryMetadataMixin 获得记忆计数缓存、
档位判断、检索审计与去重方法。仅依赖 self._mm 组件 + _memory_utils 纯函数。
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger

from memory._memory_utils import _normalize_for_dedupe


class MemoryMetadataMixin:
    """记忆计数缓存/档位判断/检索审计/去重方法组。"""

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

    async def _has_duplicate(self, summary: str, scope: Any | None = None) -> bool:
        """检查是否存在归一化后内容相同的已有记忆（只对 is_raw=0 的提炼知识生效）。

        mem0 SPEC 优化：原始记忆（is_raw=1）不去重，保证 append-only 可追溯。

        Args:
            scope: Scope 对象。传入时只在同 scope 内查重。
        """
        normalized = _normalize_for_dedupe(summary)
        if len(normalized) < 10:
            return False
        try:
            # 用 FTS 搜索相关记忆，然后精确匹配
            if scope is not None:
                # scope 过滤：只查 is_raw=0 的提炼知识
                candidates = await self._mm.memory.search_memories_fts_scoped(
                    summary, scope=scope, limit=5, is_raw=0
                )
            else:
                candidates = await self._mm.memory.search_memories_fts(summary, limit=5)
            for c in candidates:
                # 只对 is_raw=0 的记忆判断重复
                if c.get("is_raw", 0) == 0 and _normalize_for_dedupe(c.get("summary", "")) == normalized:
                    return True
            # FTS 无结果时也检查最近记忆
            recent = await self._mm.memory.get_episodic_recent(limit=10)
            for r in recent:
                if r.get("is_raw", 0) == 0 and _normalize_for_dedupe(r.get("summary", "")) == normalized:
                    return True
        except (OSError, TypeError):
            logger.debug("memory_manager.is_duplicate_check_failed", exc_info=True)
        return False
