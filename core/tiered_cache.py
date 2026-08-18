"""三级缓存架构 (P4) — L1 内存 + L2 文件 + L3 持久化

参考:
- Caffeine Window TinyLFU (淘汰算法)
- Redis L2 KV Cache Reuse (Layered caching)
- cachestack (Tiered caching with backfill)

特性:
- L1 (内存): LRU + TTL, 命中 ~0.1ms
- L2 (文件): diskcache, 命中 ~5ms, 跨会话
- L3 (持久化): SQLite/数据库, 命中 ~20ms, 跨进程
- 自动 backfill: 命中下层时回填上层
- Stampede protection: per-key async lock 防止雪崩
- 统计指标: 命中率/延迟/token 节省
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger


def _cache_key(*args: Any, **kwargs: Any) -> str:
    """生成稳定缓存键"""
    raw = json.dumps({"args": list(args), "kwargs": kwargs},
                     sort_keys=True, default=str)
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()


# ============================================================
# L1: 内存缓存 (LRU + TTL)
# ============================================================

class L1MemoryCache:
    """L1 内存缓存 — LRU + TTL, 最快"""

    def __init__(self, maxsize: int = 1024, default_ttl: float = 300.0) -> None:
        """初始化 L1 内存缓存.

        Args:
            maxsize: 最大条目数, 默认 1024
            default_ttl: 默认 TTL 秒, 默认 300
        """
        self._store: OrderedDict = OrderedDict()
        self._ttls: dict = {}
        self._maxsize = maxsize
        self._default_ttl = default_ttl
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        """按 key 取值, 命中则更新 LRU 顺序.

        Args:
            key: 缓存键

        Returns:
            命中返回值, 未命中或已过期返回 None
        """
        with self._lock:
            if key not in self._store:
                self._misses += 1
                return None
            if self._ttls.get(key, 0) < time.time():
                self._store.pop(key, None)
                self._ttls.pop(key, None)
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return self._store[key]

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """写入键值, 超容量时淘汰最旧条目.

        Args:
            key: 缓存键
            value: 缓存值
            ttl: 自定义 TTL 秒, None 表示使用默认值
        """
        with self._lock:
            self._store[key] = value
            self._ttls[key] = time.time() + (ttl or self._default_ttl)
            self._store.move_to_end(key)
            while len(self._store) > self._maxsize:
                k, _ = self._store.popitem(last=False)
                self._ttls.pop(k, None)

    def invalidate(self, prefix: str = "") -> int:
        """按前缀失效缓存条目.

        Args:
            prefix: 键前缀, 空字符串表示清空全部

        Returns:
            实际清除的条目数
        """
        with self._lock:
            if not prefix:
                n = len(self._store)
                self._store.clear()
                self._ttls.clear()
                return n
            keys = [k for k in self._store if k.startswith(prefix)]
            for k in keys:
                self._store.pop(k, None)
                self._ttls.pop(k, None)
            return len(keys)

    def stats(self) -> dict:
        """返回 L1 缓存统计 (大小/命中/未命中/命中率)."""
        with self._lock:
            total = self._hits + self._misses
            return {
                "layer": "L1",
                "size": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self._hits / max(1, total),
            }


# ============================================================
# L2: 文件缓存 (diskcache-style, 跨会话)
# ============================================================

class L2FileCache:
    """L2 文件缓存 — 跨会话, 零依赖"""

    def __init__(self, cache_dir: str | Path, default_ttl: float = 3600.0,
                  max_entries: int = 10000) -> None:
        """初始化 L2 文件缓存.

        Args:
            cache_dir: 缓存目录
            default_ttl: 默认 TTL 秒, 默认 3600
            max_entries: F6 最大缓存条目数，超出时淘汰最旧的
        """
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._default_ttl = default_ttl
        self._max_entries = max_entries
        self._hits = 0
        self._misses = 0
        self._lock = asyncio.Lock()
        self._set_count = 0

    def _path(self, key: str) -> Path:
        # 分片存储避免单目录文件过多
        sub = self._dir / key[:2]
        sub.mkdir(parents=True, exist_ok=True)
        return sub / f"{key}.json"

    def get(self, key: str) -> Any | None:
        """按 key 从文件读取值, 过期或损坏返回 None."""
        p = self._path(key)
        if not p.exists():
            self._misses += 1
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("expires_at", 0) < time.time():
                p.unlink(missing_ok=True)
                self._misses += 1
                return None
            self._hits += 1
            return data["value"]
        except Exception:
            logger.debug("tiered_cache.L2_get_read_error: {}", exc_info=True)
            self._misses += 1
            return None

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """原子写入键值到文件 (含过期时间)."""
        p = self._path(key)
        data = {
            "value": value,
            "expires_at": time.time() + (ttl or self._default_ttl),
            "created_at": time.time(),
        }
        # 原子写入
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        # F6: 定期淘汰过期和最旧条目（每 100 次写入检查一次）
        self._set_count += 1
        if self._set_count % 100 == 0:
            self._evict_expired_and_oldest()

    def _evict_expired_and_oldest(self) -> None:
        """F6: 淘汰过期文件，如仍超限则按 created_at 淘汰最旧的"""
        try:
            files = list(self._dir.rglob("*.json"))
            now = time.time()
            # 1. 先淘汰过期的
            for f in files:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if data.get("expires_at", 0) < now:
                        f.unlink(missing_ok=True)
                except Exception:
                    logger.debug("tiered_cache.L2_evict_read_error: {}", exc_info=True)
                    f.unlink(missing_ok=True)
            # 2. 如仍超限，按修改时间淘汰最旧的
            files = list(self._dir.rglob("*.json"))
            if len(files) > self._max_entries:
                files.sort(key=lambda f: f.stat().st_mtime)
                evict_count = len(files) - self._max_entries
                for f in files[:evict_count]:
                    try:
                        f.unlink(missing_ok=True)
                    except Exception:
                        logger.debug("tiered_cache.L2_evict_unlink_error: {}", exc_info=True)
        except Exception:
            logger.debug("tiered_cache.L2_evict_failed: {}", exc_info=True)

    def invalidate(self, prefix: str = "") -> int:
        """按前缀失效缓存文件.

        Args:
            prefix: 键前缀, 空字符串表示全部

        Returns:
            删除的文件数
        """
        n = 0
        for p in self._dir.rglob("*.json"):
            if prefix and not p.stem.startswith(prefix):
                continue
            try:
                p.unlink()
                n += 1
            except Exception:
                logger.debug("tiered_cache.L2_invalidate_unlink_error: {}", exc_info=True)
        return n

    def stats(self) -> dict:
        """返回 L2 缓存统计 (目录/命中/未命中)."""
        return {
            "layer": "L2",
            "dir": str(self._dir),
            "hits": self._hits,
            "misses": self._misses,
        }


# ============================================================
# L3: SQLite 持久化缓存 (跨进程)
# ============================================================

class L3SQLiteCache:
    """L3 SQLite 持久化缓存 — 跨进程"""

    def __init__(self, db_path: str | Path, default_ttl: float = 86400.0,
                  max_entries: int = 50000) -> None:
        """初始化 L3 SQLite 缓存并建表.

        Args:
            db_path: SQLite 数据库路径
            default_ttl: 默认 TTL 秒, 默认 86400
            max_entries: F6 最大缓存条目数，超出时淘汰最旧的
        """
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._default_ttl = default_ttl
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._db), timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._hits = 0
        self._misses = 0
        self._set_count = 0

    def _init_schema(self) -> None:
        try:
            with self._lock:
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS cache (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        expires_at REAL NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_expires ON cache(expires_at)")
                self._conn.commit()
        except Exception as e:
            logger.debug("_init_schema SQLite error: {}", e)

    def get(self, key: str) -> Any | None:
        """按 key 从 SQLite 读取值, 过期则删除并返回 None."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value, expires_at FROM cache WHERE key=?", (key,)
                ).fetchone()
                if not row:
                    self._misses += 1
                    return None
                if row["expires_at"] < time.time():
                    self._conn.execute("DELETE FROM cache WHERE key=?", (key,))
                    self._conn.commit()
                    self._misses += 1
                    return None
                self._hits += 1
                return json.loads(row["value"])
        except Exception as e:
            logger.debug("L3SQLiteCache.get({}) error: {}", key, e)
            return None

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """写入键值 (覆盖已存在记录)."""
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO cache(key, value, expires_at, created_at) VALUES (?,?,?,?)",
                    (key, json.dumps(value, ensure_ascii=False),
                     time.time() + (ttl or self._default_ttl), time.time())
                )
                self._conn.commit()
        except Exception as e:
            logger.debug("L3SQLiteCache.set({}) error: {}", key, e)
        # F6: 定期淘汰过期和超限条目（每 100 次写入检查一次）
        self._set_count += 1
        if self._set_count % 100 == 0:
            self._evict_expired_and_oldest()

    def _evict_expired_and_oldest(self) -> None:
        """F6: 淘汰过期条目，如仍超限则按 created_at 淘汰最旧的"""
        try:
            with self._lock:
                # 1. 先淘汰过期的
                self._conn.execute("DELETE FROM cache WHERE expires_at < ?", (time.time(),))
                # 2. 如仍超限，按 created_at 淘汰最旧的
                row = self._conn.execute("SELECT COUNT(*) as cnt FROM cache").fetchone()
                count = row["cnt"] if row else 0
                if count > self._max_entries:
                    evict_count = count - self._max_entries
                    self._conn.execute(
                        "DELETE FROM cache WHERE key IN ("
                        "SELECT key FROM cache ORDER BY created_at ASC LIMIT ?)",
                        (evict_count,)
                    )
                self._conn.commit()
        except Exception:
            logger.debug("tiered_cache.L3_evict_failed: {}", exc_info=True)

    def invalidate(self, prefix: str = "") -> int:
        """按前缀失效缓存.

        Args:
            prefix: 键前缀, 空字符串表示全部

        Returns:
            删除的行数
        """
        with self._lock:
            if prefix:
                escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                cur = self._conn.execute(
                    "DELETE FROM cache WHERE key LIKE ? ESCAPE '\\'", (f"{escaped}%",)
                )
            else:
                cur = self._conn.execute("DELETE FROM cache")
            self._conn.commit()
            return cur.rowcount

    def cleanup_expired(self) -> int:
        """清理所有过期条目, 返回删除行数."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM cache WHERE expires_at < ?", (time.time(),)
            )
            self._conn.commit()
            return cur.rowcount

    def stats(self) -> dict:
        """返回 L3 缓存统计 (数据库路径/大小/命中/未命中)."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM cache").fetchone()
        return {
            "layer": "L3",
            "db": str(self._db),
            "size": row["n"] if row else 0,
            "hits": self._hits,
            "misses": self._misses,
        }


# ============================================================
# TieredCache — 三级协调器
# ============================================================

class TieredCache:
    """三级缓存协调器

    查询顺序: L1 → L2 → L3 → loader
    命中下层时自动 backfill 上层
    Per-key lock 防止 stampede
    """

    def __init__(self, cache_dir: str | Path, db_path: str | Path,
                 l1_size: int = 1024, l1_ttl: float = 300.0,
                 l2_ttl: float = 3600.0, l3_ttl: float = 86400.0) -> None:
        """初始化三级缓存协调器.

        Args:
            cache_dir: L2 文件缓存目录
            db_path: L3 SQLite 数据库路径
            l1_size: L1 内存缓存最大条目数, 默认 1024
            l1_ttl: L1 默认 TTL 秒, 默认 300
            l2_ttl: L2 默认 TTL 秒, 默认 3600
            l3_ttl: L3 默认 TTL 秒, 默认 86400
        """
        self._l1 = L1MemoryCache(maxsize=l1_size, default_ttl=l1_ttl)
        self._l2 = L2FileCache(cache_dir=cache_dir, default_ttl=l2_ttl)
        self._l3 = L3SQLiteCache(db_path=db_path, default_ttl=l3_ttl)
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._total_saved_ms = 0.0

    async def _get_lock(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            if key in self._locks:
                return self._locks[key]
            # 高水位淘汰: 移除未被持有的锁, 淘汰到 128
            if len(self._locks) > 256:
                evictable = [k for k, v in self._locks.items() if not v.locked()]
                for k in evictable:
                    del self._locks[k]
                    if len(self._locks) <= 128:
                        break
            lock = asyncio.Lock()
            self._locks[key] = lock
            return lock

    async def get(self, key: str, loader: Callable | None = None,
                  ttl: float | None = None) -> Any:
        """查询缓存, 未命中时调用 loader 加载

        Args:
            key: 缓存键
            loader: async 同步函数, 用于加载值
            ttl: 自定义 TTL
        """
        # L1
        v = self._l1.get(key)
        if v is not None:
            return v

        # Stampede protection
        lock = await self._get_lock(key)
        async with lock:
            # 二次检查 L1 (其他协程可能已加载)
            v = self._l1.get(key)
            if v is not None:
                return v

            # L2
            v = self._l2.get(key)
            if v is not None:
                self._l1.set(key, v, ttl)
                return v

            # L3
            v = self._l3.get(key)
            if v is not None:
                self._l1.set(key, v, ttl)
                self._l2.set(key, v, ttl)
                return v

            # Loader
            if loader is None:
                return None
            t0 = time.time()
            if asyncio.iscoroutinefunction(loader):
                v = await loader()
            else:
                v = await asyncio.to_thread(loader)
            self._total_saved_ms += (time.time() - t0) * 1000

            # Backfill 所有层
            self._l1.set(key, v, ttl)
            self._l2.set(key, v, ttl)
            self._l3.set(key, v, ttl)
            return v

    def invalidate(self, prefix: str = "") -> None:
        """失效缓存 (所有层)"""
        self._l1.invalidate(prefix)
        self._l2.invalidate(prefix)
        self._l3.invalidate(prefix)

    def cleanup(self) -> int:
        """清理过期条目"""
        return self._l3.cleanup_expired()

    def stats(self) -> dict:
        """返回三级缓存综合统计 (含节省的累计耗时)."""
        return {
            "l1": self._l1.stats(),
            "l2": self._l2.stats(),
            "l3": self._l3.stats(),
            "saved_ms": self._total_saved_ms,
        }


# ============================================================
# 全局单例
# ============================================================

_cache: TieredCache | None = None


def get_tiered_cache() -> TieredCache:
    """获取全局三级缓存实例"""
    global _cache
    if _cache is None:
        from config import WORKSPACE_DIR
        cache_dir = Path(WORKSPACE_DIR) / "cache" / "L2"
        db_path = Path(WORKSPACE_DIR) / "cache" / "L3.db"
        _cache = TieredCache(cache_dir=cache_dir, db_path=db_path)
    return _cache


def cached(ttl: float | None = None, key_fn: Callable | None = None) -> Any:
    """装饰器: 三级缓存函数结果

    用法:
        @cached(ttl=600)
        async def fetch_weather(city: str):
            return await api.get_weather(city)

    Args:
        ttl: 自定义 TTL 秒, None 表示使用各层默认
        key_fn: 自定义缓存键生成函数

    Returns:
        装饰器函数
    """
    def decorator(func: Any) -> Any:
        """包裹原函数的装饰器内层."""
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            """实际执行的异步包装, 命中缓存则直接返回."""
            cache = get_tiered_cache()
            k = key_fn(*args, **kwargs) if key_fn else _cache_key(*args, **kwargs)
            # 修复：若 func 是协程函数，loader 必须返回协程对象本身（而非调用结果），
            # 否则 TieredCache.get 内部 iscoroutinefunction(loader) 为 False，
            # 会走 asyncio.to_thread(loader) 把协程对象塞进线程池返回错误结果。
            if asyncio.iscoroutinefunction(func):
                async def _async_loader() -> Any:
                    return await func(*args, **kwargs)
                loader: Any = _async_loader
            else:
                loader = lambda: func(*args, **kwargs)  # noqa: E731
            return await cache.get(k, loader=loader, ttl=ttl)
        return wrapper
    return decorator


import functools
