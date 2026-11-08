"""审计 M2（剩余处）：greeting 抽签结果批量落盘。

背景：greeting_scheduler.tick 里每条 random 计划都单独 await execute 写
next_fire_times/drawn_date，tick 内 N 次往返；且 fire() 抛错时该行抽签结果
丢失（下个 tick 重抽，与已发内容不一致）。改为攒批 + finally 冲刷。
"""
from __future__ import annotations

import types

import pytest

from web.greeting_scheduler import GreetingScheduler


class _FakeDB:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def executemany(self, sql: str, rows) -> None:
        self.calls.append((sql, list(rows)))

    async def execute(self, sql: str, params=None) -> None:  # pragma: no cover - 不该被调用
        self.calls.append(("execute", (sql, params)))


def _scheduler(db: _FakeDB):
    return types.SimpleNamespace(core=types.SimpleNamespace(db=db))


@pytest.mark.asyncio
async def test_flush_writes_one_executemany_for_many_rows():
    db = _FakeDB()
    rows = [('["540"]', "2026-11-08", 1), ('["600"]', "2026-11-08", 2),
            ('["660"]', "2026-11-08", 3)]
    await GreetingScheduler._flush_draw_updates(_scheduler(db), rows)
    assert len(db.calls) == 1
    sql, params = db.calls[0]
    assert "UPDATE greeting_schedules" in sql
    assert params == rows


@pytest.mark.asyncio
async def test_flush_noop_when_empty():
    db = _FakeDB()
    await GreetingScheduler._flush_draw_updates(_scheduler(db), [])
    assert db.calls == []


@pytest.mark.asyncio
async def test_flush_swallows_db_error():
    """写回失败只告警：下一个 tick 会重新抽签，不能因此整轮 tick 崩掉。"""
    class _Boom(_FakeDB):
        async def executemany(self, sql, rows):
            raise RuntimeError("database is locked")

    await GreetingScheduler._flush_draw_updates(_scheduler(_Boom()), [("[]", "d", 1)])
