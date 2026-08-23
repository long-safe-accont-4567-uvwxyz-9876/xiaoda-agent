"""AgentCore 组合结构契约测试（技术债 P1-2）。

背景：AgentCore 由 12 个 Mixin 拼装（3 直接 + MessageProcessorMixin 内 9 个），
所有 Mixin 共享同一个巨型 self。完整解耦是多会话工程；本测试冻结当前组合
清单——任何人新增/删除 Mixin 必须显式更新此文件，防止金字塔无声生长。
目标架构方向（Mixin → 组合式 service）见 docs/ 后续 ADR。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_core.core import AgentCore
from agent_core.message_processor import MessageProcessorMixin

# AgentCore 的直接基类（除 object 外）
EXPECTED_DIRECT_BASES = [
    "MessageProcessorMixin",
    "ToolExecutorMixin",
    "SubAgentManagerMixin",
]

# MessageProcessorMixin 的基类链（除 object 外），顺序即 MRO 优先级
EXPECTED_MP_BASES = [
    "StreamingMixin",
    "ChatTargetMixin",
    "VisionMixin",
    "PersonaMixin",
    "ReplyDedupMixin",
    "MainPathMixin",
    "VerificationMixin",
    "GreetingMixin",
    "VoiceMixin",
]


def _direct_base_names(cls: type) -> list[str]:
    return [b.__name__ for b in cls.__bases__ if b is not object]


def test_agent_core_direct_bases_frozen() -> None:
    assert _direct_base_names(AgentCore) == EXPECTED_DIRECT_BASES


def test_message_processor_bases_frozen() -> None:
    assert _direct_base_names(MessageProcessorMixin) == EXPECTED_MP_BASES


def test_total_mixin_count_is_12() -> None:
    """金字塔总数冻结：新增职责请走组合/service，而不是再加一层 Mixin。"""
    mro_names = {c.__name__ for c in AgentCore.__mro__ if c is not object}
    expected = set(EXPECTED_DIRECT_BASES) | set(EXPECTED_MP_BASES) | {"AgentCore"}
    assert mro_names == expected
