"""结构化 Agent 间消息协议 — 替代字符串前缀约定 [NAHIDA_PENDING]/[KLEE_PENDING]。

当前仅定义数据结构，不改变现有字符串流程（行为变更属于后续任务）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentMessage:
    """Agent 间结构化消息。

    用于替代 [NAHIDA_PENDING]/[KLEE_PENDING] 字符串前缀约定，提供类型化、
    可序列化的消息载体，便于跨 Agent 通信与日志追踪。

    Attributes:
        sender: 发送方 Agent 名称（如 "klee"、"xiaoda"）
        receiver: 接收方 Agent 名称
        msg_type: 消息类型 — "request"/"response"/"question"/"status"
        content: 消息正文
        context: 附加上下文（默认空 dict）
        timestamp: 消息创建时间戳（默认 time.time()）
    """

    sender: str
    receiver: str
    msg_type: str  # "request" | "response" | "question" | "status"
    content: str
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "sender": self.sender,
            "receiver": self.receiver,
            "msg_type": self.msg_type,
            "content": self.content,
            "context": dict(self.context),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentMessage":
        """从字典构造实例。

        缺少 context 时使用空 dict；缺少 timestamp 时使用当前时间。
        """
        return cls(
            sender=data["sender"],
            receiver=data["receiver"],
            msg_type=data["msg_type"],
            content=data["content"],
            context=dict(data.get("context", {})),
            timestamp=data.get("timestamp", time.time()),
        )

    def is_delegate_request(self) -> bool:
        """判断是否为委托请求（msg_type == "request"）。"""
        return self.msg_type == "request"
