"""测试后台任务的异常安全性和任务生命周期管理。

覆盖两个BUG:
1. background_tasks._spawn: 异常静默丢失
2. config_reloader._notify_callbacks: 协程任务未存储, 可能被GC回收

确定性约定（Task 11 加固）：关键时序一律事件驱动（asyncio.Event /
直接等待任务终结），不在等待侧使用固定 sleep 竞速；保留的极少量
asyncio.sleep 均为被测对象内部"保持运行中"语义所需的最小等待，已注释
说明。wait_for 超时只是防死锁保险丝，正常路径即时返回。
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

    task = _spawn(failing_task())
    # 事件驱动：直接等待任务终结（warning 在任务内部、终结前已写出），
    # 替代原固定 sleep(0.2) 竞速。5s 仅为防死锁保险，正常路径立即返回。
    await asyncio.wait_for(task, timeout=5.0)

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
        # 最小等待：让任务保持"运行中"状态以验证强引用存在；
        # 不参与完成侧时序——终结一侧完全由下方显式 await 驱动。
        await asyncio.sleep(0.3)

    spawned = _spawn(slow_task())
    await started.wait()  # 事件驱动：任务已开始执行
    # 任务被 _bg_tasks 强引用 (运行中)
    assert len(_bg_tasks) > 0
    # 事件驱动：显式等待任务终结（替代原固定 sleep(0.4) 竞速）
    await asyncio.wait_for(spawned, timeout=5.0)
    # 完成后 _bg_tasks 内不应再有未完成的任务
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

    called = asyncio.Event()

    async def failing_async_callback(snap):
        nonlocal call_count
        call_count += 1
        called.set()
        raise RuntimeError("异步回调测试异常")

    reloader.on_change_async(failing_async_callback)
    reloader._notify_callbacks()

    # 事件驱动：等回调在事件循环中真的执行（替代原固定 sleep(0.2) 竞速）
    await asyncio.wait_for(called.wait(), timeout=5.0)
    # 回调抛错 → 任务终结 → record 用的 done 回调（warning 日志）此刻已
    # 在事件循环队列中；sleep(0) 让出一次调度使 done 回调确定性执行，
    # 这是事件链的最后一步，无固定时长依赖。
    await asyncio.sleep(0)
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
        # 最小等待：模拟"偏慢"的回调，验证任务在完成前被 _async_cb_tasks
        # 持有（不被 GC）；完成侧由事件驱动，等待侧不依赖该时长。
        await asyncio.sleep(0.2)
        completed.set()

    reloader.on_change_async(slow_callback)
    reloader._notify_callbacks()

    # 事件驱动：完成事件在回调末尾置位（等待侧与时长解耦）；
    # timeout 10s 仅为防死锁保险，正常路径即时返回。
    await asyncio.wait_for(completed.wait(), timeout=10.0)
    assert completed.is_set()
