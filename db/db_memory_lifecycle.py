"""MemoryDB 的删除/统计/时间检索方法组 —— 拆分自 db/db_memory.py。"""
from __future__ import annotations

from typing import Any

from loguru import logger

from db.db_memory_utils import _sql_placeholders


class LifecycleMixin:
    """删除/统计/时间检索方法组。"""

    async def get_all_memories(self, limit: int = 100) -> Any:
        """获取所有活跃记忆（排除已归档）"""
        cursor = await self._read_conn().execute(
            "SELECT * FROM episodic_memories "
            "WHERE session_id != 'archived' AND status != 'archived' "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_memory(self, memory_id: int, auto_commit: bool = True) -> None:
        """Delete mutable knowledge; ordinary deletion only archives raw records."""
        deleted = await self._conn.execute(
            "DELETE FROM episodic_memories WHERE id=? AND is_raw=0", (memory_id,)
        )
        await self._conn.execute(
            "UPDATE episodic_memories SET status='archived' "
            "WHERE id=? AND is_raw=1",
            (memory_id,),
        )
        if deleted.rowcount > 0:
            try:
                await self._conn.execute(
                    "DELETE FROM episodic_memory_fts WHERE id=?", (memory_id,)
                )
            except Exception as e:
                logger.debug("db_memory.fts_delete_failed", error=str(e))
        if auto_commit:
            await self._conn.commit()

    async def delete_memories_batch(self, memory_ids: list[int],
                                     vector_store: Any = None,
                                     auto_commit: bool = True) -> None:
        """Delete knowledge in bulk and archive raw records without hard deletion."""
        if not memory_ids:
            return
        placeholders = _sql_placeholders(memory_ids)
        cursor = await self._read_conn().execute(
            f"SELECT id, is_raw FROM episodic_memories WHERE id IN ({placeholders})",
            memory_ids,
        )
        rows = await cursor.fetchall()
        knowledge_ids = [int(row["id"]) for row in rows if not row["is_raw"]]
        if vector_store is not None:
            for memory_id in knowledge_ids:
                try:
                    await vector_store.delete(memory_id)
                except Exception as e:
                    logger.error(
                        "db_memory.vec_delete_batch_failed",
                        memory_id=memory_id,
                        error=str(e),
                    )
                    raise
        await self._conn.execute(
            f"UPDATE episodic_memories SET status='archived' "
            f"WHERE is_raw=1 AND id IN ({placeholders})",
            memory_ids,
        )
        if knowledge_ids:
            knowledge_placeholders = _sql_placeholders(knowledge_ids)
            await self._conn.execute(
                f"DELETE FROM episodic_memories "
                f"WHERE is_raw=0 AND id IN ({knowledge_placeholders})",
                knowledge_ids,
            )
            try:
                await self._conn.execute(
                    f"DELETE FROM episodic_memory_fts "
                    f"WHERE id IN ({knowledge_placeholders})",
                    knowledge_ids,
                )
            except Exception as e:
                logger.debug("db_memory.fts_delete_batch_failed", error=str(e))
        if auto_commit:
            await self._conn.commit()

    async def delete_memory_with_vector(self, memory_id: int, vector_store: Any=None,
                                        auto_commit: bool = True) -> None:
        """Delete mutable knowledge with its vector; raw records are only archived."""
        row = await self.get_memory_by_id(memory_id)
        if row and not row.get("is_raw") and vector_store:
            try:
                await vector_store.delete(memory_id)
            except Exception as e:
                logger.error(
                    "db_memory.vec_delete_failed",
                    memory_id=memory_id,
                    error=str(e),
                )
                raise
        await self.delete_memory(memory_id, auto_commit=auto_commit)

    async def hard_delete_raw_for_user_request(
        self,
        memory_id: int,
        vector_store: Any = None,
        auto_commit: bool = True,
    ) -> bool:
        """Hard-delete one raw record only for an explicit user forget request."""
        row = await self.get_memory_by_id(memory_id)
        if not row or not row.get("is_raw"):
            return False
        if vector_store:
            await vector_store.delete(memory_id)
        cursor = await self._conn.execute(
            "DELETE FROM episodic_memories WHERE id=? AND is_raw=1", (memory_id,)
        )
        try:
            await self._conn.execute(
                "DELETE FROM episodic_memory_fts WHERE id=?", (memory_id,)
            )
        except Exception as e:
            logger.debug("db_memory.fts_delete_raw_forget_failed", error=str(e))
        if auto_commit:
            await self._conn.commit()
        return cursor.rowcount > 0

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
            available = getattr(self, "_episodic_columns_cache", None)
            if available is None:
                schema_cursor = await self._read_conn().execute(
                    "PRAGMA table_info(episodic_memories)"
                )
                available = {row[1] for row in await schema_cursor.fetchall()}
                self._episodic_columns_cache = available
            status_filter = " AND status != 'archived'" if "status" in available else ""
            cursor = await self._read_conn().execute(
                "SELECT id FROM episodic_memories "
                "WHERE is_raw=1 AND session_id != 'archived'" + status_filter
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
