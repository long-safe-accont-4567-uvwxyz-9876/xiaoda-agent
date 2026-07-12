"""CancelToken — 协程取消令牌，支持超时自动取消 + 主动取消。

使用方式：
    token = CancelToken(timeout=60.0)
    try:
        token.check()
        result = await some_long_running_task()
        token.check()
    except CancellationError:
        ...
    finally:
        token.cleanup()

    # 主动取消
    token.cancel("agent_request")
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from loguru import logger


class CancellationError(Exception):
    """任务被取消时抛出的异常。"""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason
        super().__init__(f"task cancelled: {reason}")


class CancelToken:
    """协程取消令牌。

    Args:
        timeout: 超时秒数，None 表示永不超时
    """

    def __init__(self, timeout: Optional[float] = 60.0) -> None:
        self._cancelled = False
        self._reason = ""
        self._timeout = timeout
        self._created_at = time.monotonic()
        self._timer_task: asyncio.Task | None = None
        if timeout is not None and timeout > 0:
            self._timer_task = asyncio.create_task(self._timeout_watch())

    async def _timeout_watch(self) -> None:
        try:
            await asyncio.sleep(self._timeout)
            if not self._cancelled:
                self._cancelled = True
                self._reason = f"timeout({self._timeout}s)"
                logger.info("cancel_token.timeout_cancelled timeout={}s", self._timeout)
        except asyncio.CancelledError:
            pass

    @property
    def is_cancelled(self) -> bool:
        if self._timeout is not None and self._timeout > 0:
            if not self._cancelled and time.monotonic() - self._created_at > self._timeout:
                self._cancelled = True
                self._reason = f"timeout({self._timeout}s)"
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason

    def cancel(self, reason: str = "manual") -> None:
        if not self._cancelled:
            self._cancelled = True
            self._reason = reason
            logger.info("cancel_token.cancelled reason={}", reason)

    def check(self) -> None:
        if self.is_cancelled:
            raise CancellationError(self._reason)

    def cleanup(self) -> None:
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            self._timer_task = None
