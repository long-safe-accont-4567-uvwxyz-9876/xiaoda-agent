from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from web.media_tasks import MediaTaskQueue
from web import ws_hub


@pytest.mark.asyncio
async def test_media_queue_stop_waits_for_worker_cleanup():
    queue = MediaTaskQueue.__new__(MediaTaskQueue)
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def worker():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    queue._worker = asyncio.create_task(worker())
    await started.wait()
    await queue.stop()

    assert cleaned.is_set()
    assert queue._worker is None


@pytest.mark.asyncio
async def test_media_cleanup_can_stop_and_restart(monkeypatch):
    monkeypatch.setattr(ws_hub, "_MEDIA_CLEANUP_TASK", None)
    monkeypatch.setattr(ws_hub, "_cleanup_old_media", lambda: 0)
    monkeypatch.setattr(ws_hub, "_MEDIA_CLEANUP_INTERVAL_SECONDS", 3600)

    ws_hub.start_media_cleanup()
    first = ws_hub._MEDIA_CLEANUP_TASK
    assert first is not None
    await ws_hub.stop_media_cleanup()
    assert first.done()
    assert ws_hub._MEDIA_CLEANUP_TASK is None

    ws_hub.start_media_cleanup()
    second = ws_hub._MEDIA_CLEANUP_TASK
    assert second is not None and second is not first
    await ws_hub.stop_media_cleanup()
