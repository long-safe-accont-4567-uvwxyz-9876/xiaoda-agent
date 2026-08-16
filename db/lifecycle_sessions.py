"""LifecycleSessionMixin — 会话生命周期 + cron 状态。

自 db/database.py 拆分（上帝文件 Phase 4）：函数体逐字节搬移，仅缩进调整。
依赖 DatabaseManager 的 self._conn / self.write_transaction /
self._write_tx_active / self.insert_audit_log（MRO 保证）。
"""
from __future__ import annotations

import time


class LifecycleSessionMixin:
    async def create_session(self, user_openid: str = "", auto_commit: bool = True) -> str:
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        session_id = f"SES-{date_str}-{int(now % 100000):05d}"
        await self._conn.execute(
            """INSERT INTO sessions
               (id, user_openid, started_at, ended_at, status)
               VALUES (?, ?, ?, ?, 'active')""",
            (session_id, user_openid, now, now),
        )
        if auto_commit:
            await self._conn.commit()
        return session_id

    async def get_active_session(self, user_openid: str, idle_seconds: int = 1800) -> dict | None:
        cutoff = time.time() - idle_seconds
        cursor = await self._conn.execute(
            """SELECT * FROM sessions
               WHERE user_openid=? AND status='active' AND ended_at >= ?
               ORDER BY ended_at DESC LIMIT 1""",
            (user_openid, cutoff),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_session(self, session_id: str, cost_usd: float = 0,
                              cache_hit: int = 0, cache_miss: int = 0,
                              auto_commit: bool = True) -> None:
        now = time.time()
        await self._conn.execute(
            """UPDATE sessions
               SET turn_count = turn_count + 1,
                   total_cost_usd = total_cost_usd + ?,
                   cache_hit_tokens = cache_hit_tokens + ?,
                   cache_miss_tokens = cache_miss_tokens + ?,
                   ended_at = ?
               WHERE id=?""",
            (cost_usd, cache_hit, cache_miss, now, session_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def archive_session(self, session_id: str, summary: str = "",
                               auto_commit: bool = True) -> None:
        now = time.time()
        await self._conn.execute(
            """UPDATE sessions
               SET status='archived', summary=?, ended_at=?
               WHERE id=?""",
            (summary, now, session_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def get_archived_sessions(self, user_openid: str = "", limit: int = 10) -> list[dict]:
        if user_openid:
            cursor = await self._conn.execute(
                """SELECT * FROM sessions
                   WHERE user_openid=? AND status='archived'
                   ORDER BY ended_at DESC LIMIT ?""",
                (user_openid, limit),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT * FROM sessions
                   WHERE status='archived'
                   ORDER BY ended_at DESC LIMIT ?""",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_active_sessions(self, limit: int = 10) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT * FROM sessions
               WHERE status='active'
               ORDER BY ended_at DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def auto_archive_stale_sessions(self, idle_seconds: int = 3600,
                                           auto_commit: bool = True) -> int:
        cutoff = time.time() - idle_seconds
        cursor = await self._conn.execute(
            """UPDATE sessions
               SET status='archived', ended_at=?
               WHERE status='active' AND ended_at > 0 AND ended_at < ?""",
            (time.time(), cutoff),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.rowcount

    async def get_cron_last_run(self, task_name: str) -> float | None:
        cursor = await self._conn.execute(
            "SELECT last_run FROM cron_last_run WHERE task_name=?", (task_name,)
        )
        row = await cursor.fetchone()
        return row["last_run"] if row else None

    async def set_cron_last_run(self, task_name: str, ts: float | None = None,
                                 auto_commit: bool = True) -> None:
        ts = ts or time.time()
        sql = "INSERT OR REPLACE INTO cron_last_run (task_name, last_run) VALUES (?, ?)"
        if not auto_commit or self._write_tx_active.get():
            await self._conn.execute(sql, (task_name, ts))
            return
        async with self.write_transaction() as conn:
            await conn.execute(sql, (task_name, ts))

