"""子代理 A2A 共享黑板 —— 协程安全的 KV 存储，支持 TTL 和订阅通知。

子代理间通过 key-value 交换信息，避免重复工作：
- 子代理委托前可读取相关 key，复用已有产出
- 子代理完成后将结果写入黑板，供父代理汇总或其他子代理引用
- 订阅机制支持「下次 put 时通知」的一次性 Event

线程安全说明：基于 asyncio.Lock 实现协程安全（单事件循环内并发安全），
不跨线程/跨进程共享。
"""
import asyncio
import time
from typing import Any

from loguru import logger


class _Entry:
    __slots__ = ("value", "agent_name", "expire_at", "subscribers")

    def __init__(self, value: Any, agent_name: str, ttl: float):
        self.value = value
        self.agent_name = agent_name
        # ttl <= 0 表示永不过期（订阅占位条目使用）
        self.expire_at = time.monotonic() + ttl if ttl > 0 else None
        self.subscribers: list[asyncio.Event] = []


class SharedBlackboard:
    """共享黑板：子代理间通过 key-value 交换信息，避免重复工作。

    所有方法均为协程安全（asyncio.Lock 保护）。TTL 默认 600 秒，
    可通过 put(ttl=...) 单次覆盖；subscribe 创建的占位条目永不过期，
    以保证订阅者在收到首次 put 前不会被清理。
    """

    def __init__(self, default_ttl: float = 600.0):
        self._store: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._default_ttl = default_ttl

    async def put(self, key: str, value: Any, agent_name: str = "",
                  ttl: float | None = None) -> None:
        """写入 key-value，记录写入者 agent_name，并通知该 key 的所有订阅者。

        Args:
            key: 键名
            value: 值（任意可序列化对象）
            agent_name: 写入者标识（子代理名）
            ttl: 单次 TTL（秒），None 表示使用 default_ttl，<= 0 表示永不过期
        """
        async with self._lock:
            effective_ttl = self._default_ttl if ttl is None else ttl
            entry = _Entry(value, agent_name, effective_ttl)
            old = self._store.get(key)
            self._store[key] = entry
            # 通知旧条目上的订阅者（一次性 Event）
            if old and old.subscribers:
                for ev in old.subscribers:
                    ev.set()
            logger.debug("blackboard.put key={} agent={} ttl={}",
                         key, agent_name, effective_ttl)

    async def get(self, key: str) -> Any | None:
        """读取 key 的值；过期则清理并返回 None；不存在返回 None。"""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expire_at and time.monotonic() > entry.expire_at:
                del self._store[key]
                return None
            return entry.value

    async def get_with_meta(self, key: str) -> dict | None:
        """读取 key 的值及元信息（agent_name），过期返回 None。

        Returns:
            {"value": ..., "agent_name": ...} 或 None
        """
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expire_at and time.monotonic() > entry.expire_at:
                del self._store[key]
                return None
            return {"value": entry.value, "agent_name": entry.agent_name}

    async def subscribe(self, key: str) -> asyncio.Event:
        """订阅 key 的变更通知，返回一个 asyncio.Event。

        Event 在「下次 put 该 key 时」被 set。订阅者通常 await 该 Event，
        随后可调用 get(key) 读取新值。如需持续监听需重新订阅。

        若 key 不存在，会创建一个永不过期的占位条目（value=None），
        以保证订阅在首次 put 前不会被 TTL 清理。
        """
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                # 占位条目 ttl<=0 永不过期，避免订阅者在收到 put 前被清理
                entry = _Entry(None, "", 0)
                self._store[key] = entry
            ev = asyncio.Event()
            entry.subscribers.append(ev)
            return ev

    async def keys(self, prefix: str = "") -> list[str]:
        """返回所有未过期的 key 列表（可按前缀过滤），供父代理汇总时枚举子代理产出。"""
        async with self._lock:
            now = time.monotonic()
            result = []
            expired = []
            for k, e in self._store.items():
                if e.expire_at and now > e.expire_at:
                    expired.append(k)
                    continue
                if not prefix or k.startswith(prefix):
                    result.append(k)
            for k in expired:
                del self._store[k]
            return result

    async def cleanup_expired(self) -> int:
        """清理所有过期条目，返回清理数量。可由后台任务周期性调用。"""
        now = time.monotonic()
        async with self._lock:
            expired_keys = [k for k, e in self._store.items()
                            if e.expire_at and now > e.expire_at]
            for k in expired_keys:
                del self._store[k]
        if expired_keys:
            logger.debug("blackboard.cleanup count={}", len(expired_keys))
        return len(expired_keys)

    async def start_cleanup_task(self, interval: float = 300.0) -> asyncio.Task:
        """启动后台周期清理任务，每 interval 秒清理一次过期条目。

        返回 asyncio.Task，调用方可保存引用以便关闭时取消。
        """
        async def _loop():
            while True:
                try:
                    await asyncio.sleep(interval)
                    await self.cleanup_expired()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning("blackboard.cleanup_task_error error={}", e)

        task = asyncio.create_task(_loop(), name="blackboard-cleanup")
        return task
