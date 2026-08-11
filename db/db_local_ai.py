"""db.db_local_ai — installed_models 表的原始 SQL CRUD。

设计参考 db_temporal_memory.py：构造时接收 (conn, write_transaction)，
多语句写事务统一走 write_transaction() 串行化锁，防止 aiosqlite 单连接
共享事务状态导致的脏事务/数据丢失。

表 installed_models（迁移 v25）记录本地已安装模型（含内置 BGE 嵌入模型），
提供 list/get/insert/update/delete 原子操作及 bundled 模型种子查询。
所有 metadata 字段以 TEXT(json.dumps, ensure_ascii=False) 形式存储；
installed_at 字段以 ISO-8601 字符串存储（保留 UTC 时区信息）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import weakref
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import aiosqlite

_TRANSACTION_LOCKS: weakref.WeakKeyDictionary[aiosqlite.Connection, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


class ModelMutationStatus(str, Enum):
    NOT_FOUND = "not_found"
    BUNDLED = "bundled"
    UPDATED = "updated"
    DELETED = "deleted"


def transaction_lock_for(conn: aiosqlite.Connection) -> asyncio.Lock:
    lock = _TRANSACTION_LOCKS.get(conn)
    if lock is None:
        lock = asyncio.Lock()
        _TRANSACTION_LOCKS[conn] = lock
    return lock


def _utcnow_iso() -> str:
    """返回当前 UTC 时间的 ISO-8601 字符串（带 Z 后缀）。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _datetime_to_iso(value: datetime | str) -> str:
    """将 datetime 或 ISO-8601 字符串统一编码为 ISO-8601 TEXT 存储值。"""
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        # 校验可解析，再回写规范格式
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.isoformat().replace("+00:00", "Z")
    raise TypeError(f"installed_at must be datetime or ISO string, got {type(value).__name__}")


def _iso_to_datetime(value: str) -> datetime:
    """将 ISO-8601 TEXT 还原为 timezone-aware UTC datetime。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    """将 aiosqlite.Row 转为可序列化 dict，并将 metadata TEXT 解析为 dict。"""
    data = dict(row)
    raw_meta = data.get("metadata")
    if raw_meta is None or raw_meta == "":
        data["metadata"] = {}
    elif isinstance(raw_meta, str):
        try:
            data["metadata"] = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            data["metadata"] = {}
    return data


class LocalAIDB:
    """installed_models 表的只读视图与原子写操作。"""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        write_transaction: Callable[[], contextlib.AbstractAsyncContextManager] | None = None,
    ) -> None:
        self._conn = conn
        self._write_transaction = write_transaction
        self._transaction_lock = transaction_lock_for(conn)

    @contextlib.asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """统一事务边界：优先使用 DatabaseManager.write_transaction 串行化锁。"""
        if self._write_transaction is not None:
            async with self._write_transaction() as conn:
                yield conn
            return
        async with self._transaction_lock:
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                await self._conn.commit()
            except BaseException:
                await asyncio.shield(self._conn.rollback())
                raise

    # ── 查询 ──────────────────────────────────────────────

    async def list_models(self) -> list[dict[str, Any]]:
        """返回所有已安装模型，按 installed_at 升序排列。"""
        cursor = await self._conn.execute(
            "SELECT * FROM installed_models ORDER BY installed_at ASC"
        )
        rows = await cursor.fetchall()
        return [_row_to_dict(row) for row in rows]

    async def get_model(self, model_id: str) -> dict[str, Any] | None:
        """按主键查询单个模型，不存在返回 None。"""
        cursor = await self._conn.execute(
            "SELECT * FROM installed_models WHERE id = ?",
            (model_id,),
        )
        row = await cursor.fetchone()
        return _row_to_dict(row) if row is not None else None

    async def count_by_directory(self, directory: str) -> int:
        """返回与指定 directory 路径冲突的记录数（应仅 0 或 1）。"""
        cursor = await self._conn.execute(
            "SELECT COUNT(*) AS n FROM installed_models WHERE directory = ?",
            (directory,),
        )
        row = await cursor.fetchone()
        return int(row["n"]) if row is not None else 0

    async def bundled_entry_exists(self, model_id: str) -> bool:
        """检查指定 ID 的 bundled 模型是否已存在（用于幂等种子）。"""
        cursor = await self._conn.execute(
            "SELECT 1 FROM installed_models WHERE id = ? LIMIT 1",
            (model_id,),
        )
        return await cursor.fetchone() is not None

    # ── 写入 ──────────────────────────────────────────────

    async def insert_model(
        self, record: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any] | None]:
        """插入一条 installed_models 记录。

        数据库唯一约束负责冲突仲裁，冲突时返回字段名，成功时返回插入记录。
        """
        async with self._transaction() as conn:
            try:
                cursor = await conn.execute(
                    """
                    INSERT INTO installed_models (
                        id, catalog_id, revision, purpose, directory,
                        manifest_checksum, validation_state, ownership,
                        installed_at, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING *
                    """,
                    (
                        record["id"],
                        record["catalog_id"],
                        record["revision"],
                        record["purpose"],
                        record["directory"],
                        record["manifest_checksum"],
                        record["validation_state"],
                        record["ownership"],
                        _datetime_to_iso(record["installed_at"]),
                        json.dumps(record.get("metadata", {}), ensure_ascii=False),
                    ),
                )
            except aiosqlite.IntegrityError:
                cursor = await conn.execute(
                    "SELECT id, directory FROM installed_models WHERE id = ? OR directory = ?",
                    (record["id"], record["directory"]),
                )
                conflicts = await cursor.fetchall()
                if any(row["id"] == record["id"] for row in conflicts):
                    return "id", None
                if any(row["directory"] == record["directory"] for row in conflicts):
                    return "directory", None
                raise
            saved = await cursor.fetchone()
            return None, _row_to_dict(saved)

    async def mark_validation_if_mutable(
        self, model_id: str, validation_state: str, manifest_checksum: str
    ) -> tuple[ModelMutationStatus, dict[str, Any] | None]:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE installed_models
                SET validation_state = ?, manifest_checksum = ?
                WHERE id = ? AND ownership != 'bundled'
                RETURNING *
                """,
                (validation_state, manifest_checksum, model_id),
            )
            saved = await cursor.fetchone()
            if saved is not None:
                return ModelMutationStatus.UPDATED, _row_to_dict(saved)
            cursor = await conn.execute(
                "SELECT ownership FROM installed_models WHERE id = ?",
                (model_id,),
            )
            row = await cursor.fetchone()
            if row is not None and row["ownership"] == "bundled":
                return ModelMutationStatus.BUNDLED, None
            return ModelMutationStatus.NOT_FOUND, None

    async def delete_if_mutable(self, model_id: str) -> ModelMutationStatus:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM installed_models
                WHERE id = ? AND ownership != 'bundled'
                RETURNING id
                """,
                (model_id,),
            )
            row = await cursor.fetchone()
            if row is not None:
                return ModelMutationStatus.DELETED
            cursor = await conn.execute(
                "SELECT ownership FROM installed_models WHERE id = ?",
                (model_id,),
            )
            row = await cursor.fetchone()
            if row is not None and row["ownership"] == "bundled":
                return ModelMutationStatus.BUNDLED
            return ModelMutationStatus.NOT_FOUND

    # ── 种子（迁移用） ───────────────────────────────────

    async def seed_bundled_model(self, record: dict[str, Any]) -> None:
        """幂等插入 bundled 模型记录：若已存在则跳过。

        迁移函数 _migrate_v25() 调用此方法，确保重跑迁移不会产生重复 bundled
        条目（即使 schema_version 被人工回退也不会重复插入）。
        """
        if await self.bundled_entry_exists(record["id"]):
            return
        async with self._transaction() as conn:
            # 二次防御：INSERT OR IGNORE 即使并发也安全
            await conn.execute(
                """
                INSERT OR IGNORE INTO installed_models (
                    id, catalog_id, revision, purpose, directory,
                    manifest_checksum, validation_state, ownership,
                    installed_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    record["catalog_id"],
                    record["revision"],
                    record["purpose"],
                    record["directory"],
                    record["manifest_checksum"],
                    record["validation_state"],
                    record["ownership"],
                    _datetime_to_iso(record["installed_at"]),
                    json.dumps(record.get("metadata", {}), ensure_ascii=False),
                ),
            )


__all__ = ["LocalAIDB", "ModelMutationStatus", "transaction_lock_for"]
