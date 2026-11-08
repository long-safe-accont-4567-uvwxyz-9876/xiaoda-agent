"""审计 M1：通用只读入口 fetch_all 必须走只读连接池。

背景（2026-11-08 只读实测）：db/database.py 建了 _READ_POOL_SIZE=8 条只读
连接、get_read_conn() 轮询取用，但通用读入口 fetch_all() 用的是 self._conn
主连接，写路径 execute() 默认 auto_commit=True 每条语句一次 commit，读写
串行在同一条 aiosqlite 线程上（进程内 12 条 _connection_worker_thread）。

本测试用"主连接一旦被 SELECT 命中就抛错"作为判据，并覆盖三条边界：
PRAGMA 留主连接、写事务体内留主连接、只读连接失败回退主连接。
"""
from __future__ import annotations

import pytest

from db.database import DatabaseManager


async def _make_db(tmp_path) -> DatabaseManager:
    db = DatabaseManager(db_path=tmp_path / "agent.db")
    await db.init()
    return db


@pytest.mark.asyncio
async def test_select_routed_to_read_pool(tmp_path):
    db = await _make_db(tmp_path)
    try:
        assert db._read_pool, "只读连接池没有建立（8 条期望）"
        assert all(id(c) != id(db._conn) for c in db._read_pool)

        async def _boom(*_a, **_k):
            raise AssertionError("SELECT 打到了主连接，应走只读连接池")

        db._conn.execute_fetchall = _boom  # type: ignore[method-assign]
        rows = await db.fetch_all("SELECT 1 AS one")
        assert rows == [{"one": 1}]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pragma_stays_on_main_connection(tmp_path):
    db = await _make_db(tmp_path)
    try:
        markers = []

        async def _main_marker(sql, params=()):
            markers.append(sql)
            return [{"via": "main"}]

        db._conn.execute_fetchall = _main_marker  # type: ignore[method-assign]
        rows = await db.fetch_all("PRAGMA table_info(conversation_logs)")
        assert rows == [{"via": "main"}]
        assert markers == ["PRAGMA table_info(conversation_logs)"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_select_inside_write_transaction_stays_on_main(tmp_path):
    """写事务体内必须读到自己的未提交数据，不能换连接。"""
    db = await _make_db(tmp_path)
    try:
        async def _boom(*_a, **_k):
            raise AssertionError("写事务体内的 SELECT 不应走只读连接")

        for conn in db._read_pool:
            conn.execute_fetchall = _boom  # type: ignore[method-assign]
        async with db.write_transaction():
            rows = await db.fetch_all("SELECT 1 AS one")
        assert rows == [{"one": 1}]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_read_pool_failure_falls_back_to_main(tmp_path):
    db = await _make_db(tmp_path)
    try:
        async def _broken(*_a, **_k):
            raise OSError("read-only conn is gone")

        for conn in db._read_pool:
            conn.execute_fetchall = _broken  # type: ignore[method-assign]
        rows = await db.fetch_all("SELECT 1 AS one")
        assert rows == [{"one": 1}]
    finally:
        await db.close()
