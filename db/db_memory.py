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



class MemoryDB(ChildChunkMixin, EntityMixin, EpisodicMixin):
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
        fts_query = _build_fts_query(query)
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

    async def update_memory_enrichment(self, memory_id: int, summary: str = "",
                                        entities: str = "", event_type: str = "",
                                        metadata_json: str = "", auto_commit: bool = True) -> bool:
        """后台 LLM 提取完成后，更新记忆条目的结构化字段。

        Args:
            memory_id: 记忆 ID
            summary: LLM 提取的更高质量摘要（可选，空则不更新）
            entities: 实体列表（JSON 字符串，如 '["小妲", "爸爸", "QQ"]'）
            event_type: 事件类型（如 '对话/决策/偏好/事件'）
            metadata_json: 元数据 JSON（如 '{"decision": "重启服务", "mood": "开心"}'）
        """
        try:
            sets = []
            params = []
            if summary:
                sets.append("summary = ?")
                params.append(summary)
            if entities:
                sets.append("entities = ?")
                params.append(entities)
            if event_type:
                sets.append("event_type = ?")
                params.append(event_type)
            if metadata_json:
                sets.append("metadata_json = ?")
                params.append(metadata_json)
            if not sets:
                return False
            params.append(memory_id)
            await self._conn.execute(
                f"UPDATE episodic_memories SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            if auto_commit:
                await self._conn.commit()
            # 如果 summary 更新了，同步更新 FTS 索引
            if summary:
                await self._sync_fts(memory_id, summary, "db_memory.fts_update_failed",
                                     auto_commit=auto_commit)
            return True
        except Exception as e:
            logger.warning("db_memory.enrichment_update_failed", error=str(e))
            return False

    async def insert_portrait(self, content: str, version: int = 1,
                               source_ids: str = "", change_log: str = "",
                               auto_commit: bool = True) -> int:
        cursor = await self._conn.execute(
            """INSERT INTO user_portrait (content, version, source_ids, change_log, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (content, version, source_ids, change_log, time.time()),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.lastrowid

    async def get_latest_portrait(self) -> dict | None:
        cursor = await self._read_conn().execute(
            "SELECT * FROM user_portrait ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def insert_consolidation_candidate(self, source: str, kind: str, summary: str,
                                              confidence: float = 0.5, importance: float = 0.5,
                                              metadata_json: str = "{}",
                                              auto_commit: bool = True) -> int:
        cursor = await self._conn.execute(
            """INSERT INTO consolidation_candidates
               (timestamp, source, kind, summary, confidence, importance, status, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (time.time(), source, kind, summary, confidence, importance, metadata_json, time.time()),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.lastrowid

    async def mark_candidate_applied(self, candidate_id: int, target_memory_id: int,
                                      auto_commit: bool = True) -> None:
        await self._conn.execute(
            "UPDATE consolidation_candidates SET status='applied', target_memory_id=? WHERE id=?",
            (target_memory_id, candidate_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def update_rag_status(self, memory_id: int, rag_status: str, rag_synced_at: float | None = None) -> None:
        """更新记忆的 RAG 索引状态"""
        valid_statuses = ('pending', 'indexed', 'failed', 'excluded')
        if rag_status not in valid_statuses:
            raise ValueError(f"rag_status must be one of {valid_statuses}, got '{rag_status}'")
        if rag_status == 'indexed' and rag_synced_at is None:
            rag_synced_at = time.time()
        if rag_synced_at is not None:
            await self._conn.execute(
                "UPDATE episodic_memories SET rag_status=?, rag_synced_at=? WHERE id=?",
                (rag_status, rag_synced_at, memory_id),
            )
        else:
            await self._conn.execute(
                "UPDATE episodic_memories SET rag_status=? WHERE id=?",
                (rag_status, memory_id),
            )
        await self._conn.commit()

    async def update_doc_id(self, memory_id: int, doc_id: str) -> None:
        """更新记忆关联的文档 ID"""
        await self._conn.execute(
            "UPDATE episodic_memories SET doc_id=? WHERE id=?",
            (doc_id, memory_id),
        )
        await self._conn.commit()

    async def get_pending_memories(self, limit: int = 100) -> list[dict]:
        """查询待索引的 RAG 记忆（rag_status='pending'），按时间升序"""
        cursor = await self._read_conn().execute(
            """SELECT id, timestamp, summary, importance FROM episodic_memories
               WHERE rag_status='pending'
               ORDER BY timestamp ASC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── P3 记忆蒸馏相关 ──────────────────────────────────────

    async def get_episodic_count_undistilled(self) -> int:
        """统计未蒸馏的情景记忆数量（distilled=0）"""
        try:
            cursor = await self._read_conn().execute(
                "SELECT COUNT(*) as cnt FROM episodic_memories WHERE distilled=0"
            )
            row = await cursor.fetchone()
            return row["cnt"] if row else 0
        except Exception as e:
            # 旧库可能没有 distilled 列，降级返回总计数
            logger.debug("db_memory.undistilled_count_failed", error=str(e))
            return await self.get_episodic_count()

    async def get_distill_candidates(self, limit: int = 30) -> list[dict]:
        """查询最旧的未蒸馏记忆（按时间升序），用于蒸馏压缩"""
        try:
            cursor = await self._read_conn().execute(
                """SELECT id, timestamp, summary, importance FROM episodic_memories
                   WHERE distilled=0
                   ORDER BY timestamp ASC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.distill_candidates_failed", error=str(e))
            return []

    async def mark_memories_distilled(self, memory_ids: list[int],
                                       auto_commit: bool = True) -> None:
        """将指定记忆标记为已蒸馏（distilled=1），保留不删除"""
        if not memory_ids:
            return
        placeholders = _sql_placeholders(memory_ids)
        try:
            await self._conn.execute(
                f"UPDATE episodic_memories SET distilled=1 WHERE id IN ({placeholders})",
                memory_ids,
            )
            if auto_commit:
                await self._conn.commit()
        except Exception as e:
            logger.warning("db_memory.mark_distilled_failed", error=str(e))

    async def insert_memory_summary(self, summary_text: str, memory_count: int,
                                     auto_commit: bool = True) -> int:
        """写入一条蒸馏摘要记录，返回摘要 id"""
        cursor = await self._conn.execute(
            """INSERT INTO memory_summaries (summary_text, created_at, memory_count)
               VALUES (?, ?, ?)""",
            (summary_text, time.time(), memory_count),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.lastrowid

    async def get_memory_summaries(self, limit: int = 5) -> list[dict]:
        """获取最近的蒸馏摘要（按时间降序）"""
        try:
            cursor = await self._read_conn().execute(
                """SELECT id, summary_text, created_at, memory_count
                   FROM memory_summaries
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.get_summaries_failed", error=str(e))
            return []

    async def get_recent_undistilled(self, limit: int = 20) -> list[dict]:
        """获取最近的未蒸馏记忆（按时间降序），用于构建记忆提示"""
        try:
            cursor = await self._read_conn().execute(
                """SELECT id, timestamp, summary, importance FROM episodic_memories
                   WHERE distilled=0
                   ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            # 旧库可能没有 distilled 列，降级返回所有最近记忆
            logger.debug("db_memory.recent_undistilled_failed", error=str(e))
            return await self.get_episodic_recent(limit=limit)

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
