"""botpy SDK 补丁：心跳超时检测、session 失效重连、连接池异常恢复。

从 qq_bot_adapter.py 拆分而来，将 botpy 库的 monkey-patch 集中管理，
便于 SDK 升级时统一审查与移除。

补丁清单：
1. _patched_is_system_event: 心跳 ACK 时间戳记录（超时检测前置）
2. _patched_send_heart: 心跳超时检测 + 强制断开重连
3. _patched_on_closed: session 失效码（4007/4009）清空 session_id，
   限频码（4008）保留 session 走 RESUME
4. _patched_pool_init: 连接池异常恢复 + 指数退避重连
"""
from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger


def apply_botpy_patches() -> None:
    """应用所有 botpy SDK 补丁（幂等，重复调用安全）。"""
    from botpy.gateway import BotWebSocket
    from botpy.client import Client as _BotpyClient

    _patch_heartbeat(BotWebSocket)
    _patch_on_closed(BotWebSocket)
    _patch_pool_init(_BotpyClient)


def _patch_heartbeat(ws_cls: type) -> None:
    """补丁心跳：ACK 时间戳记录 + 超时检测 + 强制断开重连。"""
    _original_is_system_event = ws_cls._is_system_event

    async def _patched_is_system_event(self: Any, message_event: Any, ws: Any) -> Any:
        event_op = message_event.get("op")
        if event_op == ws_cls.WS_HEARTBEAT_ACK:
            self._last_heartbeat_ack = asyncio.get_running_loop().time()
        return await _original_is_system_event(self, message_event, ws)

    ws_cls._is_system_event = _patched_is_system_event

    _original_send_heart = ws_cls._send_heart

    async def _patched_send_heart(self: Any, interval: Any) -> None:
        _log = __import__("botpy.logging", fromlist=["get_logger"]).get_logger()
        _log.info("[botpy] 心跳维持启动（带超时检测）...")
        self._last_heartbeat_ack = asyncio.get_running_loop().time()
        missed_acks = 0
        while True:
            if self._conn is None:
                _log.debug("[botpy] 连接已关闭!")
                return
            if self._conn.closed:
                _log.debug("[botpy] ws连接已关闭, 心跳检测停止")
                return

            payload = {
                "op": self.WS_HEARTBEAT,
                "d": self._session["last_seq"],
            }
            try:
                await self.send_msg(__import__("json").dumps(payload))
            except Exception as e:
                _log.warning(f"[botpy] 心跳发送失败，连接可能已关闭: {e}")
                return
            await asyncio.sleep(interval)

            now = asyncio.get_running_loop().time()
            if now - self._last_heartbeat_ack > interval * 4:
                missed_acks += 1
                _log.warning(
                    f"[botpy] 心跳ACK超时 ({missed_acks}次), "
                    f"上次ACK: {int(now - self._last_heartbeat_ack)}秒前"
                )
                if missed_acks >= 3:
                    _log.warning("[botpy] 心跳ACK连续超时，强制断开重连!")
                    await self._conn.close()
                    return
            else:
                missed_acks = 0

    ws_cls._send_heart = _patched_send_heart


def _patch_on_closed(ws_cls: type) -> None:
    """补丁 on_closed：session 失效码清空 session_id 走 IDENTIFY 重连。

    根因：botpy _INVALID_RECONNECT_CODE=[9001,9005] 不含 4009(Session timed out)，
    导致 4009 后 session_id 未清空 → 重连用 ws_resume → RESUME 已超时 session 无效
    → QQ 网关接受连接但不推送消息 → bot 在线却收不到任何用户消息。

    CodeRabbit 修复：4008 是限频（rate limited），session 仍有效，应保留 session_id
    走 ws_resume（RESUME）。原实现把 4008 纳入清空 session_id → 重连走 ws_identify
    → 丢失未 ACK 的消息。
    """
    _original_on_closed = ws_cls.on_closed
    _SESSION_INVALID_CODES = {4007, 4009}

    async def _patched_on_closed(self: Any, close_status_code: Any, close_msg: Any) -> Any:
        _botpy_log = __import__("botpy.logging", fromlist=["get_logger"]).get_logger()
        if close_status_code in _SESSION_INVALID_CODES:
            _botpy_log.warning(
                f"[botpy] session失效(code={close_status_code})，清空session强制IDENTIFY重连"
            )
            self._session["session_id"] = ""
            self._session["last_seq"] = 0
        elif close_status_code == 4008:
            _botpy_log.warning(
                f"[botpy] 限频(code=4008)，保留session走RESUME，等待botpy backoff重连"
            )
        await _original_on_closed(self, close_status_code, close_msg)

    ws_cls.on_closed = _patched_on_closed


def _patch_pool_init(client_cls: type) -> None:
    """补丁连接池：异常恢复 + 指数退避重连。

    修复：multi_run 返回的协程对象恒为 truthy，原 `if coroutine: ... else: 重新登录`
    的 else 分支永不执行（死代码）。删除死分支，直接 await。
    """
    _original_pool_init = client_cls._pool_init

    async def _patched_pool_init(self: Any, token: Any, session_interval: Any) -> Any:
        _botpy_log = __import__("botpy.logging", fromlist=["get_logger"]).get_logger()
        for i in range(self._ws_ap["shards"]):
            session = {
                "session_id": "",
                "last_seq": 0,
                "intent": self.intents,
                "token": token,
                "url": self._ws_ap["url"],
                "shards": {"shard_id": i, "shard_count": self._ws_ap["shards"]},
            }
            self._connection.add(session)

        loop = self._connection.loop

        def _loop_exception_handler(_loop: Any, context: Any) -> None:
            _loop.default_exception_handler(context)
            exception = context.get("exception")
            if isinstance(exception, ZeroDivisionError):
                _loop.stop()

        loop.set_exception_handler(_loop_exception_handler)

        recon_attempts = 0
        max_recon_delay = 60

        while not self._closed:
            _botpy_log.debug("[botpy] 会话循环检查...")
            try:
                coroutine = self._connection.multi_run(session_interval)
                if self.ret_coro:
                    return coroutine
                await coroutine
                recon_attempts = 0
                if not self._closed:
                    await asyncio.sleep(0.1)
            except (TimeoutError, OSError, RuntimeError, ConnectionError) as e:
                recon_attempts += 1
                delay = min(5 * (2 ** min(recon_attempts - 1, 4)), max_recon_delay)
                _botpy_log.error(
                    f"[botpy] 会话异常: {e}, {delay}秒后重试 (第{recon_attempts}次)"
                )
                await asyncio.sleep(delay)
                try:
                    await self._bot_login(token)
                    for i in range(self._ws_ap["shards"]):
                        session = {
                            "session_id": "",
                            "last_seq": 0,
                            "intent": self.intents,
                            "token": token,
                            "url": self._ws_ap["url"],
                            "shards": {"shard_id": i, "shard_count": self._ws_ap["shards"]},
                        }
                        self._connection.add(session)
                except (OSError, RuntimeError, ConnectionError) as login_err:
                    _botpy_log.error(f"[botpy] 异常后重新登录失败: {login_err}")
        return None

    client_cls._pool_init = _patched_pool_init