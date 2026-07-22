import time

import aiosqlite
from loguru import logger


class LearningDB:
    """管理学习记录与错误反馈数据的读写。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        conn.row_factory = aiosqlite.Row

    async def commit(self) -> None:
        await self._conn.commit()

    async def insert_learning(self, category: str, priority: str, summary: str,
                               details: str = "", suggested_action: str = "",
                               source: str = "conversation",
                               pattern_key: str = "",
                               auto_commit: bool = True) -> str:
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        learning_id = f"LRN-{date_str}-{int(now % 10000):04d}"
        try:
            await self._conn.execute(
                """INSERT INTO learnings
                   (learning_id, category, priority, status, summary, details,
                    suggested_action, source, pattern_key, recurrence_count,
                    first_seen, last_seen, created_at)
                   VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (learning_id, category, priority, summary, details,
                 suggested_action, source, pattern_key, now, now, now),
            )
            if auto_commit:
                await self._conn.commit()
            return learning_id
        except Exception as e:
            logger.warning("db_learning.insert_learning_failed", error=str(e), summary=summary)
            return ""

    async def find_learning_by_pattern(self, pattern_key: str) -> dict | None:
        if not pattern_key:
            return None
        cursor = await self._conn.execute(
            "SELECT * FROM learnings WHERE pattern_key=? AND status='pending' LIMIT 1",
            (pattern_key,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def bump_learning_recurrence(self, learning_id: str,
                                        auto_commit: bool = True) -> bool:
        now = time.time()
        cursor = await self._conn.execute(
            """UPDATE learnings
               SET recurrence_count = recurrence_count + 1, last_seen = ?
               WHERE learning_id=?""",
            (now, learning_id),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.rowcount > 0

    async def resolve_learning(self, learning_id: str, resolution: str = "",
                                auto_commit: bool = True) -> bool:
        cursor = await self._conn.execute(
            "UPDATE learnings SET status='resolved' WHERE learning_id=?",
            (learning_id,),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.rowcount > 0

    async def promote_learning(self, learning_id: str, target: str = "system_prompt",
                                auto_commit: bool = True) -> bool:
        cursor = await self._conn.execute(
            "UPDATE learnings SET status='promoted' WHERE learning_id=?",
            (learning_id,),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.rowcount > 0

    async def get_promoted_learnings(self) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM learnings WHERE status='promoted' ORDER BY last_seen DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def search_learnings(self, query: str = "", category: str = "",
                                status: str = "", limit: int = 20) -> list[dict]:
        conditions = []
        params = []
        if query:
            conditions.append("summary LIKE ?")
            params.append(f"%{query}%")
        if category:
            conditions.append("category=?")
            params.append(category)
        if status:
            conditions.append("status=?")
            params.append(status)
        if not conditions:
            conditions.append("1=1")
        cursor = await self._conn.execute(
            "SELECT * FROM learnings WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ?",
            [*params, limit],
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_promotable_learnings(self, min_recurrence: int = 3) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT * FROM learnings
               WHERE status='pending' AND recurrence_count >= ?
               ORDER BY recurrence_count DESC, last_seen DESC""",
            (min_recurrence,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def insert_error(self, summary: str, error_text: str = "",
                            context: str = "", suggested_fix: str = "",
                            priority: str = "high",
                            auto_commit: bool = True) -> str:
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        error_id = f"ERR-{date_str}-{int(now % 10000):04d}"
        try:
            await self._conn.execute(
                """INSERT INTO errors
                   (error_id, priority, status, summary, error_text, context,
                    suggested_fix, created_at)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)""",
                (error_id, priority, summary, error_text[:500], context[:300],
                 suggested_fix, now),
            )
            if auto_commit:
                await self._conn.commit()
            return error_id
        except Exception as e:
            logger.warning("db_learning.insert_error_failed", error=str(e), summary=summary)
            return ""

    async def insert_feature_request(self, capability: str, user_context: str = "",
                                      complexity: str = "medium",
                                      auto_commit: bool = True) -> str:
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        request_id = f"FEAT-{date_str}-{int(now % 10000):04d}"
        try:
            await self._conn.execute(
                """INSERT INTO feature_requests
                   (request_id, priority, status, capability, user_context,
                    complexity, frequency, created_at)
                   VALUES (?, 'medium', 'pending', ?, ?, ?, 'first_time', ?)""",
                (request_id, capability, user_context[:300], complexity, now),
            )
            if auto_commit:
                await self._conn.commit()
            return request_id
        except Exception as e:
            logger.warning("db_learning.insert_feature_request_failed", error=str(e), capability=capability)
            return ""
