"""工具调用过程 → WebSocket 可视化事件（ToolCallCard 数据源）。

agent_core._execute_tool_with_hooks 中调用；无 WebUI 连接时为 no-op。
"""
from __future__ import annotations

import json

from loguru import logger

# msg_id/conn_id 统一使用 web._msg_context 的 ContextVar（由 ws_hub 消息分发处设置）。
# 消除双 contextvar：本模块原先自定义 current_msg_id，而 ws_hub 设置的是
# _msg_context 的对象，导致事件 msg_id 恒为空、对不上消息气泡。
from web._msg_context import current_conn_id, current_msg_id

# 敏感参数键名片段（大小写不敏感子串匹配）：命中值替换为 ***
_SENSITIVE_KEY_MARKERS: tuple[str, ...] = (
    "token", "secret", "password", "passwd", "api_key", "key",
    "authorization", "cookie", "credential",
)

_ARGS_PREVIEW_MAX_LEN = 200


def _redact_args_preview(arguments: dict | None) -> str:
    """序列化参数预览：敏感键值脱敏后截断到 200 字符。

    键名（大小写不敏感）含 token/secret/password/passwd/api_key/key/
    authorization/cookie/credential 片段的值替换为 ***，防止工具参数中的
    凭据经 WebSocket 事件泄露。
    """
    if not arguments:
        return ""
    redacted = {
        k: ("***" if any(m in str(k).lower() for m in _SENSITIVE_KEY_MARKERS) else v)
        for k, v in arguments.items()
    }
    try:
        return json.dumps(redacted, ensure_ascii=False)[:_ARGS_PREVIEW_MAX_LEN]
    except (TypeError, ValueError):
        return str(redacted)[:_ARGS_PREVIEW_MAX_LEN]


async def emit_tool_event(phase: str, tool_name: str, arguments: dict | None = None,
                          ok: bool | None = None, elapsed_ms: int | None = None) -> None:
    """工具事件可视化推送。

    修复：本函数绝不在工具执行路径上 await 发送。慢/挂起 WebSocket 连接会让
    发送阻塞数秒，直接拖慢工具本身（实测 list_stickers 在 WebUI 连接存在时
    被拖到 10s，进而吃掉验证循环墙钟导致降级）。
    用 _spawn 后台推送：任务被跟踪引用（不丢失、不被 GC），事件仍会送达，仅异步。

    定向发送（审计修复 2026-08-29）：事件只发给当前请求来源连接
    （conn_id 由 ws_hub 分发时设置）；conn 为空（QQ/CLI 等非 WS 入口）跳过
    发送。不再 broadcast 工具参数给所有连接，防跨会话泄露；args_preview
    中的敏感键值先脱敏（见 _redact_args_preview）。
    """
    try:
        from web.ws_hub import manager
        if manager.active_count == 0:
            return
        event = {
            "type": "tool_event",
            "msg_id": current_msg_id.get(),
            "phase": phase,
            "tool": tool_name,
            "args_preview": _redact_args_preview(arguments),
            "ok": ok,
            "elapsed_ms": elapsed_ms,
        }
        # 定向发送：仅发起来源连接；无来源连接（QQ/CLI）不发送
        conn_id = current_conn_id.get()
        if not conn_id:
            return
        # fire-and-forget：可视化推送不阻塞工具执行；_spawn 保证任务被跟踪不丢
        from core.background_tasks import _spawn
        _spawn(manager.send_to(conn_id, event))
    except (RuntimeError, OSError, ConnectionError, ImportError):
        logger.debug("tool_events.emit_error", exc_info=True)
