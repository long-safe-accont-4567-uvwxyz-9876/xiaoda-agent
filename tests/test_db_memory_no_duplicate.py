"""验证 db_memory.py 中 get_recent_conversations 无重复定义且返回正确顺序。

缺陷 D1: 曾存在两个同名方法定义，后一个覆盖前一个，导致 reversed(rows) 逻辑被静默丢弃，
返回顺序与预期相反（最新的对话在最前而非最末）。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock


class _FakeRow:
    """模拟 aiosqlite.Row：支持 dict() 转换和 key 索引。"""
    def __init__(self, data: dict) -> None:
        self._data = data
    def __getitem__(self, k):
        return self._data[k]
    def keys(self):
        return self._data.keys()


@pytest.mark.asyncio
async def test_get_recent_conversations_returns_oldest_first():
    """get_recent_conversations 应返回由旧到新的顺序（reversed(rows)）。"""
    from db.db_memory import MemoryDB

    mock_conn = MagicMock()
    # 模拟 fetchall 返回 id 降序的行（数据库 ORDER BY id DESC）
    mock_rows = [
        _FakeRow({"id": 3, "msg": "latest"}),
        _FakeRow({"id": 2, "msg": "middle"}),
        _FakeRow({"id": 1, "msg": "oldest"}),
    ]
    mock_cursor = MagicMock()
    mock_cursor.fetchall = AsyncMock(return_value=mock_rows)
    mock_conn.execute = AsyncMock(return_value=mock_cursor)
    mock_conn.row_factory = None

    db = MemoryDB(mock_conn)
    results = await db.get_recent_conversations(limit=3)

    # reversed(rows) 后， oldest 应在最前，latest 应在最后
    assert [r["id"] for r in results] == [1, 2, 3]


def test_get_recent_conversations_defined_once():
    """通过 inspect 确认类中只有一个 get_recent_conversations 定义。"""
    import inspect
    from db.db_memory import MemoryDB

    members = inspect.getmembers(MemoryDB, predicate=inspect.isfunction)
    names = [name for name, _ in members]
    assert names.count("get_recent_conversations") == 1, (
        "MemoryDB 中 get_recent_conversations 应只定义一次，"
        "多次定义会导致后一个静默覆盖前一个"
    )


@pytest.mark.asyncio
async def test_get_recent_conversations_filters_by_user_id():
    """传入 user_id 时应带 WHERE user_id = ? 过滤。"""
    from db.db_memory import MemoryDB

    mock_conn = MagicMock()
    mock_rows = [
        _FakeRow({"id": 2, "user_id": "u1"}),
        _FakeRow({"id": 1, "user_id": "u1"}),
    ]
    mock_cursor = MagicMock()
    mock_cursor.fetchall = AsyncMock(return_value=mock_rows)
    mock_conn.execute = AsyncMock(return_value=mock_cursor)
    mock_conn.row_factory = None

    db = MemoryDB(mock_conn)
    results = await db.get_recent_conversations(limit=5, user_id="u1")

    # 验证 SQL 带 user_id 参数
    call_args = mock_conn.execute.call_args
    assert "user_id = ?" in call_args[0][0]
    assert call_args[0][1] == ("u1", 5)
    assert all(r["user_id"] == "u1" for r in results)
