"""P0-09: background_tasks._spawn() 无事件循环保护 — 测试"""
from __future__ import annotations

import asyncio

import pytest

from core.background_tasks import _bg_tasks, _spawn


def test_spawn_no_running_loop():
    """_spawn() outside async context should not crash, just log error."""
    async def dummy():
        pass

    _spawn(dummy())
    assert len(_bg_tasks) == 0


def test_spawn_no_running_loop_cleanup():
    """_spawn() outside async context should close the coroutine (no leak)."""
    async def dummy():
        pass

    coro = dummy()
    _spawn(coro)
    assert coro.cr_running is False


@pytest.mark.asyncio
async def test_spawn_with_running_loop():
    """_spawn() inside async context should create task normally."""
    from core.background_tasks import _bg_tasks
    before = len(_bg_tasks)

    async def dummy():
        await asyncio.sleep(0.01)

    _spawn(dummy())
    assert len(_bg_tasks) == before + 1

    await asyncio.sleep(0.05)
    assert len(_bg_tasks) == before
