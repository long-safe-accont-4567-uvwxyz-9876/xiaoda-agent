"""MemoryDB 的情景记忆 CRUD 方法组 —— 拆分自 db/db_memory.py。

Mixin 组合：MemoryDB 继承 EpisodicMixin 获得情景记忆写入/更新/归档方法。
_sync_fts 保留在 MemoryDB（跨组共享且依赖模块级计数器 _record_fts_sync_failure），
本 Mixin 方法经 MRO 调用 self._sync_fts()。
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger

from db.db_memory_utils import _sql_placeholders


class EpisodicMixin:
    """情景记忆写入/更新/归档方法组。"""

    async def migrate_add_source_column(self) -> None:
        """迁移：为旧库的 episodic_memories 表添加 source 列（已存在则忽略）"""
        try:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN source TEXT DEFAULT 'user'"
            )
            await self._conn.commit()
        except Exception as e:
            # 列已存在时忽略
            logger.debug("db_memory.migrate_add_source_column skipped: {}", e)

    async def insert_episodic_memory(self, summary: str, importance: float = 0.5,
                                      emotion_label: str = "", session_id: str = "user",
                                      embedding_id: int = -1, auto_commit: bool = True,
                                      source: str = "user",
                                      scope: Any | None = None,
                                      is_raw: int = 0,
                                      memory_type: str = "event",
                                      phase: str | None = None,
                                      stability: float | None = None,
                                      reinforcement_count: int | None = None) -> Any:
        """插入情景记忆。

        Args:
            scope: Scope 对象（mem0 SPEC 优化）。传入时使用 scope 的 user_id/session_id/agent_id。
            is_raw: 0=提炼知识（允许 UPDATE/DELETE），1=原始记录（append-only）。
        """
        # scope 优先级高于单独的 session_id 参数
        if scope is not None:
            user_id = scope.user_id
            agent_id = scope.agent_id
            session_id = scope.session_id
        else:
            user_id = "default"
            agent_id = "xiaoda"
        columns = [
            "timestamp", "summary", "importance", "emotion_label", "session_id",
            "embedding_id", "source", "user_id", "agent_id", "is_raw",
        ]
        values: list[Any] = [
            time.time(), summary, importance, emotion_label, session_id,
            embedding_id, source, user_id, agent_id, is_raw,
        ]
        available = getattr(self, "_episodic_columns_cache", None)
        if available is None:
            schema_cursor = await self._conn.execute(
                "PRAGMA table_info(episodic_memories)"
            )
            available = {row[1] for row in await schema_cursor.fetchall()}
            self._episodic_columns_cache = available
        optional_values = {
            "memory_type": memory_type,
            "phase": phase or "buffer",
            "stability": stability if stability is not None else 3.0,
            "reinforcement_count": (
                reinforcement_count if reinforcement_count is not None else 0
            ),
        }
        for column, value in optional_values.items():
            if column in available:
                columns.append(column)
                values.append(value)
        placeholders = ", ".join("?" for _ in columns)
        cursor = await self._conn.execute(
            f"INSERT INTO episodic_memories ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            values,
        )
        mem_id = cursor.lastrowid
        if auto_commit:
            await self._conn.commit()
        # 同步写入 FTS 索引
        await self._sync_fts(mem_id, summary, "db_memory.fts_insert_failed",
                             delete_first=False, auto_commit=auto_commit)
        return mem_id

    async def get_memory_by_id(self, memory_id: int) -> dict | None:
        cursor = await self._read_conn().execute(
            "SELECT * FROM episodic_memories WHERE id=?", (memory_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_memories_by_ids(self, ids: list[int]) -> list[dict]:
        """批量获取记忆记录（向量检索后批量 JOIN 主表，消除 N 次逐条查询）"""
        if not ids:
            return []
        # 参数化占位符，防止 SQL 注入
        placeholders = _sql_placeholders(ids)
        cursor = await self._read_conn().execute(
            f"SELECT * FROM episodic_memories WHERE id IN ({placeholders})",
            ids,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_pending_memory_classifications(
        self, scope: Any, limit: int = 50
    ) -> list[dict]:
        """Return one bounded pending classification batch for a user/agent scope."""
        bounded_limit = max(1, min(int(limit), 50))
        cursor = await self._read_conn().execute(
            "SELECT * FROM episodic_memories "
            "WHERE user_id=? AND agent_id=? AND classification_status='pending' "
            "ORDER BY id ASC LIMIT ?",
            (scope.user_id, scope.agent_id, bounded_limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def update_memory_classification(
        self,
        memory_id: int,
        *,
        memory_type: str,
        importance: float,
        classification_status: str,
        classification_version: int,
        classified_at: float,
        phase: str,
        stability: float,
        reinforcement_count: int,
        auto_commit: bool = True,
    ) -> bool:
        """Atomically persist classification, effective importance, and FSRS state."""
        cursor = await self._conn.execute(
            "UPDATE episodic_memories SET memory_type=?, importance=?, "
            "classification_status=?, classification_version=?, classified_at=?, "
            "phase=?, stability=?, reinforcement_count=? WHERE id=?",
            (
                memory_type,
                importance,
                classification_status,
                classification_version,
                classified_at,
                phase,
                stability,
                reinforcement_count,
                memory_id,
            ),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.rowcount > 0

    async def update_emotion_label(self, mem_id: int, label: str) -> None:
        await self._conn.execute(
            "UPDATE episodic_memories SET emotion_label = ? WHERE id = ?",
            (label, mem_id),
        )
        await self._conn.commit()

    async def update_distill_status(self, mem_id: int, status: str) -> None:
        """更新蒸馏状态字段（不污染 emotion_label）。"""
        await self._conn.execute(
            "UPDATE episodic_memories SET distill_status = ? WHERE id = ?",
            (status, mem_id),
        )
        await self._conn.commit()

    async def update_memory_summary(self, mem_id: int, new_summary: str) -> None:
        cursor = await self._conn.execute(
            "UPDATE episodic_memories SET summary = ? WHERE id = ? AND is_raw = 0",
            (new_summary, mem_id),
        )
        if cursor.rowcount > 0:
            await self._sync_fts(
                mem_id,
                new_summary,
                "db_memory.fts_sync_on_summary_update_failed",
            )
        await self._conn.commit()

    async def update_fallback_raw(self, mem_id: int, new_summary: str, label: str,
                                    distill_status: str = "") -> None:
        if distill_status:
            cursor = await self._conn.execute(
                "UPDATE episodic_memories SET summary = ?, emotion_label = ?, "
                "distill_status = ? WHERE id = ? AND is_raw = 0",
                (new_summary, label, distill_status, mem_id),
            )
            await self._conn.execute(
                "UPDATE episodic_memories SET emotion_label = ?, distill_status = ? "
                "WHERE id = ? AND is_raw = 1",
                (label, distill_status, mem_id),
            )
        else:
            cursor = await self._conn.execute(
                "UPDATE episodic_memories SET summary = ?, emotion_label = ? "
                "WHERE id = ? AND is_raw = 0",
                (new_summary, label, mem_id),
            )
            await self._conn.execute(
                "UPDATE episodic_memories SET emotion_label = ? "
                "WHERE id = ? AND is_raw = 1",
                (label, mem_id),
            )
        if cursor.rowcount > 0:
            await self._sync_fts(
                mem_id, new_summary, "db_memory.fts_sync_on_fallback_failed"
            )
        await self._conn.commit()

    async def increment_access_count(self, memory_id: int, auto_commit: bool = True) -> None:
        """递增记忆访问计数（检索强化）"""
        await self._conn.execute(
            "UPDATE episodic_memories SET access_count = access_count + 1 WHERE id = ?",
            (memory_id,),
        )
        if auto_commit:
            await self._conn.commit()

    async def batch_increment_access_count(self, memory_ids: list[int],
                                            auto_commit: bool = True) -> None:
        """批量递增记忆访问计数（消除 N+1：单条 UPDATE + IN 子句）。

        行为等价于对每个 id 调用 increment_access_count(id, auto_commit=False)，
        但只发一次 SQL。所有 id 统一 +1（与单条版本语义一致）。
        """
        if not memory_ids:
            return
        placeholders = _sql_placeholders(memory_ids)
        await self._conn.execute(
            f"UPDATE episodic_memories SET access_count = access_count + 1 "
            f"WHERE id IN ({placeholders})",
            memory_ids,
        )
        if auto_commit:
            await self._conn.commit()

    async def archive_memory(self, memory_id: int) -> None:
        """Archive a memory without changing immutable raw scope fields."""
        await self._conn.execute(
            "UPDATE episodic_memories SET "
            "status = CASE WHEN is_raw = 1 THEN 'archived' ELSE status END, "
            "session_id = CASE WHEN is_raw = 0 THEN 'archived' ELSE session_id END "
            "WHERE id = ?",
            (memory_id,),
        )
        await self._conn.commit()

    async def archive_memories_batch(self, memory_ids: list[int]) -> None:
        """Archive memories in one update while preserving raw scope."""
        if not memory_ids:
            return
        placeholders = _sql_placeholders(memory_ids)
        await self._conn.execute(
            "UPDATE episodic_memories SET "
            "status = CASE WHEN is_raw = 1 THEN 'archived' ELSE status END, "
            "session_id = CASE WHEN is_raw = 0 THEN 'archived' ELSE session_id END "
            f"WHERE id IN ({placeholders})",
            memory_ids,
        )
        await self._conn.commit()
