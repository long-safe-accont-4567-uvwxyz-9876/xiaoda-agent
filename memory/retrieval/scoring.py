"""FSRS 评分 / 内容去重 / recency 加成 / 统一评分公式 / 话题触发器 / touch。

拆分自 memory/_retrieval_engine.py（纯移动，行为零变化）。
方法经由 self._mm 访问 MemoryManager 依赖与状态，与拆分前语义完全一致。
"""
import asyncio
import datetime as _datetime
import time
from typing import Any

from loguru import logger

from core.background_tasks import _spawn
from memory._memory_utils import (
    _char_bigrams,
    _extract_topic_keywords,
    _normalize_score,
)
from memory.fsrs_model import S_INIT, MemoryPhase, MemoryState, ReinforcementSignal


class ScoringTouchMixin:
    """评分与后处理组：FSRS、去重、final_score、topic trigger、touch。"""

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
            declared_score_kind = r.get("score_kind")
            if declared_score_kind == "source":
                score_kind = "source"
                retrieval_raw = r.get("score", r.get("rrf_score", 0.0))
            elif "rerank_score" in r:
                score_kind = "rerank"
                retrieval_raw = r.get("rerank_score", 0.0)
            elif "rrf_score" in r:
                score_kind = "rrf"
                retrieval_raw = r.get("rrf_score", 0.0)
            else:
                score_kind = r.get("score_kind", "source")
                retrieval_raw = r.get("score", 0.0)
            retrieval_score = _normalize_score(retrieval_raw, default=0.0)
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
            r["score_kind"] = score_kind
            r["retrieval_score"] = retrieval_score
            if score_kind != "rerank":
                r.pop("rerank_score", None)
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
                retrieval_score * _w_rerank
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
