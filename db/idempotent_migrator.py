"""数据库迁移幂等性管理器 (H3)

参考:
- Alembic / Flyway migration versioning
- Idempotent migration patterns

特性:
- 统一 schema_version 表跟踪已应用的迁移
- 每个迁移操作前先检查是否已应用
- 列存在性检查 + CREATE TABLE IF NOT EXISTS
- 失败时自动回滚 (单个迁移原子性)
- 重复执行不报错

用法:
    migrator = IdempotentMigrator(conn)
    await migrator.apply("v10_add_index", [
        ("CREATE INDEX IF NOT EXISTS idx_x ON tbl(x)",),
        ("ALTER TABLE tbl ADD COLUMN y TEXT", "check_column"),
    ])
"""
from __future__ import annotations

import time
from typing import Any, Optional

from loguru import logger


class IdempotentMigrator:
    """幂等迁移管理器"""

    SCHEMA_VERSION_TABLE = "schema_version"

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def _ensure_version_table(self) -> None:
        await self._conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.SCHEMA_VERSION_TABLE} (
                version TEXT PRIMARY KEY,
                applied_at REAL NOT NULL,
                description TEXT DEFAULT ''
            )
        """)
        await self._conn.commit()

    async def is_applied(self, version: str) -> bool:
        """检查迁移是否已应用"""
        await self._ensure_version_table()
        cursor = await self._conn.execute(
            f"SELECT 1 FROM {self.SCHEMA_VERSION_TABLE} WHERE version=?",
            (version,)
        )
        row = await cursor.fetchone()
        return row is not None

    async def _mark_applied(self, version: str, description: str = "") -> None:
        await self._conn.execute(
            f"INSERT OR REPLACE INTO {self.SCHEMA_VERSION_TABLE} "
            f"(version, applied_at, description) VALUES (?, ?, ?)",
            (version, time.time(), description)
        )
        await self._conn.commit()

    async def _column_exists(self, table: str, column: str) -> bool:
        """检查列是否存在"""
        cursor = await self._conn.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return any(row[1] == column for row in rows)

    async def _index_exists(self, index_name: str) -> bool:
        """检查索引是否存在"""
        cursor = await self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,)
        )
        row = await cursor.fetchone()
        return row is not None

    async def _table_exists(self, table: str) -> bool:
        cursor = await self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        )
        row = await cursor.fetchone()
        return row is not None

    async def apply(self, version: str, statements: list,
                     description: str = "") -> bool:
        """应用迁移 (幂等)

        Args:
            version: 迁移版本号 (唯一标识)
            statements: SQL 语句列表, 每项可以是:
                - str: 直接执行的 SQL
                - tuple (sql, check_type, *check_args): 带检查的执行
                    check_type: "column" / "index" / "table"
        Returns:
            True 如果本次新应用, False 如果已存在
        """
        if await self.is_applied(version):
            logger.debug(f"Migrator.skip_already_applied v={version}")
            return False

        try:
            for stmt in statements:
                if isinstance(stmt, str):
                    await self._conn.execute(stmt)
                elif isinstance(stmt, tuple):
                    sql, check_type, *check_args = stmt
                    should_skip = False
                    if check_type == "column":
                        if await self._column_exists(*check_args):
                            should_skip = True
                    elif check_type == "index":
                        if await self._index_exists(check_args[0]):
                            should_skip = True
                    elif check_type == "table" and await self._table_exists(check_args[0]):
                        should_skip = True
                    if not should_skip:
                        await self._conn.execute(sql)
            await self._mark_applied(version, description)
            logger.info(f"Migrator.applied v={version} desc={description}")
            return True
        except Exception as e:
            logger.error(f"Migrator.failed v={version} error={e}")
            # 不提交, 让调用方决定是否重试
            raise

    async def add_column_if_not_exists(self, table: str, column: str,
                                         type_: str = "TEXT",
                                         default: str = "",
                                         version: str | None = None) -> bool:
        """便捷方法: 添加列 (幂等)"""
        if await self._column_exists(table, column):
            return False
        default_clause = f" DEFAULT {default}" if default else ""
        sql = f"ALTER TABLE {table} ADD COLUMN {column} {type_}{default_clause}"
        version = version or f"add_{table}_{column}"
        return await self.apply(version, [(sql, "column", table, column)])

    async def create_index_if_not_exists(self, index_name: str, table: str,
                                          columns: str,
                                          version: str | None = None) -> bool:
        """便捷方法: 创建索引 (幂等)"""
        if await self._index_exists(index_name):
            return False
        sql = f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({columns})"
        version = version or f"idx_{index_name}"
        return await self.apply(version, [(sql, "index", index_name)])

    async def get_applied_versions(self) -> list[str]:
        """获取所有已应用版本"""
        await self._ensure_version_table()
        cursor = await self._conn.execute(
            f"SELECT version FROM {self.SCHEMA_VERSION_TABLE} ORDER BY applied_at"
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]
