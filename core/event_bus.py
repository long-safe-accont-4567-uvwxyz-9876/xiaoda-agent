"""AgentEventBus — 子代理生命周期事件总线。
解耦主会话与子代理调度，事件定向投递给当前 User：

设计原则：
- EventBus 不绑定传输层，不搞订阅/广播
- emit 时找到当前 session 的 User，调用 user.deliver(event)
- User 按自身渠道类型决定投递方式（CLI直接打印/Web ws推送/QQ仅开始通知）
- 事件类型严格定义，不传任意 dict
- 本地部署单用户项目，永远只有一个消费者：当前 User
"""
from __future__ import annotations

import time
import uuid
import contextvars
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from agent_core.user_base import UserBase


class AgentEventType(str, Enum):
    """子代理事件类型枚举。"""
    SUB_STARTED = "sub_started"
    SUB_PROGRESS = "sub_progress"
    SUB_COMPLETED = "sub_completed"
    SUB_FAILED = "sub_failed"
    SUB_CANCELLED = "sub_cancelled"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"


@dataclass
class AgentEvent:
    """子代理事件数据类。

    Attributes:
        type: 事件类型
        agent: 目标子代理名（xiaoli/xiaolang 等）
        task_id: 任务唯一标识
        data: 事件附加数据（tool_name/result_preview 等）
        timestamp: 事件时间戳
    """
    type: AgentEventType
    agent: str
    task_id: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# 当前 session 绑定的 User（ContextVar 实现协程安全）
_current_user: ContextVar["UserBase | None"] = ContextVar("event_bus_user", default=None)


class AgentEventBus:
    """子代理事件总线 — 定向投递，不是广播。

    使用方式：
        # 全局单例
        bus = AgentEventBus()

        # session 开始时绑定 User
        token = bus.bind_user(user)

        # 发射事件 → 自动投递给绑定的 User
        await bus.emit(AgentEvent(type=AgentEventType.SUB_STARTED, ...))

        # session 结束时解绑
        bus.unbind_user(token)
    """

    def bind_user(self, user: "UserBase") -> contextvars.Token:
        """绑定当前 session 的 User。返回 Token，调用方必须在 finally 中调用 unbind_user(token)。"""
        return _current_user.set(user)

    def unbind_user(self, token: contextvars.Token | None = None) -> None:
        """解绑 User（session 结束时调用）。传入 bind_user 返回的 Token 以安全恢复上下文。"""
        if token is not None:
            try:
                _current_user.reset(token)
            except (ValueError, LookupError):
                logger.debug("event_bus.unbind_noop: token already consumed or context mismatch")
        else:
            _current_user.set(None)

    @property
    def bound_user(self) -> "UserBase | None":
        """当前绑定的 User。"""
        return _current_user.get()

    async def emit(self, event: AgentEvent) -> None:
        """发射事件，投递给当前绑定的 User。

        如果没有绑定 User（比如初始化阶段），静默忽略。
        User.deliver() 异常不中断调用方。
        """
        user = _current_user.get()
        if user is None:
            return
        try:
            await user.deliver(event)
        except Exception as e:
            logger.debug("event_bus.deliver_error type={} error={}",
                         event.type, str(e)[:100])


def gen_task_id(agent: str, input_hint: str = "") -> str:
    """生成任务唯一标识。"""
    return f"{agent}_{uuid.uuid4().hex[:8]}"


# ── 全局单例 ──────────────────────────────────────────────────
event_bus = AgentEventBus()