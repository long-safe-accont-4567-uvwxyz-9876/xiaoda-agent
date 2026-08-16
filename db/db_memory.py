import asyncio
import time
from typing import Any

import aiosqlite
from loguru import logger


# FTS 同步失败计数（模块级，便于外部观测/对账）。主表写入成功后 FTS 索引同步
# 失败会导致记忆"查不到"，此前仅 debug 静默吞掉，无任何告警与计数。
_fts_sync_failures = 0


def get_fts_sync_failures() -> int:
    """返回 FTS 同步失败累计计数（便于外部观测/对账）。"""
    return _fts_sync_failures


def _record_fts_sync_failure(event: str, error: Exception) -> None:
    """记录一次 FTS 同步失败：告警 + 递增计数，避免静默丢失索引。"""
    global _fts_sync_failures
    _fts_sync_failures += 1
    logger.warning(event, error=str(error), fts_sync_failures_total=_fts_sync_failures)

from db.db_memory_utils import (  # noqa: E402,F401
    compute_missing_vec_ids, _parse_entity_list, _sql_placeholders,
    _entity_like_conditions, _rows_to_entity_results, _scope_where,
    _rows_to_fts_results,
)
from db.db_memory_child import ChildChunkMixin
from db.db_memory_entity import EntityMixin
from db.db_memory_episodic import EpisodicMixin
from db.db_memory_search import SearchMixin
from db.db_memory_distill import DistillPortraitMixin



class MemoryDB(ChildChunkMixin, EntityMixin, EpisodicMixin, SearchMixin, DistillPortraitMixin):
    """管理情景记忆、画像等记忆数据的读写。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        conn.row_factory = aiosqlite.Row
        # 只读连接池（由 DatabaseManager.init 注入）：检索方法分流使用，
        # 避免 7 路检索通道排队同一个主连接导致总耗时=各通道之和
        self._read_pool: list[aiosqlite.Connection] = []
        self._read_idx = 0

    def _read_conn(self) -> aiosqlite.Connection:
        """取只读连接（round-robin），池空时回退主连接（保留原行为）。"""
        if not self._read_pool:
            return self._conn
        conn = self._read_pool[self._read_idx % len(self._read_pool)]
        self._read_idx += 1
        return conn

    async def commit(self) -> None:
        await self._conn.commit()

    async def rollback(self) -> None:
        """回滚当前事务。用于 auto_commit=False 批量操作失败时清理脏事务状态。

        根因：aiosqlite 单连接共享事务状态，auto_commit=False 操作若不 commit/rollback，
        事务会残留在连接上，后续协程的 DB 操作在脏事务中执行 → "SQL logic error"。
        """
        await self._conn.rollback()


    async def _sync_fts(self, memory_id: int, summary: str, event_label: str, *,
                        delete_first: bool = True, auto_commit: bool = False) -> None:
        """同步 episodic_memory_fts 索引：分词 → (可选 DELETE) → INSERT → (可选 commit)。

        失败统一走 _record_fts_sync_failure（告警 + 计数），不抛出。
        """
        try:
            from db.fts_utils import _tokenize_for_fts
            tokenized = _tokenize_for_fts(summary)
            if tokenized.strip():
                if delete_first:
                    await self._conn.execute(
                        "DELETE FROM episodic_memory_fts WHERE id = ?", (memory_id,))
                await self._conn.execute(
                    "INSERT INTO episodic_memory_fts(id, summary_index) VALUES(?, ?)",
                    (memory_id, tokenized),
                )
                if auto_commit:
                    await self._conn.commit()
        except Exception as e:
            _record_fts_sync_failure(event_label, e)




    async def get_all_memories(self, limit: int = 100) -> Any:
        """获取所有活跃记忆（排除已归档）"""
        cursor = await self._read_conn().execute(
            "SELECT * FROM episodic_memories WHERE session_id != 'archived' ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_memory(self, memory_id: int, auto_commit: bool = True) -> None:
        await self._conn.execute("DELETE FROM episodic_memories WHERE id=?", (memory_id,))
        # 同步删除 FTS 记录
        try:
            await self._conn.execute("DELETE FROM episodic_memory_fts WHERE id=?", (memory_id,))
        except Exception as e:
            logger.debug("db_memory.fts_delete_failed", error=str(e))
        if auto_commit:
            await self._conn.commit()

    async def delete_memories_batch(self, memory_ids: list[int],
                                     vector_store: Any = None,
                                     auto_commit: bool = True) -> None:
        """批量删除记忆，同步批量删除 FTS 索引与向量（消除 N+1 查询）。

        保留 delete_memory 的 FTS 副作用：批量删除主表后批量删除 FTS 记录。
        若传入 vector_store，则先逐条删除向量（memories_vec），避免孤儿向量。
        """
        if not memory_ids:
            return
        # 先清理向量（与 delete_memory_with_vector 保持一致：先向量后主表）
        if vector_store is not None:
            for mid in memory_ids:
                try:
                    await vector_store.delete(mid)
                except Exception as e:
                    logger.error("db_memory.vec_delete_batch_failed",
                                 memory_id=mid, error=str(e))
                    # 单条向量删除失败不阻塞主表清理，但向上抛出由调用方决策
                    raise
        placeholders = _sql_placeholders(memory_ids)
        await self._conn.execute(
            f"DELETE FROM episodic_memories WHERE id IN ({placeholders})",
            memory_ids,
        )
        # 同步批量删除 FTS 记录（保留 delete_memory 的副作用）
        try:
            await self._conn.execute(
                f"DELETE FROM episodic_memory_fts WHERE id IN ({placeholders})",
                memory_ids,
            )
        except Exception as e:
            logger.debug("db_memory.fts_delete_batch_failed", error=str(e))
        if auto_commit:
            await self._conn.commit()

    async def delete_memory_with_vector(self, memory_id: int, vector_store: Any=None, auto_commit: bool = True) -> None:
        """统一删除：先删向量，再删记忆"""
        if vector_store:
            try:
                await vector_store.delete(memory_id)
            except Exception as e:
                logger.error("db_memory.vec_delete_failed", memory_id=memory_id, error=str(e))
                raise
        await self.delete_memory(memory_id, auto_commit=auto_commit)

    async def get_episodic_recent(self, limit: int = 50) -> Any:
        cursor = await self._read_conn().execute(
            """SELECT * FROM episodic_memories
               ORDER BY timestamp DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_episodic_count(self) -> int:
        cursor = await self._read_conn().execute("SELECT COUNT(*) as cnt FROM episodic_memories")
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def get_raw_memory_ids(self) -> list[int]:
        """获取主表所有 is_raw=1 且未归档的记忆 id（用于向量索引对账）。"""
        try:
            cursor = await self._read_conn().execute(
                "SELECT id FROM episodic_memories WHERE is_raw=1 AND session_id != 'archived'"
            )
            rows = await cursor.fetchall()
            return [int(r["id"]) for r in rows]
        except Exception as e:
            logger.warning("db_memory.get_raw_memory_ids_failed", error=str(e))
            return []

    async def get_unmigrated_memories(self, limit: int = 50) -> list[dict]:
        """获取未迁移到 concept_nodes 的记忆"""
        async with self._read_conn().execute(
            """SELECT em.id, em.summary FROM episodic_memories em
               WHERE em.id NOT IN (SELECT source_mem_id FROM concept_nodes
                                   WHERE source_mem_id IS NOT NULL)
               ORDER BY em.timestamp ASC LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [{"id": r["id"], "summary": r["summary"]} for r in rows]

    async def search_memories_by_time(self, start_ts: float, end_ts: float, limit: int = 20) -> list[dict]:
        """按时间范围检索记忆（用于"昨天/上周发生了什么"这类查询）。

        Args:
            start_ts: 起始时间戳（秒）
            end_ts: 结束时间戳（秒）
            limit: 返回条数上限
        """
        return await self._search_by_time_impl(start_ts, end_ts, limit, None, None)

    async def search_memories_fts_with_time(self, query: str, start_ts: float,
                                             end_ts: float, limit: int = 10) -> list[dict]:
        """FTS 全文检索 + 时间范围过滤（混合查询）。"""
        from db.fts_utils import _build_fts_query
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        try:
            cursor = await self._read_conn().execute(
                """SELECT em.*, bm25(episodic_memory_fts) AS score
                   FROM episodic_memory_fts
                   JOIN episodic_memories em ON em.id = episodic_memory_fts.id
                   WHERE episodic_memory_fts MATCH ?
                     AND em.timestamp >= ? AND em.timestamp < ?
                   ORDER BY score ASC, em.importance DESC, em.timestamp DESC
                   LIMIT ?""",
                (fts_query, start_ts, end_ts, limit),
            )
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["score"] = -d.get("score", 0)
                results.append(d)
            return results
        except Exception as e:
            logger.warning("db_memory.fts_time_search_failed", error=str(e))
            return []


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
