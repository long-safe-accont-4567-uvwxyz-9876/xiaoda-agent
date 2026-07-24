"""SharedBlackboardDB — SQLite 背板黑板，支持跨进程共享。

与 SharedBlackboard（asyncio.Lock 单进程）不同，SharedBlackboardDB 使用 SQLite
WAL 模式作为背板，多进程/多 worker 可安全共享数据。

适用场景：
- Web 多 worker 部署
- CLI + QQ Bot 同时运行
- 任何需要跨进程共享子代理产出的场景
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any

from loguru import logger


class SharedBlackboardDB:
    """SQLite 背板黑板 — 跨进程安全。

    Args:
        db_path: SQLite 数据库文件路径
        default_ttl: 默认 TTL（秒）
    """

    def __init__(self, db_path: str, default_ttl: float = 600.0) -> None:
        self._db_path = db_path
        self._default_ttl = default_ttl
        # asyncio.Lock 仅用于同事件循环内去重（防止同 event loop 并发写冲突）。
        # 跨进程安全依赖 SQLite WAL + busy_timeout。
        self._lock = asyncio.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取 SQLite 连接，统一设置 WAL + busy_timeout。"""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        """初始化数据库表。"""
        conn = None
        try:
            conn = self._get_conn()
            conn.execute("""CREATE TABLE IF NOT EXISTS blackboard (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                agent_name TEXT NOT NULL DEFAULT '',
                expire_at REAL,
                created_at REAL NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_blackboard_expire ON blackboard(expire_at)")
            conn.commit()
        except Exception as e:
            logger.warning("blackboard_db.init_failed error={}", e)
        finally:
            if conn:
                conn.close()

    def _serialize(self, value: Any) -> str:
        """序列化值为 JSON 字符串。"""
        return json.dumps(value, ensure_ascii=False, default=str)

    def _deserialize(self, raw: str) -> Any:
        """反序列化 JSON 字符串。"""
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    async def put(self, key: str, value: Any, agent_name: str = "",
                  ttl: float | None = None) -> None:
        """写入 key-value，记录写入者。"""
        async with self._lock:
            effective_ttl = self._default_ttl if ttl is None else ttl
            expire_at = time.time() + effective_ttl if effective_ttl > 0 else None
            raw_value = self._serialize(value)
            now = time.time()

            def _do() -> None:
                conn = None
                try:
                    conn = self._get_conn()
                    conn.execute(
                        "INSERT OR REPLACE INTO blackboard (key, value, agent_name, expire_at, created_at) VALUES (?, ?, ?, ?, ?)",
                        (key, raw_value, agent_name, expire_at, now)
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning("blackboard_db.put_failed key={} error={}", key, e)
                finally:
                    if conn:
                        conn.close()

            await asyncio.get_running_loop().run_in_executor(None, _do)
            logger.debug("blackboard_db.put key={} agent={} ttl={}", key, agent_name, effective_ttl)

    async def get(self, key: str) -> Any | None:
        """读取 key 的值；过期则清理并返回 None。"""
        async with self._lock:
            def _do() -> Any | None:
                conn = None
                try:
                    conn = self._get_conn()
                    cur = conn.execute(
                        "SELECT value, expire_at FROM blackboard WHERE key = ?", (key,)
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    raw_value, expire_at = row
                    if expire_at is not None and time.time() > expire_at:
                        conn.execute("DELETE FROM blackboard WHERE key = ?", (key,))
                        conn.commit()
                        return None
                    return self._deserialize(raw_value)
                except Exception as e:
                    logger.warning("blackboard_db.get_failed key={} error={}", key, e)
                    return None
                finally:
                    if conn:
                        conn.close()

            return await asyncio.get_running_loop().run_in_executor(None, _do)

    async def get_with_meta(self, key: str) -> dict | None:
        """读取 key 的值及元信息（含 created_at）。"""
        async with self._lock:
            def _do() -> dict | None:
                conn = None
                try:
                    conn = self._get_conn()
                    cur = conn.execute(
                        "SELECT value, agent_name, expire_at, created_at FROM blackboard WHERE key = ?", (key,)
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    raw_value, agent_name, expire_at, created_at = row
                    if expire_at is not None and time.time() > expire_at:
                        conn.execute("DELETE FROM blackboard WHERE key = ?", (key,))
                        conn.commit()
                        return None
                    return {"value": self._deserialize(raw_value), "agent_name": agent_name, "created_at": created_at}
                except Exception as e:
                    logger.warning("blackboard_db.get_meta_failed key={} error={}", key, e)
                    return None
                finally:
                    if conn:
                        conn.close()

            return await asyncio.get_running_loop().run_in_executor(None, _do)

    async def keys(self, prefix: str = "") -> list[str]:
        """返回所有未过期的 key（可按前缀过滤）。"""
        async with self._lock:
            def _do() -> list[str]:
                conn = None
                try:
                    conn = self._get_conn()
                    now = time.time()
                    if prefix:
                        cur = conn.execute(
                            "SELECT key FROM blackboard WHERE key LIKE ? AND (expire_at IS NULL OR expire_at > ?)",
                            (prefix + "%", now)
                        )
                    else:
                        cur = conn.execute(
                            "SELECT key FROM blackboard WHERE expire_at IS NULL OR expire_at > ?",
                            (now,)
                        )
                    return [row[0] for row in cur.fetchall()]
                except Exception as e:
                    logger.warning("blackboard_db.keys_failed error={}", e)
                    return []
                finally:
                    if conn:
                        conn.close()

            return await asyncio.get_running_loop().run_in_executor(None, _do)

    async def cleanup_expired(self) -> int:
        """清理所有过期条目。"""
        async with self._lock:
            def _do() -> int:
                conn = None
                try:
                    conn = self._get_conn()
                    now = time.time()
                    cur = conn.execute(
                        "DELETE FROM blackboard WHERE expire_at IS NOT NULL AND expire_at < ?",
                        (now,)
                    )
                    conn.commit()
                    return cur.rowcount
                except Exception as e:
                    logger.warning("blackboard_db.cleanup_failed error={}", e)
                    return 0
                finally:
                    if conn:
                        conn.close()

            cleaned = await asyncio.get_running_loop().run_in_executor(None, _do)
            if cleaned:
                logger.debug("blackboard_db.cleanup count={}", cleaned)
            return cleaned
