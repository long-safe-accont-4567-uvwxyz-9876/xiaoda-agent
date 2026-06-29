"""WebSocket 消息 ID 上下文 —— 解耦 web.tool_events 与 web.ws_hub 的循环.

原 web.tool_events 顶层定义 current_msg_id, 函数内 `from web.ws_hub import manager`;
而 web.ws_hub 函数内 `from web.tool_events import current_msg_id`, 形成循环:
    web.tool_events <-> web.ws_hub

将 current_msg_id ContextVar 移到本模块, 该模块不依赖任何 web 子模块,
tool_events 与 ws_hub 都从本模块导入, 循环消除.
"""
import contextvars

# 当前请求关联的 msg_id (由 ws_hub 在 process 前设置, 使工具事件能对上消息气泡)
current_msg_id: contextvars.ContextVar[str] = contextvars.ContextVar("ws_msg_id", default="")
