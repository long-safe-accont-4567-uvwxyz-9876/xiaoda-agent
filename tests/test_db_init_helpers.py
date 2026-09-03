"""db/database.py init 拆分的 helper 单元测试。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from db.database import DatabaseManager


@pytest.mark.asyncio
async def test_close_if_present_noop_when_none():
    db = DatabaseManager.__new__(DatabaseManager)
    await db._close_if_present(None, "test")  # 不抛异常


@pytest.mark.asyncio
async def test_close_if_present_closes_and_swallows_error():
    db = DatabaseManager.__new__(DatabaseManager)
    conn = MagicMock()
    conn.close = AsyncMock(side_effect=RuntimeError("boom"))
    await db._close_if_present(conn, "test")  # 不抛异常
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_readonly_probe_failure_closes_candidate_connection(monkeypatch, tmp_path):
    db = DatabaseManager(tmp_path / "readonly-probe.db")
    conn = MagicMock()
    conn.execute = AsyncMock(
        side_effect=[MagicMock(), MagicMock(), RuntimeError("probe failed")]
    )
    conn.close = AsyncMock()
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr("db.database.aiosqlite.connect", connect)

    await db._init_readonly_conn()

    conn.close.assert_awaited_once()
    assert db._readonly_conn is None


def test_ensure_writable_dir_ok(tmp_path):
    db = DatabaseManager.__new__(DatabaseManager)
    db.db_path = tmp_path / "agent.db"
    db._ensure_writable_dir()  # 不抛异常


def test_init_helpers_exist():
    db = DatabaseManager.__new__(DatabaseManager)
    assert callable(db._close_if_present)
    assert callable(db._ensure_writable_dir)
    assert callable(db._init_readonly_conn)
