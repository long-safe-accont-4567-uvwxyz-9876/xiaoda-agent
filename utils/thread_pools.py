"""双线程池隔离 — 延迟敏感与可等重活分池运行。

背景：asyncio.to_thread 与 run_in_executor(None) 共用同一个默认执行器
(worker ≈ min(32, 核数+4))。Orange Pi 核数少，一次工具风暴或 ffmpeg
转码占满默认池后，每条消息的记忆向量检索/embed 会在其后排队——这正是
"embed 冷启动超时"类问题的结构性温床。

收敛约定：
- 延迟敏感（每条消息必经、任务短小）：用 to_thread_hot()。如
  memory/vector_store 的检索/写入、对话状态的轻量文件 IO。
- 可等重活（秒级以上，晚几百毫秒无所谓）：用 to_thread_heavy()。如
  ffmpeg/silk 转码、模型下载解压、限流状态/信念表等批量持久化。
- 工具执行已由 ToolExecutor._tool_threadpool 独立成池（真实事故教训：
  计算器 88s），维持不动。

新代码请直接使用本模块而非裸 to_thread；触碰旧站点时顺手迁移（boy-scout）。
"""
from __future__ import annotations

import asyncio
import functools
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")

_CPU_COUNT = os.cpu_count() or 4

# 延迟敏感池：任务短小（ms~百 ms 级），worker 数不必多但必须始终有空位
LATENCY_POOL: ThreadPoolExecutor = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="hot-io")

# 可等重活池：单个任务可占住 worker 数十秒，占满也不影响检索链路
HEAVY_POOL: ThreadPoolExecutor = ThreadPoolExecutor(
    max_workers=max(2, min(_CPU_COUNT // 2, 4)), thread_name_prefix="heavy-io")


async def _run_in(pool: ThreadPoolExecutor,
                  func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """与 asyncio.to_thread 同参语义，但显式指定目标线程池。"""
    loop = asyncio.get_running_loop()
    call = functools.partial(func, **kwargs) if kwargs else func
    return await loop.run_in_executor(pool, call, *args)


async def to_thread_hot(func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """延迟敏感路径：记忆检索/embed/sqlite 热查询等。"""
    return await _run_in(LATENCY_POOL, func, *args, **kwargs)


async def to_thread_heavy(func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """可等重活：转码/下载/批量持久化等，允许在队列中等待空闲 worker。"""
    return await _run_in(HEAVY_POOL, func, *args, **kwargs)
