from typing import Any

import asyncio

import aiosqlite
from loguru import logger

from db.db_memory_child import ChildChunkMixin
from db.db_memory_distill import DistillPortraitMixin
from db.db_memory_emotion import EmotionRecallMixin
from db.db_memory_entity import EntityMixin
from db.db_memory_episodic import EpisodicMixin
from db.db_memory_lifecycle import LifecycleMixin
from db.db_memory_search import SearchMixin
from db.db_memory_utils import (  # noqa: F401
    _entity_like_conditions,
    _parse_entity_list,
    _rows_to_entity_results,
    _rows_to_fts_results,
    _scope_where,
    _sql_placeholders,
    active_memory_visibility_sql,
    compute_missing_vec_ids,
)

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


class MemoryDB(ChildChunkMixin, EntityMixin, EpisodicMixin, SearchMixin, DistillPortraitMixin, LifecycleMixin, EmotionRecallMixin):
    """管理情景记忆、画像等记忆数据的读写。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        conn.row_factory = aiosqlite.Row
        # 只读连接池（由 DatabaseManager.init 注入）：检索方法分流使用，
        # 避免 7 路检索通道排队同一个主连接导致总耗时=各通道之和
        self._read_pool: list[aiosqlite.Connection] = []
        self._read_idx = 0
        self.reconciliation: Any = None
        # 数据库小任务B-1：事务守卫（DatabaseManager.init 经 attach_tx_guard
        # 注入；未注入的独立实例保持历史裸 execute+commit 行为）
        self._tx_guard: Any = None

    def attach_tx_guard(self, tx_active: Any) -> None:
        """注入事务守卫：感知外层 write_transaction 并共享连接级写锁。"""
        from db.db_memory_utils import WriteTxGuard
        self._tx_guard = WriteTxGuard(self._conn, tx_active)

    def _read_conn(self) -> aiosqlite.Connection:
        """取只读连接（round-robin），池空时回退主连接（保留原行为）。"""
        if not self._read_pool:
            return self._conn
        conn = self._read_pool[self._read_idx % len(self._read_pool)]
        self._read_idx += 1
        return conn

    async def get_visible_memory_id_page(
        self,
        scope: Any,
        *,
        before_id: int | None = None,
        page_size: int = 1000,
    ) -> dict[str, Any]:
        """Return one recent-first keyset page inside a privacy boundary."""
        bounded_size = max(1, min(int(page_size), 5000))
        visibility = active_memory_visibility_sql("em")
        scope_where, scope_params = _scope_where(
            scope, table="em", include_archived_filter=True,
        )
        cursor_where = " AND em.id < ?" if before_id is not None else ""
        cursor_params = [int(before_id)] if before_id is not None else []
        cursor = await self._read_conn().execute(
            f"SELECT em.id FROM episodic_memories em WHERE {visibility}"
            f"{scope_where}{cursor_where} ORDER BY em.id DESC LIMIT ?",
            [*scope_params, *cursor_params, bounded_size + 1],
        )
        rows = await cursor.fetchall()
        ids = [int(row["id"]) for row in rows[:bounded_size]]
        return {
            "ids": ids,
            "next_cursor": ids[-1] if ids else before_id,
            "has_more": len(rows) > bounded_size,
        }

    async def get_visible_child_id_page(
        self,
        scope: Any,
        *,
        before_id: int | None = None,
        page_size: int = 1000,
    ) -> dict[str, Any]:
        """Return child rowids whose parents are visible, recent-first by child id."""
        bounded_size = max(1, min(int(page_size), 5000))
        visibility = active_memory_visibility_sql("em")
        scope_where, scope_params = _scope_where(
            scope, table="em", include_archived_filter=True,
        )
        cursor_where = " AND mc.id < ?" if before_id is not None else ""
        cursor_params = [int(before_id)] if before_id is not None else []
        cursor = await self._read_conn().execute(
            "SELECT mc.id FROM memory_child_chunks mc "
            "JOIN episodic_memories em ON em.id=mc.parent_id "
            f"WHERE {visibility}{scope_where}{cursor_where} "
            "ORDER BY mc.id DESC LIMIT ?",
            [*scope_params, *cursor_params, bounded_size + 1],
        )
        rows = await cursor.fetchall()
        ids = [int(row["id"]) for row in rows[:bounded_size]]
        return {
            "ids": ids,
            "next_cursor": ids[-1] if ids else before_id,
            "has_more": len(rows) > bounded_size,
        }

    async def get_visible_memory_ids(
        self, scope: Any, limit: int | None = None
    ) -> list[int]:
        """Compatibility collector over keyset pages; production recall streams pages."""
        collected: list[int] = []
        cursor: int | None = None
        while limit is None or len(collected) < limit:
            page_size = min(1000, limit - len(collected)) if limit else 1000
            page = await self.get_visible_memory_id_page(
                scope, before_id=cursor, page_size=page_size
            )
            collected.extend(page["ids"])
            if not page["has_more"] or not page["ids"]:
                break
            cursor = page["next_cursor"]
        return collected

    async def get_visible_child_ids(
        self, scope: Any, limit: int | None = None
    ) -> list[int]:
        """Compatibility collector over child keyset pages."""
        collected: list[int] = []
        cursor: int | None = None
        while limit is None or len(collected) < limit:
            page_size = min(1000, limit - len(collected)) if limit else 1000
            page = await self.get_visible_child_id_page(
                scope, before_id=cursor, page_size=page_size
            )
            collected.extend(page["ids"])
            if not page["has_more"] or not page["ids"]:
                break
            cursor = page["next_cursor"]
        return collected

    async def get_visible_memories_by_ids(
        self, ids: list[int], *, scope: Any | None = None
    ) -> list[dict]:
        """Batch-read only memories visible to normal retrieval channels."""
        if not ids:
            return []
        placeholders = _sql_placeholders(ids)
        visibility = active_memory_visibility_sql("em")
        where = f"em.id IN ({placeholders}) AND {visibility}"
        params: list[Any] = list(ids)
        if scope is not None:
            scope_where, scope_params = _scope_where(
                scope, table="em", include_archived_filter=True,
            )
            where += scope_where
            params.extend(scope_params)
        try:
            cursor = await self._read_conn().execute(
                f"SELECT em.* FROM episodic_memories em WHERE {where}", params
            )
            return [dict(row) for row in await cursor.fetchall()]
        except Exception as exc:
            if "no such table: memory_knowledge_sources" not in str(exc) and "no such column" not in str(exc):
                raise
            return await self.get_memories_by_ids(ids)

    async def get_retrieval_epoch(self, scope: Any) -> int:
        if self.reconciliation is None:
            return 0
        return await self.reconciliation.get_retrieval_epoch(
            scope.user_id, scope.agent_id
        )

    async def register_reconciliation_candidate(
        self, knowledge_id: int, raw_id: int, scope: Any
    ) -> int:
        if self.reconciliation is None:
            return 0
        return await self.reconciliation.register_candidate(
            knowledge_id,
            raw_id,
            user_id=scope.user_id,
            agent_id=scope.agent_id,
        )

    async def _search_fts_impl(self, query: str, limit: int, scope: Any | None,
                               is_raw: int | None, event_label: str) -> list[dict]:
        from db.fts_utils import _build_fts_query

        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        visibility = active_memory_visibility_sql("em")
        where = f"episodic_memory_fts MATCH ? AND {visibility}"
        params: list[Any] = [fts_query]
        if scope is not None:
            scope_where, scope_params = _scope_where(
                scope, table="em", include_archived_filter=True,
            )
            where += scope_where
            params.extend(scope_params)
        if is_raw is not None:
            where += " AND em.is_raw=?"
            params.append(is_raw)
        params.append(limit)
        try:
            cursor = await self._read_conn().execute(
                f"""SELECT em.*, bm25(episodic_memory_fts) AS score
                    FROM episodic_memory_fts
                    JOIN episodic_memories em ON em.id=episodic_memory_fts.id
                    WHERE {where}
                    ORDER BY score ASC, em.importance DESC, em.timestamp DESC
                    LIMIT ?""",
                params,
            )
            return _rows_to_fts_results(await cursor.fetchall())
        except Exception as exc:
            if "no such table: memory_knowledge_sources" not in str(exc) and "no such column" not in str(exc):
                logger.warning(event_label, error=str(exc))
                return []
            return await super()._search_fts_impl(
                query, limit, scope, is_raw, event_label
            )

    async def _search_by_time_impl(self, start_ts: float, end_ts: float,
                                   limit: int, scope: Any | None,
                                   is_raw: int | None) -> list[dict]:
        visibility = active_memory_visibility_sql("em")
        where = f"em.timestamp>=? AND em.timestamp<? AND {visibility}"
        params: list[Any] = [start_ts, end_ts]
        if scope is not None:
            scope_where, scope_params = _scope_where(
                scope, table="em", include_archived_filter=True,
            )
            where += scope_where
            params.extend(scope_params)
        if is_raw is not None:
            where += " AND em.is_raw=?"
            params.append(is_raw)
        params.append(limit)
        try:
            cursor = await self._read_conn().execute(
                f"SELECT em.* FROM episodic_memories em WHERE {where} "
                "ORDER BY em.timestamp DESC LIMIT ?",
                params,
            )
            return [dict(row) for row in await cursor.fetchall()]
        except Exception as exc:
            if "no such table: memory_knowledge_sources" not in str(exc) and "no such column" not in str(exc):
                raise
            return await super()._search_by_time_impl(
                start_ts, end_ts, limit, scope, is_raw
            )

    async def search_memories_vec_scoped(self, memory_ids: list[int], scope: Any,
                                          limit: int = 50,
                                          is_raw: int | None = None) -> list[dict]:
        rows = await self.get_visible_memories_by_ids(memory_ids, scope=scope)
        if is_raw is not None:
            rows = [row for row in rows if row.get("is_raw") == is_raw]
        return rows[:limit]

    async def search_child_fts(
        self, query: str, limit: int = 20, scope: Any | None = None
    ) -> list[dict]:
        from db.fts_utils import _build_fts_query

        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        visibility = active_memory_visibility_sql("em")
        where = f"memory_child_chunks_fts MATCH ? AND {visibility}"
        params: list[Any] = [fts_query]
        if scope is not None:
            scope_where, scope_params = _scope_where(
                scope, table="em", include_archived_filter=True,
            )
            where += scope_where
            params.extend(scope_params)
        params.append(limit)
        try:
            cursor = await self._read_conn().execute(
                f"""SELECT mc.id, mc.parent_id, mc.content, mc.chunk_type,
                            mc.importance, bm25(memory_child_chunks_fts) AS score
                    FROM memory_child_chunks_fts fts
                    JOIN memory_child_chunks mc ON fts.rowid=mc.id
                    JOIN episodic_memories em ON em.id=mc.parent_id
                    WHERE {where}
                    ORDER BY score LIMIT ?""",
                params,
            )
            return [dict(row) for row in await cursor.fetchall()]
        except Exception as exc:
            if "no such table: memory_knowledge_sources" not in str(exc) and "no such column" not in str(exc):
                logger.warning("db_memory.child_fts_search_failed", error=str(exc))
                return []
            return await super().search_child_fts(query, limit, scope=scope)

    async def get_child_parent_ids(self, child_ids: list[int]) -> list[int]:
        if not child_ids:
            return []
        placeholders = _sql_placeholders(child_ids)
        visibility = active_memory_visibility_sql("em")
        try:
            cursor = await self._read_conn().execute(
                f"""SELECT DISTINCT mc.parent_id FROM memory_child_chunks mc
                    JOIN episodic_memories em ON em.id=mc.parent_id
                    WHERE mc.id IN ({placeholders}) AND {visibility}""",
                child_ids,
            )
            return [int(row["parent_id"]) for row in await cursor.fetchall()]
        except Exception as exc:
            if "no such table: memory_knowledge_sources" not in str(exc) and "no such column" not in str(exc):
                raise
            return await super().get_child_parent_ids(child_ids)

    async def get_memories_by_entity_names_scoped(self, entity_names: list[str],
                                                   scope: Any, limit: int = 10,
                                                   is_raw: int | None = 0) -> list[dict]:
        if not entity_names:
            return []
        placeholders = _sql_placeholders(entity_names)
        visibility = active_memory_visibility_sql("em")
        where = f"me.name IN ({placeholders}) AND {visibility}"
        params: list[Any] = list(entity_names)
        scope_where, scope_params = _scope_where(
            scope, table="em", include_archived_filter=True,
        )
        where += scope_where
        params.extend(scope_params)
        if is_raw is not None:
            where += " AND em.is_raw=?"
            params.append(is_raw)
        params.append(limit)
        try:
            cursor = await self._read_conn().execute(
                f"""SELECT DISTINCT em.* FROM entity_memory_links eml
                    JOIN memory_entities me ON me.id=eml.entity_id
                    JOIN episodic_memories em ON em.id=eml.memory_id
                    WHERE {where}
                    ORDER BY em.importance DESC, em.timestamp DESC LIMIT ?""",
                params,
            )
            return [dict(row) for row in await cursor.fetchall()]
        except Exception as exc:
            if "no such table: memory_knowledge_sources" not in str(exc) and "no such column" not in str(exc):
                logger.warning("db_memory.entity_names_scoped_search_failed", error=str(exc))
                return []
            return await super().get_memories_by_entity_names_scoped(
                entity_names, scope, limit, is_raw
            )

    async def _search_entities_impl(self, entity_names: list[str], limit: int,
                                    scope: Any | None,
                                    event_label: str) -> list[dict]:
        if not entity_names:
            return []
        conditions, params = _entity_like_conditions(entity_names)
        visibility = active_memory_visibility_sql("em")
        where = f"em.session_id!='archived' AND ({conditions}) AND {visibility}"
        if scope is not None:
            scope_where, scope_params = _scope_where(
                scope, table="em", include_archived_filter=False,
            )
            where += scope_where
            params.extend(scope_params)
        try:
            cursor = await self._read_conn().execute(
                f"SELECT em.* FROM episodic_memories em WHERE {where} "
                "ORDER BY em.importance DESC, em.timestamp DESC LIMIT ?",
                [*params, limit],
            )
            return _rows_to_entity_results(await cursor.fetchall())
        except Exception as exc:
            if "no such table: memory_knowledge_sources" not in str(exc) and "no such column" not in str(exc):
                logger.warning(event_label, error=str(exc))
                return []
            return await super()._search_entities_impl(
                entity_names, limit, scope, event_label
            )

    async def _search_by_emotion_impl(self, emotion_labels: list[str], limit: int,
                                      scope: Any | None,
                                      event_label: str) -> list[dict]:
        clean = [str(label).strip() for label in emotion_labels if str(label).strip()]
        if not clean:
            return []
        placeholders = _sql_placeholders(clean)
        visibility = active_memory_visibility_sql("em")
        where = f"em.emotion_label IN ({placeholders}) AND {visibility}"
        params: list[Any] = list(clean)
        if scope is not None:
            scope_where, scope_params = _scope_where(
                scope, table="em", include_archived_filter=True,
            )
            where += scope_where
            params.extend(scope_params)
        else:
            where += " AND em.session_id!='archived'"
        params.append(limit)
        try:
            cursor = await self._read_conn().execute(
                f"SELECT em.* FROM episodic_memories em WHERE {where} "
                "ORDER BY em.importance DESC, em.timestamp DESC LIMIT ?",
                params,
            )
            return [dict(row) for row in await cursor.fetchall()]
        except Exception as exc:
            if "no such table: memory_knowledge_sources" not in str(exc) and "no such column" not in str(exc):
                logger.warning(event_label, error=str(exc))
                return []
            return await super()._search_by_emotion_impl(
                emotion_labels, limit, scope, event_label
            )

    async def get_recent_undistilled(self, limit: int = 20) -> list[dict]:
        visibility = active_memory_visibility_sql("em")
        try:
            cursor = await self._read_conn().execute(
                f"""SELECT em.id, em.timestamp, em.summary, em.importance
                    FROM episodic_memories em
                    WHERE em.distilled=0 AND {visibility}
                    ORDER BY em.timestamp DESC LIMIT ?""",
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]
        except Exception as exc:
            if "no such table: memory_knowledge_sources" not in str(exc) and "no such column" not in str(exc):
                raise
            return await super().get_recent_undistilled(limit)

    async def commit(self) -> None:
        await self._conn.commit()

    async def rollback(self) -> None:
        """回滚当前事务。用于 auto_commit=False 批量操作失败时清理脏事务状态。

        根因：aiosqlite 单连接共享事务状态，auto_commit=False 操作若不 commit/rollback，
        事务会残留在连接上，后续协程的 DB 操作在脏事务中执行 → "SQL logic error"。
        """
        await self._conn.rollback()


    async def _sync_fts(self, memory_id: int, summary: str, event_label: str, *,
                        delete_first: bool = True, auto_commit: bool = False,
                        strict: bool = False) -> None:
        """同步 episodic_memory_fts 索引：分词 → (可选 DELETE) → INSERT → (可选 commit)。

        失败统一走 _record_fts_sync_failure（告警 + 计数），不抛出。
        """
        try:
            from db.fts_utils import _tokenize_for_fts
            # jieba 分词是同步 CPU 操作（首调加载词典 1-2s，之后每次 10-50ms），
            # 在事件循环内直接执行会冻结整个 asyncio 服务；移到线程池
            tokenized = await asyncio.to_thread(_tokenize_for_fts, summary)
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
        except (ImportError, OSError, RuntimeError, ValueError) as e:
            _record_fts_sync_failure(event_label, e)
            if strict:
                raise




        except Exception as e:
            logger.exception(".db.db_memory._sync_fts_unexpected")
            _record_fts_sync_failure(event_label, e)
            if strict:
                raise




