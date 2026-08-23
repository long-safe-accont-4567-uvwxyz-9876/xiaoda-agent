"""RRF 融合 + Entity Boost + Reranker 精排。

拆分自 memory/_retrieval_engine.py（纯移动，行为零变化）。
方法经由 self._mm 访问 MemoryManager 依赖与状态，与拆分前语义完全一致。
"""
import time
from typing import Any

from loguru import logger

from memory._memory_utils import _stage_log, reciprocal_rank_fusion
from memory.retrieval.channels import RecallChannels


class FusionRerankMixin:
    """融合与精排组：加权 RRF、Entity Boost、Reranker 精排/降级。"""

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
