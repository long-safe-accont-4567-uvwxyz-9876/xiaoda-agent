import time
import uuid

import aiosqlite
from loguru import logger


class AnalyticsDB:
    """管理 API 用量、成本等分析数据的读写。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        conn.row_factory = aiosqlite.Row

    async def commit(self) -> None:
        await self._conn.commit()

    async def insert_api_usage(self, user_openid: str = "", session_id: str = "",
                                model: str = "", task_type: str = "",
                                prompt_tokens: int = 0, completion_tokens: int = 0,
                                cache_hit_tokens: int = 0, cache_miss_tokens: int = 0,
                                cost_usd: float = 0.0,
                                auto_commit: bool = True) -> str:
        now = time.time()
        usage_id = f"API-{uuid.uuid4().hex}"
        try:
            await self._conn.execute(
                """INSERT INTO api_usage
                   (id, user_openid, session_id, model, task_type,
                    prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens,
                    cost_usd, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (usage_id, user_openid, session_id, model, task_type,
                 prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens,
                 cost_usd, now),
            )
            if auto_commit:
                await self._conn.commit()
            return usage_id
        except Exception:
            return ""

    async def batch_insert_api_usage(self, records: list[dict],
                                      auto_commit: bool = True) -> None:
        if not records:
            return
        now = time.time()
        rows = []
        for r in records:
            usage_id = f"API-{uuid.uuid4().hex}"
            rows.append((
                usage_id,
                r.get("user_openid", ""),
                r.get("session_id", ""),
                r.get("model", ""),
                r.get("task_type", ""),
                r.get("prompt_tokens", 0),
                r.get("completion_tokens", 0),
                r.get("cache_hit_tokens", 0),
                r.get("cache_miss_tokens", 0),
                r.get("cost_usd", 0.0),
                r.get("created_at", now),
            ))
        try:
            await self._conn.executemany(
                """INSERT OR IGNORE INTO api_usage
                   (id, user_openid, session_id, model, task_type,
                    prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens,
                    cost_usd, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            if auto_commit:
                await self._conn.commit()
        except Exception as e:
            logger.warning("db.batch_api_usage_failed", error=str(e))

    async def insert_proactive_message(self, user_id: str, message_type: str,
                                        content: str,
                                        auto_commit: bool = True) -> int:
        cursor = await self._conn.execute(
            """INSERT INTO proactive_messages (user_id, message_type, content, sent_at)
               VALUES (?, ?, ?, ?)""",
            (user_id, message_type, content, time.time()),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.lastrowid

    async def get_recent_proactive_messages(self, user_id: str,
                                              limit: int = 10) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT * FROM proactive_messages
               WHERE user_id=? ORDER BY sent_at DESC LIMIT ?""",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_daily_cost(self, date_str: str | None = None) -> dict:
        if not date_str:
            date_str = time.strftime("%Y%m%d")
        start = time.mktime(time.strptime(date_str, "%Y%m%d"))
        end = start + 86400
        cursor = await self._conn.execute(
            """SELECT SUM(cost_usd) as total_cost,
                      SUM(prompt_tokens) as total_prompt,
                      SUM(completion_tokens) as total_completion,
                      SUM(cache_hit_tokens) as total_cache_hit,
                      SUM(cache_miss_tokens) as total_cache_miss,
                      COUNT(*) as call_count
               FROM api_usage WHERE created_at >= ? AND created_at < ?""",
            (start, end),
        )
        row = await cursor.fetchone()
        if row and row["total_cost"] is not None:
            total_tokens = (row["total_cache_hit"] or 0) + (row["total_cache_miss"] or 0)
            return {
                "date": date_str,
                "total_cost_usd": round(row["total_cost"], 6),
                "total_prompt_tokens": row["total_prompt"] or 0,
                "total_completion_tokens": row["total_completion"] or 0,
                "cache_hit_tokens": row["total_cache_hit"] or 0,
                "cache_miss_tokens": row["total_cache_miss"] or 0,
                "cache_hit_ratio": round((row["total_cache_hit"] or 0) / total_tokens, 3) if total_tokens > 0 else 0.0,
                "call_count": row["call_count"] or 0,
            }
        return {"date": date_str, "total_cost_usd": 0, "call_count": 0}

    async def get_user_cost(self, user_openid: str, days: int = 7) -> dict:
        cutoff = time.time() - days * 86400
        cursor = await self._conn.execute(
            """SELECT SUM(cost_usd) as total_cost, COUNT(*) as call_count
               FROM api_usage WHERE user_openid=? AND created_at >= ?""",
            (user_openid, cutoff),
        )
        row = await cursor.fetchone()
        return {
            "user_openid": user_openid,
            "days": days,
            "total_cost_usd": round(row["total_cost"], 6) if row and row["total_cost"] else 0,
            "call_count": row["call_count"] if row else 0,
        }

    async def get_cost_breakdown(self, days: int = 7) -> list[dict]:
        cutoff = time.time() - days * 86400
        cursor = await self._conn.execute(
            """SELECT task_type, model,
                      SUM(cost_usd) as total_cost,
                      SUM(prompt_tokens) as total_prompt,
                      SUM(completion_tokens) as total_completion,
                      COUNT(*) as call_count
               FROM api_usage WHERE created_at >= ?
               GROUP BY task_type, model ORDER BY total_cost DESC""",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def batch_insert_events(self, events: list[dict],
                                   auto_commit: bool = True) -> None:
        if not events:
            return
        rows = []
        for e in events:
            rows.append((
                e.get("event_type", ""),
                e.get("user_openid", ""),
                e.get("session_id", ""),
                e.get("detail", "")[:500],
                e.get("created_at", time.time()),
            ))
        try:
            await self._conn.executemany(
                """INSERT INTO agent_events (event_type, user_openid, session_id, detail, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                rows,
            )
            if auto_commit:
                await self._conn.commit()
        except Exception as e:
            logger.warning("db.batch_events_failed", error=str(e))

    async def get_recent_events(self, event_type: str = "", limit: int = 50) -> list[dict]:
        if event_type:
            cursor = await self._conn.execute(
                """SELECT * FROM agent_events
                   WHERE event_type=? ORDER BY created_at DESC LIMIT ?""",
                (event_type, limit),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT * FROM agent_events
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
