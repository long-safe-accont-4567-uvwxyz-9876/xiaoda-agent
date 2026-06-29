"""agent_core 共享常量与数据类型 — 解耦子模块之间的循环导入.

将 ProcessResult / RequestContext / UserIdentity / _current_request_ctx /
DEGRADED_REPLY 等模块级常量定义在此处, 由 agent_core.core 与各 Mixin
(sub_agent_manager / tool_executor / message_processor) 共同导入.

这样 Mixin 不再需要 `from agent_core.core import ...`, 真正的循环导入被打破,
agent_core.core 可以正常完成模块初始化.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── 模块级常量 ─────────────────────────────────────────────────
DEGRADED_REPLY = "嗯……人家现在有点不太舒服，等会儿再聊好不好？"


# ── 请求级 ContextVar (跨协程传递当前请求上下文) ──────────────
_current_request_ctx: ContextVar["RequestContext | None"] = ContextVar(
    "_current_request_ctx", default=None
)


# ── 数据类型 ──────────────────────────────────────────────────
@dataclass
class ProcessResult:
    """Agent 处理结果"""
    reply: str
    emotion: str = ""
    sticker_path: Path | None = None
    audio_path: Path | None = None
    tool_results: list = field(default_factory=list)
    image_paths: list[Path] = field(default_factory=list)
    video_path: Path | None = None
    # Task 6: TTS 异步化标记。为 True 时 audio_path 为空，需由调用方在后台合成并推送
    tts_pending: bool = False
    tts_text: str = ""
    # 并行调度异常归一化：当子代理执行抛出异常时，记录原始错误文本（空串表示成功）
    error: str = ""


@dataclass
class RequestContext:
    """请求级临时状态，每次 process() 调用创建一个新实例，避免并发请求时状态互相污染。"""
    session_id: str = ""
    user_openid: str = ""
    user_id: str = ""
    user_input: str = ""
    status_callback: Any = None
    handled_by_tool_call: bool = False
    last_user_emotion: str = ""
    delegate_depth: int = 0
    is_master: bool = True
    identity: Any = None  # UserIdentity 运行时身份解析结果


@dataclass
class UserIdentity:
    """运行时用户身份解析结果。基于 openID/UID 稳定标识，不依赖消息内容。"""
    is_owner: bool
    display_name: str
    address_term: str  # 称谓：主人→"爸爸"，其他→"用户"

    @staticmethod
    def default_owner() -> "UserIdentity":
        """返回默认主人身份 (称谓为 '爸爸')."""
        return UserIdentity(is_owner=True, display_name="爸爸", address_term="爸爸")

    @staticmethod
    def default_guest() -> "UserIdentity":
        """返回默认访客身份 (称谓为 '用户')."""
        return UserIdentity(is_owner=False, display_name="用户", address_term="用户")
