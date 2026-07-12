"""
行为信号流 — 对齐 reprobe/interceptor.py 的激活采集模式。

设计参考:
- reprobe/interceptor.py: Interceptor 区分 prefill/token 两种采集模式
- reprobe/monitor.py: Monitor 的 history 列表 + _flush_step()
- jlens/hooks.py: ActivationRecorder 的上下文管理器模式
"""
from dataclasses import dataclass, field
from typing import Any
import time
import asyncio
import weakref
from collections import deque
from loguru import logger


@dataclass
class SignalEntry:
    """单条行为信号"""
    signal_type: str
    value: float
    source: str
    timestamp: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)


class BehavioralSignalStream:
    """
    持续行为信号流 — 对齐 reprobe/interceptor.py 的激活采集模式。
    """

    def __init__(self, max_history: int = 1000, flush_interval: float = 1.0):
        self._buffer: deque[SignalEntry] = deque(maxlen=max_history)
        self._subscribers: dict[str, weakref.WeakSet[asyncio.Event]] = {}
        self._flush_interval = flush_interval
        self._last_flush = time.monotonic()

    async def emit(self, signal_type: str, value: float, source: str = "", **meta) -> None:
        """发射一条行为信号。对齐 reprobe/interceptor 的 _flush 模式。"""
        try:
            entry = SignalEntry(signal_type=signal_type, value=value, source=source, meta=meta)
            self._buffer.append(entry)
            if signal_type in self._subscribers:
                for ev in list(self._subscribers[signal_type]):
                    ev.set()
        except Exception as e:
            logger.warning(f"behavioral_signal.emit_failed: {e}")

    async def subscribe(self, signal_type: str) -> asyncio.Event:
        """订阅特定信号类型 — 对齐 shared_blackboard.subscribe()
        使用 WeakSet，订阅者 Event 被销毁后自动移除，无内存泄漏。
        """
        if signal_type not in self._subscribers:
            self._subscribers[signal_type] = weakref.WeakSet()
        ev = asyncio.Event()
        self._subscribers[signal_type].add(ev)
        return ev

    def unsubscribe(self, signal_type: str, event: asyncio.Event) -> None:
        """显式取消订阅。"""
        if signal_type in self._subscribers:
            self._subscribers[signal_type].discard(event)

    def get_history(self, signal_type: str = "", last_n: int = 100) -> list[SignalEntry]:
        """获取历史信号 — 对齐 reprobe/monitor.py: Monitor.get_history()"""
        if signal_type:
            entries = [e for e in self._buffer if e.signal_type == signal_type]
        else:
            entries = list(self._buffer)
        return entries[-last_n:]

    def aggregate(self, signal_type: str, strategy: str = "mean_of_means") -> float:
        """聚合信号 — 对齐 reprobe/monitor.py: Monitor.score() 的三种策略"""
        entries = [e for e in self._buffer if e.signal_type == signal_type]
        if not entries:
            return 0.0
        values = [e.value for e in entries]
        if strategy == "max_of_means":
            return max(values)
        elif strategy == "mean_of_means":
            return sum(values) / len(values)
        elif strategy == "max_absolute":
            return max(abs(v) for v in values)
        return sum(values) / len(values)