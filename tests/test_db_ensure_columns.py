"""_ensure_columns 幂等列添加的单元测试。"""
from __future__ import annotations

import pytest

from db.database import DatabaseManager


@pytest.mark.asyncio
async def test_ensure_columns_adds_missing_and_skips_existing(tmp_path):
    db = DatabaseManager(tmp_path / "t.db")
    await db.init()
    try:
        await db.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")

        # 首次：添加两列
        await db._ensure_columns("t", {
            "a": "a REAL DEFAULT 0.5",
            "b": "b TEXT DEFAULT 'x'",
        })
        cols = {r["name"] for r in await db.fetch_all("PRAGMA table_info(t)")}
        assert {"a", "b"} <= cols

        # 再次：列已存在，不应报错，且 schema 不变
        await db._ensure_columns("t", {"a": "a REAL DEFAULT 0.5"})
        cols2 = {r["name"] for r in await db.fetch_all("PRAGMA table_info(t)")}
        assert cols2 == cols
    finally:
        await db.close()
