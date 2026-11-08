"""审计 M5：长驻循环用 long_running 声明，不再每轮刷 bg.task_slow。

背景（2026-11-08 只读实测）：web/server.py 的 _local_ai_health_loop 是
while True 常驻循环，进程活多久它的 elapsed 就多大（观测到 25079.7s），
_spawn 的 30s 阈值必然触发 bg.task_slow，把真正的慢任务告警埋进噪声。
"""
from __future__ import annotations

import types

import pytest
from loguru import logger as loguru_logger

import core.background_tasks as bg


@pytest.fixture
def records():
    items: list[tuple[str, str]] = []
    handler_id = loguru_logger.add(
        lambda m: items.append((m.record["level"].name, m.record["message"])),
        level="DEBUG",
    )
    try:
        yield items
    finally:
        loguru_logger.remove(handler_id)


def _freeze_clock(monkeypatch, jump: float = 100.0) -> None:
    """让每次 time.time() 前进 jump 秒，稳定越过 30s 慢任务阈值。"""
    now = [1_000_000.0]

    def _time() -> float:
        now[0] += jump
        return now[0]

    monkeypatch.setattr(bg, "time", types.SimpleNamespace(time=_time))


@pytest.mark.asyncio
async def test_default_task_still_warns(monkeypatch, records):
    _freeze_clock(monkeypatch)

    async def _noop() -> None:
        return None

    await bg._spawn(_noop())
    assert any("bg.task_slow" in msg for _lvl, msg in records)


@pytest.mark.asyncio
async def test_long_running_task_does_not_warn(monkeypatch, records):
    _freeze_clock(monkeypatch)

    async def _noop() -> None:
        return None

    await bg._spawn(_noop(), long_running=True)
    assert not any("bg.task_slow" in msg for _lvl, msg in records)
    assert any("bg.task_done" in msg and "long_running=True" in msg
               for _lvl, msg in records)
