"""MemoryManager 维护/调度/回忆相关方法的抽取实现。

本模块中的 MemoryMaintenance 持有 MemoryManager 实例的引用（self._mm），
所有维护/调度/回忆逻辑通过 self._mm 访问依赖与状态，保证与重构前行为完全一致，
同时避免 `memory_manager` 的反向 import（循环依赖）。
"""
from typing import Any
import asyncio
import time
from loguru import logger

from db.db_memory import compute_missing_vec_ids
from utils.atomic_write import atomic_json_write


class MemoryMaintenance:
    """维护引擎：承载 MemoryManager 的维护/调度/回忆公开/私有方法逻辑。

    构造时注入 MemoryManager 实例（mm），所有属性与方法访问都经由 self._mm
    转发，从而保留实例级 monkeypatch 与共享状态语义。
    """

    def __init__(self, mm: Any) -> None:
        self._mm = mm

    async def reconcile_vector_index_gap(self) -> int:
        """对账检测：统计主表已落盘（is_raw=1）但向量索引缺失的记忆数量。

        进程崩溃时 is_raw=1 的记忆已写入主表，但 fire-and-forget 的 vec.upsert
        可能未完成，导致向量检索搜不到这些记忆。返回缺失数量，>0 时记录 warning
        提示需要重建向量索引。
        """
        if not self._mm.vec or not getattr(self._mm.vec, "_vec_conn", None):
            return 0
        get_raw_ids = getattr(self._mm.memory, "get_raw_memory_ids", None)
        get_vec_ids = getattr(self._mm.vec, "get_memories_vec_rowids", None)
        if get_raw_ids is None or get_vec_ids is None:
            return 0
        try:
            raw_ids = await get_raw_ids()
            if not raw_ids:
                return 0
            vec_ids = await asyncio.to_thread(get_vec_ids)
            missing = compute_missing_vec_ids(list(raw_ids), set(vec_ids))
            if missing:
                logger.warning("memory.vector_index_gap_detected",
                               missing_count=len(missing),
                               hint="is_raw=1 记忆已落主表但向量索引缺失，需重建")
            return len(missing)
        except Exception as e:
            logger.warning("memory.vector_reconcile_failed", error=str(e))
            return 0

    async def try_idle_encode(self, context: dict, force: bool = False,
                              scope: Any | None = None) -> None:
        now = time.time()
        if not force and not self._mm._pending_encode:
            return
        if not force and now - self._mm._last_message_time < self._mm.IDLE_THRESHOLD:
            return
        if now - self._mm._last_encode_time < self._mm.ENCODE_COOLDOWN:
            return

        generation = self._mm._encode_generation
        try:
            if scope is None:
                from memory.scope import current_scope
                scope = current_scope()
            await self._mm.encode_memory(context, scope=scope)
        except BaseException:
            self._mm._pending_encode = True
            raise
        self._mm._pending_encode = self._mm._encode_generation != generation

    def _save_state_json(self, summary: str, importance: float, emotion: str) -> None:
        """原子写入记忆状态到 JSON 文件"""
        try:
            from pathlib import Path
            # 使用用户数据目录，避免写入 _MEIPASS 只读目录
            try:
                from config import MEMORY_STATE_DIR
                state_dir = MEMORY_STATE_DIR
            except ImportError:
                # 避免在 PyInstaller frozen 模式下写入 _MEIPASS 只读目录
                state_dir = Path.home() / ".ai-agent" / "memory_state"
            state_dir.mkdir(parents=True, exist_ok=True)
            state_path = str(state_dir / "memory_state.json")
            data = {
                "last_summary": summary[:500],
                "last_importance": importance,
                "last_emotion": emotion,
                "last_encode_time": self._mm._last_encode_time,
            }
            atomic_json_write(state_path, data)
        except Exception as e:
            logger.warning("memory.state_json_save_failed", error=str(e))

    async def distill_old_memories(self) -> int:
        """P3: 蒸馏超过阈值的旧记忆为摘要。

        查询未蒸馏记忆数量，若超过 MAX_EPISODIC_MEMORIES 阈值，
        取最旧的 MEMORY_DISTILL_BATCH 条蒸馏为摘要，并标记为 distilled=1。

        Returns:
            本次蒸馏的记忆条数（0 表示未触发或无候选）
        """
        import config
        max_memories = getattr(config, "MAX_EPISODIC_MEMORIES", 200)
        batch = getattr(config, "MEMORY_DISTILL_BATCH", 30)

        try:
            count = await self._mm.memory.get_episodic_count_undistilled()
            if count <= max_memories:
                return 0

            candidates = await self._mm.memory.get_distill_candidates(limit=batch)
            if not candidates:
                return 0

            summary = await self._mm.distiller.distill(candidates)
            if not summary:
                logger.warning("memory.distill_empty_summary", candidates=len(candidates))
                return 0

            # 写入摘要表 + 标记原记忆为已蒸馏（同一事务，避免重复蒸馏）
            # 根因修复：用 db.write_transaction() 串行化多语句写事务，失败/取消由 finally
            # shield(rollback) 统一处理，异常传播到外层 except 记录并 return 0。
            memory_ids = [c["id"] for c in candidates if c.get("id") is not None]
            async with self._mm.db.write_transaction():
                await self._mm.memory.insert_memory_summary(
                    summary_text=summary, memory_count=len(candidates), auto_commit=False,
                )
                await self._mm.memory.mark_memories_distilled(memory_ids, auto_commit=False)

            logger.info("memory.distilled",
                        count=len(candidates),
                        undistilled_before=count,
                        summary_len=len(summary))
            # G13: 失效扩散激活 recall 缓存
            if getattr(self._mm, 'spreading_engine', None) and self._mm.spreading_engine:
                self._mm.spreading_engine.clear_cache()
            return len(candidates)
        except Exception as e:
            logger.warning("memory.distill_failed", error=str(e))
            return 0

    async def run_scheduled_recall(self, *, hours_back: float = 3.0,
                                    min_importance: float = 0.6,
                                    min_memories: int = 3) -> int:
        """主动检索 B：定时回忆任务。

        从 hours_back 小时前到现在，取重要性 >= min_importance 的记忆，
        若数量 >= min_memories，调用 distill_recall 整理成"回忆笔记"，
        写入 memory_recall_notes 表。后续 retrieve_memories/build_memory_prompt
        可主动拉取这些笔记作为高密度上下文。

        Args:
            hours_back: 回顾窗口的小时数（默认 3h）
            min_importance: 重要性下限（默认 0.6）
            min_memories: 触发整理的最小记忆条数（少于则跳过本次）

        Returns:
            本次整理的源记忆条数（0 表示未触发或无候选）
        """
        try:
            now = time.time()
            window_start = now - hours_back * 3600.0
            candidates = await self._mm.memory.get_high_importance_since(
                start_ts=window_start,
                min_importance=min_importance,
                limit=50,
            )
            if len(candidates) < min_memories:
                logger.debug("memory.recall_skipped",
                             reason="insufficient_memories",
                             count=len(candidates),
                             min=min_memories)
                return 0

            # 调用叙事风格蒸馏
            note = await self._mm.distiller.distill_recall(candidates)
            if not note:
                logger.warning("memory.recall_empty_note", candidates=len(candidates))
                return 0

            # 从候选中提取标签（前 5 个实体的并集，便于日后按标签检索）
            tags_set: list[str] = []
            seen = set()
            for c in candidates[:10]:
                ents = (c.get("entities") or "").strip()
                if ents:
                    for e in ents.split("|"):
                        ent = e.strip()
                        if ent and ent not in seen and len(ent) >= 2:
                            seen.add(ent)
                            tags_set.append(ent)
                        if len(tags_set) >= 5:
                            break
                if len(tags_set) >= 5:
                    break
            tags = "|".join(tags_set)

            # 用第一条记忆的时间戳作为 window_start 的实际值（更精确）
            try:
                real_start = min(float(c.get("timestamp", now)) for c in candidates)
            except (ValueError, TypeError):
                real_start = window_start

            source_ids = ",".join(str(c.get("id", "")) for c in candidates if c.get("id"))

            note_id = await self._mm.memory.insert_recall_note(
                window_start=real_start,
                window_end=now,
                summary=note,
                memory_count=len(candidates),
                min_importance=min_importance,
                source_memory_ids=source_ids,
                title=f"回忆笔记 {time.strftime('%m-%d %H:%M', time.localtime(real_start))}~{time.strftime('%H:%M', time.localtime(now))}",
                tags=tags,
            )

            logger.info("memory.recall_note_created",
                        note_id=note_id,
                        source_count=len(candidates),
                        window_hours=hours_back,
                        note_len=len(note))
            return len(candidates)
        except Exception as e:
            logger.warning("memory.run_scheduled_recall_failed", error=str(e))
            return 0

    async def retrieve_comfort_memories(self, limit: int = 2,
                                          scope: Any | None = None) -> list[dict]:
        """主动检索 C：情绪触发 — 检索"安抚性记忆"。

        当检测到用户情绪低落（valence=negative）时，主动检索带正面情绪标签
        的历史记忆（喜悦/happy），作为"安抚素材"注入上下文，让小妲能
        回忆起"曾经让用户开心的事"来温柔陪伴。

        DB 中 emotion_label 列历史数据是中文（喜悦），统一模式后是英文（happy），
        所以两种标签都查，避免漏检。

        Args:
            limit: 返回条数上限（默认 2，避免上下文膨胀）
            scope: Scope 对象。None 时使用默认 Scope()。

        Returns:
            安抚性记忆列表，每条带 emotion_trigger="comfort" 标记
        """
        if scope is None:
            from memory.scope import Scope
            scope = Scope()
        try:
            # 正面情绪标签：中文 + 英文双查
            # 喜悦 = happy；害羞有时也带正面色彩（用户被逗笑），但保守起见只取喜悦
            comfort_labels = ["喜悦", "happy"]
            results = await self._mm.memory.search_memories_by_emotion_scoped(
                comfort_labels, limit=limit, scope=scope
            )
            for r in results:
                # 标记来源，便于 prompt 层区分和调试
                r["emotion_trigger"] = "comfort"
            if results:
                logger.debug("memory.comfort_memories_retrieved",
                             count=len(results),
                             labels=comfort_labels)
            return results
        except Exception as e:
            logger.warning("memory.retrieve_comfort_memories_failed", error=str(e))
            return []

    async def build_memory_prompt(self, recent_limit: int = 20,
                                   summary_limit: int = 5,
                                   include_recall_note: bool = True) -> str:
        """P3: 构建记忆提示文本，优先使用蒸馏摘要 + 近期未蒸馏记忆。

        Args:
            recent_limit: 近期未蒸馏记忆条数上限
            summary_limit: 蒸馏摘要条数上限
            include_recall_note: 是否在提示开头注入最近一条定时回忆笔记

        Returns:
            记忆提示文本，无内容时返回空串。
        """
        try:
            summaries = await self._mm.memory.get_memory_summaries(limit=summary_limit)
            recent = await self._mm.memory.get_recent_undistilled(limit=recent_limit)
            recall_notes = []
            if include_recall_note:
                # 只取最近 1 条回忆笔记（避免上下文膨胀）
                recall_notes = await self._mm.memory.get_recent_recall_notes(limit=1)
        except Exception as e:
            logger.debug("memory.build_prompt_failed", error=str(e))
            return ""

        if not summaries and not recent and not recall_notes:
            return ""

        parts = []
        # 定时回忆笔记放在最前——用自然叙述式而非列表
        if recall_notes:
            parts.append("（最近想到的事）")
            for rn in recall_notes:
                text = (rn.get("summary") or "").strip()
                if text:
                    parts.append(text)

        if summaries:
            parts.append("（以前发生过的事）")
            for s in summaries:
                text = (s.get("summary_text") or "").strip()
                if text:
                    parts.append(text)

        if recent:
            if parts:
                parts.append("（最近经历的事）")
            else:
                parts.append("（记得的事）")
            for r in reversed(recent):  # 按时间升序展示
                text = (r.get("summary") or "").strip()
                if text:
                    parts.append(text)

        return "\n".join(parts)
