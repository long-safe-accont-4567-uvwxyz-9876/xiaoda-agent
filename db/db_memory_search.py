"""MemoryDB 的会话查询 + 检索方法组 —— 拆分自 db/db_memory.py。

Mixin 组合：MemoryDB 继承 SearchMixin 获得会话查询与检索
（重要性/FTS/时间/向量/作用域）方法。仅依赖 self._conn/_read_conn()
+ db_memory_utils 纯函数，无循环依赖。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from db.db_memory_utils import _sql_placeholders, _scope_where, _rows_to_fts_results


class SearchMixin:
    """会话查询 + 检索方法组。"""

    async def get_recent_conversations(self, limit: int = 20, user_id: str = "") -> Any:
        """获取最近的对话记录。支持按 user_id 过滤（群聊场景下隔离不同用户的历史）。"""
        if user_id:
            cursor = await self._read_conn().execute(
                """SELECT * FROM conversation_logs
                   WHERE user_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            )
        else:
            cursor = await self._read_conn().execute(
                """SELECT * FROM conversation_logs
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in reversed(rows)]

    async def get_conversations_by_time_range(self, start_ts: float, end_ts: float,
                                               user_id: str = "", limit: int = 50) -> list[dict]:
        """按时间范围查询 conversation_logs 原始对话。用于时间型回忆查询。

        CodeRabbit 复审修复：原 ORDER BY timestamp ASC LIMIT ? 在记录数超过 limit 时
        返回最早的记录而非最新的，导致 restore_from_db 恢复过期对话。
        改为先 DESC 取最新 limit 条，再反转为时间升序，确保始终返回最近对话。
        """
        params: list = [start_ts, end_ts]
        where = "WHERE timestamp >= ? AND timestamp <= ?"
        if user_id:
            where += " AND user_id = ?"
            params.append(user_id)
        params.append(limit)
        cursor = await self._read_conn().execute(
            f"""SELECT timestamp, user_message, assistant_reply FROM conversation_logs
                {where} ORDER BY timestamp DESC LIMIT ?""",
            params,
        )
        rows = await cursor.fetchall()
        result = [dict(r) for r in rows]
        # 反转为时间升序，保持调用方的时序预期
        result.reverse()
        return result

    async def _search_by_importance_impl(self, min_importance: float, limit: int,
                                         scope: Any | None) -> list[dict]:
        if scope is None:
            cursor = await self._read_conn().execute(
                """SELECT * FROM episodic_memories
                   WHERE importance >= ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (min_importance, limit),
            )
        else:
            scope_where, scope_params = _scope_where(scope)
            cursor = await self._read_conn().execute(
                f"""SELECT * FROM episodic_memories
                   WHERE importance >= ?{scope_where}
                   ORDER BY timestamp DESC LIMIT ?""",
                [min_importance, *scope_params, limit],
            )
        rows = await cursor.fetchall()
        result = [dict(r) for r in rows]
        logger.debug("db_memory.search_by_importance", scoped=scope is not None,
                     limit=limit, count=len(result))
        return result

    async def search_memories_by_importance(self, min_importance: float = 0.3, limit: int = 10) -> Any:
        return await self._search_by_importance_impl(min_importance, limit, None)

    async def search_memories_by_importance_scoped(self, min_importance: float = 0.3,
                                                     limit: int = 10,
                                                     scope: Any | None = None) -> list[dict]:
        """按重要性排序检索 + scope 过滤（mem0 SPEC 优化）。

        Args:
            scope: Scope 对象。None 时退回无 scope 版本。
        """
        if scope is None:
            return await self.search_memories_by_importance(min_importance, limit)
        return await self._search_by_importance_impl(min_importance, limit, scope)

    async def _search_fts_impl(self, query: str, limit: int, scope: Any | None,
                               is_raw: int | None, event_label: str) -> list[dict]:
        from db.fts_utils import _build_fts_query
        try:
            import config as _cfg
            _drop_single = getattr(_cfg, "FTS_DROP_CJK_SINGLE", False)
            _filter_stop = getattr(_cfg, "FTS_CJK_STOP_WORDS_FILTER", False)
        except (ImportError, AttributeError):
            _drop_single = False
            _filter_stop = False
        fts_query = _build_fts_query(query, drop_cjk_single=_drop_single,
                                     filter_stop_words=_filter_stop)
        if not fts_query:
            return []
        try:
            if scope is None:
                cursor = await self._read_conn().execute(
                    """SELECT em.*, bm25(episodic_memory_fts) AS score
                       FROM episodic_memory_fts
                       JOIN episodic_memories em ON em.id = episodic_memory_fts.id
                       WHERE episodic_memory_fts MATCH ?
                       ORDER BY score ASC, em.importance DESC, em.timestamp DESC
                       LIMIT ?""",
                    (fts_query, limit),
                )
            else:
                scope_where, scope_params = _scope_where(scope, is_raw=is_raw, table="em")
                cursor = await self._read_conn().execute(
                    f"""SELECT em.*, bm25(episodic_memory_fts) AS score
                       FROM episodic_memory_fts
                       JOIN episodic_memories em ON em.id = episodic_memory_fts.id
                       WHERE episodic_memory_fts MATCH ?{scope_where}
                       ORDER BY score ASC, em.importance DESC, em.timestamp DESC
                       LIMIT ?""",
                    [fts_query, *scope_params, limit],
                )
            rows = await cursor.fetchall()
            result = _rows_to_fts_results(rows)
            logger.debug("db_memory.search_fts", scoped=scope is not None,
                         limit=limit, count=len(result))
            return result
        except Exception as e:
            logger.warning(event_label, error=str(e))
            return []

    async def search_memories_fts(self, query: str, limit: int = 20) -> list[dict]:
        """FTS5 BM25 全文检索"""
        return await self._search_fts_impl(query, limit, None, None, "db_memory.fts_search_failed")

    async def search_memories_fts_scoped(self, query: str, scope: Any,
                                          limit: int = 20,
                                          is_raw: int | None = None) -> list[dict]:
        """FTS5 全文检索 + scope 过滤（mem0 SPEC 优化）。

        Args:
            scope: Scope 对象
            limit: 返回条数上限
            is_raw: None=不限, 0=只查提炼知识, 1=只查原始记录
        """
        return await self._search_fts_impl(query, limit, scope, is_raw, "db_memory.fts_scoped_search_failed")

    async def _search_by_time_impl(self, start_ts: float, end_ts: float, limit: int,
                                   scope: Any | None, is_raw: int | None) -> list[dict]:
        if scope is None:
            cursor = await self._read_conn().execute(
                """SELECT * FROM episodic_memories
                   WHERE timestamp >= ? AND timestamp < ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (start_ts, end_ts, limit),
            )
        else:
            scope_where, scope_params = _scope_where(scope, is_raw=is_raw)
            cursor = await self._read_conn().execute(
                f"""SELECT * FROM episodic_memories
                   WHERE timestamp >= ? AND timestamp < ?{scope_where}
                   ORDER BY timestamp DESC LIMIT ?""",
                [start_ts, end_ts, *scope_params, limit],
            )
        rows = await cursor.fetchall()
        result = [dict(r) for r in rows]
        logger.debug("db_memory.search_by_time", scoped=scope is not None,
                     limit=limit, count=len(result))
        return result

    async def search_memories_by_time_scoped(self, start_ts: float, end_ts: float,
                                              scope: Any, limit: int = 20,
                                              is_raw: int | None = None) -> list[dict]:
        """按时间范围检索记忆 + scope 过滤（mem0 SPEC 优化）。

        Args:
            scope: Scope 对象
            is_raw: None=不限, 0=只查提炼知识, 1=只查原始记录
        """
        try:
            return await self._search_by_time_impl(start_ts, end_ts, limit, scope, is_raw)
        except Exception as e:
            logger.warning("db_memory.time_scoped_search_failed", error=str(e))
            return []

    async def search_memories_vec_scoped(self, memory_ids: list[int], scope: Any,
                                          limit: int = 50,
                                          is_raw: int | None = None) -> list[dict]:
        """向量检索结果 + scope 过滤（从 memory_ids 中筛选符合 scope 的记录）。

        Args:
            memory_ids: 向量检索返回的 memory_id 列表
            scope: Scope 对象
            is_raw: None=不限, 0=只查提炼知识, 1=只查原始记录
        """
        if not memory_ids:
            return []
        try:
            placeholders = _sql_placeholders(memory_ids)
            scope_where, scope_params = _scope_where(scope, is_raw=is_raw)
            params: list = [*memory_ids, *scope_params, limit]
            cursor = await self._read_conn().execute(
                f"""SELECT * FROM episodic_memories
                   WHERE id IN ({placeholders})
                     {scope_where}
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.vec_scoped_search_failed", error=str(e))
            return []

    async def get_episodic_count_scoped(self, scope: Any, is_raw: int | None = None) -> int:
        """获取 scope 内的记忆总数（用于冷启动档位判断）"""
        try:
            scope_where, scope_params = _scope_where(scope, is_raw=is_raw)
            cursor = await self._read_conn().execute(
                f"SELECT COUNT(*) as cnt FROM episodic_memories "
                f"WHERE 1=1{scope_where}",
                scope_params,
            )
            row = await cursor.fetchone()
            return row["cnt"] if row else 0
        except Exception as e:
            logger.warning("db_memory.count_scoped_failed", error=str(e))
            return 0