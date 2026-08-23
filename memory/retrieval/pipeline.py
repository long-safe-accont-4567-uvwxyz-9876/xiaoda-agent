"""检索管线：顶层入口（retrieve_memories）+ 七路召回编排/空路剔除/单路短路/raw 兜底。

拆分自 memory/_retrieval_engine.py（纯移动，行为零变化）。
RetrievalEngine 在此组合各职责 Mixin：
- RecallChannelMixin  (retrieval.channels)        通道实现 FTS/Vec/HyDE/扩散/时间/对话日志/selector
- FusionRerankMixin   (retrieval.fusion)          RRF 融合 + Entity Boost + Reranker 精排
- QueryTransformMixin (retrieval.query_transform) 查询理解/变换/多查询调度
- ScoringTouchMixin   (retrieval.scoring)         FSRS 评分/去重/topic/touch
"""
import asyncio
import time
from typing import Any

from loguru import logger

from core.background_tasks import _spawn
from memory._memory_utils import _stage_log
from memory._retrieval_engine_entity import EntityKgBoostMixin
from memory._retrieval_engine_meta import MemoryMetadataMixin
from memory.retrieval.channels import RecallChannelMixin, RecallChannels
from memory.retrieval.fusion import FusionRerankMixin
from memory.retrieval.query_transform import QueryTransformMixin
from memory.retrieval.scoring import ScoringTouchMixin

__all__ = ["RecallChannels", "RetrievalEngine"]


class RetrievalEngine(RecallChannelMixin, FusionRerankMixin, QueryTransformMixin,
                      ScoringTouchMixin, EntityKgBoostMixin, MemoryMetadataMixin):
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
