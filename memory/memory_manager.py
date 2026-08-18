from typing import Any

from loguru import logger

from config import get_agent_display_name  # noqa: F401
from db.database import DatabaseManager
from db.db_memory import MemoryDB

# FTS5 分词工具从 db.fts_utils 导入 (打破 db <-> memory 循环); 这里 re-export
# 保持向后兼容 (其他模块仍可 `from memory.memory_manager import _tokenize_for_fts`)
from db.fts_utils import _tokenize_for_fts  # noqa: F401
from memory._memory_encoder import MemoryEncoder
from memory._memory_maintenance import MemoryMaintenance

# 以下 _memory_utils re-export 仅保留有真实外部消费者（`from memory.memory_manager import X`）的符号。
from memory._memory_utils import (  # noqa: F401
    _normalize_for_dedupe,
    _normalize_score,
    _parse_temporal_query,
    reciprocal_rank_fusion,
    validate_memory_content,
)
from memory._retrieval_engine import RetrievalEngine

from .fsrs_model import FSRSModel, estimate_initial_difficulty  # noqa: F401
from .memory_distiller import MemoryDistiller
from .query_cache import QueryCache
from .retrieval_assessor import RetrievalAssessor
from .vector_store import VectorStore


class MemoryManager:
    """管理情景记忆的编码、检索、去重与遗忘等核心流程。"""

    IDLE_THRESHOLD = 30
    ENCODE_COOLDOWN = 60

    def __init__(self, db: DatabaseManager, memory: MemoryDB,
                 vector_store: VectorStore | None = None,
                 router: Any | None=None, knowledge_graph: Any | None=None, security_filter: Any | None=None,
                 reranker: Any | None=None, query_transformer: Any | None=None,
                 governance: Any | None=None,
                 entity_extractor: Any | None=None,
                 entity_store: Any | None=None,
                 reranker_service: Any | None=None) -> None:
        self.db = db
        self.memory = memory
        self.vec = vector_store
        self.router = router
        self.kg = knowledge_graph
        self._security_filter = security_filter
        self._reranker = reranker_service or reranker
        self._reranker_service = reranker_service
        self._query_transformer = query_transformer
        self._governance = governance
        self.entity_extractor = entity_extractor
        self.entity_store = entity_store
        self._kg_v2_engine: Any = None
        self._last_message_time: float = 0
        self._last_encode_time: float = 0
        self._pending_encode = False
        self._encode_generation = 0
        self._last_lazy_migrate_ts: float = 0
        # P3 记忆蒸馏器（使用硅基流动免费模型，失败降级到 router）
        self.distiller = MemoryDistiller(router=router)
        # 冷启动路由: 记忆计数缓存 (TTL 60s, 避免每次检索都 COUNT 全表)
        self._memory_count_cache: int | None = None
        self._memory_count_ts: float = 0
        # 查询语义缓存：基于嵌入向量余弦相似度匹配，命中则跳过完整检索流水线
        import config as _cfg
        self._query_cache = QueryCache(
            embed_func=self._get_query_embedding_func(),
            threshold=getattr(_cfg, 'QUERY_CACHE_THRESHOLD', 0.88),
            max_size=getattr(_cfg, 'QUERY_CACHE_MAX_SIZE', 256),
            ttl=getattr(_cfg, 'QUERY_CACHE_TTL', 300),
        )
        # CRAG 检索评估器：评估检索结果质量，低置信度时触发兜底策略
        self._assessor = RetrievalAssessor()
        # FSRS-DSR 模型实例（无状态纯计算，复用避免热路径重复创建）
        self._fsrs = FSRSModel()

        # 扩散激活引擎（第五路 RRF 通道）
        self.concept_graph = None
        self.spreading_engine = None
        try:
            from db.db_concept import ConceptDB
            from memory.concept_graph import ConceptGraph
            from memory.key_extractor import KeyExtractor
            from memory.spreading_activation import SpreadingActivationEngine
            if hasattr(self, 'db') and self.db and hasattr(self.db, '_conn') and self.db._conn is not None:
                concept_db = ConceptDB(self.db._conn)
                self._concept_db = concept_db
                self._key_extractor = KeyExtractor()
                self.concept_graph = ConceptGraph(concept_db, self._key_extractor)
                self.spreading_engine = SpreadingActivationEngine(
                    concept_db, self.vec, self._key_extractor)
                logger.info("memory.spreading_activation_enabled")
        except Exception as e:
            logger.warning("memory.spreading_activation_init_failed",
                          error=str(e))

        # Confirm/Correct 机制
        self.confirm_correct = None
        if self.concept_graph and self.spreading_engine:
            try:
                from memory.confirm_correct import ConfirmCorrect
                self.confirm_correct = ConfirmCorrect(
                    self._concept_db, self.spreading_engine, self.memory,
                    self._key_extractor)
                logger.info("memory.confirm_correct_enabled")
            except Exception as e:
                logger.warning("memory.confirm_correct_init_failed",
                              error=str(e))

        # 检索引擎：承载检索相关方法逻辑，通过 self._mm 访问本实例依赖/状态
        self._retrieval = RetrievalEngine(self)

        # 编码引擎：承载编码/写入/蒸馏相关方法逻辑，通过 self._mm 访问本实例依赖/状态
        self._encoder = MemoryEncoder(self)

        # 维护引擎：承载维护/调度/回忆相关方法逻辑，通过 self._mm 访问本实例依赖/状态
        self._maintenance = MemoryMaintenance(self)

    def __getattr__(self, name: str):
        # 兼容通过 MemoryManager.__new__ 构造（跳过 __init__）的测试/轻量实例：
        # 访问 _retrieval/_encoder 时懒创建引擎，保证委托方法在无 __init__ 时也可用。
        if name == "_retrieval":
            engine = RetrievalEngine(self)
            object.__setattr__(self, "_retrieval", engine)
            return engine
        if name == "_encoder":
            encoder = MemoryEncoder(self)
            object.__setattr__(self, "_encoder", encoder)
            return encoder
        if name == "_maintenance":
            maintenance = MemoryMaintenance(self)
            object.__setattr__(self, "_maintenance", maintenance)
            return maintenance
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
        )

    def _get_query_embedding_func(self):
        """返回查询嵌入函数（复用 VectorStore.embed），不可用时返回 None。

        VectorStore 未注入或未配置 embed_client 时 embed 返回空列表，
        QueryCache 会据此降级为禁用缓存。
        """
        if self.vec is not None:
            async def _embed_query(text: str) -> list[float]:
                vectors = await self.vec.embed([text])
                return vectors[0] if vectors else []

            return _embed_query
        return None

    def set_knowledge_graph(self, kg: Any) -> None:
        self.kg = kg

    async def reconcile_vector_index_gap(self) -> int:
        return await self._maintenance.reconcile_vector_index_gap()

    def set_kg_v2_engine(self, engine: Any) -> None:
        """注入 KGSearchEngine 实例 (KG v2 混合检索)。"""
        self._kg_v2_engine = engine

    def set_governance(self, governance: Any) -> None:
        """注入 ContextGovernance 实例 (ContextNest 哈希链 + 审计追踪)。"""
        self._governance = governance

    # ── 冷启动路由: 记忆计数 + 档位判断 ──────────────────────────
    async def _get_memory_count(self) -> int:
        return await self._retrieval._get_memory_count()

    def invalidate_memory_count_cache(self) -> None:
        self._retrieval.invalidate_memory_count_cache()

    async def get_memory_tier(self) -> str:
        return await self._retrieval.get_memory_tier()

    async def audit_retrieval(self, response_id: str,
                                memories: list[dict] | None) -> int:
        return await self._retrieval.audit_retrieval(response_id, memories)

    async def _has_duplicate(self, summary: str, scope: Any | None = None) -> bool:
        return await self._retrieval._has_duplicate(summary, scope=scope)

    def signal_new_message(self) -> None:
        self._retrieval.signal_new_message()

    async def retrieve_memories_hybrid(self, query: str, k: int = 5,
                                        use_reranker: bool = True,
                                        use_kg: bool = True,
                                        scope: Any | None = None,
                                        include_raw: bool = True,
                                        query_vec: list[float] | None = None) -> list[dict]:
        return await self._retrieval.retrieve_memories_hybrid(
            query, k=k, use_reranker=use_reranker, use_kg=use_kg, scope=scope,
            include_raw=include_raw, query_vec=query_vec)

    async def _hybrid_fts_search(self, query: str, k: int) -> list[dict]:
        return await self._retrieval._hybrid_fts_search(query, k)

    async def _hybrid_vec_search(self, query: str, k: int,
                                 candidate_ids: list[int] | None = None,
                                 is_raw: int | None = None,
                                 scope: Any | None = None,
                                 query_vec: list[float] | None = None) -> list[dict]:
        return await self._retrieval._hybrid_vec_search(
            query, k, candidate_ids=candidate_ids, is_raw=is_raw,
            scope=scope, query_vec=query_vec)

    async def _spreading_recall(self, query: str, limit: int,
                                scope: Any | None = None) -> list[dict]:
        return await self._retrieval._spreading_recall(query, limit, scope=scope)

    def _extract_deterministic_selectors(self, query: str) -> dict[str, Any]:
        return self._retrieval._extract_deterministic_selectors(query)

    async def _get_candidate_ids_by_selectors(self, selectors: dict,
                                                limit: int = 200,
                                                scope: Any | None = None) -> list[int] | None:
        return await self._retrieval._get_candidate_ids_by_selectors(
            selectors, limit=limit, scope=scope)

    async def rerank_with_selected_local_model(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
    ) -> list[dict]:
        return await self._retrieval.rerank_with_selected_local_model(
            query, documents, top_n=top_n)

    async def _insert_indexed_children(
        self,
        parent_id: int,
        children: list[dict],
        importance: float,
    ) -> bool:
        return await self._retrieval._insert_indexed_children(
            parent_id, children, importance)

    async def _hybrid_rerank(self, query: str, fused: list[tuple[str, float]],
                              all_items: dict[str, dict], k: int) -> list[dict] | None:
        return await self._retrieval._hybrid_rerank(query, fused, all_items, k)

    def _is_retrieval_simple(self, query: str) -> bool:
        return self._retrieval._is_retrieval_simple(query)

    def _suggest_k(self, query: str, default_k: int = 8) -> int:
        return self._retrieval._suggest_k(query, default_k=default_k)

    async def retrieve_memories(self, query: str, k: int = 5, context: str = "",
                                 _retry_attempted: bool = False,
                                 scope: Any | None = None,
                                 conv_user_id: str = "",
                                 apply_min_score: bool = True) -> list[dict]:
        return await self._retrieval.retrieve_memories(
            query, k=k, context=context, _retry_attempted=_retry_attempted,
            scope=scope, conv_user_id=conv_user_id,
            apply_min_score=apply_min_score)

    async def _try_temporal_search(self, query: str, k: int,
                                    scope: Any | None = None,
                                    include_raw: bool = False,
                                    conv_user_id: str = "") -> list[dict] | None:
        return await self._retrieval._try_temporal_search(
            query, k, scope=scope, include_raw=include_raw,
            conv_user_id=conv_user_id)

    async def _search_conversation_logs(self, start_ts: float, end_ts: float,
                                         scope: Any | None, k: int,
                                         conv_user_id: str = "") -> list[dict]:
        return await self._retrieval._search_conversation_logs(
            start_ts, end_ts, scope, k, conv_user_id=conv_user_id)

    async def _apply_reranker_to_results(self, query: str, results: list[dict],
                                          k: int) -> list[dict]:
        return await self._retrieval._apply_reranker_to_results(query, results, k)

    async def _transform_queries(self, query: str, context: str) -> list[str]:
        return await self._retrieval._transform_queries(query, context)

    async def _multi_query_parallel_search(self, queries: list[str], query: str,
                                             k: int,
                                             scope: Any | None = None) -> list[dict]:
        return await self._retrieval._multi_query_parallel_search(
            queries, query, k, scope=scope)

    async def _multi_query_serial_search(self, queries: list[str], k: int,
                                           scope: Any | None = None) -> list[dict]:
        return await self._retrieval._multi_query_serial_search(
            queries, k, scope=scope)

    async def _vector_fallback_search(self, query: str, k: int,
                                       scope: Any | None = None) -> list[dict]:
        return await self._retrieval._vector_fallback_search(query, k, scope=scope)

    async def _importance_fallback_search(self, k: int,
                                           scope: Any | None = None) -> list[dict]:
        return await self._retrieval._importance_fallback_search(k, scope=scope)

    async def _apply_fsrs_scoring(self, results: list[dict]) -> list[dict]:
        return await self._retrieval._apply_fsrs_scoring(results)

    async def _batch_migrate_phase(self, migrations: list[tuple[int, str, float, float, float, int]]) -> None:
        return await self._retrieval._batch_migrate_phase(migrations)

    def _dedup_by_content_similarity(self, results: list[dict], threshold: float = 0.7) -> list[dict]:
        return self._retrieval._dedup_by_content_similarity(results, threshold=threshold)

    def _compute_recency_boost(self, item: dict) -> float:
        return self._retrieval._compute_recency_boost(item)

    async def _compute_final_scores(self, query: str, results: list[dict],
                                      config: Any,
                                      query_entities: set[str] | None = None) -> None:
        return await self._retrieval._compute_final_scores(
            query, results, config, query_entities)

    async def _apply_topic_trigger(self, query: str, results: list[dict],
                                     k: int,
                                     scope: Any | None = None) -> list[dict]:
        return await self._retrieval._apply_topic_trigger(
            query, results, k, scope=scope)

    async def _batch_touch_memories(self, mem_ids: list[int | str]) -> None:
        return await self._retrieval._batch_touch_memories(mem_ids)

    async def _apply_kg_context_enhance(self, results: list[dict]) -> None:
        return await self._retrieval._apply_kg_context_enhance(results)

    async def encode_memory(self, context: dict, scope: Any | None = None) -> None:
        await self._encoder.encode_memory(context, scope=scope)

    async def _extract_and_link_entities(self, memory_id: int, summary: str,
                                          scope: Any) -> None:
        return await self._encoder._extract_and_link_entities(memory_id, summary, scope)

    async def _entity_recall(self, query: str, scope: Any,
                              recall_limit: int = 50) -> list[dict]:
        return await self._retrieval._entity_recall(query, scope, recall_limit=recall_limit)

    async def _apply_entity_boost(self, query: str, candidates: list[dict],
                                   scope: Any) -> list[dict]:
        return await self._retrieval._apply_entity_boost(query, candidates, scope)

    async def _hybrid_fts_search_scoped(self, query: str, k: int,
                                         scope: Any, is_raw: int | None) -> list[dict]:
        return await self._retrieval._hybrid_fts_search_scoped(
            query, k, scope, is_raw)

    async def _distill_to_knowledge(self, raw_id: int, summary: str,
                                     scope: Any, importance: float = 0.5,
                                     emotion: str = "", _retry: int = 0,
                                     full_text: str = "") -> None:
        return await self._encoder._distill_to_knowledge(
            raw_id, summary, scope, importance, emotion, _retry,
            full_text=full_text)

    async def _save_fallback_raw(self, raw_id: int, truncated_summary: str,
                                  full_text: str) -> None:
        return await self._encoder._save_fallback_raw(raw_id, truncated_summary, full_text)

    async def _find_similar_knowledge(self, summary: str,
                                       scope: Any) -> dict | None:
        return await self._encoder._find_similar_knowledge(summary, scope)

    async def _update_knowledge(self, knowledge_id: int, new_content: str,
                                 raw_id: int, scope: Any) -> None:
        return await self._encoder._update_knowledge(knowledge_id, new_content, raw_id, scope)

    def _generate_summary(self, exchanges: list[dict]) -> str:
        return self._encoder._generate_summary(exchanges)

    def _split_into_children(self, exchanges: list[dict], parent_id: int,
                             parent_summary: str) -> list[dict]:
        return self._encoder._split_into_children(exchanges, parent_id, parent_summary)

    async def _enrich_memory_async(self, mem_id: int, exchanges: list[dict]) -> None:
        return await self._encoder._enrich_memory_async(mem_id, exchanges)

    def _estimate_importance(self, exchanges: list[dict], context: dict) -> float:
        return self._encoder._estimate_importance(exchanges, context)

    async def try_idle_encode(self, context: dict, force: bool = False,
                              scope: Any | None = None) -> None:
        await self._maintenance.try_idle_encode(context, force=force, scope=scope)

    def _save_state_json(self, summary: str, importance: float, emotion: str) -> None:
        self._maintenance._save_state_json(summary, importance, emotion)

    async def distill_old_memories(self) -> int:
        return await self._maintenance.distill_old_memories()

    async def run_scheduled_recall(self, *, hours_back: float = 3.0,
                                    min_importance: float = 0.6,
                                    min_memories: int = 3) -> int:
        return await self._maintenance.run_scheduled_recall(
            hours_back=hours_back, min_importance=min_importance,
            min_memories=min_memories)

    async def retrieve_comfort_memories(self, limit: int = 2,
                                          scope: Any | None = None) -> list[dict]:
        return await self._maintenance.retrieve_comfort_memories(
            limit=limit, scope=scope)

    async def build_memory_prompt(self, recent_limit: int = 20,
                                   summary_limit: int = 5,
                                   include_recall_note: bool = True) -> str:
        return await self._maintenance.build_memory_prompt(
            recent_limit=recent_limit, summary_limit=summary_limit,
            include_recall_note=include_recall_note)

    async def shutdown(self) -> str:
        if self.vec:
            await self.vec.close()
        return "done"
