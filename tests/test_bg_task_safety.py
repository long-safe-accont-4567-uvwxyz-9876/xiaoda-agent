"""测试后台任务的异常安全性和任务生命周期管理。

覆盖两个BUG:
1. background_tasks._spawn: 异常静默丢失
2. config_reloader._notify_callbacks: 协程任务未存储, 可能被GC回收
"""
import asyncio

import pytest

# ── BUG: background_tasks._spawn 异常静默丢失 ──────────────

@pytest.mark.asyncio
async def test_spawn_logs_exception_on_task_failure(caplog):
    """_spawn 创建的任务失败时应记录异常, 不应静默吞没。"""
    from loguru import logger

    from core.background_tasks import _spawn

    warnings_seen = []

    def log_sink(message):
        text = str(message)
        if "task_failed" in text:
            warnings_seen.append(text)

    handler_id = logger.add(log_sink, level="WARNING", format="{message}")

    async def failing_task():
        raise RuntimeError("后台任务测试异常")

    _spawn(failing_task())
    await asyncio.sleep(0.2)

    logger.remove(handler_id)

    # 验证: 异常被记录到日志
    assert len(warnings_seen) > 0, "后台任务失败应记录warning日志"


@pytest.mark.asyncio
async def test_spawn_task_not_garbage_collected():
    """_spawn 创建的任务应被强引用, 不被GC回收。"""
    from core.background_tasks import _bg_tasks, _spawn

    started = asyncio.Event()

    async def slow_task():
        started.set()
        await asyncio.sleep(0.3)

    _spawn(slow_task())
    await started.wait()
    # 任务应在 _bg_tasks 中 (强引用)
    assert len(_bg_tasks) > 0
    await asyncio.sleep(0.4)
    # 完成后应被移除
    assert all(t.done() for t in _bg_tasks)


# ── BUG: config_reloader 协程任务未存储 ──────────────────────

def _make_reloader(tmp_path):
    """创建一个测试用的 ConfigReloader (不依赖真实配置文件)。"""
    from core.config_reloader import ConfigReloader
    config_file = tmp_path / "test_config.json5"
    config_file.write_text('{"test": "value"}')
    reloader = ConfigReloader(str(config_file))
    return reloader


@pytest.mark.asyncio
async def test_config_reloader_async_callback_exception_logged(tmp_path):
    """config_reloader 异步回调失败时应记录异常, 不应静默丢失。"""
    from loguru import logger

    reloader = _make_reloader(tmp_path)
    call_count = 0

    warnings_seen = []
    handler_id = logger.add(lambda m: warnings_seen.append(str(m)),
                             level="WARNING", format="{message}")

    async def failing_async_callback(snap):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("异步回调测试异常")

    reloader.on_change_async(failing_async_callback)
    reloader._notify_callbacks()

    await asyncio.sleep(0.2)
    logger.remove(handler_id)

    assert call_count == 1, "异步回调应被调用"
    # 应有warning日志记录异常
    assert any("callback" in w.lower() or "failed" in w.lower()
                for w in warnings_seen), \
        f"异步回调异常应记录warning日志, 实际warnings: {warnings_seen}"


@pytest.mark.asyncio
async def test_config_reloader_async_task_stored_not_gc(tmp_path):
    """config_reloader 创建的异步任务应被存储, 不被GC回收。"""

    reloader = _make_reloader(tmp_path)
    completed = asyncio.Event()

    async def slow_callback(snap):
        await asyncio.sleep(0.2)
        completed.set()

    reloader.on_change_async(slow_callback)
    reloader._notify_callbacks()

    # 任务应能完成 (未被GC回收)
    await asyncio.wait_for(completed.wait(), timeout=1.0)
    assert completed.is_set()
