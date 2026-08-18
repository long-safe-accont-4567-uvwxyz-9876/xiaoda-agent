"""验证 _spawn 默认无超时语义与显式超时的行为差异。

覆盖 Task 7 的两项断言：
1. 默认 timeout=None 时，sleep(0.2) 的协程能完整跑完，不被取消。
2. 显式传 timeout=0.05 时，慢协程被取消并记录 bg.task_timeout 日志。
"""
from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from core.background_tasks import _spawn, _bg_tasks


def _attach_warning_sink() -> tuple[list[str], int]:
    """挂一个 loguru sink 收集 WARNING 日志文本，返回 (列表, handler_id)。"""
    seen: list[str] = []

    def _sink(message):
        seen.append(str(message))

    handler_id = logger.add(_sink, level="WARNING", format="{message}")
    return seen, handler_id


async def test_spawn_default_no_timeout_runs_to_completion():
    """默认 timeout=None 时，慢协程不应被取消，应完整执行到末尾。"""
    completed = asyncio.Event()

    async def slow_coro():
        await asyncio.sleep(0.2)
        completed.set()

    _spawn(slow_coro())
    # 给协程足够时间跑完；若被取消 completed 不会 set
    await asyncio.sleep(0.4)
    assert completed.is_set(), "默认无超时时，sleep(0.2) 协程应能跑完"


async def test_spawn_explicit_short_timeout_logs_task_timeout():
    """显式 timeout=0.05 时，慢协程被取消并记录 bg.task_timeout。"""
    seen, handler_id = _attach_warning_sink()
    try:
        ran_to_end = False

        async def slow_coro():
            await asyncio.sleep(0.5)
            # 若协程未被取消走到这里，标记以供断言
            nonlocal ran_to_end
            ran_to_end = True

        _spawn(slow_coro(), timeout=0.05)
        # 等到超时 + 日志落盘
        await asyncio.sleep(0.3)
        assert not ran_to_end, "协程应在 timeout=0.05 时被取消，不应走到末尾"
        assert any("bg.task_timeout" in m for m in seen), \
            f"应记录 bg.task_timeout 日志，实际 warnings: {seen}"
    finally:
        logger.remove(handler_id)


async def test_spawn_default_no_timeout_keeps_strong_ref():
    """默认无超时时，_bg_tasks 仍持有强引用直到任务完成。"""
    started = asyncio.Event()

    async def slow_coro():
        started.set()
        await asyncio.sleep(0.15)

    _spawn(slow_coro())
    await started.wait()
    assert len(_bg_tasks) > 0, "任务运行期间应在 _bg_tasks 中保持强引用"
    await asyncio.sleep(0.3)
    assert all(t.done() for t in _bg_tasks), "任务完成后应被回调移除"
