"""异步兼容层 — 消除 async 上下文中的同步阻塞调用

提供统一工具，让原本在 async 函数中调用 time.sleep() / requests.get() 的代码
改为非阻塞版本，避免阻塞事件循环。

参考: PEP 525 (async generators), asyncio.to_thread (Python 3.9+)
"""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, Iterable



async def async_sleep(seconds: float) -> None:
    """非阻塞 sleep，等价于 time.sleep 但不阻塞事件循环"""
    await asyncio.sleep(seconds)


async def async_sleep_range(retries: int, base_delay: float = 1.0,
                            max_delay: float = 60.0, cap: int = 6) -> float:
    """指数退避 sleep，返回本次延迟时长

    Args:
        retries: 已重试次数（从 1 开始）
        base_delay: 基础延迟
        max_delay: 最大延迟
        cap: 指数增长上限（防止指数爆炸）
    """
    delay = min(base_delay * (2 ** min(retries - 1, cap)), max_delay)
    await asyncio.sleep(delay)
    return delay


async def run_sync(func: Callable, *args: Any, **kwargs: Any) -> Any:
    """在线程池中运行同步函数，不阻塞事件循环

    用法:
        result = await run_sync(requests.get, url, timeout=10)
    """
    return await asyncio.to_thread(func, *args, **kwargs)


async def gather_with_concurrency(coros: Iterable, limit: int = 10,
                                  return_exceptions: bool = False) -> list:
    """并发执行多个协程，限制最大并发数

    用法:
        results = await gather_with_concurrency(
            [fetch(u) for u in urls], limit=5
        )
    """
    semaphore = asyncio.Semaphore(limit)

    async def _wrap(coro: Any) -> Any:
        async with semaphore:
            return await coro

    return await asyncio.gather(
        *[_wrap(c) for c in coros],
        return_exceptions=return_exceptions,
    )


def sync_to_async(func: Callable) -> Callable:
    """装饰器：把同步函数转成协程函数

    用法:
        @sync_to_async
        def my_sync_func(x):
            return x * 2

        result = await my_sync_func(10)
    """
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper


async def http_get_json(url: str, timeout: float = 10.0,
                         headers: dict | None = None) -> dict:
    """非阻塞 HTTP GET，返回 JSON

    优先使用 httpx（已安装），否则回退到线程池中的 requests。
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=headers)
            return r.json()
    except ImportError:
        import requests
        return await run_sync(
            lambda: requests.get(url, headers=headers, timeout=timeout).json()
        )


async def http_post_json(url: str, json: dict | None = None,
                          timeout: float = 10.0,
                          headers: dict | None = None) -> dict:
    """非阻塞 HTTP POST，返回 JSON"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=json, headers=headers)
            return r.json()
    except ImportError:
        import requests
        return await run_sync(
            lambda: requests.post(url, json=json, headers=headers,
                                   timeout=timeout).json()
        )


async def wait_for(predicate: Callable[[], bool], timeout: float = 30.0,
                     interval: float = 0.5) -> bool:
    """等待条件成立，非阻塞

    用法:
        ok = await wait_for(lambda: server.is_ready(), timeout=10)
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False
