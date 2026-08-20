"""RetrievalEngine 的实体/KG 增强方法组 —— 拆分自 _retrieval_engine.py。

Mixin 组合：RetrievalEngine 继承 EntityKgBoostMixin 获得实体召回、
Entity Boost 与 KG 上下文增强方法。仅依赖 self._mm 组件 + _memory_utils
纯函数，无循环依赖。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from memory._memory_utils import _extract_entities


class EntityKgBoostMixin:
    """实体召回 + Entity Boost + KG 上下文增强方法组。"""

    async def _apply_kg_context_enhance(self, results: list[dict]) -> None:
        """KG 上下文增强：对 top-2 记忆提取实体并补充相关知识点。"""
        if not (self._mm.kg and results):
            return
        try:
            entity_names: list[str] = []
            for r in results[:2]:
                summary = r.get("summary", "")
                candidates = await asyncio.to_thread(_extract_entities, summary)
                for word in candidates:
                    if word not in ("用户", "助手", "人家"):
                        entity_names.append(word)
            entity_names = list(set(entity_names))[:3]
            if entity_names:
                knowledge = await self._mm.kg.get_related_knowledge(entity_names)
                if knowledge:
                    kg_context = await self._mm.kg.format_knowledge_context(knowledge)
                    if kg_context and results:
                        results[0]["kg_context"] = kg_context
        except Exception as e:
            logger.debug("memory.kg_expand_failed", error=str(e))

    async def _entity_recall(self, query: str, scope: Any,
                              recall_limit: int = 50) -> list[dict]:
        """第6路召回：通过实体名反查记忆（mem0 SPEC 优化）。

        流程：
        1. EntityExtractor 规则快抽查询中的实体名（<10ms，不触发 LLM）
        2. EntityStore.recall_by_entities 反查关联记忆

        Args:
            query: 用户查询
            scope: Scope 对象
            recall_limit: 召回上限
        Returns:
            记忆 dict 列表。失败返回空列表（降级）。
        """
        if not self._mm.entity_store or not self._mm.entity_extractor:
            return []
        try:
            entities = self._mm.entity_extractor._rule_based_extract(query)
            if not entities:
                return []
            entity_names = [e.name for e in entities]
            results = await self._mm.entity_store.recall_by_entities(
                entity_names, scope=scope, limit=recall_limit
            )
            for r in results:
                r["entity_recall"] = True
            return results
        except Exception as e:
            logger.debug("memory.entity_recall_failed", error=str(e))
            return []

    async def _apply_entity_boost(self, query: str, candidates: list[dict],
                                   scope: Any) -> list[dict]:
        """精排阶段计算 Entity Boost 并加分（mem0 SPEC 优化）。

        对每个候选记忆，计算其关联实体与查询实体的 boost 值，
        加到 rrf_score 上。

        Args:
            query: 用户查询
            candidates: 候选记忆列表（含 rrf_score）
            scope: Scope 对象
        Returns:
            加分后的候选列表（按 rrf_score 降序）
        """
        if not self._mm.entity_extractor or not self._mm.entity_store:
            return candidates
        if not candidates:
            return candidates
        try:
            query_entities_list = await self._mm.entity_extractor.extract(query, importance=0.3)
            query_entity_names = {e.name for e in query_entities_list}
            if not query_entity_names:
                return candidates

            now = time.time()
            for candidate in candidates:
                mem_id = candidate.get("id")
                if mem_id is None:
                    continue
                boost = await self._mm.entity_store.get_query_entities_boost(
                    mem_id, query_entity_names, now=now
                )
                if boost > 0:
                    candidate["rrf_score"] = candidate.get("rrf_score", 0.0) + boost
                    candidate["entity_boost"] = boost

            candidates.sort(key=lambda x: x.get("rrf_score", 0.0), reverse=True)
            return candidates
        except Exception as e:
            logger.debug("memory.apply_entity_boost_failed", error=str(e))
            return candidates

    async def _hybrid_fts_search_scoped(self, query: str, k: int,
                                         scope: Any, is_raw: int | None,
                                         rewritten_query: str | None = None) -> list[dict]:
        """FTS 检索 + scope 过滤 + 改写查询补充检索

        当 QueryTransformer 可用时，额外用改写后的查询做一次 FTS 检索，
        合并去重后返回。这解决了"饮食偏好"无法 FTS 命中"香菜/豆浆"的问题：
        改写后查询包含推断的关联关键词，FTS 可直接匹配。

        Args:
            rewritten_query: 外部已改写的查询（避免重复调 LLM）。None 时内部改写。
        """
        if not self._mm.memory:
            return []
        try:
            primary_results = await self._mm.memory.search_memories_fts_scoped(
                query, scope=scope, limit=k * 2, is_raw=is_raw
            )

            # FTS 改写查询补充：用改写后的查询做补充 FTS 检索
            # 仅当 QueryTransformer 可用且改写结果与原查询不同时执行
            if self._mm._query_transformer and self._mm._query_transformer.available:
                try:
                    _rewritten = rewritten_query
                    if _rewritten is None:
                        _rewritten = await self._mm._query_transformer.rewrite_query(query, "")
                    if _rewritten and _rewritten != query:
                        rewritten_results = await self._mm.memory.search_memories_fts_scoped(
                            _rewritten, scope=scope, limit=k, is_raw=is_raw
                        )
                        if rewritten_results:
                            # 合并去重：改写查询结果补充原查询未命中的记忆
                            seen_ids = {r.get("id") for r in primary_results}
                            for r in rewritten_results:
                                if r.get("id") not in seen_ids:
                                    seen_ids.add(r.get("id"))
                                    primary_results.append(r)
                except Exception as e:
                    logger.debug("memory.fts_rewrite_supplement_failed", error=str(e))

            return primary_results
        except Exception as e:
            logger.warning("memory.fts_scoped_search_failed", error=str(e))
            return []