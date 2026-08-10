from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from core.background_tasks import BackgroundTaskManager, _spawn


@pytest.mark.asyncio
async def test_manager_shutdown_only_cancels_owned_tasks():
    first = BackgroundTaskManager(MagicMock(), MagicMock())
    second = BackgroundTaskManager(MagicMock(), MagicMock())
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    async def first_job():
        first_started.set()
        await asyncio.Event().wait()

    async def second_job():
        second_started.set()
        await release_second.wait()

    first.start_background_task(first_job())
    second.start_background_task(second_job())
    await first_started.wait()
    await second_started.wait()

    await first.cancel_background_tasks()

    assert not second.get_owned_tasks().pop().cancelled()
    release_second.set()
    await asyncio.sleep(0)
    await second.cancel_background_tasks()


@pytest.mark.asyncio
async def test_nested_spawn_inherits_manager_ownership():
    manager = BackgroundTaskManager(MagicMock(), MagicMock())
    child_started = asyncio.Event()

    async def child():
        child_started.set()
        await asyncio.Event().wait()

    async def parent():
        _spawn(child())

    manager.start_background_task(parent())
    await child_started.wait()
    owned = manager.get_owned_tasks()
    assert len(owned) == 1

    await manager.cancel_background_tasks()
    assert not manager.get_owned_tasks()
