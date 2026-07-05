"""委派请求与结果数据结构 — 替代字符串前缀约定 [KLEE_PENDING]/[NAHIDA_PENDING]。

当前仅定义数据类，不改变现有字符串流程（行为变更需要更多测试）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DelegationRequest:
    """子代理委派请求。

    Attributes:
        type: 委派目标类型 — "klee" 或 "xiaoda"
        question: 委派的问题/任务
        delegator: 发起委派的 Agent 名称
        depth: 委派深度（防止无限递归）
    """

    type: str  # "klee" or "xiaoda"
    question: str
    delegator: str
    depth: int = 0


@dataclass
class DelegationResult:
    """子代理委派结果。

    Attributes:
        success: 是否成功
        reply: 回复内容（成功时）
        error: 错误信息（失败时）
    """

    success: bool
    reply: str = ""
    error: str = ""
