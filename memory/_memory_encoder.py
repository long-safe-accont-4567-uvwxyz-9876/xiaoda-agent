"""MemoryManager 编码/写入/蒸馏相关方法的抽取实现。

本模块中的 MemoryEncoder 持有 MemoryManager 实例的引用（self._mm），
所有编码逻辑通过 self._mm 访问依赖与状态，保证与重构前行为完全一致，
同时避免 `memory_manager` 的反向 import（循环依赖）。
"""
import asyncio
import json
import re
import time
from typing import Any

from loguru import logger

from config import get_agent_display_name
from core.background_tasks import _bg_tasks, _spawn
from memory._memory_utils import (
    RuleBasedMemoryExtractor,
    _char_bigrams,
    _log_task_exception,
    validate_memory_content,
)
from memory.enrichment import (
    CLASSIFICATION_VERSION,
    build_classification_prompt,
    parse_memory_enrichment,
)

from .fsrs_model import (
    S_INIT,
    S_PERMANENT,
    estimate_initial_difficulty,
    should_be_permanent_on_create,
)


class MemoryEncoder:
    """编码引擎：承载 MemoryManager 的编码/写入/蒸馏相关方法逻辑。

    构造时注入 MemoryManager 实例（mm），所有属性与方法访问都经由 self._mm
    转发，从而保留实例级 monkeypatch 与共享状态语义。
    """

    def __init__(self, mm: Any) -> None:
        self._mm = mm

    async def encode_memory(self, context: dict, scope: Any | None = None) -> None:
        """编码记忆（ADD-only 架构）。

        mem0 SPEC 优化：
        1. 写入 is_raw=1 的原始记忆（append-only，不去重，不覆盖）
        2. 异步触发实体提取+链接
        3. 异步触发蒸馏（生成 is_raw=0 的提炼知识，Task 7 实现）

        Args:
            context: 包含 exchanges 列表的上下文
            scope: Scope 对象。None 时使用默认 Scope()。
        """
        # scope 默认值
        if scope is None:
            from memory.scope import Scope
            scope = Scope()

        exchanges = context.get("exchanges", [])
        if not exchanges or len(exchanges) < 2:
            return

        # 同步操作移到线程池：防止事件循环被阻塞导致所有后台任务卡死
        # 根因修复：原代码 5 个 to_thread 串行执行（_generate_summary/validate_memory/
        # security_scan/_estimate_importance/rule_extractor），线程池满时每个排队等待，
        # 总排队时间 = 5 × 单次排队，导致 _encode_task 卡 90s 超时（13:07:04 案例：
        # bg.memory_encode_start 后无 encode_pre_done，_generate_summary 在 to_thread
        # 排队等待 ~90s）。合并为 1 个 to_thread 后只占 1 个线程，5 个操作在线程内
        # 串行执行（纯 CPU <1s），排队时间降为 1 × 单次排队，大幅降低线程池争用。
        _t0 = time.time()
        emotion = context.get("emotion", {}).get("primary", "")

        summary, validation, threat_result, importance, rule_matches, _gen_secs, _sec_secs = \
            await asyncio.to_thread(self._prep_encode_sync, exchanges, context)
        _t1 = time.time()
        # 诊断：to_thread 总耗时（含排队）vs 各步骤纯执行时间
        # 若 total >> gen+sec，说明 to_thread 线程池排队（线程池被并发检索等占用）
        if _t1 - _t0 > 2:
            logger.warning("memory.encode_slow_step",
                           step="prep_encode_total",
                           total_ms=int((_t1 - _t0) * 1000),
                           generate_summary_ms=int(_gen_secs * 1000),
                           security_scan_ms=int(_sec_secs * 1000),
                           hint="to_thread 线程池排队或同步操作慢")

        if validation:
            logger.warning("memory.safety_blocked", reason=validation)
            return

        if not threat_result.is_safe and threat_result.action == "block":
            logger.warning("memory.security_blocked", threat=threat_result.threat_type)
            return

        if rule_matches:
            best_rule = max(rule_matches, key=lambda r: r["importance"])
            importance = max(importance, best_rule["importance"])

        _t4 = time.time()
        logger.info("memory.encode_pre_done",
                    prep_ms=int((_t4 - _t0) * 1000),
                    security_ms=int(_sec_secs * 1000))
        try:
            # 写入候选审计表
            candidate_id = await self._mm.memory.insert_consolidation_candidate(
                source="encode",
                kind=rule_matches[0]["kind"] if rule_matches else "episodic",
                summary=summary,
                confidence=rule_matches[0]["confidence"] if rule_matches else 0.5,
                importance=importance,
            )
            _t5 = time.time()
            logger.info("memory.encode_candidate_done",
                        candidate_ms=int((_t5 - _t4) * 1000))

            # ADD-only: 写入 is_raw=1 的原始记忆（不去重，不覆盖）
            mem_id = await self._mm.memory.insert_episodic_memory(
                summary=summary,
                importance=importance,
                emotion_label=emotion,
                scope=scope,
                is_raw=1,
            )
            _t6 = time.time()
            logger.info("memory.encode_episodic_done",
                        episodic_ms=int((_t6 - _t5) * 1000), mem_id=mem_id)

            # Initialize FSRS state for new memory
            now_ts = time.time()
            initial_difficulty = estimate_initial_difficulty(summary, emotion)
            # P2+ 事实类永久：含生日/电话/地址等关键词的记忆直接置 PERMANENT，
            # 跳过 BUFFER/DECAY 衰减，避免关键事实被遗忘。
            # stability 直接给 S_PERMANENT，phase=permanent，rc=1（视为已强化）。
            if should_be_permanent_on_create(summary):
                init_stability = S_PERMANENT
                init_phase = "permanent"
                init_rc = 1
                logger.info("memory.fact_permanent",
                            mem_id=mem_id, summary=summary[:50])
            else:
                init_stability = S_INIT
                init_phase = "buffer"
                init_rc = 0
            await self._mm.memory.update_fsrs_state(
                mem_id,
                difficulty=initial_difficulty,
                stability=init_stability,
                phase=init_phase,
                last_review=now_ts,
                reinforcement_count=init_rc,
            )

            # 标记候选已应用
            await self._mm.memory.mark_candidate_applied(candidate_id, mem_id)

            # ContextNest A3: 记录初始版本哈希链 (tamper-evident)
            if self._mm._governance:
                # CodeRabbit 复审修复：治理版本行必须在调度 _indexing_task 之前提交，
                # 否则异步任务失败时治理版本行可能永远不会被提交。
                # 根因修复：用 db.write_transaction() 串行化多语句写事务，async with 退出时
                # 即 commit（在 _indexing_task 调度前）；异常/取消由 finally shield(rollback)。
                try:
                    async with self._mm.db.write_transaction():
                        await self._mm._governance.record_initial_version(mem_id, summary, auto_commit=False)
                except Exception as e:
                    logger.debug("memory.governance_init_failed", error=str(e))

            # ── 索引层（vec + concept_graph + children）改为 fire-and-forget ──
            # 根因：这些操作涉及 embed API（6.9s）+ auto_link 遍历全部节点 + insert_child_chunk 循环，
            # 长时间占用共享 aiosqlite 连接，导致其他后台任务（flush_costs/auto_note/extract_instincts）
            # 的 DB 操作排队等待 45s+ 超时。episodic memory 已写入，索引层是优化层，可异步补建。
            # 改为 create_task 后，编码主流程立即继续到 entity/distill/save_state 并快速返回，释放 DB 连接。
            _idx_task = asyncio.create_task(
                self._indexing_task(mem_id, summary, importance, exchanges))
            _bg_tasks.add(_idx_task)
            _idx_task.add_done_callback(_bg_tasks.discard)
            _idx_task.add_done_callback(_log_task_exception)

            # ── mem0 SPEC: 异步触发实体提取+链接 ──
            self._schedule_entity_extraction(mem_id, summary, scope)

            await self._finalize_encode(
                mem_id, summary, scope, importance, emotion, exchanges
            )
        except Exception as e:
            logger.warning("memory.encode_failed", error=str(e))

        self._schedule_kg_extraction(summary, scope)

    def _prep_encode_sync(self, exchanges: list[dict], context: dict) -> tuple:
        """单线程完成所有同步预处理：摘要→安全过滤→安全扫描→重要性→规则提取。

        返回 (summary, validation, threat, importance, rule_matches,
               gen_secs, sec_secs)；validation/threat 非空时后续字段为 None。
        """
        _s0 = time.time()
        summary = self._mm._generate_summary(exchanges)
        _s1 = time.time()
        validation = validate_memory_content(summary)
        if validation:
            return summary, validation, None, 0.0, [], _s1 - _s0, 0.0
        _s2 = time.time()
        from security.security import SecurityFilter
        security = self._mm._security_filter or SecurityFilter()
        threat = security.scan_threats(summary, scope="strict")
        _s3 = time.time()
        if not threat.is_safe and threat.action == "block":
            return summary, validation, threat, 0.0, [], _s1 - _s0, _s3 - _s2
        importance = self._mm._estimate_importance(exchanges, context)
        # 规则提取增强重要性
        user_msg = ""
        assistant_msg = ""
        for msg in exchanges[-6:]:
            if msg.get("role") == "user":
                user_msg += msg.get("content", "") + " "
            elif msg.get("role") == "assistant":
                assistant_msg += msg.get("content", "") + " "
        rule_extractor = RuleBasedMemoryExtractor()
        rule_matches = rule_extractor.extract(user_msg, assistant_msg)
        return summary, validation, threat, importance, rule_matches, _s1 - _s0, _s3 - _s2

    async def _indexing_task(self, mem_id: int, summary: str,
                             importance: float, exchanges: list[dict]) -> None:
        """索引层 fire-and-forget 任务：vec_upsert → concept_graph → 父子 chunk。"""
        _it0 = time.time()
        # 1. vec_upsert（15s 超时）
        if self._mm.vec and summary:
            try:
                await asyncio.wait_for(self._mm.vec.upsert(mem_id, summary), timeout=15.0)
            except asyncio.TimeoutError:
                logger.error("degradation_triggered memory.encode_vec_upsert_timeout "
                             "hint=vec_upsert 15s 超时，跳过向量索引（episodic 已保存）")
            except Exception as e:
                logger.warning("memory.initial_vec_upsert_failed", error=str(e))
        _it1 = time.time()
        if _it1 - _it0 > 3:
            logger.warning("memory.encode_slow_step", step="vec_upsert",
                           elapsed_ms=int((_it1 - _it0) * 1000))

        # 2. concept_graph 双写（15s 超时）
        if self._mm.concept_graph and mem_id:
            try:
                await asyncio.wait_for(
                    self._mm.concept_graph.remember(summary, source_mem_id=mem_id),
                    timeout=15.0)
            except asyncio.TimeoutError:
                logger.error("degradation_triggered memory.encode_concept_timeout "
                             "hint=concept_graph 15s 超时，跳过（lazy_migrate 可补）")
            except Exception as e:
                logger.warning("memory.concept_dual_write_failed", error=str(e))
        _it2 = time.time()
        if _it2 - _it1 > 3:
            logger.warning("memory.encode_slow_step", step="concept_graph",
                           elapsed_ms=int((_it2 - _it1) * 1000))

        # 3. 父子Chunk: 生成并写入子chunk（整体 25s 超时保护）
        import config as _cfg
        if getattr(_cfg, 'PARENT_CHILD_CHUNK_ENABLED', True):
            try:
                async def _do_children():
                    children = await asyncio.to_thread(
                        self._mm._split_into_children, exchanges, mem_id, summary)
                    if not children or not self._mm.vec:
                        return
                    indexed = await asyncio.wait_for(
                        self._mm._insert_indexed_children(mem_id, children, importance),
                        timeout=20.0,
                    )
                    if indexed:
                        logger.debug("memory.child_chunks_created",
                                     parent_id=mem_id, count=len(children))
                await asyncio.wait_for(_do_children(), timeout=25.0)
            except asyncio.TimeoutError:
                logger.error("degradation_triggered memory.encode_children_section_timeout "
                             "hint=子chunk生成+写入 25s 整体超时，跳过（episodic 已保存）")
            except Exception as e:
                logger.warning("memory.child_chunk_failed", error=str(e))
        _it3 = time.time()
        logger.info("memory.indexing_done",
                    total_ms=int((_it3 - _it0) * 1000), mem_id=mem_id)

    def _schedule_entity_extraction(self, mem_id: int, summary: str, scope: Any) -> None:
        """异步调度实体提取+链接（fire-and-forget）。"""
        if self._mm.entity_extractor and self._mm.entity_store:
            try:
                _entity_task = asyncio.create_task(
                    self._mm._extract_and_link_entities(mem_id, summary, scope)
                )
                _bg_tasks.add(_entity_task)
                _entity_task.add_done_callback(_bg_tasks.discard)
                def _log_entity_exception(t: asyncio.Task) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc:
                        logger.warning("memory.entity_async_failed", error=str(exc))
                _entity_task.add_done_callback(_log_entity_exception)
            except Exception as e:
                logger.debug("memory.entity_spawn_failed", error=str(e))

    @staticmethod
    def _build_full_text(exchanges: list[dict]) -> str:
        full_text_parts = []
        for msg in exchanges[-6:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and content:
                full_text_parts.append(f"你说了：{content}")
            elif role == "assistant" and content:
                full_text_parts.append(f"我回应：{content}")
        return "；".join(full_text_parts)[:3000]

    def _schedule_enrichment_pipeline(
        self,
        mem_id: int,
        summary: str,
        scope: Any,
        importance: float,
        emotion: str,
        exchanges: list[dict],
    ) -> None:
        """Schedule one strongly referenced enrichment-then-distill task."""
        try:
            task = asyncio.create_task(
                self._run_enrichment_pipeline(
                    mem_id, summary, scope, importance, emotion, exchanges
                )
            )
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)
            task.add_done_callback(_log_task_exception)
        except Exception as e:
            logger.debug("memory.enrichment_pipeline_spawn_failed", error=str(e))

    def _schedule_legacy_enrichment_and_distill(
        self,
        mem_id: int,
        summary: str,
        scope: Any,
        importance: float,
        emotion: str,
        exchanges: list[dict],
    ) -> None:
        """Preserve the disabled-switch fixed point with independent tasks."""
        try:
            enrichment_task = asyncio.create_task(
                self._mm._enrich_memory_async(mem_id, exchanges)
            )
            _bg_tasks.add(enrichment_task)
            enrichment_task.add_done_callback(_bg_tasks.discard)
            enrichment_task.add_done_callback(_log_task_exception)
            if self._mm.distiller:
                distill_task = asyncio.create_task(
                    self._mm._distill_to_knowledge(
                        mem_id,
                        summary,
                        scope,
                        importance,
                        emotion,
                        full_text=self._build_full_text(exchanges),
                    )
                )
                _bg_tasks.add(distill_task)
                distill_task.add_done_callback(_bg_tasks.discard)
                distill_task.add_done_callback(_log_task_exception)
        except Exception as e:
            logger.debug("memory.legacy_pipeline_spawn_failed", error=str(e))

    async def _run_enrichment_pipeline(
        self,
        mem_id: int,
        summary: str,
        scope: Any,
        importance: float,
        emotion: str,
        exchanges: list[dict],
    ) -> None:
        """Run enrichment serially before distillation; cancellation stops the chain."""
        try:
            await self._mm._enrich_memory_async(mem_id, exchanges)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("memory.enrichment_pipeline_failed", error=str(e))
        if self._mm.distiller:
            await self._mm._distill_to_knowledge(
                mem_id,
                summary,
                scope,
                importance,
                emotion,
                full_text=self._build_full_text(exchanges),
            )

    async def _finalize_encode(self, mem_id: int, summary: str, scope: Any,
                               importance: float, emotion: str,
                               exchanges: list[dict]) -> None:
        """编码收尾：更新状态、失效缓存、保存状态文件并调度受控流水线。"""
        self._mm._last_encode_time = time.time()
        logger.info("memory.encoded", summary=summary[:80], importance=importance, is_raw=1)

        # 冷启动路由: 新记忆写入后失效计数缓存, 下次检索立即感知档位变化
        self._mm.invalidate_memory_count_cache()

        # G13+P1-6: 失效读缓存总线（查询缓存 fire-and-forget + 扩散激活 recall 缓存）
        self._mm.invalidate_read_caches()

        # 同步文件写入移到线程池（USB 盘 I/O 可能慢）
        await asyncio.to_thread(self._mm._save_state_json, summary, importance, emotion)

        import config as _cfg

        if getattr(_cfg, "MEMORY_TYPE_ENRICHMENT_ENABLED", False):
            self._schedule_enrichment_pipeline(
                mem_id, summary, scope, importance, emotion, exchanges
            )
        else:
            self._schedule_legacy_enrichment_and_distill(
                mem_id, summary, scope, importance, emotion, exchanges
            )

    def _schedule_kg_extraction(self, summary: str, scope: Any) -> None:
        """异步调度知识图谱提取（fire-and-forget）。"""
        if self._mm.kg and summary:
            # KG 提取改为 fire-and-forget：auto_extract_and_merge 内部调用 LLM（10s+8s 超时）
            # + DB merge 操作，直接 await 会阻塞主编码流程 10-20s。
            # 知识图谱是增强层，可异步补建，不应阻塞记忆编码主流程。
            try:
                _kg_task = asyncio.create_task(
                    self._mm.kg.auto_extract_and_merge(summary, scope=scope)
                )
                # 强引用：与索引/实体/蒸馏任务一致加入 _bg_tasks，
                # 否则任务仅由局部变量持有，可能在完成前被 GC 回收
                _bg_tasks.add(_kg_task)
                _kg_task.add_done_callback(_bg_tasks.discard)
                _kg_task.add_done_callback(_log_task_exception)
            except Exception as e:
                logger.debug("memory.kg_spawn_failed", error=str(e))

    async def _extract_and_link_entities(self, memory_id: int, summary: str,
                                          scope: Any) -> None:
        """异步提取实体并建立反向链接（mem0 SPEC 优化）。

        Args:
            memory_id: 原始记忆 ID
            summary: 记忆摘要文本
            scope: Scope 对象
        """
        if not self._mm.entity_extractor or not self._mm.entity_store:
            return
        try:
            # 提取实体
            entities = await self._mm.entity_extractor.extract(summary, importance=0.5)
            if not entities:
                return
            # 链接到记忆
            linked = await self._mm.entity_store.link_entities(memory_id, entities, scope=scope)
            logger.debug("memory.entities_linked",
                         memory_id=memory_id, count=linked)
        except Exception as e:
            logger.debug("memory.extract_link_entities_failed", error=str(e))

    async def _distill_to_knowledge(self, raw_id: int, summary: str,
                                     scope: Any, importance: float = 0.5,
                                     emotion: str = "", _retry: int = 0,
                                     full_text: str = "") -> None:
        """将原始记忆蒸馏为提炼知识（允许 UPDATE/DELETE）。

        mem0 SPEC 优化 ADD-only 架构：
        1. 调用 MemoryDistiller 蒸馏
        2. 检查是否已有相似的提炼知识（is_raw=0, 同 scope）
        3a. 有相似 → UPDATE（合并/增强）
        3b. 无相似 → 新建提炼知识（is_raw=0）

        蒸馏失败时异步重试最多 2 次（间隔 30s/60s），避免免费模型超时导致记忆丢失。

        Args:
            raw_id: 原始记忆 ID
            summary: 原始记忆摘要
            scope: Scope 对象
            importance: 重要性
            emotion: 情感标签
            _retry: 当前重试次数（内部使用）
            full_text: 完整对话原文（蒸馏失败时回填用）
        """
        if not self._mm.distiller:
            return
        try:
            try:
                existing = await self._mm.memory.get_memory_by_id(raw_id)
                if existing:
                    ds = existing.get("distill_status", "")
                    if ds == "failed":
                        # 允许重试：清除 failed 状态，重新蒸馏
                        logger.info("memory.distill_retry", raw_id=raw_id)
                        await self._mm.memory.update_distill_status(raw_id, "")
            except Exception as e:
                logger.debug("memory_manager.distill_status_check_failed", error=str(e))
            # 1. 蒸馏（调用已有 MemoryDistiller，传入单条记忆）
            distilled = await self._mm.distiller.distill([{"summary": summary, "timestamp": time.time()}])
            if not distilled or not distilled.strip():
                if _retry < 2:
                    delay = 30 * (_retry + 1)
                    logger.info("memory.distill_empty_retry", raw_id=raw_id,
                               retry=_retry + 1, delay_s=delay)
                    _captured_scope = scope
                    _captured_full_text = full_text

                    async def _retry_distill() -> None:
                        await asyncio.sleep(delay)
                        await self._mm._distill_to_knowledge(
                            raw_id, summary, _captured_scope,
                            importance, emotion, _retry + 1,
                            full_text=_captured_full_text,
                        )

                    _spawn(_retry_distill())
                else:
                    logger.warning("memory.distill_exhausted_retries", raw_id=raw_id)
                    await self._mm._save_fallback_raw(raw_id, summary, full_text)
                return

            # 2. 检查是否已有相似的提炼知识
            similar = await self._mm._find_similar_knowledge(distilled, scope=scope)

            if similar:
                # 3a. 有相似知识 → UPDATE（合并）
                updated = await self._mm._update_knowledge(
                    similar["id"], distilled, raw_id, scope
                )
                if not updated:
                    return
            else:
                # 3b. 无相似知识 → 新建提炼知识（is_raw=0）
                raw = await self._mm.memory.get_memory_by_id(raw_id) or {}
                knowledge_importance = max(
                    importance, float(raw.get("importance", importance))
                )
                knowledge_id = await self._mm.memory.insert_episodic_memory(
                    summary=distilled,
                    importance=knowledge_importance,
                    emotion_label=emotion,
                    scope=scope,
                    is_raw=0,
                    memory_type=raw.get("memory_type", "event"),
                    phase=raw.get("phase", "buffer"),
                    stability=raw.get("stability", S_INIT),
                    reinforcement_count=raw.get("reinforcement_count", 0),
                )
                if self._mm.vec and knowledge_id:
                    try:
                        await self._mm.vec.upsert(knowledge_id, distilled)
                    except Exception as e:
                        logger.debug("memory.distill_vec_upsert_failed", error=str(e))
                self._schedule_reconciliation_candidate(
                    knowledge_id, raw_id, scope
                )
                logger.info("memory.distilled_new",
                           raw_id=raw_id, knowledge_id=knowledge_id)
            # 蒸馏完成后失效查询缓存：新提炼知识需被后续检索感知
            # G13+P1-6: 失效读缓存总线
            self._mm.invalidate_read_caches()
        except Exception as e:
            logger.warning("memory.distill_to_knowledge_failed",
                          raw_id=raw_id, retry=_retry, error=str(e))
            if _retry < 2:
                delay = 30 * (_retry + 1)
                _captured_scope = scope
                _captured_full_text = full_text

                async def _retry_distill_exc() -> None:
                    await asyncio.sleep(delay)
                    await self._mm._distill_to_knowledge(
                        raw_id, summary, _captured_scope,
                        importance, emotion, _retry + 1,
                        full_text=_captured_full_text,
                    )

                _spawn(_retry_distill_exc())
            else:
                await self._mm._save_fallback_raw(raw_id, summary, full_text)

    def _schedule_reconciliation_candidate(
        self, knowledge_id: int, raw_id: int, scope: Any
    ) -> None:
        """Register a candidate, then fire one independent worker pass."""
        repository = getattr(self._mm.memory, "reconciliation", None)
        if repository is None:
            return

        async def _register() -> None:
            try:
                from memory.reconciliation_policy import configured_policy
                from memory.reconciliation_service import run_pending_once

                effective_mode, _ = configured_policy()
                await repository.register_candidate(
                    knowledge_id,
                    raw_id,
                    user_id=scope.user_id,
                    agent_id=scope.agent_id,
                    mode=effective_mode,
                )
                _spawn(
                    run_pending_once(
                        self._mm,
                        user_id=scope.user_id,
                        agent_id=scope.agent_id,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "memory.reconciliation_enqueue_failed",
                    knowledge_id=knowledge_id,
                    raw_id=raw_id,
                    error=str(exc),
                )

        _spawn(_register())

    async def _save_fallback_raw(self, raw_id: int, truncated_summary: str,
                                  full_text: str) -> None:
        """Mark fallback state without modifying append-only raw content."""
        try:
            await self._mm.memory.update_distill_status(raw_id, "failed")
            self._mm.invalidate_read_caches()
        except Exception as e:
            logger.error("degradation_triggered memory.fallback_save_failed raw_id={} error={}",
                         raw_id, str(e))

    async def _find_similar_knowledge(self, summary: str,
                                       scope: Any) -> dict | None:
        """查找相似的提炼知识（is_raw=0, 同 scope）。

        使用 FTS 召回候选 + 字符 bigram Jaccard 相似度阈值过滤，
        避免 FTS 的宽松 token 匹配导致不相关知识被误合并
        （如 "用户喜欢Python" 误匹配 "用户喜欢Java"）。

        Args:
            summary: 待查重的摘要
            scope: Scope 对象
        Returns:
            相似的记忆 dict，或 None
        """
        try:
            candidates = await self._mm.memory.search_memories_fts_scoped(
                summary, scope=scope, limit=5, is_raw=0
            )
            if not candidates:
                return None
            query_bigrams = _char_bigrams(summary)
            if not query_bigrams:
                return None
            for c in candidates:
                candidate_bigrams = _char_bigrams(c.get("summary", ""))
                if not candidate_bigrams:
                    continue
                intersection = query_bigrams & candidate_bigrams
                union = query_bigrams | candidate_bigrams
                jaccard = len(intersection) / len(union) if union else 0.0
                if jaccard >= 0.4:
                    return c
            return None
        except Exception as e:
            logger.debug("memory.find_similar_knowledge_failed", error=str(e))
            return None

    @staticmethod
    def _merge_canonical_state(existing: dict, raw: dict) -> dict:
        """Choose the stronger derived state without weakening canonical knowledge."""
        existing_type = existing.get("memory_type", "event")
        raw_type = raw.get("memory_type", "event")
        if raw_type == "fact" or existing_type == "fact":
            memory_type = "fact"
        elif raw_type in {"relation", "affect"}:
            memory_type = (
                raw_type
                if existing_type in {"event", "instruction"}
                else existing_type
            )
        elif raw_type == "instruction" and existing_type == "event":
            memory_type = "instruction"
        else:
            memory_type = existing_type

        phase_rank = {
            "buffer": 0,
            "decayed": 0,
            "reinforced": 1,
            "permanent": 2,
            "archived": 0,
        }
        existing_phase = existing.get("phase", "buffer")
        raw_phase = raw.get("phase", "buffer")
        phase = (
            raw_phase
            if phase_rank.get(raw_phase, 0) > phase_rank.get(existing_phase, 0)
            else existing_phase
        )
        if memory_type == "fact":
            phase = "permanent"
        elif memory_type in {"affect", "relation"} and phase_rank.get(phase, 0) < 1:
            phase = "reinforced"

        stability = max(
            float(existing.get("stability", S_INIT)),
            float(raw.get("stability", S_INIT)),
        )
        reinforcement_count = max(
            int(existing.get("reinforcement_count", 0)),
            int(raw.get("reinforcement_count", 0)),
        )
        importance = max(
            float(existing.get("importance", 0.5)),
            float(raw.get("importance", 0.5)),
        )
        if memory_type == "fact":
            stability = max(stability, S_PERMANENT)
            reinforcement_count = max(reinforcement_count, 1)
        elif memory_type in {"affect", "relation"}:
            importance = max(importance, 0.6)
            stability = max(stability, S_INIT)
            reinforcement_count = max(reinforcement_count, 1)
        return {
            "memory_type": memory_type,
            "importance": importance,
            "phase": phase,
            "stability": stability,
            "reinforcement_count": reinforcement_count,
        }

    async def _update_knowledge(self, knowledge_id: int, new_content: str,
                                 raw_id: int, scope: Any) -> None:
        """更新已有提炼知识（合并新信息）。

        Args:
            knowledge_id: 提炼知识 ID
            new_content: 新蒸馏的内容
            raw_id: 原始记忆 ID（用于溯源）
            scope: Scope 对象
        """
        try:
            # 1. 获取已有知识
            existing = await self._mm.memory.get_memory_by_id(knowledge_id)
            if not existing:
                return

            # 2. LLM 合并新旧知识
            merged = await self._mm.distiller.merge_knowledge(
                existing=existing.get("summary", ""),
                new_content=new_content,
            )

            # 3. 更新记录（version+1，追加 source_raw_ids 溯源链）
            import json
            existing_meta = {}
            try:
                raw_meta = existing.get("metadata_json") or "{}"
                if isinstance(raw_meta, str):
                    existing_meta = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                existing_meta = {}
            source_raw_ids: list = existing_meta.get("source_raw_ids", [])
            if raw_id not in source_raw_ids:
                source_raw_ids.append(raw_id)
            raw = await self._mm.memory.get_memory_by_id(raw_id) or {}
            merged_state = self._merge_canonical_state(existing, raw)
            async with self._mm.db.write_transaction():
                updated = await self._mm.memory.merge_memory_knowledge_state(
                    memory_id=knowledge_id,
                    summary=merged,
                    metadata_json=json.dumps({
                        "source_raw_ids": source_raw_ids,
                        "merged_at": time.time(),
                    }),
                    auto_commit=False,
                    strict=True,
                    **merged_state,
                )
                if not updated:
                    raise RuntimeError(
                        f"canonical memory {knowledge_id} was not updated"
                    )
                if self._mm._governance:
                    await self._mm._governance.record_version_update(
                        knowledge_id,
                        merged,
                        auto_commit=False,
                        strict=True,
                    )
                repository = getattr(self._mm.memory, "reconciliation", None)
                if repository is not None:
                    await repository.add_knowledge_source(
                        knowledge_id, raw_id, transactional=False
                    )

            # 4. 向量更新
            if self._mm.vec:
                try:
                    await self._mm.vec.upsert(knowledge_id, merged)
                except Exception as e:
                    logger.debug("memory.update_knowledge_vec_failed", error=str(e))

            try:
                self._mm.invalidate_read_caches()
            except Exception as e:
                logger.debug("memory.update_knowledge_cache_invalidation_failed", error=str(e))

            logger.info("memory.knowledge_updated",
                       knowledge_id=knowledge_id, raw_id=raw_id)
        except Exception as e:
            logger.warning("memory.update_knowledge_failed", error=str(e))

    def _generate_summary(self, exchanges: list[dict]) -> str:
        parts = []
        for msg in exchanges[-6:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            # Defense-in-depth: strip emotion tags that may have leaked into history
            if content:
                content = re.sub(r'\[emotion:[^\]]*\]', '', content)
                content = re.sub(r'\[\w+/stickers:[^\]]*\]', '', content)
                content = content.strip()
            if role == "user" and content:
                # P0 修复（数据库原文蹦出 + 旁观者视角根因）：
                # 原格式 "用户说: xxx" 是数据库字段名格式，LLM 回复时会模仿。
                # 改为第一人称叙事格式 "你说了：xxx"（用户=你），避免 LLM 直接引用 "用户说:"。
                parts.append(f"你说了：{content[:400]}")
            elif role == "assistant" and content:
                # 标记为回复内容，避免被误认为事实性记忆
                # 第一人称（小妲=我）让 LLM 把记忆当成自己的经历，而非旁观者复述
                parts.append(f"我回应：{content[:400]}")

        total_budget = 1500
        joined = "；".join(parts)
        if len(joined) <= total_budget:
            return joined
        kept = []
        remaining = total_budget
        for part in reversed(parts):
            if remaining <= 0:
                break
            if len(part) <= remaining:
                kept.append(part)
                remaining -= len(part) + 1
            else:
                kept.append(part[:remaining])
                remaining = 0
        kept.reverse()
        return "；".join(kept)

    def _split_into_children(self, exchanges: list[dict], parent_id: int,
                             parent_summary: str) -> list[dict]:
        """将对话轮次切分为子chunk，带重叠窗口和 Contextual Retrieval 前缀。

        Returns:
            [{content, embed_content, chunk_type, weight, overlap_hash}, ...]
        """
        import hashlib

        import config as _cfg

        overlap_chars = getattr(_cfg, 'CHILD_CHUNK_OVERLAP_CHARS', 30)
        max_len = getattr(_cfg, 'CHILD_CHUNK_SEGMENT_MAX_LEN', 200)
        max_children = getattr(_cfg, 'CHILD_CHUNK_MAX_PER_PARENT', 10)
        contextual = getattr(_cfg, 'CONTEXTUAL_RETRIEVAL_ENABLED', True)

        children: list[dict] = []
        prev_tail = ""

        for msg in exchanges[-8:]:  # 扩大到8轮
            if len(children) >= max_children:
                break
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                continue

            prefix = "用户说：" if role == "user" else ""
            text = f"{prefix}{content[:max_len]}"

            # 重叠窗口
            overlap_hash = ""
            if prev_tail and overlap_chars > 0:
                overlap = prev_tail[-overlap_chars:]
                overlap_hash = hashlib.sha256(overlap.encode()).hexdigest()[:8]
                text = f"{overlap}…{text}"

            # Contextual Retrieval: 注入父摘要前缀到 embed_content（受 CONTEXTUAL_RETRIEVAL_ENABLED 控制）
            # Bug 修复：原版读取 contextual 标志后未使用，"始终注入"导致与 enrich 路径
            # （_split_into_enrich_children 受同一开关控制）行为不一致——用户设
            # CONTEXTUAL_RETRIEVAL_ENABLED=false 时本函数仍注入前缀。现尊重开关，与 enrich 路径一致。
            if contextual and parent_summary:
                embed_content = f"[上下文: {parent_summary[:80]}] {text}"
            else:
                embed_content = text

            weight = 1.0 if role == 'user' else 0.8

            children.append({
                'content': text,
                'embed_content': embed_content,
                'chunk_type': 'segment',
                'weight': weight,
                'overlap_hash': overlap_hash,
            })

            prev_tail = text

        return children

    async def _enrich_memory_async(self, mem_id: int, exchanges: list[dict]) -> None:
        """Run the existing single LLM call and safely enrich one raw memory."""
        import config as _cfg

        enrichment_enabled = getattr(
            _cfg, "MEMORY_TYPE_ENRICHMENT_ENABLED", False
        )
        text = self._build_enrichment_text(exchanges)
        if not text or len(text) < 10:
            return

        prompt = build_classification_prompt(text)

        try:
            result = await self._mm.distiller._call_free_model(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
            )
            if not result:
                raise ValueError("empty enrichment response")
            parsed = parse_memory_enrichment(result)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(
                "memory.enrich_failed",
                error=str(e),
                error_type=type(e).__name__,
            )
            if enrichment_enabled:
                await self._persist_memory_classification(
                    mem_id,
                    memory_type="event",
                    classification_status="fallback",
                    llm_importance=None,
                )
            return

        data = {
            "entities": list(parsed.entities),
            "event_type": parsed.event_type,
            "metadata": parsed.metadata,
            "summary": parsed.summary,
        }
        await self._mm.memory.update_memory_enrichment(
            mem_id,
            summary="",
            entities=json.dumps(data["entities"], ensure_ascii=False),
            event_type=parsed.event_type,
            metadata_json=json.dumps(parsed.metadata, ensure_ascii=False),
        )

        if enrichment_enabled:
            await self._persist_memory_classification(
                mem_id,
                memory_type=parsed.memory_type,
                classification_status=parsed.classification_status,
                llm_importance=parsed.importance,
            )

        logger.info(
            "memory.enriched",
            mem_id=mem_id,
            event_type=parsed.event_type,
            memory_type=parsed.memory_type if enrichment_enabled else "disabled",
            entities_count=len(parsed.entities),
        )
        await self._write_enrichment_child_chunks(
            mem_id, data, "", parsed.summary
        )
        self._mm.invalidate_query_cache()

    async def _persist_memory_classification(
        self,
        mem_id: int,
        *,
        memory_type: str,
        classification_status: str,
        llm_importance: float | None,
    ) -> None:
        """Apply type invariants without lowering rule-derived importance or phase."""
        current = await self._mm.memory.get_memory_by_id(mem_id)
        if not current:
            return
        importance = float(current.get("importance", 0.5))
        if llm_importance is not None:
            importance = max(importance, llm_importance)
        phase = current.get("phase", "buffer")
        stability = float(current.get("stability", S_INIT))
        reinforcement_count = int(current.get("reinforcement_count", 0))

        if memory_type == "fact":
            phase = "permanent"
            stability = max(stability, S_PERMANENT)
            reinforcement_count = max(reinforcement_count, 1)
        elif memory_type in {"affect", "relation"}:
            importance = max(importance, 0.6)
            if phase != "permanent":
                phase = "reinforced"
            stability = max(stability, S_INIT)
            reinforcement_count = max(reinforcement_count, 1)

        await self._mm.memory.update_memory_classification(
            mem_id,
            memory_type=memory_type,
            importance=importance,
            classification_status=classification_status,
            classification_version=CLASSIFICATION_VERSION,
            classified_at=time.time(),
            phase=phase,
            stability=stability,
            reinforcement_count=reinforcement_count,
        )

    @staticmethod
    def _build_enrichment_text(exchanges: list[dict]) -> str:
        """构建对话文本（比 _generate_summary 保留更多内容，给 LLM 更多上下文）。"""
        lines = []
        for msg in exchanges[-6:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and content:
                lines.append(f"用户: {content[:300]}")
            elif role == "assistant" and content:
                lines.append(f"{get_agent_display_name('xiaoda')}: {content[:300]}")
        return "\n".join(lines)

    @staticmethod
    def _extract_enrichment_json(result: str) -> dict:
        """去除 think 标签 + 提取 JSON（LLM 可能返回带 markdown 代码块的）。"""
        import re
        if "<think>" in result:
            result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()
        json_str = result
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0]
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0]
        json_str = json_str.strip()
        import json
        return json.loads(json_str)

    async def _write_enrichment_child_chunks(self, mem_id: int, data: dict,
                                             update_summary: str, new_summary: str) -> None:
        """Phase 2: enrichment 子chunk化 — 实体/决策/话题写入子chunk + 批量嵌入。"""
        import config as _enrich_cfg
        if not (getattr(_enrich_cfg, 'PARENT_CHILD_CHUNK_ENABLED', True) and self._mm.vec):
            return
        try:
            enrich_parent_summary = update_summary or new_summary or ""
            enrich_children: list[tuple[int, str]] = []
            entity_list = data.get("entities", [])
            meta = data.get("metadata", {})
            decision = meta.get("decision", "").strip()
            topic = meta.get("topic", "").strip()

            # 实体子chunk
            for ent in entity_list[:5]:  # 最多5个实体
                ent_str = str(ent).strip()
                if not ent_str or len(ent_str) < 2:
                    continue
                content = f"实体: {ent_str}"
                embed_content = (f"[上下文: {enrich_parent_summary[:80]}] {content}"
                                 if getattr(_enrich_cfg, 'CONTEXTUAL_RETRIEVAL_ENABLED', True)
                                 else content)
                cid = await self._mm.memory.insert_child_chunk(
                    parent_id=mem_id, content=content,
                    embed_content=embed_content, chunk_type='entity',
                    importance=0.7)
                enrich_children.append((cid, embed_content))

            # 决策子chunk
            if decision and len(decision) >= 5:
                content = f"决策: {decision}"
                embed_content = (f"[上下文: {enrich_parent_summary[:80]}] {content}"
                                 if getattr(_enrich_cfg, 'CONTEXTUAL_RETRIEVAL_ENABLED', True)
                                 else content)
                cid = await self._mm.memory.insert_child_chunk(
                    parent_id=mem_id, content=content,
                    embed_content=embed_content, chunk_type='decision',
                    importance=0.9)
                enrich_children.append((cid, embed_content))

            # 话题子chunk
            if topic and len(topic) >= 2:
                content = f"话题: {topic}"
                embed_content = (f"[上下文: {enrich_parent_summary[:80]}] {content}"
                                 if getattr(_enrich_cfg, 'CONTEXTUAL_RETRIEVAL_ENABLED', True)
                                 else content)
                cid = await self._mm.memory.insert_child_chunk(
                    parent_id=mem_id, content=content,
                    embed_content=embed_content, chunk_type='topic',
                    importance=0.6)
                enrich_children.append((cid, embed_content))

            # 批量嵌入
            if enrich_children:
                await self._mm.vec.batch_upsert_children(enrich_children)
                logger.debug("memory.enrich_child_chunks",
                             parent_id=mem_id, count=len(enrich_children))
        except Exception as e:
            logger.debug("memory.enrich_child_failed",
                         error=str(e), error_type=type(e).__name__)

    def _estimate_importance(self, exchanges: list[dict], context: dict) -> float:
        importance = 0.3

        emotion = context.get("emotion", {})
        if emotion.get("primary") in ("悲伤", "愤怒", "焦虑", "恐惧"):
            importance += 0.3
        elif emotion.get("primary") in ("喜悦", "感激", "期待"):
            importance += 0.1

        total_len = sum(len(m.get("content", "")) for m in exchanges)
        if total_len > 500:
            importance += 0.2

        return min(importance, 1.0)
