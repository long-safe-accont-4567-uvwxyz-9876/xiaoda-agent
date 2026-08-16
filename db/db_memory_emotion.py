"""MemoryDB 的情绪检索 + 定时回忆笔记 + FSRS 方法组 —— 拆分自 db/db_memory.py。"""
from __future__ import annotations

import time

from loguru import logger

from db.db_memory_utils import _sql_placeholders, _scope_where


class EmotionRecallMixin:
    """情绪检索 + 定时回忆笔记 + FSRS 方法组。"""

    # ── 主动检索 B/C：定时回忆笔记 + 情绪触发检索 ────────────────

    async def _search_by_emotion_impl(self, emotion_labels: list[str], limit: int,
                                      scope: Any | None, event_label: str) -> list[dict]:
        if not emotion_labels:
            return []
        # 防注入：标签是有限集合，但仍做白名单校验
        clean_labels = [str(line).strip() for line in emotion_labels if str(line).strip()]
        if not clean_labels:
            return []
        placeholders = _sql_placeholders(clean_labels)
        try:
            if scope is None:
                cursor = await self._read_conn().execute(
                    f"""SELECT * FROM episodic_memories
                        WHERE emotion_label IN ({placeholders})
                          AND session_id != 'archived'
                        ORDER BY importance DESC, timestamp DESC LIMIT ?""",
                    (*clean_labels, limit),
                )
            else:
                scope_where, scope_params = _scope_where(scope)
                cursor = await self._read_conn().execute(
                    f"""SELECT * FROM episodic_memories
                        WHERE emotion_label IN ({placeholders}){scope_where}
                        ORDER BY importance DESC, timestamp DESC LIMIT ?""",
                    [*clean_labels, *scope_params, limit],
                )
            rows = await cursor.fetchall()
            result = [dict(r) for r in rows]
            logger.debug("db_memory.search_by_emotion", scoped=scope is not None,
                         limit=limit, count=len(result))
            return result
        except Exception as e:
            logger.warning(event_label, error=str(e))
            return []

    async def search_memories_by_emotion(self, emotion_labels: list[str],
                                          limit: int = 5) -> list[dict]:
        """按情绪标签检索记忆（用于情绪触发主动检索）。

        Args:
            emotion_labels: 目标情绪标签列表（如 ["喜悦", "happy"]）。
                            DB 中 emotion_label 列可能存中文或英文，调用方应同时传入两种。
            limit: 返回条数上限

        Returns:
            匹配的记忆列表，按 importance DESC, timestamp DESC 排序
        """
        return await self._search_by_emotion_impl(
            emotion_labels, limit, None, "db_memory.search_by_emotion_failed")

    async def search_memories_by_emotion_scoped(self, emotion_labels: list[str],
                                                  limit: int = 5,
                                                  scope: Any | None = None) -> list[dict]:
        """按情绪标签检索记忆 + scope 过滤（mem0 SPEC 优化）。

        Args:
            emotion_labels: 目标情绪标签列表
            limit: 返回条数上限
            scope: Scope 对象。None 时退回无 scope 版本。
        """
        if scope is None:
            return await self.search_memories_by_emotion(emotion_labels, limit)
        return await self._search_by_emotion_impl(
            emotion_labels, limit, scope, "db_memory.search_by_emotion_scoped_failed")

    async def get_high_importance_since(self, start_ts: float,
                                         min_importance: float = 0.6,
                                         limit: int = 50) -> list[dict]:
        """获取自 start_ts 起、重要性 >= min_importance 的记忆（按重要性降序）。

        供定时回忆任务筛选用：单次 SQL 完成时间窗 + 重要性组合查询，
        避免在 Python 层二次过滤。
        """
        try:
            cursor = await self._read_conn().execute(
                """SELECT * FROM episodic_memories
                   WHERE timestamp >= ? AND importance >= ?
                   ORDER BY importance DESC, timestamp DESC LIMIT ?""",
                (start_ts, min_importance, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.get_high_importance_since_failed", error=str(e))
            return []

    async def insert_recall_note(self, *, window_start: float, window_end: float,
                                  summary: str, memory_count: int,
                                  min_importance: float = 0.6,
                                  source_memory_ids: str = "",
                                  title: str = "", tags: str = "",
                                  auto_commit: bool = True) -> int:
        """写入一条定时回忆笔记。

        Args:
            window_start/end: 该笔记覆盖的时间窗（秒级时间戳）
            summary: LLM 蒸馏后的回忆摘要
            memory_count: 参与整理的源记忆条数
            source_memory_ids: 逗号分隔的源记忆 ID 列表（便于追溯）
            title/tags: 可选的标题和标签（便于检索）
        """
        try:
            cursor = await self._conn.execute(
                """INSERT INTO memory_recall_notes
                   (created_at, window_start, window_end, min_importance,
                    source_memory_ids, memory_count, title, summary, tags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (time.time(), window_start, window_end, min_importance,
                 source_memory_ids, memory_count, title, summary, tags),
            )
            note_id = cursor.lastrowid
            if auto_commit:
                await self._conn.commit()
            return note_id or 0
        except Exception as e:
            logger.warning("db_memory.insert_recall_note_failed", error=str(e))
            return 0

    async def get_recent_recall_notes(self, limit: int = 5,
                                       since_ts: float = 0.0) -> list[dict]:
        """获取最近的回忆笔记（按 created_at 降序）。

        Args:
            limit: 返回条数上限
            since_ts: 若 >0，仅返回 created_at >= since_ts 的笔记（用于"最近 N 小时"）
        """
        try:
            if since_ts > 0:
                cursor = await self._read_conn().execute(
                    """SELECT * FROM memory_recall_notes
                       WHERE created_at >= ?
                       ORDER BY created_at DESC LIMIT ?""",
                    (since_ts, limit),
                )
            else:
                cursor = await self._read_conn().execute(
                    """SELECT * FROM memory_recall_notes
                       ORDER BY created_at DESC LIMIT ?""",
                    (limit,),
                )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.get_recent_recall_notes_failed", error=str(e))
            return []


    async def update_fsrs_state(self, memory_id: int, difficulty: float,
                                 stability: float, phase: str,
                                 last_review: float,
                                 reinforcement_count: int,
                                 auto_commit: bool = True) -> None:
        await self._conn.execute(
            """UPDATE episodic_memories
               SET difficulty=?, stability=?, phase=?, last_review=?,
                   reinforcement_count=?
               WHERE id=?""",
            (difficulty, stability, phase, last_review, reinforcement_count, memory_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def get_memories_since(self, since_ts: float,
                                  limit: int = 200) -> list[dict]:
        cursor = await self._read_conn().execute(
            """SELECT * FROM episodic_memories
               WHERE timestamp >= ? AND session_id != 'archived'
               ORDER BY timestamp DESC LIMIT ?""",
            (since_ts, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
