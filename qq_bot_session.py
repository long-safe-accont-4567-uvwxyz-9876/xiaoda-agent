"""QQ Bot C2C Session 缓存管理 Mixin。

从 qq_bot_adapter.py 拆分而来，负责 C2C 会话的内存缓存管理：
- TTL 过期清理
- FIFO 上限淘汰（防多用户长期运行内存泄漏）
- DB 查询降级（缓存命中时跳过 DB，避免 SQLite WAL 锁阻塞）
- 临时 session_id（qq_tmp_）检测与跳过
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any

from loguru import logger


class QQSessionMixin:
    """C2C session 缓存管理方法组。

    要求宿主类提供以下属性：
    - self._c2c_session_cache: dict[str, str]
    - self._c2c_session_cache_ts: dict[str, float]
    - self._c2c_session_cache_ttl: int
    - self._C2C_SESSION_CACHE_MAX_SIZE: int
    - self.agent: AgentCore 实例（提供 get_session / create_session）
    """

    def _prune_c2c_session_cache(self) -> None:
        """清理 C2C session 缓存中的过期与超限条目。

        1. 删除超过 TTL 的过期条目（避免永久驻留）
        2. 超过 MAX_SIZE 时按 FIFO（最早 ts）淘汰最旧条目（防多用户长期运行内存泄漏）
        """
        now = time.time()
        expired = [
            k for k, ts in self._c2c_session_cache_ts.items()
            if now - ts > self._c2c_session_cache_ttl
        ]
        for k in expired:
            self._c2c_session_cache.pop(k, None)
            self._c2c_session_cache_ts.pop(k, None)
        overflow = len(self._c2c_session_cache) - self._C2C_SESSION_CACHE_MAX_SIZE
        if overflow > 0:
            sorted_keys = sorted(self._c2c_session_cache_ts.items(), key=lambda kv: kv[1])
            for k, _ in sorted_keys[:overflow]:
                self._c2c_session_cache.pop(k, None)
                self._c2c_session_cache_ts.pop(k, None)

    def _invalidate_c2c_session(self, user_openid: str) -> None:
        """主动失效指定用户的 session_id 缓存。

        场景: agent.process 抛错（session 失效、被删除等）时调用，
        保证下次消息重新查 DB 获取最新 session_id。
        """
        self._c2c_session_cache.pop(user_openid, None)
        self._c2c_session_cache_ts.pop(user_openid, None)

    def _set_c2c_session_cache(self, user_openid: str, sid: str) -> None:
        """统一缓存写入 + 立即执行 size cap。

        替代分散的 `cache[k]=v; ts[k]=time.time()` 模式，确保写入后
        立即淘汰 overflow，不依赖下次 _get_or_create 的 pre-lookup prune。
        """
        self._c2c_session_cache[user_openid] = sid
        self._c2c_session_cache_ts[user_openid] = time.time()
        self._prune_c2c_session_cache()

    async def _get_or_create_c2c_session(self, user_openid: str) -> str:
        """获取或创建会话，失败时返回空字符串。添加超时保护防止 DB 锁长期阻塞消息处理。

        优化：使用内存缓存避免每条消息都查 DB。
        根因：单连接 SQLite + WAL 模式下，并发写操作会阻塞读，
              导致 get_active_session 超时 5 秒触发 c2c_session_timeout。
        修复：首次成功后缓存 session_id 1 小时，避免重复查询；
              仅在缓存失效或会话不存在时才查 DB。
        """
        self._prune_c2c_session_cache()
        cached_sid = self._c2c_session_cache.get(user_openid)
        cached_ts = self._c2c_session_cache_ts.get(user_openid, 0)
        if (cached_sid
                and not cached_sid.startswith("qq_tmp_")
                and (time.time() - cached_ts < self._c2c_session_cache_ttl)):
            return cached_sid

        deadline = time.monotonic() + 20.0
        try:
            session = await asyncio.wait_for(
                self.agent.get_session(user_openid),
                timeout=max(deadline - time.monotonic(), 0.1),
            )
            if session:
                sid = session["id"]
                self._set_c2c_session_cache(user_openid, sid)
                return sid
            sid = await asyncio.wait_for(
                self.agent.create_session(user_openid),
                timeout=max(deadline - time.monotonic(), 0.1),
            )
            self._set_c2c_session_cache(user_openid, sid)
            return sid
        except (TimeoutError, sqlite3.OperationalError) as e:
            logger.warning(
                "qq_bot.c2c_session_db_error openid={} error={}, retrying",
                user_openid, str(e)[:100],
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error("qq_bot.c2c_session_deadline_exhausted openid={}", user_openid)
                return f"qq_tmp_{user_openid[:16]}"
            try:
                session = await asyncio.wait_for(
                    self.agent.get_session(user_openid),
                    timeout=remaining,
                )
                if session:
                    sid = session["id"]
                    self._set_c2c_session_cache(user_openid, sid)
                    return sid
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.error("qq_bot.c2c_session_deadline_exhausted openid={}", user_openid)
                    return f"qq_tmp_{user_openid[:16]}"
                sid = await asyncio.wait_for(
                    self.agent.create_session(user_openid),
                    timeout=remaining,
                )
                self._set_c2c_session_cache(user_openid, sid)
                return sid
            except (TimeoutError, sqlite3.OperationalError) as e2:
                logger.error(
                    "qq_bot.c2c_session_db_error_retry openid={} error={}",
                    user_openid, str(e2)[:100],
                )
                return f"qq_tmp_{user_openid[:16]}"
        except (KeyError, OSError, RuntimeError) as e:
            logger.error("qq_bot.c2c_session_failed: {}", e, exc_info=True)
            return f"qq_tmp_{user_openid[:16]}"