"""MemoryManager 检索相关方法的抽取实现。

本模块中的 RetrievalEngine 持有 MemoryManager 实例的引用（self._mm），
所有检索逻辑通过 self._mm 访问依赖与状态，保证与重构前行为完全一致，
同时避免 `memory_manager` 的反向 import（循环依赖）。
"""
from typing import Any, NamedTuple
import asyncio
import time
import datetime as _datetime
from loguru import logger

from core.background_tasks import _spawn
from local_ai.integration.errors import is_structured_local_unavailable

from memory._memory_utils import (
    _stage_log,
    _parse_temporal_query,
    _extract_topic_keywords,
    _char_bigrams,
    _natural_time_desc,
    _normalize_score,
    reciprocal_rank_fusion,
)
from .fsrs_model import MemoryState, MemoryPhase, ReinforcementSignal, S_INIT
from memory._retrieval_engine_entity import EntityKgBoostMixin
from memory._retrieval_engine_meta import MemoryMetadataMixin


class RecallChannels(NamedTuple):
    """七路召回结果打包（温/热用户并行召回的一次性产物）。

    原先 `_run_multi_recall` 返回裸 7 元组，`_resolve_fallback_or_single_channel`
    与 `_fuse_and_rank` 各自接收 7 个通道位置参数（总参数 16 个）。
    打包为 NamedTuple 后签名降至 8/9 参数，字段名自文档化。
    """
    fts_items: list
    vec_items: list
    kg_items: list
    child_items: list
    spread_items: list
    entity_items: list
    kg_v2_items: list


class RetrievalEngine(EntityKgBoostMixin, MemoryMetadataMixin):
    """检索引擎：承载 MemoryManager 的检索公开/私有方法逻辑。

    构造时注入 MemoryManager 实例（mm），所有属性与方法访问都经由 self._mm
    转发，从而保留实例级 monkeypatch 与共享状态语义。
    """

    def __init__(self, mm: Any) -> None:
        self._mm = mm

    # ── 冷启动路由: 记忆计数 + 档位判断 ──────────────────────────

    def signal_new_message(self) -> None:
        self._mm._last_message_time = time.time()
        self._mm._encode_generation += 1
        self._mm._pending_encode = True

    async def retrieve_memories_hybrid(self, query: str, k: int = 5,
                                        use_reranker: bool = True,
                                        use_kg: bool = True,
                                        scope: Any | None = None,
                                        include_raw: bool = True,
                                        query_vec: list[float] | None = None) -> list[dict]:
        """FTS + 向量 + KG + 子chunk + 扩散 + 实体 六路 RRF 混合检索 + Reranker 精排

        mem0 SPEC 优化：
        - 新增第6路：EntityStore.recall_by_entities
        - 新增 Entity Boost：精排阶段加分
        - 新增 scope 过滤：user_id + agent_id 隔离
        - 新增 include_raw：是否包含 is_raw=1 的原始记忆

        冷启动三段路由 (工业标准, 对标 Dify/Coze):
        - cold (0条):  纯 FTS, 向量检索完全关闭 → 零 Embedding 开销
        - warm (1~10条): FTS + 向量低权重融合, 向量仅做补充
        - hot  (>10条):  FTS + 向量均衡融合 (原有行为)

        Args:
            scope: Scope 对象。None 时使用默认 Scope()。
            include_raw: False=只查提炼知识（is_raw=0），True=查所有记忆
            use_reranker: 是否在本方法内调用 Reranker 精排。A3 并行检索场景下会置为
                False，由调用方对合并后的候选池做一次性批量 Reranker。闲聊型查询
                也会置为 False 以节省 Reranker 调用成本。
            use_kg: 是否启用 KG 第三路召回。闲聊型查询置为 False 避免不必要的
                KG 检索开销。
            query_vec: P1-4 预计算查询向量（多查询场景批量 embed 后复用），
                None 时各向量通道内部 embed。
        """
        # scope 默认值
        if scope is None:
            from memory.scope import Scope
            scope = Scope()

        _start = time.time()
        is_raw_filter = None if include_raw else 0

        # 候选集大小参数化（可通过 config 配置）
        import config as _cfg
        recall_limit = getattr(_cfg, 'RAG_RECALL_LIMIT', 50)  # 每路召回 Top-N
        rerank_limit = getattr(_cfg, 'RAG_RERANK_LIMIT', 50)   # RRF 融合后送 Reranker 的数量

        # ── 冷启动路由: 判断用户记忆档位 ──
        tier = await self._mm.get_memory_tier()
        is_cold = tier == "cold"
        is_warm = tier == "warm"

        # 冷用户: 仅 FTS (scope 过滤), 完全跳过向量检索 (零 Embedding 开销)
        if is_cold:
            return await self._cold_start_recall(query, k, recall_limit, scope, is_raw_filter, query_vec, _start)

        # 懒迁移：concept_nodes 数 < episodic_memories 数时触发（5分钟节流）
        await self._maybe_lazy_migrate()

        # ── 温/热用户: 并行执行 FTS、向量、KG、子chunk、扩散、实体、KG v2 七路召回 ──
        # ContextNest A1: 提取确定性 selector → 候选集, 向量检索在候选集内排序
        selectors = self._mm._extract_deterministic_selectors(query)
        candidate_ids = await self._mm._get_candidate_ids_by_selectors(
            selectors, limit=recall_limit * 6, scope=scope)
        if candidate_ids is not None:
            logger.debug("memory.deterministic_selector",
                         selector_keys=[sk for sk in selectors if sk != "has_selectors"],
                         candidate_count=len(candidate_ids))

        channels = await self._run_multi_recall(
            query, recall_limit, scope, is_raw_filter, query_vec, candidate_ids, use_kg)

        routed = await self._resolve_fallback_or_single_channel(
            channels, query, k, tier, _start, candidate_ids, recall_limit, scope, query_vec)
        if routed is not None:
            return routed

        return await self._fuse_and_rank(
            query, k, use_reranker, tier, is_warm, rerank_limit,
            channels, scope, _start)

    async def _recall_kg_v2(self, query: str, recall_limit: int) -> list[dict]:
        """KG v2: 直接返回 KG 事实/实体作为上下文候选。"""
        import config as _v2_cfg
        if not getattr(_v2_cfg, 'KG_V2_ENABLED', False) or not getattr(self._mm, '_kg_v2_engine', None):
            return []
        try:
            results = await self._mm._kg_v2_engine.search(query, top_k=recall_limit)
            if not results:
                return []
            # 将 KG 事实格式化为 dict 供上下文使用
            formatted = []
            for r in results:
                if r.get("type") == "relation":
                    formatted.append({
                        "summary": r.get("fact", ""),
                        "source": "kg_v2",
                        "rrf_score": r.get("rrf_score", 0),
                    })
                elif r.get("type") == "entity":
                    summary_text = f"{r.get('name', '')}({r.get('kind', '')}): {r.get('summary', '')}"
                    formatted.append({
                        "summary": summary_text,
                        "source": "kg_v2",
                        "rrf_score": r.get("rrf_score", 0),
                    })
            return formatted
        except Exception as e:
            logger.debug("memory.kg_v2_recall_failed", error=str(e))
            return []

    async def _cold_start_recall(self, query: str, k: int, recall_limit: int,
                                 scope: Any, is_raw_filter: Any,
                                 query_vec: list[float] | None,
                                 _start: float) -> list[dict]:
        """冷启动: 仅 FTS (scope 过滤), 完全跳过向量检索 (零 Embedding 开销)。

        但 FTS 无结果时仍尝试向量检索作为兜底（避免 cold_max > 0 时丢失向量召回）。
        """
        fts_items, kg_v2_items = await asyncio.gather(
            self._mm._hybrid_fts_search_scoped(
                query, recall_limit, scope, is_raw_filter),
            self._recall_kg_v2(query, recall_limit),
        )
        if fts_items:
            results = fts_items[:k]
            # KG v2 事实作为补充候选追加 (已带 rrf_score, 不参与 ID-based 去重)
            if kg_v2_items and len(results) < k:
                results.extend(kg_v2_items[:k - len(results)])
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier="cold", results=len(results),
                        duration_ms=int((time.time() - _start) * 1000))
            return results
        # FTS 无结果，尝试向量兜底 + KG v2
        vec_items = await self._mm._hybrid_vec_search(query, recall_limit, is_raw=is_raw_filter, scope=scope, query_vec=query_vec)
        if vec_items:
            results = vec_items[:k]
            if kg_v2_items and len(results) < k:
                results.extend(kg_v2_items[:k - len(results)])
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier="cold+vec_fallback", results=len(results),
                        duration_ms=int((time.time() - _start) * 1000))
            return results
        # FTS + 向量均无结果, 仅返回 KG v2 事实 (若存在)
        if kg_v2_items:
            results = kg_v2_items[:k]
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier="cold+kg_v2_only", results=len(results),
                        duration_ms=int((time.time() - _start) * 1000))
            return results
        logger.info("memory.search", event="memory_search",
                    query=query[:100], tier="cold", results=0,
                    duration_ms=int((time.time() - _start) * 1000))
        return []

    async def _maybe_lazy_migrate(self) -> None:
        """懒迁移：concept_nodes 数 < episodic_memories 数时触发（5分钟节流）。"""
        if not self._mm.concept_graph:
            return
        if time.time() - self._mm._last_lazy_migrate_ts > 300:  # 5分钟
            try:
                self._mm._last_lazy_migrate_ts = time.time()
                ep_count = await self._mm.memory.get_episodic_count()
                node_count = await self._mm.spreading_engine.db.get_node_count()
                if node_count < ep_count:
                    unmigrated = await self._mm.memory.get_unmigrated_memories(limit=50)
                    if unmigrated:
                        await self._mm.concept_graph.lazy_migrate(unmigrated, limit=50)
                        # G13: lazy_migrate 写入新 concept_nodes，失效 recall 缓存
                        # 避免命中陈旧 (query, top_k) 缓存而遗漏新迁移节点（TTL 最长 5 分钟）
                        self._mm.invalidate_spread_cache()
            except Exception as e:
                logger.debug("memory.lazy_migrate_failed", error=str(e))

    async def _recall_kg(self, query: str, recall_limit: int, scope: Any,
                         use_kg: bool) -> list[dict]:
        """KG 召回（KG 可用时启用第三路，失败/空结果自动降级为两路融合）。"""
        if not self._mm.kg or not use_kg:
            return []
        try:
            related_names = await self._mm.kg.recall_by_query(query, limit=recall_limit)
            if not related_names:
                return []
            return await self._mm.memory.search_memories_by_entities_scoped(
                related_names, limit=recall_limit, scope=scope)
        except Exception as e:
            logger.debug("memory.kg_recall_failed", error=str(e))
            return []

    async def _recall_child(self, query: str, recall_limit: int, scope: Any,
                            query_vec: list[float] | None) -> list[dict]:
        """子chunk FTS+Vec并行检索 → 映射到父chunk记录。"""
        import config as _child_cfg
        if not getattr(_child_cfg, 'PARENT_CHILD_CHUNK_ENABLED', True):
            return []
        try:
            # 子chunk FTS + Vec 并行
            async def _child_vec_recall() -> list[int]:
                if not self._mm.vec or not self._mm.vec.enabled:
                    return []
                # 根因修复（2026-07-29）：移除外层 3s wait_for 超时（治标）。
                # embed client 已配 connect=15s + max_retries=0 + 共享 httpx client，
                # 内层 embed 有 10s 单次超时 + 重试保护。原外层 3s 必然先于内层 10s 触发，
                # 导致 embed 重试机制完全失效，网络抖动时子chunk向量召回被错误跳过。
                # P1-4: 复用上层预计算的 query_vec（多查询场景批量 embed），
                # 未提供时回退独立 embed
                _qv = query_vec
                if _qv is None:
                    query_vectors = await self._mm.vec.embed([query])
                    _qv = query_vectors[0] if query_vectors else []
                if not _qv:
                    return []
                results = await self._mm.vec.search_child(_qv, top_k=recall_limit)
                if not results:
                    return []
                child_ids = [r["id"] for r in results]
                return await self._mm.memory.get_child_parent_ids(child_ids)

            # return_exceptions=True：两个独立检索任务互不取消。
            # 修复 RuntimeWarning: coroutine '_child_vec_recall' was never awaited：
            # 原实现 search_child_fts 抛异常时 gather 立即取消 _child_vec_recall，
            # 若 _child_vec_recall 尚未被 event loop 调度，coroutine 被创建后
            # 未 await 即被丢弃 → Python RuntimeWarning + 记忆检索逻辑未执行。
            # return_exceptions=True 让两个 task 都完成执行，异常作为结果返回。
            #
            # 协程泄漏防御：先创建协程对象再传入 gather。
            # 若 gather 因参数非 awaitable（如测试中 mock 返回 MagicMock）在
            # 同步阶段抛异常，已创建的协程未被调度 → 手动 close 避免 RuntimeWarning。
            _child_fts_coro = self._mm.memory.search_child_fts(query, recall_limit)
            _child_vec_coro = _child_vec_recall()
            try:
                _child_results = await asyncio.gather(
                    _child_fts_coro,
                    _child_vec_coro,
                    return_exceptions=True,
                )
            except (ImportError, OSError, RuntimeError, ValueError):
                # gather 同步阶段失败（参数非 awaitable），
                # 关闭未调度的协程避免 "was never awaited" 警告
                for _c in (_child_fts_coro, _child_vec_coro):
                    if asyncio.iscoroutine(_c):
                        _c.close()
                raise
            except Exception:
                logger.exception(".memory._retrieval_engine.unexpected")
                # gather 同步阶段失败（参数非 awaitable），
                # 关闭未调度的协程避免 "was never awaited" 警告
                for _c in (_child_fts_coro, _child_vec_coro):
                    if asyncio.iscoroutine(_c):
                        _c.close()
                raise
            # 分别处理异常：一个检索通道失败不影响另一个的结果
            if isinstance(_child_results[0], Exception):
                logger.debug("memory.child_fts_failed",
                             error=f"{type(_child_results[0]).__name__}: {_child_results[0]}")
                child_fts_results = []
            else:
                child_fts_results = _child_results[0]
            if isinstance(_child_results[1], Exception):
                logger.debug("memory.child_vec_recall_failed",
                             error=f"{type(_child_results[1]).__name__}: {_child_results[1]}")
                child_vec_parent_ids = []
            else:
                child_vec_parent_ids = _child_results[1]

            # 合并 parent_ids（去重）
            parent_ids: set[int] = set()
            for r in child_fts_results:
                parent_ids.add(r["parent_id"])
            for pid in child_vec_parent_ids:
                parent_ids.add(pid)

            if not parent_ids:
                return []

            # 获取父chunk完整记录
            parent_mems = await self._mm.memory.get_memories_by_ids(list(parent_ids))
            # scope 后过滤：子chunk向量检索是全局的，需确保父记忆不跨用户泄露
            parent_mems = [pm for pm in parent_mems
                           if pm.get("user_id") == scope.user_id
                           and pm.get("agent_id") == scope.agent_id]
            for pm in parent_mems:
                pm["child_recall"] = True
            return parent_mems
        except Exception as e:
            logger.debug("memory.child_recall_failed", error=str(e))
            return []

    async def _timed(self, channel: str, coro: Any, query: str) -> Any:
        """每通道独立计时（并行执行，各通道耗时互不影响，日志定位最慢通道）。"""
        _ch_st = time.time()
        try:
            return await coro
        finally:
            _stage_log(f"channel_{channel}", _ch_st, query)

    async def _run_multi_recall(self, query: str, recall_limit: int, scope: Any,
                                is_raw_filter: Any, query_vec: list[float] | None,
                                candidate_ids: Any, use_kg: bool) -> RecallChannels:
        """温/热用户: 并行执行 FTS、向量、KG、子chunk、扩散、实体、KG v2 七路召回。"""
        logger.info("memory.gather_start", query=query[:30])

        fts_items, vec_items, kg_items, child_items, spread_items, entity_items, kg_v2_items = await asyncio.gather(
            self._timed("fts", self._mm._hybrid_fts_search_scoped(query, recall_limit, scope, is_raw_filter), query),
            self._timed("vec", self._mm._hybrid_vec_search(query, recall_limit, candidate_ids=candidate_ids, is_raw=is_raw_filter, scope=scope, query_vec=query_vec), query),
            self._timed("kg", self._recall_kg(query, recall_limit, scope, use_kg), query),
            self._timed("child", self._recall_child(query, recall_limit, scope, query_vec), query),
            self._timed("spreading", self._mm._spreading_recall(query, recall_limit, scope=scope), query),
            self._timed("entity", self._mm._entity_recall(query, scope, recall_limit), query),
            self._timed("kg_v2", self._recall_kg_v2(query, recall_limit), query),
        )
        return RecallChannels(
            fts_items, vec_items, kg_items, child_items,
            spread_items, entity_items, kg_v2_items,
        )

    async def _resolve_fallback_or_single_channel(
            self, channels: RecallChannels,
            query: str, k: int, tier: str, _start: float, candidate_ids: Any,
            recall_limit: int, scope: Any,
            query_vec: list[float] | None) -> list[dict] | None:
        """空通道自动剔除 + 单路短路返回。

        七路都空则 fallback 查原始记忆（蒸馏失败时兜底）；仅一路（或仅 KG v2）
        有结果时直接返回；否则返回 None 交给 RRF 融合。
        """
        fts_items, vec_items, kg_items, child_items, spread_items, entity_items, kg_v2_items = channels
        # 空通道自动剔除: 七路都空则 fallback 查原始记忆（蒸馏失败时兜底）
        if not fts_items and not vec_items and not kg_items and not child_items and not spread_items and not entity_items and not kg_v2_items:
            # Fallback: 用相同 FTS+Vec 检索，但 include_raw（is_raw=0 和 is_raw=1 都返回）
            raw_fts, raw_vec = await asyncio.gather(
                self._mm._hybrid_fts_search_scoped(query, recall_limit, scope, is_raw_filter=None),
                self._mm._hybrid_vec_search(query, recall_limit, candidate_ids=candidate_ids, is_raw=None, scope=scope, query_vec=query_vec),
            )
            raw_results = (raw_fts or []) + (raw_vec or [])
            if raw_results:
                # 去重 + 按 score 排序
                seen_ids: set = set()
                deduped: list = []
                for r in raw_results:
                    rid = r.get("id")
                    if rid and rid not in seen_ids:
                        seen_ids.add(rid)
                        deduped.append(r)
                deduped.sort(key=lambda x: x.get("score", x.get("rrf_score", 0)), reverse=True)
                results = deduped[:k]
                logger.info("memory.search", event="memory_search",
                            query=query[:100], tier=f"{tier}+raw_fallback",
                            results=len(results),
                            duration_ms=int((time.time() - _start) * 1000))
                return results
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=0,
                        duration_ms=int((time.time() - _start) * 1000))
            return []
        # 仅 KG v2 有结果: 直接返回 (KG v2 事实已带 rrf_score, 无需补全)
        if not fts_items and not vec_items and not kg_items and not child_items and kg_v2_items:
            results = kg_v2_items[:k]
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=len(results),
                        duration_ms=int((time.time() - _start) * 1000))
            return results

        # 单路短路：7 个通道各自「只有这一路有结果」的判定 + 补 rrf_score。
        # 原实现 7 个 if 块结构几乎一致，差异仅在：哪一路、score 取值字段、是否追加 kg_v2。
        # 数据驱动表统一处理（契约见 tests/test_retrieval_single_channel.py）。
        # 字段说明：channel_items=该路候选；empty_checks=其余必须为空的通道名；
        # score_key=补 rrf_score 时取值的字段（带 fallback 链）；append_kg_v2=是否补 kg_v2。
        single_channel_rules = [
            ("child",   ("fts", "vec", "kg", "spread", "entity"),
             ("score",),                          True),
            ("kg",     ("fts", "vec", "child", "spread", "entity"),
             ("score",),                          True),
            ("vec",    ("fts", "kg", "child", "spread", "entity"),
             ("similarity", "score"),             True),
            ("fts",    ("vec", "kg", "child", "spread", "entity"),
             ("score",),                          True),
            ("spread", ("fts", "vec", "kg", "child", "entity"),
             ("spreading_score", "score"),        False),
            ("entity", ("fts", "vec", "kg", "child", "spread"),
             ("score",),                          False),
        ]
        channels_map = {
            "fts": fts_items, "vec": vec_items, "kg": kg_items,
            "child": child_items, "spread": spread_items, "entity": entity_items,
        }
        for active, empties, score_keys, append_kg_v2 in single_channel_rules:
            active_items = channels_map[active]
            if not active_items:
                continue
            if all(not channels_map[other] for other in empties):
                return self._return_single_channel(
                    active_items, k, kg_v2_items if append_kg_v2 else [],
                    query, tier, _start, score_keys)
        return None

    @staticmethod
    def _return_single_channel(items: list, k: int, kg_v2_items: list,
                                query: str, tier: str, _start: float,
                                score_keys: tuple[str, ...]) -> list[dict]:
        """单路短路统一后处理：补 rrf_score → 切片 k → 可选补 kg_v2 → log → 返回。"""
        for item in items:
            # 按字段链取值：vec 走 (similarity, score)，其它走 (score,)
            val = next((item.get(key) for key in score_keys if item.get(key) is not None), 0.0)
            item.setdefault("rrf_score", val)
        results = items[:k]
        if kg_v2_items and len(results) < k:
            results.extend(kg_v2_items[:k - len(results)])
        logger.info("memory.search", event="memory_search",
                    query=query[:100], tier=tier, results=len(results),
                    duration_ms=int((time.time() - _start) * 1000))
        return results

    async def _fuse_and_rank(self, query: str, k: int, use_reranker: bool, tier: str,
                             is_warm: bool, rerank_limit: int,
                             channels: RecallChannels, scope: Any,
                             _start: float) -> list[dict]:
        """加权 RRF 融合（多路，空通道自动剔除）+ Entity Boost + Reranker 精排。"""
        fts_items, vec_items, kg_items, child_items, spread_items, entity_items, kg_v2_items = channels
        try:
            import config as _cfg
            warm_vec_weight = getattr(_cfg, "MEMORY_WARM_VEC_WEIGHT", 0.6)
            rank_penalty = getattr(_cfg, "RAG_RRF_RANK_PENALTY", 1.0)
        except (ImportError, AttributeError):
            warm_vec_weight = 0.6
            rank_penalty = 1.0
        # 温用户: 向量低权重 (default 0.6:1.0); 热用户: 均衡 (1.0:1.0)
        # P0-2 调整：0.2→0.6，避免温用户期间语义召回被过度压制导致"记不住"
        if is_warm:
            fts_weight, vec_weight = 1.0, warm_vec_weight
        else:
            fts_weight, vec_weight = 1.0, 1.0

        ranked_lists, weights = self._build_fusion_lists(
            fts_items, vec_items, kg_items, child_items, spread_items, entity_items,
            fts_weight, vec_weight)
        fused = reciprocal_rank_fusion(
            ranked_lists, limit=rerank_limit,
            weights=weights, rank_penalty=rank_penalty,
        )

        # 按 RRF 排序获取完整记录（合并所有通道候选）
        all_items = self._merge_all_items(
            fts_items, vec_items, kg_items, child_items, spread_items, entity_items)

        # ── mem0 SPEC: Entity Boost 精排加分 ──
        candidates = []
        for item_id, rrf_score in fused:
            if item_id in all_items:
                item = all_items[item_id]
                item["rrf_score"] = rrf_score
                candidates.append(item)
        candidates = await self._mm._apply_entity_boost(query, candidates, scope)

        # Reranker 精排（可用时）；不可用/失败返回 None 走降级
        reranked_results = await self._apply_reranker(
            query, k, use_reranker, fused, all_items, candidates, scope, tier, _start, kg_v2_items)
        if reranked_results is not None:
            return reranked_results

        # 降级：无 Reranker 或 Reranker 失败时走 candidates (已含 entity boost)
        final = candidates[:k]
        # KG v2 事实作为补充候选追加 (已带 rrf_score, 不参与 ID-based 去重)
        # 先切片再追加, 确保至少有部分 kg_v2 命中能露出
        if kg_v2_items and len(final) < k:
            final.extend(kg_v2_items[:k - len(final)])
        logger.info("memory.search", event="memory_search",
                    query=query[:100], tier=tier, results=len(final),
                    duration_ms=int((time.time() - _start) * 1000))
        return final

    @staticmethod
    def _build_fusion_lists(fts_items: list, vec_items: list, kg_items: list,
                            child_items: list, spread_items: list, entity_items: list,
                            fts_weight: float, vec_weight: float) -> tuple[list, list]:
        """构建多路 ID 列表与权重（KG/子chunk/扩散/实体 通道无结果时自动降级）。"""
        fts_ids = [str(item["id"]) for item in fts_items]
        vec_ids = [str(item["id"]) for item in vec_items]
        ranked_lists = [fts_ids, vec_ids]
        weights = [fts_weight, vec_weight]
        if kg_items:
            for _kitem in kg_items:
                _kitem["kg_recall"] = True
            ranked_lists.append([str(item["id"]) for item in kg_items])
            weights.append(0.6)  # KG 通道权重（P1-1: 0.8→0.6，联想召回降权避免串台）
        if child_items:
            ranked_lists.append([str(item["id"]) for item in child_items])
            weights.append(0.7)  # 子chunk召回的父chunk权重（P1-1: 0.9→0.7）
        if spread_items:
            ranked_lists.append([str(item["id"]) for item in spread_items])
            weights.append(0.4)  # 扩散激活权重（P1-1: 0.85→0.4，间接联想降权）
        if entity_items:
            ranked_lists.append([str(item["id"]) for item in entity_items])
            weights.append(0.5)  # 实体召回权重（P1-1: 0.7→0.5）
        return ranked_lists, weights

    @staticmethod
    def _merge_all_items(fts_items: list, vec_items: list, kg_items: list,
                         child_items: list, spread_items: list, entity_items: list) -> dict[str, dict]:
        """合并所有通道候选（同一 id 合并布尔标记 + 保留较高 score）。

        注意: 同一 id 在多通道出现时需合并标记（如 kg_recall），避免后通道覆盖前通道标记。
        """
        all_items: dict[str, dict] = {}
        for item in fts_items + vec_items + kg_items + child_items + spread_items + entity_items:
            key = str(item["id"])
            if key in all_items:
                existing = all_items[key]
                # 合并布尔标记，任一通道命中即为 True
                for mark_key in ("kg_recall", "child_recall"):
                    if item.get(mark_key):
                        existing[mark_key] = True
                # 保留较高的 score（各通道归一化方式不同，取最大值更安全）
                if item.get("score", 0) > existing.get("score", 0):
                    existing["score"] = item["score"]
            else:
                all_items[key] = item
        return all_items

    async def _apply_reranker(self, query: str, k: int, use_reranker: bool,
                              fused: list, all_items: dict[str, dict],
                              candidates: list, scope: Any, tier: str,
                              _start: float, kg_v2_items: list) -> list[dict] | None:
        """Reranker 精排；不可用/失败返回 None 走降级。

        local reranker service 已选但模型不可用时 raise（而非静默降级）。
        """
        reranker_available = bool(self._mm._reranker and self._mm._reranker.available)
        if use_reranker and self._mm._reranker_service is not None and not reranker_available:
            from local_ai.integration.reranker import LocalRerankerUnavailableError

            raise LocalRerankerUnavailableError("selected local reranker model is unavailable")
        if not (use_reranker and reranker_available and len(candidates) > k):
            return None
        # 根因修复（2026-07-29）：移除外层 5s wait_for 超时（治标）。
        # reranker 已用共享 httpx client（connect=15s）+ 单次请求 5s timeout，
        # _hybrid_rerank 内部有 try/except 返回 None（失败降级到 RRF 排序）。
        __st = time.time()
        reranked = await self._mm._hybrid_rerank(query, fused, all_items, k)
        _stage_log("hybrid_rerank", __st, query)
        if not reranked:
            return None
        # 对 reranked 也应用 entity boost
        reranked = await self._mm._apply_entity_boost(query, reranked, scope)
        # KG v2 事实作为补充候选追加 (已带 rrf_score, 不参与 Reranker ID-based 排序)
        # 先切片再追加, 避免 reranker 返回 k 条时 [:k] 丢弃全部 kg_v2_items
        results = reranked[:k]
        if kg_v2_items and len(results) < k:
            results.extend(kg_v2_items[:k - len(results)])
        logger.info("memory.search", event="memory_search",
                    query=query[:100], tier=tier, results=len(results),
                    duration_ms=int((time.time() - _start) * 1000))
        return results

    async def _hybrid_vec_search(self, query: str, k: int,
                                 candidate_ids: list[int] | None = None,
                                 is_raw: int | None = None,
                                 scope: Any | None = None,
                                 query_vec: list[float] | None = None) -> list[dict]:
        """向量检索 + 批量 JOIN：一次查询获取所有向量命中的记忆记录

        ContextNest A1: candidate_ids 提供时, 向量检索只在确定性候选集内排序,
        候选集本身由 metadata selector (时间/重要性) 产生, Jaccard 1.0。
        is_raw: None=不过滤, 0=只查蒸馏知识, 1=只查原始记忆
        scope: 非空时后过滤 user_id/agent_id，防止跨用户记忆泄露。
        query_vec: P1-4 预计算查询向量（多查询场景批量 embed 后复用），None 时内部 embed
        """
        if not self._mm.vec:
            return []
        try:
            # 根因修复（2026-07-29）：移除外层 3.5s wait_for 超时（治标）。
            # embed client 已配 connect=15s + max_retries=0，vec.search 内部调 embed
            # （有 10s 单次超时 + 重试保护）+ 本地 sqlite_vec 搜索（毫秒级）。
            # 原 3.5s 超时在 embed 慢时必然先触发，导致向量通道被跳过 → "想不起来"。
            # 注：原注释"embed 6.9s 击穿 8s"的根因正是 connect=5s 过短，现已修复。
            __st = time.time()
            # HyDE（假设文档嵌入）：开启时生成假设答案文档，与原查询向量混合检索。
            # 默认关闭（HYDE_ENABLED=False），避免查询变换跑偏（同多查询扩展教训）。
            import config as _hyde_cfg
            _hyde_enabled = getattr(_hyde_cfg, "HYDE_ENABLED", False)
            _hyde_doc = None
            if _hyde_enabled and self._mm._query_transformer and self._mm._query_transformer.available:
                try:
                    _hyde_doc = await self._mm._query_transformer.generate_hyde_document(query)
                except Exception as e:
                    logger.debug("memory.hyde_failed", error=str(e))
                    _hyde_doc = None
            if _hyde_doc:
                vec_results = await self._mm.vec.search_with_hyde(
                    query, hyde_doc=_hyde_doc, alpha=0.4,
                    k=k * 2, candidate_ids=candidate_ids,
                )
                _stage_log("vec_embed_hyde_search", __st, query)
            else:
                vec_results = await self._mm.vec.search(
                    query, top_k=k * 2, candidate_ids=candidate_ids, deterministic=True,
                    query_vec=query_vec,
                )
                _stage_log("vec_embed_search", __st, query)
            if not vec_results:
                return []
            vec_ids = [row_id for row_id, _ in vec_results]
            vec_mems = await self._mm.memory.get_memories_by_ids(vec_ids)
            if is_raw is not None:
                vec_mems = [m for m in vec_mems if m.get("is_raw") == is_raw]
            if scope is not None:
                vec_mems = [m for m in vec_mems
                            if m.get("user_id") == scope.user_id
                            and m.get("agent_id") == scope.agent_id]
            # 构建 id -> memory 映射，按 distance 排序组装结果
            vec_mem_map = {m["id"]: m for m in vec_mems}

            # 治本修复（TDD test_rag_quality_root_fix）：
            # 原 _hybrid_vec_search 用相对归一化 (1 - distance/max_dist) 美化距离，
            # 即使最远的向量也接近 1.0 高分，导致 Python query 召回亲密内容。
            # 改用绝对 L2 距离阈值：distance > RAG_VEC_MAX_DISTANCE 的向量直接丢弃，
            # 不进入 RRF 融合，从源头杜绝噪声。
            import config as _cfg
            _max_distance = getattr(_cfg, 'RAG_VEC_MAX_DISTANCE', 1.0)
            _soft_penalty = getattr(_cfg, 'RAG_VEC_SOFT_PENALTY', 0.3)
            _filtered_count = 0
            _demoted_count = 0
            items = []
            for row_id, distance in vec_results:
                mem = vec_mem_map.get(row_id)
                if mem:
                    # P0-1: 统一绝对相似度 (1 - distance)，去掉相对归一化 (1 - distance/max_dist)。
                    # 根因：相对归一化会把过滤后最大距离映射到 0.0、最小距离映射到 1.0，
                    # 即使所有结果都距离很远（接近 RAG_VEC_MAX_DISTANCE），最相关的也有高分，
                    # 与绝对阈值过滤配合时分数失真。绝对距离 0~1.0 映射相似度 1.0~0.0，
                    # 与 RAG_MIN_FINAL_SCORE / RRF 的分数语义对齐。
                    # P0-2: 硬阈值改软降权。distance > _max_distance 不再丢弃，
                    # 而是降权保留，避免语义查询整体偏远时向量通道空转。
                    # Reranker 仍可判定相关性，噪声由 final_score 最低分过滤兜底。
                    # P0-3 修复：原实现 sim = max(0, 1-dist) * penalty，当 dist>1.0 时
                    # sim=0，乘以任何 penalty 仍为 0，降权系数完全无效！
                    # 诊断："饮食偏好"→"不吃香菜" dist=1.19，sim=0，Reranker 无法捞回。
                    # 修复：超阈值时使用 (1 - dist/max_dist*1.2) * penalty 公式，
                    # 确保 sim > 0（即使 dist 略超 max_dist），Reranker 可正常排序。
                    if distance <= _max_distance:
                        sim = max(0.0, 1.0 - distance)
                    else:
                        _demoted_count += 1
                        # 超阈值软降权：用 (1 - dist/(max_dist*1.2)) * penalty
                        # 确保在 dist 略超 max_dist 时 sim 仍为正数。
                        # 例：max_dist=1.15, dist=1.19, penalty=0.5:
                        #   sim = (1 - 1.19/1.38) * 0.5 = (1-0.862) * 0.5 = 0.069
                        # Reranker 可基于此非零分排序，而非一律 0 分无法区分。
                        sim = max(0.01, (1.0 - distance / (_max_distance * 1.2))) * _soft_penalty
                    mem["score"] = sim
                    items.append(mem)
            if _demoted_count > 0:
                logger.info("memory.vec_distance_demoted",
                            query=query[:50],
                            total=len(vec_results),
                            demoted=_demoted_count,
                            kept=len(items),
                            max_distance=_max_distance)
            return items
        except Exception as e:
            from local_ai.integration.reranker import LocalModelUnavailableError

            if isinstance(e, LocalModelUnavailableError):
                raise
            logger.warning("memory.vec_search_failed", error=str(e))
            return []

    async def _spreading_recall(self, query: str, limit: int,
                                scope: Any | None = None) -> list[dict]:
        """扩散激活第五路检索通道

        通过 SpreadingActivationEngine 检索 concept_nodes，
        将结果映射回 episodic_memories（通过 source_mem_id）。
        scope 非空时后过滤 user_id/agent_id，防止跨用户记忆泄露。

        MEMORY_RETRIEVAL_DIFFUSION=False 时跳过扩散（精准检索），
        避免通过概念图找回应被艾宾浩斯遗忘曲线衰减归档的低 importance 记忆。
        """
        if not self._mm.spreading_engine:
            return []
        # 精准检索开关：False 时跳过概念图扩散
        import config
        if not getattr(config, "MEMORY_RETRIEVAL_DIFFUSION", False):
            return []
        try:
            results = await self._mm.spreading_engine.recall(query, top_k=limit)
            if not results:
                return []
            # 映射回 episodic_memories，多 node 指向同一 memory 时取最高分。
            # recall() 结果已携带 source_mem_id（alive_nodes 内存直读），
            # 逐条 get_node 是纯冗余 DB 往返（top_k=120 时最多 120 次串行
            # 查询挤占共享 aiosqlite 连接）；仅结果缺该字段时回退查库，
            # 兼容旧引擎/Mock。
            mem_ids = []
            for r in results:
                source_mem_id = r.get("source_mem_id")
                if source_mem_id is None:
                    node = await self._mm.spreading_engine.db.get_node(r["id"])
                    source_mem_id = node.get("source_mem_id") if node else None
                if source_mem_id:
                    mem_ids.append((source_mem_id, r["score"]))
            if not mem_ids:
                return []
            # 批量获取记忆
            ids = [m[0] for m in mem_ids]
            # 多 node 指向同一 memory 时保留最高分（取 max 而非覆盖）
            score_map: dict[int, float] = {}
            for mid, score in mem_ids:
                if mid not in score_map or score > score_map[mid]:
                    score_map[mid] = score
            memories = await self._mm.memory.get_memories_by_ids(ids)
            if scope is not None:
                memories = [m for m in memories
                            if m.get("user_id") == scope.user_id
                            and m.get("agent_id") == scope.agent_id]
            for mem in memories:
                mem["spreading_score"] = score_map.get(mem["id"], 0.0)
                mem["spreading_recall"] = True
            return memories
        except Exception as e:
            logger.debug("memory.spreading_recall_failed", error=str(e))
            return []

    def _extract_deterministic_selectors(self, query: str) -> dict[str, Any]:
        """ContextNest A1: 从查询中提取确定性 selector (metadata-based, Jaccard 1.0)。

        与向量检索 (概率性, 论文实测 mean Jaccard 0.611) 互补:
        selector 先产生确定性候选集, 向量只在集内排序。

        Returns:
            dict 可选键:
            - time_range: (start_ts, end_ts) 来自"昨天/前天/上周"等时间词
            - min_importance: float  (当前留空, 由调用方按需填)
            - has_selectors: bool   是否有任何确定性 selector 可用
        """
        selectors: dict = {"has_selectors": False}
        try:
            tr = _parse_temporal_query(query)
            if tr:
                selectors["time_range"] = tr
                selectors["has_selectors"] = True
        except Exception as e:
            logger.debug("memory.selector_extract_failed", error=str(e))
        return selectors

    async def _get_candidate_ids_by_selectors(self, selectors: dict,
                                                limit: int = 200,
                                                scope: Any | None = None) -> list[int] | None:
        """根据确定性 selector 查询候选 rowid 集合。

        无 selector 返回 None (调用方走原 KNN 全量检索)。
        scope 非空时追加 user_id/agent_id 过滤，防止跨用户候选泄露。
        """
        if not selectors.get("has_selectors"):
            return None
        clauses: list[str] = []
        params: list = []
        if scope is not None:
            clauses.append("user_id = ?")
            clauses.append("agent_id = ?")
            params.extend([scope.user_id, scope.agent_id])
        if "time_range" in selectors:
            s, e = selectors["time_range"]
            clauses.append("timestamp BETWEEN ? AND ?")
            params.extend([s, e])
        if "min_importance" in selectors:
            clauses.append("importance >= ?")
            params.append(selectors["min_importance"])
        # ORDER BY id 保证候选集本身有序确定
        where = " AND ".join(clauses) if clauses else "1=1"
        params.append(limit)
        try:
            cursor = await self._mm.memory._conn.execute(
                f"SELECT id FROM episodic_memories WHERE {where} "
                f"ORDER BY id LIMIT ?",
                params,
            )
            rows = await cursor.fetchall()
            return [r[0] for r in rows] if rows else []
        except Exception as e:
            logger.debug("memory.candidate_ids_failed", error=str(e))
            return None

    async def _insert_indexed_children(
        self,
        parent_id: int,
        children: list[dict],
        importance: float,
    ) -> bool:
        child_ids = []
        error: BaseException | None = None
        child_records = [
            {
                **child,
                "importance": importance * child["weight"],
            }
            for child in children
        ]

        async def _insert_batch() -> list[int]:
            transaction = getattr(getattr(self._mm, "db", None), "write_transaction", None)
            if transaction is None:
                return await self._mm.memory.insert_child_chunks(parent_id, child_records)
            async with transaction():
                return await self._mm.memory.insert_child_chunks(
                    parent_id,
                    child_records,
                    auto_commit=False,
                )

        insert_task = asyncio.create_task(_insert_batch())
        try:
            child_ids = await asyncio.shield(insert_task)
            child_items = [
                (child_id, child["embed_content"])
                for child_id, child in zip(child_ids, children, strict=True)
            ]
            if await self._mm.vec.batch_upsert_children(child_items):
                return True
        except BaseException as caught:
            error = caught
            if isinstance(caught, asyncio.CancelledError):
                try:
                    child_ids = await asyncio.shield(insert_task)
                except (ImportError, OSError, RuntimeError, ValueError):
                    child_ids = []
                except Exception:
                    logger.exception(".memory._retrieval_engine._insert_batch_unexpected")
                    child_ids = []
        await asyncio.shield(self._mm.memory.delete_child_chunks(child_ids))
        if isinstance(error, asyncio.CancelledError):
            raise error
        return False

    async def _hybrid_rerank(self, query: str, fused: list[tuple[str, float]],
                              all_items: dict[str, dict], k: int) -> list[dict] | None:
        """Reranker 精排：基于 RRF 融合后的候选池重排序，返回 top_k 结果。

        失败时返回 None，调用方降级到 RRF 排序。
        """
        docs: list[str] = []
        idx_map: dict[int, str] = {}
        for i, (item_id, _rrf_score) in enumerate(fused):
            if item_id in all_items:
                docs.append(all_items[item_id].get("summary", ""))
                idx_map[i] = item_id
        if not docs:
            return None
        try:
            reranked = await self._mm._reranker.rerank(
                query=query,
                documents=docs,
                top_n=k,
            )
            results: list[dict] = []
            for item in reranked:
                orig_idx = item["index"]
                item_id = idx_map.get(orig_idx)
                if item_id and item_id in all_items:
                    mem = all_items[item_id]
                    mem["rerank_score"] = item["relevance_score"]
                    mem["rrf_score"] = dict(fused).get(item_id, 0)
                    results.append(mem)
            return results if results else None
        except Exception as e:
            if self._mm._reranker_service is not None:
                from local_ai.integration.reranker import LocalModelUnavailableError

                if isinstance(e, LocalModelUnavailableError):
                    raise
            logger.warning("memory.rerank_failed", error=str(e))
            return None

    def _is_retrieval_simple(self, query: str) -> bool:
        """A1: 判断查询是否足够简单，可跳过查询变换直接走混合检索

        P0 修复（用户要求"取消对话通道分类机制"）：
        移除对 SIMPLE_TASK_KEYWORDS 的依赖（已从 config.py 删除）。
        仅保留基于有效长度的启发式判断——这是检索层的查询变换优化，
        不影响对话主路径（所有消息仍统一走主路径，由 LLM 自行决定）。

        判定规则（按顺序短路）:
        1. 计算有效长度（中文字符 ×2 + 其他字符 ×1），<=15 直接判定为简单
        2. 有效长度 <=20 → 简单（中等长度，无需查询变换）
        3. 否则 → 非简单（长查询需要查询变换提升检索质量）
        """
        if not query:
            return True

        # 计算有效长度：中文字符 ×2 + 其他字符 ×1
        effective_len = 0
        for ch in query:
            if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
                effective_len += 2
            else:
                effective_len += 1

        # 规则 1：极短查询直接跳过变换
        if effective_len <= 15:
            return True

        # 规则 2：中等长度无需查询变换
        if effective_len <= 20:
            return True

        # 规则 3：长查询需要查询变换
        return False

    def _suggest_k(self, query: str, default_k: int = 8) -> int:
        """根据查询内容智能建议检索条数 k（情感陪伴型 bot）。

        策略：
        - 极短闲聊（问候/确认）：k=2，避免注入无关记忆
        - 日常闲聊：k=5~8
        - 情感/回忆/个人话题：k=10，多检索相关情感记忆
        - 涉及具体事件/人物/经历：k=10，召回更多上下文
        """
        if not query:
            return 1

        # 计算有效长度
        effective_len = 0
        for ch in query:
            if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
                effective_len += 2
            else:
                effective_len += 1

        # 情感/回忆/个人话题 → 多检索，让回复更有温度和连贯性
        # 注意：必须在长度检查之前，否则短查询会被提前截断
        emotional_indicators = (
            "记得", "想起", "回忆", "以前", "之前", "那时候", "那次",
            "喜欢", "讨厌", "开心", "难过", "伤心", "生气", "害怕",
            "担心", "焦虑", "压力", "累", "烦", "无聊", "孤独",
            "想你", "想ta", "分手", "吵架", "和好", "朋友", "家人",
            "爸妈", "生日", "节日", "考试", "面试", "工作", "辞职",
            "梦想", "未来", "以后", "遗憾", "后悔", "感恩", "幸福",
            "害怕", "勇敢", "加油", "坚持", "放弃", "努力",
            "心情", "感觉", "感受", "情绪", "状态", "最近",
        )
        query_lower = query.lower()
        for indicator in emotional_indicators:
            if indicator in query_lower:
                return min(10, default_k + 2)

        # 涉及具体事件/人物/经历
        event_indicators = (
            "发生", "那次", "那件事", "什么时候", "哪里", "谁",
            "聊天", "说过", "告诉你", "跟我说", "你记得",
            "上次", "上次说", "之前说", "你说过",
        )
        for indicator in event_indicators:
            if indicator in query_lower:
                return min(10, default_k + 1)

        # 极短查询：问候、确认、单字回复
        if effective_len <= 8:
            return 2

        # 短查询：简单闲聊
        if effective_len <= 15:
            return 5

        # 长查询：可能涉及多话题
        if effective_len > 60:
            return min(10, default_k + 2)

        return default_k

    async def retrieve_memories(self, query: str, k: int = 5, context: str = "",
                                 _retry_attempted: bool = False,
                                 scope: Any | None = None,
                                 conv_user_id: str = "",
                                 apply_min_score: bool = True) -> list[dict]:
        """检索记忆。

        P0 修复（上下文污染根因）：新增 conv_user_id 参数。
        根因：_search_conversation_logs 查 conversation_logs 时不过滤 user_id，
              导致其他用户/会话的原始对话被注入当前上下文（用户反馈
              "那是之前的数据库里面的原文直接蹦出来了"）。
        修复：conv_user_id 非空时，_search_conversation_logs 按 user_id 过滤，
              仅返回当前用户的对话记录。query_cache 也按 user_id 隔离。

        回忆工具失效根因（2026-08-05）：RAG_MIN_FINAL_SCORE 的 rerank 过滤
        会把"亲亲流程"等个人/亲密记忆（rerank 0.007-0.1，低于 0.15）整段
        清空，导致 recall 工具返回空、助手"想不起来"。该过滤的本意是给
        主动注入上下文去噪（避免技术型 query 注入无关亲密内容），但 recall
        工具是用户显式发起的回忆请求，应返回检索到的记忆由模型判断相关性，
        不应被该过滤清空。故新增 apply_min_score：默认 True（主动注入保持
        去噪），recall 工具传 False 跳过该过滤。
        """
        import config
        scope_source = "explicit"
        if scope is None:
            from memory.scope import current_scope
            scope = current_scope()
            scope_source = "bound"
        logger.bind(
            scope_user_id=scope.user_id,
            scope_session_id=scope.session_id,
            scope_agent_id=scope.agent_id,
            request_id=scope.request_id,
            scope_source=scope_source,
            conv_user_filter_present=bool(conv_user_id),
            requested_k=k,
        ).info("memory.scope_resolved")
        # 查询语义缓存：命中则直接返回，跳过完整检索流水线
        _scope_cache_prefix = f"{scope.user_id}::{scope.agent_id}"
        if getattr(config, 'QUERY_CACHE_ENABLED', True):
            _cache_key = f"{_scope_cache_prefix}::{conv_user_id}::{query}"
            logger.bind(
                scope_user_id=scope.user_id,
                scope_agent_id=scope.agent_id,
                request_id=scope.request_id,
                conv_user_filter_present=bool(conv_user_id),
            ).debug("memory.cache_lookup")
            logger.debug("memory.retrieve_stage", stage="query_cache_get", query=query[:50])
            cached = await self._mm._query_cache.get(_cache_key)
            if cached is not None:
                logger.bind(
                    scope_user_id=scope.user_id,
                    scope_agent_id=scope.agent_id,
                    request_id=scope.request_id,
                    result_count=len(cached),
                ).info("memory.cache_hit")
                return cached
            logger.bind(
                scope_user_id=scope.user_id,
                scope_agent_id=scope.agent_id,
                request_id=scope.request_id,
            ).debug("memory.cache_miss")

        # 意图路由：按查询意图调整 k 与检索通道（闲聊型跳过 KG/Reranker）
        # A4 根本修复：移除外层 asyncio.wait_for，因为 query_transform.py 内部已有超时控制
        # 双重超时（外层5s + 内层5s）会导致不必要的失败
        logger.debug("memory.retrieve_stage", stage="classify_intent", query=query[:50])
        intent = "factual"
        if self._mm._query_transformer and self._mm._query_transformer.available:
            try:
                intent = await self._mm._query_transformer.classify_intent(query)
            except Exception as e:
                logger.debug("memory_manager.classify_intent_failed", error=str(e))
                intent = "factual"

        # 按意图调整 k（宽松策略：不主动缩小k，避免丢失结果）
        if intent == "multi-hop":
            k = max(k, 8)
        # chat/factual/temporal 保持原 k

        # 时间实体识别：检测"昨天/前天/上周"等时间词，按时间范围检索
        # 这让小妲能回答"昨天发生了什么"这类纯时间查询
        # 修复：时间检索返回空时不短路，继续走语义检索兜底，避免"不知道/忘记了"
        _t_temporal = time.time()
        temporal_results = await self._mm._try_temporal_search(
            query, k, scope=scope, include_raw=True, conv_user_id=conv_user_id)
        _temporal_ms = int((time.time() - _t_temporal) * 1000)
        if _temporal_ms > 2000:
            logger.warning("memory.temporal_search_slow",
                           elapsed_ms=_temporal_ms, query=query[:50])
        if temporal_results:
            logger.info("memory.temporal_search_hit",
                        count=len(temporal_results),
                        elapsed_ms=_temporal_ms,
                        query=query[:50])
            # 时间检索命中也递增 access_count（与常规检索路径一致）
            hit_ids = [r.get("id") for r in temporal_results if r.get("id")]
            if hit_ids:
                _spawn(self._mm._batch_touch_memories(hit_ids))
            return temporal_results

        # A1: 智能短路 - 简单查询跳过查询变换，直接走混合检索
        if getattr(config, "RETRIEVAL_SMART_SKIP", True) and self._mm._is_retrieval_simple(query):
            return await self._retrieve_simple_path(
                query, k, intent, config, scope, _cache_key,
                _retry_attempted, apply_min_score)

        # 查询变换 + 多查询检索
        results = await self._run_query_search(query, context, k, scope, config)

        # 降级：纯向量检索
        if not results:
            __st = time.time()
            results = await self._mm._vector_fallback_search(query, k, scope=scope)
            _stage_log("vector_fallback", __st, query)

        # 注：移除 importance fallback（同上，会注入"重要但无关"的记忆）
        # 空结果如实返回空，由模型调 recall 工具或如实说"不记得"

        # FSRS 打分 + 综合评分 + CRAG 评估重试 + 最终排序截断
        results = await self._score_and_rank_results(
            query, results, k, config, intent, _retry_attempted, scope)

        # 话题触发器 + KG 增强 + 去重 + 统一截断 + 最低分过滤
        results = await self._postprocess_results(
            query, results, k, config, intent, apply_min_score, scope)

        # 写入缓存（P0: 使用 user_id 隔离的 cache key）
        # 治本修复（2026-08-05）：put 改 fire-and-forget。
        # 根因：_query_cache.put 内部调 embed API（网络 1-2s），await 阻塞检索返回。
        # 缓存写入不影响当前检索结果，无需让用户等待。
        if getattr(config, 'QUERY_CACHE_ENABLED', True) and results:
            _spawn(self._mm._query_cache.put(_cache_key, results))

        # 检索命中后批量递增 access_count（passive_use）
        # 修复：此前 increment_access_count 从未被调用，导致记忆永远无法进入 PERMANENT 状态
        # 这里使用 fire-and-forget 方式，不阻塞检索返回
        if results:
            hit_ids = [r.get("id") for r in results if r.get("id")]
            if hit_ids:
                _spawn(self._mm._batch_touch_memories(hit_ids))
        return results


    async def _retrieve_simple_path(self, query: str, k: int, intent: str,
                                     config, scope: Any, _cache_key: str,
                                     _retry_attempted: bool,
                                     apply_min_score: bool) -> list[dict]:
        """A1 智能短路：简单查询跳过查询变换，直接走混合检索。

        闲聊型查询跳过 KG 和 Reranker 节省检索成本；命中后与复杂路径
        统一评分逻辑（FSRS + final_score + CRAG 重试 + 去重 + 最低分过滤）。
        """
        # A1: 智能短路 - 简单查询跳过查询变换，直接走混合检索
        if getattr(config, "RETRIEVAL_SMART_SKIP", True) and self._mm._is_retrieval_simple(query):
            # 闲聊型查询跳过 KG 和 Reranker，节省检索成本
            use_reranker = intent != "chat"
            use_kg = intent != "chat"
            results = await self._mm.retrieve_memories_hybrid(
                query, k=k, use_reranker=use_reranker, use_kg=use_kg, scope=scope)
            if results:
                # 简单路径使用与复杂路径一致的评分逻辑，保证评分尺度统一
                results = await self._mm._apply_fsrs_scoring(results)
                query_entities: set[str] = set()
                if self._mm.kg:
                    try:
                        query_entities = await self._mm.kg.get_query_entities(query)
                    except Exception:
                        logger.debug("memory_manager.query_entities_failed", exc_info=True)
                await self._mm._compute_final_scores(query, results, config, query_entities)

                # CRAG 检索评估（A4 根本修复：闲聊型查询跳过 CRAG 评估）
                # 闲聊型查询不需要精确检索，CRAG 评估会产生不必要的低置信度告警
                if intent != "chat":
                    assessment = self._mm._assessor.assess(query, results)
                    if assessment["should_retry"] and not _retry_attempted:
                        logger.info("memory.crag_low_confidence",
                                    query=query[:100], confidence=assessment["confidence"])
                        # 扩大候选集重试一次
                        retry_k = k * 2
                        retry_results = await self._mm.retrieve_memories_hybrid(
                            query, k=retry_k, use_reranker=True, use_kg=True, scope=scope)
                        if retry_results:
                            retry_results = await self._mm._apply_fsrs_scoring(retry_results)
                            await self._mm._compute_final_scores(query, retry_results, config, query_entities)
                            retry_results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
                            results = retry_results[:k]
                            # 重新评估
                            reassessment = self._mm._assessor.assess(query, results)
                            logger.info("memory.crag_retry_done",
                                        confidence=reassessment["confidence"],
                                        level=reassessment["level"])

                results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
                results = results[:k]

            # 注：移除 importance fallback
            # 根因：空结果时按重要性排序返回 k 条，完全不做语义匹配，
            # 会注入"重要但无关"的记忆（如用户问天气却返回上次生日记忆）。
            # 空结果应如实返回空，由模型调 recall 工具或如实说"不记得"。

            # 内容相似度去重（与复杂路径保持一致，避免多通道 RRF 融合后返回近似重复）
            if results:
                results = self._mm._dedup_by_content_similarity(results)

            # 智能最低分过滤：非闲聊型 query 过滤低相关度噪声
            # 用 rerank_score（纯相关性）而非 final_score（综合分含 R/recency/importance 保底 ~0.22）
            # （bench_rag_e2e 实测：技术型 query 返回 rerank 0.007 的亲密内容）
            _min_score = getattr(config, 'RAG_MIN_FINAL_SCORE', 0.15)
            if apply_min_score and intent != "chat" and _min_score > 0 and results:
                _before = len(results)
                results = [r for r in results
                           if float(r.get("rerank_score", 0)) >= _min_score]
                if len(results) != _before:
                    logger.info("memory.low_score_filtered",
                                query=query[:60],
                                before=_before, after=len(results),
                                min_score=_min_score)

            # 写入缓存（P0: 使用 user_id 隔离的 cache key）
            if getattr(config, 'QUERY_CACHE_ENABLED', True) and results:
                await self._mm._query_cache.put(_cache_key, results)
            return results


    async def _run_query_search(self, query: str, context: str, k: int,
                                scope: Any | None, config) -> list[dict]:
        """查询变换 + 多查询/单查询检索。"""
        # 查询变换：改写 + 扩展
        __st = time.time()
        queries = await self._mm._transform_queries(query, context)
        _stage_log("transform_queries", __st, query)

        # 多查询检索：仅当查询变换产生多个查询时才走 multi_query 包装。
        # 单查询（查询变换被关闭/降级时返回 [query]）直接混合检索，
        # 避免包装层产生误导性的 multi_query_search 耗时日志。
        if len(queries) > 1:
            if getattr(config, "RETRIEVAL_PARALLEL_SEARCH", True):
                __st = time.time()
                all_results = await self._mm._multi_query_parallel_search(
                    queries, query, k, scope=scope)
                _stage_log("multi_query_search", __st, query)
            else:
                __st = time.time()
                all_results = await self._mm._multi_query_serial_search(
                    queries, k, scope=scope)
                _stage_log("multi_query_search", __st, query)
        else:
            # 单查询直接混合检索
            # 修复：使用 queries[0]（改写后查询）而非原始 query。
            # 根因：QUERY_EXPAND_COUNT=0 时 _transform_queries 返回 [rewritten]，
            # 但原代码用原始 query 调 retrieve_memories_hybrid，导致改写结果被丢弃，
            # FTS 无法利用改写后的关键词（如"后端代码"→"编程 Python FastAPI"）。
            # queries[0] 在改写关闭/降级时等于原始 query，行为向后兼容。
            # 注：不做双查询并行检索（原查询+改写查询同时跑），因为：
            # 1. 候选池翻倍 = 另一种查询扩展，引入噪声破坏精确率
            # 2. FTS 改写查询补充已在 _hybrid_fts_search_scoped 中实现，更精准
            # 3. 向量通道用改写查询 embed，语义覆盖已足够
            all_results = await self._mm.retrieve_memories_hybrid(
                queries[0], k=k, scope=scope)
        return all_results

    async def _score_and_rank_results(self, query: str, results: list[dict],
                                       k: int, config, intent: str,
                                       _retry_attempted: bool,
                                       scope: Any | None) -> list[dict]:
        """FSRS 打分 + 综合评分 + CRAG 评估重试 + 最终排序截断。"""
        # 流体记忆评分（艾宾浩斯遗忘曲线 + 访问强化）
        __st = time.time()
        results = await self._mm._apply_fsrs_scoring(results)
        _stage_log("fsrs_scoring", __st, query)

        # 保留实体提取用于评分增强，但不再后置追加候选
        # （KG 召回已前移到 retrieve_memories_hybrid 的并行召回阶段，统一走 RRF + Reranker）
        query_entities: set[str] = set()
        if self._mm.kg:
            try:
                __st = time.time()
                query_entities = await self._mm.kg.get_query_entities(query)
                _stage_log("kg_query_entities", __st, query)
            except Exception as e:
                logger.debug("memory.query_entities_failed", error=str(e))

        # KG 增强评分 + 综合评分 (复用已提取的 query_entities, 避免 N+1 LLM)
        await self._mm._compute_final_scores(query, results, config, query_entities)

        # CRAG 检索评估（A4 根本修复：闲聊型查询跳过 CRAG 评估）
        if intent != "chat":
            assessment = self._mm._assessor.assess(query, results)
            if assessment["should_retry"] and not _retry_attempted:
                logger.info("memory.crag_low_confidence",
                            query=query[:100], confidence=assessment["confidence"])
                # 扩大候选集重试一次
                retry_k = k * 2
                retry_results = await self._mm.retrieve_memories_hybrid(
                    query, k=retry_k, use_reranker=True, use_kg=True, scope=scope)
                if retry_results:
                    retry_results = await self._mm._apply_fsrs_scoring(retry_results)
                    await self._mm._compute_final_scores(
                        query, retry_results, config, query_entities)
                    retry_results.sort(
                        key=lambda x: x.get("final_score", 0), reverse=True)
                    results = retry_results[:k]
                    # 重新评估
                    reassessment = self._mm._assessor.assess(query, results)
                    logger.info("memory.crag_retry_done",
                                confidence=reassessment["confidence"],
                                level=reassessment["level"])

            # 注：移除 importance fallback（同上，会注入"重要但无关"的记忆）

        results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
        results = results[:k]
        return results

    async def _postprocess_results(self, query: str, results: list[dict],
                                   k: int, config, intent: str,
                                   apply_min_score: bool,
                                   scope: Any | None) -> list[dict]:
        """话题触发器 + KG 增强 + 去重 + 统一截断 + 最低分过滤。"""
        # 主动检索 A：话题触发器
        # 从 query 抽取 top-N 话题关键词，对每个词做轻量 FTS 检索，
        # 把"主题相关但未被主路命中"的记忆补充进来，扩大主动联想。
        # 这样即使主路 RRF 没召回，话题相关的旧记忆也能浮上来。
        # 修复 P1-3：本函数不再内部截断，topic_hits 会以 final_score=0.25 保留在末尾，
        # 由下面的统一截断处理。
        results = await self._mm._apply_topic_trigger(query, results, k, scope=scope)

        # KG 上下文增强（保留原有逻辑）
        await self._mm._apply_kg_context_enhance(results)

        results = self._mm._dedup_by_content_similarity(results)

        # 修复 P1-3：话题触发器修复后的统一截断
        # 允许最多 k+2 条结果（k 条主路 + 最多 2 条话题触发补充），让话题触发记忆可见。
        # 如果没有 topic_hits，截断到 k；有则保留 k+2 上限。
        _has_topic = any(r.get("topic_trigger") for r in results)
        _final_k = k + 2 if _has_topic else k
        if len(results) > _final_k:
            results = results[:_final_k]

        # 智能最低分过滤：非闲聊型 query 过滤低相关度噪声（保留话题触发记忆）
        # 用 rerank_score（纯相关性）而非 final_score（综合分含保底 ~0.22，过滤失效）
        # （bench_rag_e2e 实测：技术型 query 返回 rerank 0.007 的亲密内容）
        _min_score = getattr(config, 'RAG_MIN_FINAL_SCORE', 0.15)
        if apply_min_score and intent != "chat" and _min_score > 0 and results:
            _before = len(results)
            results = [r for r in results
                       if float(r.get("rerank_score", 0)) >= _min_score
                       or r.get("topic_trigger")]
            if len(results) != _before:
                logger.info("memory.low_score_filtered",
                            query=query[:60],
                            before=_before, after=len(results),
                            min_score=_min_score)
        return results

    async def _try_temporal_search(self, query: str, k: int,
                                    scope: Any | None = None,
                                    include_raw: bool = False,
                                    conv_user_id: str = "") -> list[dict] | None:
        """时间型查询：直接查 conversation_logs 原始对话。

        根本修复：时间查询最需要的是完整的原始对话记录，不是经过 FTS/reranker/CRAG
        多层管线过滤后的蒸馏摘要。conversation_logs 是最可靠、最完整的数据源。

        查找顺序：conversation_logs → episodic_memories（兜底）
        无时间词返回 None（调用方继续走常规语义检索）。

        P0 修复：conv_user_id 非空时按 user_id 过滤，防止跨用户对话泄露。
        """
        if scope is None:
            from memory.scope import Scope
            scope = Scope()

        _time_range = _parse_temporal_query(query)
        if not _time_range:
            return None
        start_ts, end_ts = _time_range
        try:
            # 第一优先：直接查 conversation_logs 原始对话（最可靠）
            # 时间查询用户要的是"发生了什么"，原始对话比蒸馏摘要更准确
            _conv_results = await self._mm._search_conversation_logs(
                start_ts, end_ts, scope, k * 4, conv_user_id=conv_user_id)
            if _conv_results:
                logger.debug("memory.temporal_convlogs_hit",
                             query=query[:50], count=len(_conv_results))
                return _conv_results

            # 兜底：conversation_logs 无结果时查 episodic_memories
            # （可能对话还没来得及记录，但蒸馏记忆已生成）
            is_raw_filter = None if include_raw else 0
            _time_results = await self._mm.memory.search_memories_by_time_scoped(
                start_ts, end_ts, scope=scope, limit=k * 2, is_raw=is_raw_filter
            )
            if _time_results:
                logger.debug("memory.temporal_episodic_hit",
                             query=query[:50], count=len(_time_results))
                return _time_results
            # 两级 fallback：含 is_raw=1 的原始记录
            if is_raw_filter is not None:
                _fallback_results = await self._mm.memory.search_memories_by_time_scoped(
                    start_ts, end_ts, scope=scope, limit=k * 2, is_raw=None
                )
                if _fallback_results:
                    logger.debug("memory.temporal_fallback_raw_hit",
                                 query=query[:50], count=len(_fallback_results))
                    return _fallback_results
            return []
        except Exception as e:
            logger.warning("memory.temporal_search_failed", error=str(e))
            return None

    async def _search_conversation_logs(self, start_ts: float, end_ts: float,
                                         scope: Any | None, k: int,
                                         conv_user_id: str = "") -> list[dict]:
        """查 conversation_logs 原始对话，格式化为记忆格式返回。

        P0 修复（上下文污染根因）：按 conv_user_id 过滤。
        根因：原实现不过滤 user_id，导致其他用户/会话的原始对话被注入当前上下文。
              用户反馈"那是之前的数据库里面的原文直接蹦出来了"——AI 看到了不属于
              当前用户的对话记录，导致上下文混乱、重复回复、角色出戏。
        修复：conv_user_id 非空时按 user_id 过滤，仅返回当前用户的对话。
              conv_user_id 为空时保留原行为（向后兼容，但不应在新代码中使用）。
        """
        try:
            # P0 修复：按 conv_user_id 过滤，防止跨用户对话泄露
            raw = await self._mm.memory.get_conversations_by_time_range(
                start_ts, end_ts, user_id=conv_user_id, limit=k
            )
            if not raw:
                return []
            results = []
            for row in raw:
                ts = row.get("timestamp", 0)
                user_msg = (row.get("user_message") or "")
                asst_msg = (row.get("assistant_reply") or "")
                if not user_msg and not asst_msg:
                    continue
                # 场景指令检测：用户有时发送"（场景：...格式：...）"这类
                # 元指令来控制 agent 行为，不是真正的对话内容。LLM 在回忆时
                # 会原样复述这些指令（系统 prompt 泄漏），所以需要标记为
                # "场景指令"，让 LLM 知道这不是需要复述给用户听的内容。
                if user_msg.startswith("（场景：") or user_msg.startswith("(场景："):
                    user_msg = "（场景指令，非对话内容，回忆时不要复述）"
                # 带完整日期的时间锚点 + 叙事化格式：根因修复
                # 之前格式"时间：...\n爸爸：...\n小妲：..."像数据记录，LLM 模仿输出
                # "时间线整理：⏰ 约7:09"等出戏格式。改为叙事性格式——像回忆的画面
                # 浮现，而不是日志条目。LLM 看到叙事性内容，回忆时也会用叙事性语言。
                # 同时带完整年月日，防止 LLM 被记忆内容里的日期干扰（如用户当时
                # 在回忆"7月16日"，LLM 会采用内容里的日期作为锚点）。
                if ts:
                    from datetime import datetime as _dt_cls
                    _dt = _dt_cls.fromtimestamp(float(ts))
                    _period = _natural_time_desc(float(ts))
                    time_str = f"{_dt.year}年{_dt.month}月{_dt.day}日{_period}"
                else:
                    time_str = "某时"
                # 叙事化：用"——"连接时间和对话，用"爸爸说""你回答"代替"爸爸：""小妲："
                # 这种格式让 LLM 觉得这是回忆片段，不是数据记录
                summary = f"{time_str}——\n爸爸说：{user_msg}"
                if asst_msg:
                    summary += f"\n你当时回答：{asst_msg}"
                results.append({
                    "summary": summary,
                    "timestamp": ts,
                    "importance": 0.5,
                    "type": "conversation_log",
                    "is_raw": 1,
                    "user_id": scope.user_id if scope else "",
                    "agent_id": scope.agent_id if scope else "",
                })
            return results[:k]
        except Exception as e:
            logger.warning("memory.convlogs_search_failed", error=str(e))
            return []

    async def _transform_queries(self, query: str, context: str) -> list[str]:
        """查询变换：rewrite + expand。A2 并行执行，失败降级到 [query]。

        MEMORY_RETRIEVAL_DIFFUSION=False 时跳过 expand_query（精准检索，搜什么就是什么），
        只保留 rewrite_query（查询改写优化表述，不是扩散）。
        """
        import config
        queries = [query]
        if not (self._mm._query_transformer and getattr(config, "QUERY_TRANSFORM_ENABLED", True)):
            return queries
        parallel_transform = getattr(config, "RETRIEVAL_PARALLEL_TRANSFORM", True)
        # 精准检索开关：False 时跳过 expand_query，只保留 rewrite_query
        _diffusion_enabled = getattr(config, "MEMORY_RETRIEVAL_DIFFUSION", False)
        try:
            if parallel_transform:
                queries = await self._transform_parallel(query, context, _diffusion_enabled)
            else:
                queries = await self._transform_serial(query, context, _diffusion_enabled)
        except Exception as e:
            logger.warning("memory.query_transform_failed", error=str(e))
        return queries

    async def _transform_parallel(self, query: str, context: str,
                                  diffusion_enabled: bool) -> list[str]:
        """并行/精准检索路径：rewrite + 可选 expand（各自独立 LLM 调用）。"""
        import config
        expand_count = getattr(config, "QUERY_EXPAND_COUNT", 2) if diffusion_enabled else 0
        rewrite_task = asyncio.create_task(
            self._mm._query_transformer.rewrite_query(query, context)
        )
        if expand_count > 0:
            expand_task = asyncio.create_task(
                self._mm._query_transformer.expand_query(query, n=expand_count)
            )
            rewritten, expanded = await asyncio.gather(
                rewrite_task, expand_task, return_exceptions=True
            )
            # 异常降级：rewrite 失败用原查询，expand 失败用 [query]
            if isinstance(rewritten, Exception):
                logger.warning("memory.rewrite_failed", error=str(rewritten))
                rewritten = query
            if isinstance(expanded, Exception):
                logger.warning("memory.expand_failed", error=str(expanded))
                expanded = [query]
            if not rewritten:
                rewritten = query
            if not expanded:
                expanded = [query]
            if rewritten != query:
                logger.debug("memory.query_rewritten",
                             original=query[:50], rewritten=rewritten[:50])
            # 合并：[rewritten] + [q for q in expanded if q != rewritten]
            merged = [rewritten]
            for q in expanded:
                if q != rewritten:
                    merged.append(q)
            queries = merged
            if len(queries) > 1:
                logger.debug("memory.query_expanded", count=len(queries))
            return queries

        # 精准检索：只执行 rewrite，不扩散
        rewritten = await rewrite_task
        if isinstance(rewritten, Exception):
            logger.warning("memory.rewrite_failed", error=str(rewritten))
            rewritten = query
        if not rewritten:
            rewritten = query
        if rewritten != query:
            logger.debug("memory.query_rewritten",
                         original=query[:50], rewritten=rewritten[:50])
        return [rewritten]

    async def _transform_serial(self, query: str, context: str,
                                diffusion_enabled: bool) -> list[str]:
        """串行降级路径：先 rewrite，再 expand。"""
        import config
        queries = [query]
        rewritten = await self._mm._query_transformer.rewrite_query(query, context)
        if rewritten and rewritten != query:
            queries = [rewritten]
            logger.debug("memory.query_rewritten", original=query[:50], rewritten=rewritten[:50])
        expand_count = getattr(config, "QUERY_EXPAND_COUNT", 2) if diffusion_enabled else 0
        if expand_count > 0:
            expanded = await self._mm._query_transformer.expand_query(rewritten, n=expand_count)
            if expanded and len(expanded) > 1:
                queries = expanded
                logger.debug("memory.query_expanded", count=len(queries))
        return queries

    async def _multi_query_parallel_search(self, queries: list[str], query: str,
                                             k: int,
                                             scope: Any | None = None) -> list[dict]:
        """A3: 并行多查询检索 + 批量 Reranker。

        各子查询检索时关闭内部 Reranker，统一在合并池上做一次批量精排。
        """
        precomputed_vecs = await self._batch_embed_queries(queries)
        all_results = await self._gather_hybrid_results(
            queries, precomputed_vecs, k, scope)
        return await self._batch_rerank(all_results, query, k)

    async def _batch_embed_queries(self, queries: list[str]) -> list[list[float] | None]:
        """P1-4: 合并 embed 批处理，子查询检索时复用向量，减少 embed 延迟与限流。

        批量失败时降级为 None，各子查询回退内部独立 embed（single-flight 兜底）。
        """
        precomputed_vecs: list[list[float] | None] = [None] * len(queries)
        if getattr(self._mm, "vec", None):
            try:
                batch_vecs = await self._mm.vec.embed(list(queries))
                for i, v in enumerate(batch_vecs):
                    if v:
                        precomputed_vecs[i] = v
            except Exception as e:
                logger.debug("memory.batch_embed_failed", error=str(e))
        return precomputed_vecs

    async def _gather_hybrid_results(self, queries: list[str],
                                     precomputed_vecs: list[list[float] | None],
                                     k: int, scope: Any | None) -> list[dict]:
        """并行执行各子查询的 hybrid 检索，去重合并候选池。"""
        all_results: list[dict] = []
        seen_ids: set[str] = set()
        hybrid_tasks = [
            self._mm.retrieve_memories_hybrid(
                q, k=k * 2, use_reranker=False, scope=scope,
                query_vec=precomputed_vecs[i],
            )
            for i, q in enumerate(queries)
        ]
        hybrid_results = await asyncio.gather(*hybrid_tasks, return_exceptions=True)
        for i, res in enumerate(hybrid_results):
            if isinstance(res, Exception):
                if is_structured_local_unavailable(res):
                    raise res
                logger.warning("memory.hybrid_search_failed",
                               query=queries[i][:50], error=str(res))
                continue
            for r in res:
                rid = str(r.get("id", ""))
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    all_results.append(r)
        return all_results

    async def _batch_rerank(self, all_results: list[dict], query: str,
                            k: int) -> list[dict]:
        """批量 Reranker：对合并后的候选池用原始 query 重排一次。"""
        if self._mm._reranker and self._mm._reranker.available and len(all_results) > k:
            try:
                docs = [r.get("summary", "") for r in all_results]
                # P1-5: 移除 5s 外层 wait_for（治标）。
                # 根因：reranker 已用共享 httpx client（connect=15s）+ 单次请求 5s timeout，
                # 且 _hybrid_rerank 与本方法均有 try/except 降级。原外层 5s 与内层 5s
                # 双层超时，外层必然先触发，reranker 实际耗时被截断，降级机制失效。
                reranked = await self._mm._reranker.rerank(
                    query=query,
                    documents=docs,
                    top_n=k,
                )
                reranked_results = []
                for item in reranked:
                    idx = item.get("index", -1)
                    if 0 <= idx < len(all_results):
                        mem = all_results[idx]
                        mem["rerank_score"] = item.get("relevance_score", 0.0)
                        reranked_results.append(mem)
                if reranked_results:
                    all_results = reranked_results
            except Exception as e:
                if is_structured_local_unavailable(e):
                    raise
                logger.warning("memory.batch_rerank_failed", error=str(e))
        return all_results

    async def _multi_query_serial_search(self, queries: list[str], k: int,
                                           scope: Any | None = None) -> list[dict]:
        """串行降级（原有逻辑）。"""
        all_results: list[dict] = []
        seen_ids: set[str] = set()
        for q in queries:
            try:
                hybrid_results = await self._mm.retrieve_memories_hybrid(q, k=k, scope=scope)
                for r in hybrid_results:
                    rid = str(r.get("id", ""))
                    if rid and rid not in seen_ids:
                        seen_ids.add(rid)
                        all_results.append(r)
            except Exception as e:
                if is_structured_local_unavailable(e):
                    raise
                logger.warning("memory.hybrid_search_failed", query=q[:50], error=str(e))
        return all_results

    async def _vector_fallback_search(self, query: str, k: int,
                                       scope: Any | None = None) -> list[dict]:
        """降级：纯向量检索 + 批量 JOIN。

        scope 非空时后过滤 user_id/agent_id，防止跨用户记忆泄露。
        """
        if not self._mm.vec:
            return []
        results: list[dict] = []
        try:
            vec_results = await self._mm.vec.search(query, top_k=k)
            if vec_results:
                vec_ids = [row_id for row_id, _ in vec_results]
                vec_mems = await self._mm.memory.get_memories_by_ids(vec_ids)
                # scope 后过滤：向量索引是全局的，需确保不跨用户泄露
                if scope is not None:
                    vec_mems = [m for m in vec_mems
                                if m.get("user_id") == scope.user_id
                                and m.get("agent_id") == scope.agent_id]
                # 构建 id -> memory 映射，按 distance 排序组装结果
                vec_mem_map = {m["id"]: m for m in vec_mems}
                for row_id, distance in vec_results:
                    mem = vec_mem_map.get(row_id)
                    if mem:
                        mem["score"] = 1.0 - distance
                        results.append(mem)
        except Exception as e:
            logger.warning("memory.vec_search_failed", error=str(e))
        return results

    async def _apply_fsrs_scoring(self, results: list[dict]) -> list[dict]:
        """FSRS-DSR 记忆评分（遗忘曲线 R + 状态过滤），过滤低分记忆。

        优化：
        1. 懒迁移 phase：检索时实时检查 phase 是否需要更新（BUFFER→DECAY/REINFORCED），
           无需后台任务
        2. 过滤阈值从 R<0.05 放宽到 R<0.01，避免过早遗忘有用记忆
        3. 检索命中后通过 _batch_touch_memories 异步递增 access_count 和 reinforcement_count
        """
        if not results:
            return results
        now = time.time()
        _migration_needed: list[tuple[int, str, float, int]] = []  # (id, phase, stability, rc)
        filtered: list[dict] = []
        for r in results:
            similarity = r.get("score", 0.5)
            last_review = r.get("last_review", 0.0)
            created_at = r.get("created_at", 0.0) or r.get("timestamp", 0.0)
            if last_review == 0.0:
                last_review = r.get("timestamp", 0.0)
                logger.debug("fsrs.last_review_fallback id={} using timestamp={}",
                             r.get("id"), last_review)
            try:
                phase = MemoryPhase.safe(r.get("phase", "buffer"))
            except ValueError:
                logger.warning("fsrs_invalid_phase id={} phase={}", r.get("id"), r.get("phase"))
                phase = MemoryPhase.BUFFER
            difficulty = r.get("difficulty", 5.0)
            stability = r.get("stability", 3.0)
            rc = r.get("reinforcement_count", 0)
            state = MemoryState(
                difficulty=difficulty,
                stability=stability,
                phase=phase,
                last_review=last_review,
                created_at=created_at,
                reinforcement_count=rc,
            )

            # 懒迁移：检查 phase 是否需要更新
            # FSRS transition: 21天后 BUFFER→DECAY(rc=0) 或 REINFORCED(rc>0)
            new_phase = self._mm._fsrs._compute_phase(difficulty, stability, state, now)
            if new_phase != phase:
                phase = new_phase
                state = MemoryState(
                    difficulty=difficulty, stability=stability,
                    phase=phase, last_review=last_review,
                    created_at=created_at, reinforcement_count=rc,
                )
                mem_id = r.get("id")
                if mem_id:
                    _migration_needed.append((mem_id, phase.value, difficulty, stability, last_review, rc))

            R = state.retrievability(now)
            # P1-3 修复：本次检索命中即等价于一次"刚被复习"信号，
            # 不应让排序再用旧 last_review 把 R 算到接近 0。
            # 给命中记忆一个 R 下限 0.5（即"刚复习过"的物理含义），
            # touch 后台异步更新 DB 后，下次检索的 last_review 已是本次时间。
            R = max(R, 0.5)
            fsrs_score = self._mm._fsrs.score(similarity, state, now)
            # 放宽过滤阈值：R < 0.01 才完全过滤（原 0.05 过于激进，会过早遗忘有用记忆）
            if R < 0.01:
                logger.debug("fsrs.filtered_out id={} R={:.4f} phase={}",
                             r.get("id"), R, phase.value)
                continue
            r["fluid_score"] = R
            r["fsrs_score"] = fsrs_score
            importance = r.get("importance", 0.5)
            # P0-1 修复：effective_score 不再乘 fsrs_score（含 R 衰减），
            # 避免 R 在 effective_score 与 final_score（0.25 权重）里被双重计入。
            # R 衰减只通过 final_score 的 fluid_score 分量体现一次。
            # 保留 fsrs_score 字段用于可观测性，但不参与 effective_score 计算。
            r["effective_score"] = importance * similarity
            filtered.append(r)

        # 异步批量迁移 phase（fire-and-forget，不阻塞检索返回）
        if _migration_needed:
            _spawn(self._mm._batch_migrate_phase(_migration_needed))
        return filtered

    async def _batch_migrate_phase(self, migrations: list[tuple[int, str, float, float, float, int]]) -> None:
        """异步批量迁移记忆 phase（懒迁移的持久化部分）。

        Args:
            migrations: (mem_id, phase, difficulty, stability, last_review, reinforcement_count)
        """
        try:
            for mem_id, phase, difficulty, stability, last_review, rc in migrations:
                try:
                    await self._mm.memory.update_fsrs_state(
                        mem_id,
                        difficulty=difficulty,
                        stability=stability,
                        phase=phase,
                        last_review=last_review,
                        reinforcement_count=rc,
                    )
                except (KeyError, ValueError, TypeError) as e:
                    logger.debug("fsrs.migrate_failed", mid=mem_id, error=str(e))
            logger.debug("fsrs.batch_migrated", count=len(migrations))
        except Exception as e:
            logger.warning("fsrs.batch_migrate_error", error=str(e))

    def _dedup_by_content_similarity(self, results: list[dict], threshold: float = 0.85) -> list[dict]:
        if len(results) <= 1:
            return results
        kept = []
        for r in results:
            r_bigrams = _char_bigrams(r.get("summary", ""))
            is_dup = False
            for k in kept:
                k_bigrams = _char_bigrams(k.get("summary", ""))
                if not r_bigrams or not k_bigrams:
                    continue
                jaccard = len(r_bigrams & k_bigrams) / len(r_bigrams | k_bigrams)
                if jaccard > threshold:
                    r_is_distilled = r.get("is_raw", 1) == 0
                    k_is_distilled = k.get("is_raw", 1) == 0
                    if r_is_distilled and not k_is_distilled:
                        kept.remove(k)
                        break
                    elif k_is_distilled and not r_is_distilled:
                        is_dup = True
                        break
                    elif r.get("final_score", 0) <= k.get("final_score", 0):
                        is_dup = True
                        break
                    else:
                        kept.remove(k)
                        break
            if not is_dup:
                kept.append(r)
        return kept

    def _compute_recency_boost(self, item: dict) -> float:
        """计算时间新鲜度加成 (0-1)。

        1.0 = 1小时内，0.0 = 很久以前。无时间信息给中等偏低值 0.3。
        小时级粒度，避免同一天内的记忆无法区分新鲜度。
        """
        ts = item.get("timestamp") or item.get("created_at") or item.get("updated_at")
        if not ts:
            return 0.3
        try:
            if isinstance(ts, str):
                dt = _datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            elif isinstance(ts, (int, float)):
                dt = _datetime.datetime.fromtimestamp(ts)
            else:
                return 0.3

            now = _datetime.datetime.now(dt.tzinfo)
            delta = now - dt
            hours_ago = delta.total_seconds() / 3600
            days_ago = delta.days

            if hours_ago <= 1:
                return 1.0
            if hours_ago <= 4:
                return 0.95
            if hours_ago <= 12:
                return 0.90
            if hours_ago <= 24:
                return 0.85
            if days_ago <= 1:
                return 0.70
            if days_ago <= 7:
                return 0.50
            if days_ago <= 30:
                return 0.30
            if days_ago <= 90:
                return 0.20
            return 0.10
        except Exception as e:
            logger.debug("memory_manager.time_decay_failed", error=str(e))
            return 0.3

    async def _compute_final_scores(self, query: str, results: list[dict],
                                      config: Any,
                                      query_entities: set[str] | None = None) -> None:
        """统一评分公式: final = 0.4×rerank + 0.25×R + 0.15×recency + 0.1×kg + 0.1×importance。

        R 为 FSRS-DSR Retrievability（记忆可提取性），替代旧 fluid_score。
        I6: 复用已存储的 entities 字段 + 预提取的 query_entities，
        避免 N+1 次 LLM 调用（原 get_relevance_boost 性能黑洞）。
        """
        if not results:
            return
        # KG 实体匹配加成（复用已提取的 query_entities，避免 N+1 LLM 调用）
        kg_boosts: list[float] = [0.0] * len(results)
        if self._mm.kg:
            try:
                import json
                if query_entities is None:
                    query_entities = await self._mm.kg.get_query_entities(query)
                if query_entities:
                    memory_entities_list: list[list[str]] = []
                    for r in results:
                        raw = r.get("entity_list") or r.get("entities", [])
                        if isinstance(raw, str) and raw:
                            try:
                                raw = json.loads(raw)
                            except (json.JSONDecodeError, TypeError):
                                raw = []
                        memory_entities_list.append(
                            raw if isinstance(raw, list) else [])
                    kg_boosts = await self._mm.kg.get_relevance_boost_fast(
                        query_entities, memory_entities_list)
            except Exception as e:
                logger.debug("memory.kg_boost_failed", error=str(e))
        # 统一评分公式
        for i, r in enumerate(results):
            # rerank_score: 从 rerank_score 或 rrf_score 字段获取，归一化到 0-1
            rerank_raw = r.get("rerank_score", r.get("rrf_score", 0.0))
            rerank_score = _normalize_score(rerank_raw, default=0.0)
            # R: FSRS-DSR Retrievability（_apply_fsrs_scoring 已计算）
            R = _normalize_score(r.get("fluid_score"), default=0.5)
            # kg_boost: KG 召回标记或实体匹配加成（0.5-1.0），否则 0
            kg_boost_val = kg_boosts[i] if i < len(kg_boosts) else 0.0
            if r.get("kg_recall"):
                # KG 召回候选保底 0.5
                kg_boost_val = max(kg_boost_val, 0.5)
            kg_boost = _normalize_score(kg_boost_val, default=0.0)
            # importance: 记忆重要性
            importance = _normalize_score(r.get("importance"), default=0.5)
            # recency: 时间新鲜度加成（近期记忆优先）
            recency = _normalize_score(self._mm._compute_recency_boost(r), default=0.3)
            # 写入中间分数字段（用于调试和可观测性）
            r["rerank_score"] = rerank_score
            r["fluid_score"] = R
            r["kg_boost"] = kg_boost
            r["importance_score"] = importance
            r["recency_boost"] = recency
            # 统一评分公式：从 config 读取权重，WebUI 可实时调整
            # 默认: rerank=0.60, R=0.10, recency=0.10, kg=0.10, importance=0.10
            # bench_memory_recall_vec 实测最优：rerank 主导排序，importance 仅微调
            _w_rerank = getattr(config, 'RAG_RERANK_WEIGHT', 0.60)
            _w_kg = getattr(config, 'RAG_KG_WEIGHT', 0.10)
            _w_importance = getattr(config, 'RAG_IMPORTANCE_WEIGHT', 0.10)
            _w_residual = max(0.0, 1.0 - _w_rerank - _w_kg - _w_importance) / 3.0
            r["final_score"] = (
                rerank_score * _w_rerank
                + R * _w_residual
                + recency * _w_residual
                + kg_boost * _w_kg
                + importance * _w_importance
            )

    async def _apply_topic_trigger(self, query: str, results: list[dict],
                                     k: int,
                                     scope: Any | None = None) -> list[dict]:
        """主动检索 A：话题触发器。

        从 query 抽取 top-N 话题关键词，对每个词做轻量 FTS 检索，
        把"主题相关但未被主路命中"的记忆补充进来，扩大主动联想。
        即使主路 RRF 没召回，话题相关的旧记忆也能浮上来。

        scope 非空时使用 scoped FTS 检索，防止跨用户记忆泄露。
        """
        try:
            # jieba.analyse.extract_tags 是同步 CPU 操作，包到线程池避免阻塞事件循环
            _topic_keywords = await asyncio.to_thread(_extract_topic_keywords, query, top_n=2)
            if not _topic_keywords:
                return results
            _existing_ids = {str(r.get("id", "")) for r in results}
            for _kw in _topic_keywords:
                # 跳过和原 query 完全相同的关键词（已被主路检索过）
                if _kw == query or _kw in query:
                    continue
                if scope is not None:
                    _topic_hits = await self._mm.memory.search_memories_fts_scoped(
                        _kw, scope=scope, limit=1)
                else:
                    _topic_hits = await self._mm.memory.search_memories_fts(_kw, limit=1)
                for _r in _topic_hits:
                    _rid = str(_r.get("id", ""))
                    if _rid and _rid not in _existing_ids:
                        _existing_ids.add(_rid)
                        # 标记话题触发来源，便于调试和上层 prompt 区分
                        _r["topic_trigger"] = _kw
                        # 话题触发的记忆没有 final_score，用基础分填充避免排序异常
                        # 分数设为 0.25：低于主路 reranker 命中（0.4+），但高于去重阈值，
                        # 让话题触发记忆作为"补充联想"出现在结果末尾，扩大主动联想。
                        _r.setdefault("final_score", 0.25)
                        results.append(_r)
            # 修复：移除函数内部的 [:k] 截断
            # 根因：调用方在调用本函数前已 results = results[:k] 截断（见 retrieve_memories_hybrid L1410），
            # 本函数把 topic_hits append 到末尾后，若再 [:k] 截断，刚 append 的 topic_hits 会全部被丢弃，
            # 导致话题触发器形同虚设（死代码）。
            # 修复后：让 topic_hits 超出 k 的部分保留，由调用方的 _dedup_by_content_similarity 处理后
            # 再统一截断到 k+2（见 retrieve_memories_hybrid L1416 后的截断）。
            logger.debug("memory.topic_trigger",
                         keywords=_topic_keywords,
                         added=sum(1 for r in results if r.get("topic_trigger")))
        except Exception as e:
            logger.debug("memory.topic_trigger_failed", error=str(e))
        return results

    async def _batch_touch_memories(self, mem_ids: list[int | str]) -> None:
        """批量递增记忆访问计数并更新 FSRS 状态（passive_use 信号）。

        检索命中后异步调用，不阻塞检索返回。
        - access_count += 1
        - reinforcement_count += 1（通过 FSRS reinforce）
        - last_review = now
        - 根据 phase 迁移规则更新 phase（21天后 buffer→decay，reinforced 后 stability 增长）

        修复：此前 increment_access_count 从未被调用，记忆永远无法进入 PERMANENT 状态，
        FSRS 遗忘曲线也完全不生效。
        """
        if not mem_ids:
            return
        try:
            now = time.time()
            for mid in mem_ids:
                try:
                    mem = await self._mm.memory.get_memory_by_id(mid)
                    if not mem:
                        continue
                    # 构建 MemoryState
                    created_at = mem.get("created_at", 0.0) or mem.get("timestamp", 0.0)
                    last_review = mem.get("last_review", 0.0) or created_at
                    phase_str = mem.get("phase", "buffer")
                    difficulty = mem.get("difficulty", 5.0)
                    stability = mem.get("stability", S_INIT)
                    rc = mem.get("reinforcement_count", 0)

                    state = MemoryState(
                        difficulty=difficulty,
                        stability=stability,
                        phase=MemoryPhase.safe(phase_str),
                        last_review=last_review,
                        created_at=created_at,
                        reinforcement_count=rc,
                    )
                    # PASSIVE_USE 信号：stability 增长但 growth_factor 较低
                    new_state = self._mm._fsrs.reinforce(state, ReinforcementSignal.PASSIVE_USE, now)

                    await self._mm.memory.update_fsrs_state(
                        mid,
                        difficulty=new_state.difficulty,
                        stability=new_state.stability,
                        phase=new_state.phase.value,
                        last_review=now,
                        reinforcement_count=new_state.reinforcement_count,
                    )
                    # 递增 access_count
                    await self._mm.memory.increment_access_count(mid)
                except (KeyError, ValueError, TypeError) as e:
                    logger.debug("memory.touch_failed", mid=mid, error=str(e))
            logger.debug("memory.batch_touched", count=len(mem_ids))
        except Exception as e:
            logger.warning("memory.batch_touch_error", error=str(e))