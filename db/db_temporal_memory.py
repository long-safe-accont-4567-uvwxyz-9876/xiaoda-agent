import time
from typing import Any

import aiosqlite


class TemporalMemoryDB:
    """双时态事实和偏好的只读视图与版本变更操作。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def _fetch(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(sql, tuple(params))
        return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    def _filters(values: dict[str, Any]) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in values.items():
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        return clauses, params

    async def get_current_facts(
        self,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        object: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses, params = self._filters(
            {"subject": subject, "predicate": predicate, "object": object}
        )
        clauses.extend(["status = 'active'", "valid_to IS NULL", "expired_at IS NULL"])
        sql = "SELECT * FROM memory_facts WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return await self._fetch(sql, params)

    async def get_facts_as_of(
        self,
        valid_time: float,
        known_at: float | None = None,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        object: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        known_at = time.time() if known_at is None else known_at
        clauses, params = self._filters(
            {"subject": subject, "predicate": predicate, "object": object}
        )
        clauses.extend(
            [
                "status NOT IN ('rejected', 'uncertain', 'pending_review')",
                "(valid_from IS NULL OR valid_from <= ?)",
                "(valid_to IS NULL OR valid_to > ?)",
                "learned_at <= ?",
                "(expired_at IS NULL OR expired_at > ?)",
            ]
        )
        params.extend([valid_time, valid_time, known_at, known_at])
        sql = "SELECT * FROM memory_facts WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return await self._fetch(sql, params)

    async def get_current_preferences(
        self,
        *,
        preference_key: str | None = None,
        preference_type: str | None = None,
        scope: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses, params = self._filters(
            {"preference_key": preference_key, "preference_type": preference_type, "scope": scope}
        )
        clauses.extend(["status = 'active'", "valid_to IS NULL", "expired_at IS NULL"])
        sql = "SELECT * FROM memory_preferences WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return await self._fetch(sql, params)

    async def get_preferences_as_of(
        self,
        valid_time: float,
        known_at: float | None = None,
        *,
        preference_key: str | None = None,
        preference_type: str | None = None,
        scope: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        known_at = time.time() if known_at is None else known_at
        clauses, params = self._filters(
            {"preference_key": preference_key, "preference_type": preference_type, "scope": scope}
        )
        clauses.extend(
            [
                "status NOT IN ('rejected', 'uncertain', 'pending_review')",
                "(valid_from IS NULL OR valid_from <= ?)",
                "(valid_to IS NULL OR valid_to > ?)",
                "learned_at <= ?",
                "(expired_at IS NULL OR expired_at > ?)",
            ]
        )
        params.extend([valid_time, valid_time, known_at, known_at])
        sql = "SELECT * FROM memory_preferences WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return await self._fetch(sql, params)

    async def supersede_fact(
        self,
        old_fact_id: int,
        new_fact_id: int,
        *,
        effective_at: float | None = None,
        known_at: float | None = None,
    ) -> None:
        await self._supersede(
            table="memory_facts",
            old_id=old_fact_id,
            new_id=new_fact_id,
            effective_at=effective_at,
            known_at=time.time() if known_at is None else known_at,
        )

    async def supersede_preference(
        self,
        old_preference_id: int,
        new_preference_id: int,
        *,
        effective_at: float | None = None,
        known_at: float | None = None,
    ) -> None:
        await self._supersede(
            table="memory_preferences",
            old_id=old_preference_id,
            new_id=new_preference_id,
            effective_at=effective_at,
            known_at=time.time() if known_at is None else known_at,
        )

    async def _supersede(
        self,
        *,
        table: str,
        old_id: int,
        new_id: int,
        effective_at: float | None,
        known_at: float,
    ) -> None:
        if old_id == new_id:
            raise ValueError("a record cannot supersede itself")
        if table not in ("memory_facts", "memory_preferences"):
            raise ValueError(f"Invalid table name: {table}")
        # BEGIN IMMEDIATE 立即获取写锁，串行化并发 supersede，避免双写。
        # 配合 UPDATE ... WHERE status='active' + rowcount 校验，构成乐观+悲观双保险：
        # 即使锁级别不足也通过 rowcount==0 检测到并发已 supersede。
        try:
            await self._conn.execute("BEGIN IMMEDIATE")
        except Exception:
            # 已在事务中时降级为隐式事务（aiosqlite 默认开启隐式事务）
            pass
        try:
            cursor = await self._conn.execute(
                f"SELECT id, status FROM {table} WHERE id IN (?, ?)", (old_id, new_id)
            )
            rows = {row["id"]: row for row in await cursor.fetchall()}
            if old_id not in rows or new_id not in rows:
                await self._conn.rollback()
                raise ValueError("both old and new records must exist")
            if rows[old_id]["status"] != "active":
                await self._conn.rollback()
                raise ValueError("only an active record can be superseded")
            if rows[new_id]["status"] != "active":
                await self._conn.rollback()
                raise ValueError("the superseding record must be active")
            # 关键修复：UPDATE 的 WHERE 加上 status='active' 条件，
            # 配合 cursor.rowcount==1 校验。若并发 supersede 已先把 old_id 标记为
            # superseded，本 UPDATE 影响 0 行，rowcount==0 时跳过提交，避免双写。
            if effective_at is None:
                cur = await self._conn.execute(
                    f"""UPDATE {table}
                        SET status='superseded', expired_at=?, superseded_by=?, updated_at=?
                        WHERE id=? AND status='active'""",
                    (known_at, new_id, known_at, old_id),
                )
            else:
                cur = await self._conn.execute(
                    f"""UPDATE {table}
                        SET status='superseded', valid_to=?, expired_at=?, superseded_by=?, updated_at=?
                        WHERE id=? AND status='active'""",
                    (effective_at, known_at, new_id, known_at, old_id),
                )
            if cur.rowcount != 1:
                # 并发已 supersede 该记录，本操作幂等跳过（不视为错误）
                await self._conn.rollback()
                return
            await self._conn.commit()
        except Exception:
            try:
                await self._conn.rollback()
            except Exception:
                pass
            raise
