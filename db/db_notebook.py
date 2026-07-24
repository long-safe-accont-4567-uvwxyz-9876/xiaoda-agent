import time

import aiosqlite


class NotebookDB:
    """管理笔记本条目数据的持久化。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        conn.row_factory = aiosqlite.Row

    async def commit(self) -> None:
        await self._conn.commit()

    async def insert_notebook(self, kind: str, content: str, tags: str = "",
                               importance: float = 0.5, due_date: float = 0,
                               auto_commit: bool = True) -> int:
        now = time.time()
        cursor = await self._conn.execute(
            """INSERT INTO notebook_entries
               (kind, content, tags, importance, due_date, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
            (kind, content, tags, importance, due_date, now, now),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.lastrowid

    async def get_notebook_notes(self, kind: str | None = None, limit: int = 10) -> list[dict]:
        if kind:
            cursor = await self._conn.execute(
                """SELECT * FROM notebook_entries
                   WHERE kind=? AND status='active'
                   ORDER BY updated_at DESC LIMIT ?""",
                (kind, limit),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT * FROM notebook_entries
                   WHERE status='active'
                   ORDER BY updated_at DESC LIMIT ?""",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def archive_notebook_entries(self, id_threshold: int = 0, kind: str | None = None,
                                        auto_commit: bool = True) -> None:
        if kind:
            await self._conn.execute(
                """UPDATE notebook_entries SET status='archived', updated_at=?
                   WHERE kind=? AND status='active' AND id > ?""",
                (time.time(), kind, id_threshold),
            )
        else:
            await self._conn.execute(
                """UPDATE notebook_entries SET status='archived', updated_at=?
                   WHERE status='active' AND id > ?""",
                (time.time(), id_threshold),
            )
        if auto_commit:
            await self._conn.commit()

    async def delete_notebook_entry(self, note_id: int, auto_commit: bool = True) -> bool:
        cursor = await self._conn.execute(
            "UPDATE notebook_entries SET status='archived', updated_at=? WHERE id=?",
            (time.time(), note_id),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.rowcount > 0

    async def touch_notebook_entry(self, note_id: int, auto_commit: bool = True) -> bool:
        cursor = await self._conn.execute(
            "UPDATE notebook_entries SET updated_at=? WHERE id=?",
            (time.time(), note_id),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.rowcount > 0

    async def get_due_tasks(self, window_seconds: int = 3600) -> list[dict]:
        now = time.time()
        cursor = await self._conn.execute(
            """SELECT * FROM notebook_entries
               WHERE kind='task' AND status='active'
               AND due_date > 0 AND due_date <= ? AND due_date > ?
               ORDER BY due_date ASC""",
            (now, now - window_seconds),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_pending_tasks(self, limit: int = 20) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT * FROM notebook_entries
               WHERE kind='task' AND status='active'
               ORDER BY due_date ASC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def complete_task(self, task_id: int, auto_commit: bool = True) -> None:
        await self._conn.execute(
            "UPDATE notebook_entries SET status='completed', updated_at=? WHERE id=?",
            (time.time(), task_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def remind_task(self, task_id: int, auto_commit: bool = True) -> None:
        await self._conn.execute(
            "UPDATE notebook_entries SET status='reminded', updated_at=? WHERE id=?",
            (time.time(), task_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def cancel_task(self, task_id: int, auto_commit: bool = True) -> None:
        await self._conn.execute(
            "UPDATE notebook_entries SET status='cancelled', updated_at=? WHERE id=?",
            (time.time(), task_id),
        )
        if auto_commit:
            await self._conn.commit()
