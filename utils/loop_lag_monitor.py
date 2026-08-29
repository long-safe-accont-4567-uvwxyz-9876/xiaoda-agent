"""事件循环细粒度延迟采样器。

现有 event_loop watchdog（core/background_tasks.start_event_loop_watchdog）
阈值 10s，只能抓长阻塞并打印线程栈；Windows 实机的"卡"多为亚秒级同步
IO/渲染阻塞，长期无量化数据导致无法定位（"只有 Windows 卡"老问题）。

本采样器在事件循环内以 1s 心跳自我计时：实际唤醒晚于预期超过阈值，说明
期间有协程/同步代码占住了循环，按漂移量落 WARNING。配合 10s 级 watchdog
（栈取证）形成"短阻塞量化 + 长阻塞取证"两级观测。
"""
import asyncio

from loguru import logger


async def _monitor(threshold_ms: float, interval: float) -> None:
    loop = asyncio.get_running_loop()
    while True:
        expected = loop.time() + interval
        await asyncio.sleep(interval)
        lag_ms = (loop.time() - expected) * 1000.0
        if lag_ms < threshold_ms:
            continue
        logger.warning(
            "loop.lag_detected lag_ms={:.0f} threshold_ms={:.0f} "
            "hint=期间有同步操作占住事件循环（对照前后日志定位真凶）",
            lag_ms,
            threshold_ms,
        )


def start_loop_lag_monitor(
    threshold_ms: float = 300.0,
    interval: float = 1.0,
) -> asyncio.Task | None:
    """启动采样后台任务；无运行中事件循环时返回 None（不抛错）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return None
    return asyncio.create_task(_monitor(threshold_ms, interval), name="loop-lag-monitor")
