import time
import aiosqlite
from loguru import logger


class MemoryDB:

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
        conn.row_factory = aiosqlite.Row

    async def commit(self):
        await self._conn.commit()

    async def insert_episodic_memory(self, summary: str, importance: float = 0.5,
                                      emotion_label: str = "", session_id: str = "user",
                                      embedding_id: int = -1, auto_commit: bool = True):
        cursor = await self._conn.execute(
            """INSERT INTO episodic_memories
               (timestamp, summary, importance, emotion_label, session_id, embedding_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (time.time(), summary, importance, emotion_label, session_id, embedding_id),
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

    async def get_all_memories(self):
        cursor = await self._conn.execute(
            "SELECT * FROM episodic_memories ORDER BY timestamp DESC"
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
                logger.warning("db_memory.vec_delete_failed", memory_id=memory_id, error=str(e))
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
