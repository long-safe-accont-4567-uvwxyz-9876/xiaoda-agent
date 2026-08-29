"""事件循环细粒度延迟采样器（utils/loop_lag_monitor）。

Windows 实机"卡"长期无量化数据：10s 阈值 watchdog 只能抓长阻塞，
本采样器负责亚秒级漂移的量化告警。
"""
import asyncio
import time

from loguru import logger

from utils.loop_lag_monitor import start_loop_lag_monitor


def test_lag_monitor_warns_on_sync_block():
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING")

    async def main():
        task = start_loop_lag_monitor(threshold_ms=100, interval=0.05)
        assert task is not None
        await asyncio.sleep(0.06)   # 正常心跳，不应告警
        time.sleep(0.3)             # 同步阻塞事件循环
        await asyncio.sleep(0.2)    # 唤醒后采样点应检出漂移
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    logger.remove(sink_id)
    assert any("loop.lag_detected" in m for m in messages)


def test_no_running_loop_returns_none():
    assert start_loop_lag_monitor() is None
