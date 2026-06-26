import time
import aiosqlite
from loguru import logger


class MemoryDB:

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
        conn.row_factory = aiosqlite.Row

    async def commit(self):
        await self._conn.commit()

    async def migrate_add_source_column(self):
        """迁移：为旧库的 episodic_memories 表添加 source 列（已存在则忽略）"""
        try:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN source TEXT DEFAULT 'user'"
            )
            await self._conn.commit()
        except Exception as e:
            # 列已存在时忽略
            logger.debug(f"db_memory.migrate_add_source_column skipped: {e}")

    async def insert_episodic_memory(self, summary: str, importance: float = 0.5,
                                      emotion_label: str = "", session_id: str = "user",
                                      embedding_id: int = -1, auto_commit: bool = True,
                                      source: str = "user"):
        cursor = await self._conn.execute(
            """INSERT INTO episodic_memories
               (timestamp, summary, importance, emotion_label, session_id, embedding_id, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), summary, importance, emotion_label, session_id, embedding_id, source),
        )
        mem_id = cursor.lastrowid
        if auto_commit:
            await self._conn.commit()
        # 同步写入 FTS 索引
        try:
            from memory.memory_manager import _tokenize_for_fts
            tokenized = _tokenize_for_fts(summary)
            if tokenized.strip():
                await self._conn.execute(
                    "INSERT INTO episodic_memory_fts(id, summary_index) VALUES(?, ?)",
                    (mem_id, tokenized),
                )
                if auto_commit:
                    await self._conn.commit()
        except Exception as e:
            from loguru import logger
            logger.debug("db_memory.fts_insert_failed", error=str(e))
        return mem_id

    async def get_memory_by_id(self, memory_id: int) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT * FROM episodic_memories WHERE id=?", (memory_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_memories_by_ids(self, ids: list[int]) -> list[dict]:
        """批量获取记忆记录（向量检索后批量 JOIN 主表，消除 N 次逐条查询）"""
        if not ids:
            return []
        # 参数化占位符，防止 SQL 注入
        placeholders = ",".join("?" * len(ids))
        cursor = await self._conn.execute(
            f"SELECT * FROM episodic_memories WHERE id IN ({placeholders})",
            ids,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_recent_conversations(self, limit: int = 20, user_id: str = ""):
        """获取最近的对话记录。支持按 user_id 过滤（群聊场景下隔离不同用户的历史）。"""
        if user_id:
            cursor = await self._conn.execute(
                """SELECT * FROM conversation_logs
                   WHERE user_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT * FROM conversation_logs
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def increment_access_count(self, memory_id: int):
        """递增记忆访问计数（检索强化）"""
        await self._conn.execute(
            "UPDATE episodic_memories SET access_count = access_count + 1 WHERE id = ?",
            (memory_id,),
        )
        await self._conn.commit()

    async def archive_memory(self, memory_id: int):
        """归档记忆（标记为已归档，不删除）"""
        await self._conn.execute(
            "UPDATE episodic_memories SET session_id = 'archived' WHERE id = ?",
            (memory_id,),
        )
        await self._conn.commit()

    async def get_recent_conversations(self, limit: int = 20, user_id: str = ""):
        """获取最近的对话记录。支持按 user_id 过滤（群聊场景下隔离不同用户的历史）。"""
        if user_id:
            cursor = await self._conn.execute(
                """SELECT * FROM conversation_logs
                   WHERE user_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT * FROM conversation_logs
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in reversed(rows)]

    async def search_memories_by_importance(self, min_importance: float = 0.3, limit: int = 10):
        cursor = await self._conn.execute(
            """SELECT * FROM episodic_memories
               WHERE importance >= ?
               ORDER BY timestamp DESC LIMIT ?""",
            (min_importance, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def search_memories_fts(self, query: str, limit: int = 10) -> list[dict]:
        """FTS5 BM25 全文检索"""
        from memory.memory_manager import _build_fts_query
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        try:
            cursor = await self._conn.execute(
                """SELECT em.*, bm25(episodic_memory_fts) AS score
                   FROM episodic_memory_fts
                   JOIN episodic_memories em ON em.id = episodic_memory_fts.id
                   WHERE episodic_memory_fts MATCH ?
                   ORDER BY score ASC, em.importance DESC, em.timestamp DESC
                   LIMIT ?""",
                (fts_query, limit),
            )
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                # bm25 returns negative values, convert to positive score
                d["score"] = -d.get("score", 0)
                results.append(d)
            return results
        except Exception as e:
            from loguru import logger
            logger.warning("db_memory.fts_search_failed", error=str(e))
            return []

    async def get_all_memories(self, limit: int = 100):
        """获取所有活跃记忆（排除已归档）"""
        cursor = await self._conn.execute(
            "SELECT * FROM episodic_memories WHERE session_id != 'archived' ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_memory(self, memory_id: int, auto_commit: bool = True):
        await self._conn.execute("DELETE FROM episodic_memories WHERE id=?", (memory_id,))
        # 同步删除 FTS 记录
        try:
            await self._conn.execute("DELETE FROM episodic_memory_fts WHERE id=?", (memory_id,))
        except Exception as e:
            from loguru import logger
            logger.debug("db_memory.fts_delete_failed", error=str(e))
        if auto_commit:
            await self._conn.commit()

    async def delete_memory_with_vector(self, memory_id: int, vector_store=None, auto_commit: bool = True):
        """统一删除：先删向量，再删记忆"""
        if vector_store:
            try:
                await vector_store.delete(memory_id)
            except Exception as e:
                logger.error("db_memory.vec_delete_failed", memory_id=memory_id, error=str(e))
                raise
        await self.delete_memory(memory_id, auto_commit=auto_commit)

    async def get_episodic_recent(self, limit: int = 50):
        cursor = await self._conn.execute(
            """SELECT * FROM episodic_memories
               ORDER BY timestamp DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_episodic_count(self) -> int:
        cursor = await self._conn.execute("SELECT COUNT(*) as cnt FROM episodic_memories")
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

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
        cursor = await self._conn.execute(
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
                                      auto_commit: bool = True):
        await self._conn.execute(
            "UPDATE consolidation_candidates SET status='applied', target_memory_id=? WHERE id=?",
            (target_memory_id, candidate_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def update_rag_status(self, memory_id: int, rag_status: str, rag_synced_at: float = None):
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

    async def update_doc_id(self, memory_id: int, doc_id: str):
        """更新记忆关联的文档 ID"""
        await self._conn.execute(
            "UPDATE episodic_memories SET doc_id=? WHERE id=?",
            (doc_id, memory_id),
        )
        await self._conn.commit()

    async def get_pending_memories(self, limit: int = 100) -> list[dict]:
        """查询待索引的 RAG 记忆（rag_status='pending'），按时间升序"""
        cursor = await self._conn.execute(
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
            cursor = await self._conn.execute(
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
            cursor = await self._conn.execute(
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
                                       auto_commit: bool = True):
        """将指定记忆标记为已蒸馏（distilled=1），保留不删除"""
        if not memory_ids:
            return
        placeholders = ",".join("?" * len(memory_ids))
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
            cursor = await self._conn.execute(
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
            cursor = await self._conn.execute(
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
