"""botpy SDK 兼容层 —— QQ Bot 私有 API 适配的唯一下沉点。

背景：qq-botpy==1.2.3 的网关/会话存在缺陷，需要在 SDK 私有成员上打补丁
才能稳定长跑。之前这些补丁散落在 qq_bot_adapter 内联 + 独立 patch 模块两份，
改一处漏一处（漂移）。现集中到本模块，qq_bot_adapter.py 只负责调用
install_botpy_patches()，测试只依赖本模块。

适配清单（v1.2.3）：
  1) BotWebSocket._is_system_event — 记录心跳 ACK 时间点（心跳超时检测需用）
  2) BotWebSocket._send_heart      — 心跳发送失败即退出 + ACK 连续超时强制断连
  3) BotWebSocket.on_closed        — 4007/4009 会话失效清 session 强制 IDENTIFY；
                                     4008 限频保留 session 走 RESUME
  4) botpy.client.Client._pool_init — 会话循环异常退避 + 未完成登录补登录；
                                     顺带移除原实现永假的 else 分支
  5) 文件日志从 CWD/botpy.log 重定向到项目 LOG_DIR（redirect_bot_log）

升级 SDK 代价/风险：
  - qq-botpy 未提供公共 API 级别的心跳/重连注入点，以上成员均为 SDK 私有实现，
    版本升级可能改名/删除/改签名，届时对应补丁要么失效（成员已修复则删补丁），
    要么 AttributeError（签名/名称漂移）。
  - 升级前先跑 `python botpy_compat.py`（探针 check_sdk_compat）做适配自检。
  - 升级后验证：QQ 长连跑满 30min+（心跳 ACK 不超时）、4009 触发后能重新收消息。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from loguru import logger

__all__ = [
    "install_botpy_patches",
    "redirect_bot_log",
    "check_sdk_compat",
    "reset_install_flag",
    "reset_redirect_flag",
]

try:  # botpy 缺失时（纯探针环境）降级为 None，由 install/check 显式报错
    from botpy.client import Client
    from botpy.gateway import BotWebSocket
except ImportError:  # pragma: no cover —— 仅离线/非 QQ 环境触发
    Client = None  # type: ignore[assignment,misc]
    BotWebSocket = None  # type: ignore[assignment,misc]


# ── 1. 心跳 ACK 时间记录 ──────────────────────────────────────

_original_is_system_event: Any = None


async def _patched_is_system_event(self: Any, message_event: Any, ws: Any) -> Any:
    event_op = message_event.get("op")
    if event_op == getattr(BotWebSocket, "WS_HEARTBEAT_ACK", 11):
        self._last_heartbeat_ack = asyncio.get_running_loop().time()
    return await _original_is_system_event(self, message_event, ws)


# ── 2. 心跳维持（带超时检测） ─────────────────────────────────

_original_send_heart: Any = None


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

        # 先发送心跳（捕获连接关闭异常，避免心跳任务失败导致 QQ Bot 断连）
        payload = {
            "op": self.WS_HEARTBEAT,
            "d": self._session["last_seq"],
        }
        try:
            await self.send_msg(__import__("json").dumps(payload))
        except Exception as e:
            # WebSocket 已关闭或网络异常，心跳任务退出（QQ Bot SDK 会自动重连）
            _log.warning(f"[botpy] 心跳发送失败，连接可能已关闭: {e}")
            return
        await asyncio.sleep(interval)

        # 再检查 ACK 是否超时
        now = asyncio.get_running_loop().time()
        if now - self._last_heartbeat_ack > interval * 4:
            missed_acks += 1
            _log.warning(
                f"[botpy] 心跳ACK超时 ({missed_acks}次), "
                f"距上次ACK: {int(now - self._last_heartbeat_ack)}秒")
            if missed_acks >= 3:
                _log.warning("[botpy] 心跳ACK连续超时，强制断开重连!")
                await self._conn.close()
                return
        else:
            missed_acks = 0


# ── 3. on_closed session 失效处理 ─────────────────────────────
# 根因考古（SDK 升级评估必读）：botpy 的 _INVALID_RECONNECT_CODE=[9001,9005]
# 不含 4007/4009(Session timed out)——SDK 默认把这两个关闭码当可 RESUME 重连，
# 但 session 已失效：RESUME 后 QQ 网关接受连接却不推送任何消息（bot 在线却
# 收不到用户消息）。实测 2026-07-28 起每 30 分钟 4009+ws_resume、从未
# ws_identify、消息零接收，直到本补丁强制清 session 走 IDENTIFY 才恢复。
# SDK 若将 4007/4009 纳入 _INVALID_RECONNECT_CODE（或等价修复），本补丁应删除；
# close-code 语义若有变化，需按新语义重新评估。

_original_on_closed: Any = None


async def _patched_on_closed(self: Any, close_status_code: Any, close_msg: Any) -> Any:
    _SESSION_INVALID_CODES = {4007, 4009}  # session 失效，必须重新 IDENTIFY
    _botpy_log = __import__("botpy.logging", fromlist=["get_logger"]).get_logger()
    if close_status_code in _SESSION_INVALID_CODES:
        _botpy_log.warning(
            f"[botpy] session失效(code={close_status_code})，清空session强制IDENTIFY重连")
        self._session["session_id"] = ""
        self._session["last_seq"] = 0
    elif close_status_code == 4008:
        # 4008 限频：session 仍有效，保留 session_id 走 RESUME（不丢未 ACK 消息）
        # botpy 自带 session_interval backoff，不强行 sleep 避免与重连机制冲突
        _botpy_log.warning(
            f"[botpy] 限频(code=4008)，保留session走RESUME，等待botpy backoff重连")
    await _original_on_closed(self, close_status_code, close_msg)


# ── 4. Client._pool_init 会话循环 ──────────────────────────────

_original_pool_init: Any = None


async def _patched_pool_init(self, token: Any, session_interval: Any) -> Any:
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
            # multi_run 是 async def，返回的协程对象恒为 truthy——
            # 原 `if coroutine: ... else: 重新登录` 的 else 分支永不执行（死代码）。
            # 已删除死分支，直接 await；连接中断后由 bot_connect 内部
            # ws 重连机制（RESUME/IDENTIFY）接管，外层仅维持循环与异常退避。
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
                f"[botpy] 会话异常: {e}, {delay}秒后重试 (第{recon_attempts}次)")
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


# ── 安装 ───────────────────────────────────────────────────────

_PATCH_INSTALLED = False

# 补丁挂载点（类级符号，可 hasattr 精确校验）——install 与探针共用一份清单，
# 防止"安装路径"与"自检路径"各自维护漂移。
def _patch_targets() -> list[tuple[Any, str, str]]:
    return [
        (BotWebSocket, "_is_system_event", "BotWebSocket._is_system_event"),
        (BotWebSocket, "_send_heart", "BotWebSocket._send_heart"),
        (BotWebSocket, "on_closed", "BotWebSocket.on_closed"),
        (Client, "_pool_init", "Client._pool_init"),
    ]

# 补丁方法体运行时依赖的成员。其中 _conn/_session/_ws_ap/_connection/
# ret_coro/_closed/_bot_login 是 __init__ 动态赋值的实例属性——类上
# hasattr 恒 False，只能做源码名称级探测；SDK 升级改名时提前在此暴露，
# 而不是等心跳/会话循环跑到一半才 AttributeError 断连。
_PATCH_BODY_MEMBERS: dict[str, tuple[str, ...]] = {
    "BotWebSocket": ("WS_HEARTBEAT", "WS_HEARTBEAT_ACK", "_conn", "_session"),
    "Client": ("_ws_ap", "_connection", "ret_coro", "_closed", "_bot_login"),
}


def install_botpy_patches() -> None:
    """安装全部私有 API 补丁（幂等：重复调用安全）。

    原实现位于 qq_bot_adapter.py 模块顶层直接赋值；抽离后由
    qq_bot_adapter 在首个 Client 实例化前调用一次即可。

    契约预检：先验证全部挂载点再打补丁（validate-then-apply）。
    SDK 升级漂移统一抛 ImportError——调用方按"botpy 不兼容"降级，
    而不是 AttributeError 从模块导入处炸穿；同时杜绝半安装状态
    （前 N 个已打上、标志位未置位 → 重装时把已补丁函数存为
    _original_*，原实现引用永久丢失的双重包裹）。
    """
    global _PATCH_INSTALLED
    if _PATCH_INSTALLED:
        return
    if Client is None or BotWebSocket is None:
        raise ImportError("botpy 不可用，无法安装 QQ Bot 兼容补丁")
    missing = [label for owner, attr, label in _patch_targets()
               if not hasattr(owner, attr)]
    if missing:
        raise ImportError(
            "botpy 私有 API 漂移，请跑 python botpy_compat.py 自检后对齐适配清单："
            + ", ".join(missing))
    global _original_is_system_event, _original_send_heart
    global _original_on_closed, _original_pool_init
    _original_is_system_event = BotWebSocket._is_system_event
    BotWebSocket._is_system_event = _patched_is_system_event
    _original_send_heart = BotWebSocket._send_heart
    BotWebSocket._send_heart = _patched_send_heart
    _original_on_closed = BotWebSocket.on_closed
    BotWebSocket.on_closed = _patched_on_closed
    _original_pool_init = Client._pool_init
    Client._pool_init = _patched_pool_init
    _PATCH_INSTALLED = True
    logger.info("botpy_compat.patches_installed attrs=4")


def reset_install_flag() -> None:
    """仅供测试使用：清空已安装标记，便于隔离态重装。"""
    global _PATCH_INSTALLED
    _PATCH_INSTALLED = False


# ── 5. 文件日志重定向 ──────────────────────────────────────────

_REDIRECT_DONE = False


def redirect_bot_log(log_dir: Path | str | None = None) -> None:
    """把 botpy SDK 的文件日志从 cwd/botpy.log 重定向到项目 LOG_DIR。

    背景：botpy.Client 构造时默认以 ext_handlers=True 追加 DEFAULT_FILE_HANDLER，
    文件固定在 os.getcwd()/botpy.log（TimedRotatingFileHandler），导致日志散落
    启动目录、脱离项目轮转体系。基于 SDK configure_logging 的判空幂等
    （_ext_handlers 非空即跳过追加），在首个 Client 实例化前配置一次即可生效。
    重定向失败不阻断启动，回退 SDK 默认行为。
    """
    global _REDIRECT_DONE
    if _REDIRECT_DONE:
        return
    _REDIRECT_DONE = True
    try:
        from botpy import logging as _botpy_logging

        from config_paths import LOG_DIR as _LOG_DIR
        target = Path(log_dir or _LOG_DIR)
        target.mkdir(parents=True, exist_ok=True)
        handler_cfg = dict(_botpy_logging.DEFAULT_FILE_HANDLER)
        handler_cfg["filename"] = str(target / "botpy.log")
        _botpy_logging.configure_logging(ext_handlers=[handler_cfg])
        # SDK 只会给 logs 中已存在的 logger 补挂 handler；这里显式获取一次，
        # 确保重定向 handler 一定挂载（也兼容某些导入顺序下 logger 尚未创建）。
        _botpy_logging.get_logger()
        logger.info("botpy_log.redirected_dir={}", target)
    except Exception as exc:  # noqa: BLE001 —— 日志重定向失败仅告警并回退
        logger.warning("botpy_log.redirect_skip error={}", str(exc)[:150])


def reset_redirect_flag() -> None:
    """仅供测试使用：清空重定向已执行标记。"""
    global _REDIRECT_DONE
    _REDIRECT_DONE = False


# ── SDK 兼容性自检 ──────────────────────────────────────────────

def check_sdk_compat() -> list[str]:
    """返回与当前 botpy 私有 API 的适配漂移列表（空 = 全部对齐）。

    两层探针：
    - 挂载点（类级符号）：hasattr 精确校验，与 install 同一份清单；
    - 补丁方法体依赖的成员（多为 __init__ 动态赋值的实例属性，
      类上 hasattr 恒 False）：inspect.getsource 做名称存在性检查。
      漏检后果不是启动失败而是长连中途 AttributeError 断连。
    """
    import inspect

    issues: list[str] = []
    if Client is None or BotWebSocket is None:
        issues.append("botpy 导入失败：Client/BotWebSocket 不可用")
        return issues
    for owner, attr, label in _patch_targets():
        if not hasattr(owner, attr):
            issues.append(f"{label} 已不存在（SDK 升级请评估：修复则删补丁，改名则先对齐）")
    for cls_name, members in _PATCH_BODY_MEMBERS.items():
        cls = {"BotWebSocket": BotWebSocket, "Client": Client}[cls_name]
        try:
            src = inspect.getsource(cls)
        except (OSError, TypeError):  # pragma: no cover —— 源码不可得时跳过该层
            continue
        for member in members:
            if member not in src:
                issues.append(
                    f"{cls_name}.{member} 在 SDK 源码中已消失"
                    "（补丁方法体依赖，升级请对齐）")
    return issues


if __name__ == "__main__":
    drift = check_sdk_compat()
    if drift:
        print("botpy SDK 兼容漂移：")
        for line in drift:
            print("  -", line)
        sys.exit(1)
    print("botpy SDK 私有 API 全部对齐 (qq-botpy 1.2.x)。")