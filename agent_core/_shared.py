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

# 按 finish_reason 分类的兜底文案（替代统一 DEGRADED_REPLY，让用户看到更明确的提示）
# 当 LLM 返回空内容时，根据 finish_reason 选择对应兜底文案
EMPTY_REPLY_REASON_MESSAGES: dict[str, str] = {
    # max_tokens 截断：返回的兜底文案，外层应触发"请继续"重试，这里是重试失败后的兜底
    "length": "嗯……刚才想得太长被截断了，请稍等一下再让人家回答好不好？",
    # content_filter：内容被安全过滤，明确提示用户
    "content_filter": "抱歉，刚才的回复可能涉及敏感内容被过滤了，能换个说法再问一次吗？",
    # tool_calls：LLM 想调用工具但没生成文本，由验收循环处理，这里只在最终兜底时使用
    "tool_calls": "嗯……人家正在查资料，请稍等一下再看回复好不好？",
}

# 默认兜底文案（finish_reason 未知或 None 时使用，保留原 DEGRADED_REPLY 语义）
EMPTY_REPLY_DEFAULT = DEGRADED_REPLY


def get_empty_reply_for_finish_reason(finish_reason: str | None) -> str:
    """根据 LLM finish_reason 返回对应的空回复兜底文案。

    Args:
        finish_reason: LLM 返回的 finish_reason 字段（length/content_filter/tool_calls/stop/None）

    Returns:
        对应的兜底文案字符串
    """
    if not finish_reason:
        return EMPTY_REPLY_DEFAULT
    return EMPTY_REPLY_REASON_MESSAGES.get(finish_reason, EMPTY_REPLY_DEFAULT)

# 降级/错误/拦截回复集合 — 这些回复不应写入记忆库，避免污染后续检索
# 包含：degraded reply、熔断回复、空回复、content_filter 后的截断回复、
# 按 finish_reason 分类的空回复兜底文案（length/content_filter/tool_calls）
_DEGRADED_REPLIES: frozenset[str] = frozenset({
    DEGRADED_REPLY,
    "系统需要休息一下，请稍后再试吧～",
    *EMPTY_REPLY_REASON_MESSAGES.values(),
})

# 降级回复前缀（用于模糊匹配，避免完全匹配遗漏变体）
_DEGRADED_PREFIXES: tuple[str, ...] = (
    "嗯……人家现在有点不太舒服",
    "系统需要休息一下",
    "嗯……出了点小问题",
    # 新增：按 finish_reason 分类的兜底文案前缀
    "嗯……刚才想得太长被截断",
    "抱歉，刚才的回复可能涉及敏感内容",
    "嗯……人家正在查资料",
)

# 降级回复中的特征短语（用 in 匹配，适配 agent 名称前缀等变体）
_DEGRADED_PHRASES: tuple[str, ...] = (
    "想得太入神了",
    "出了点小问题",
    # 新增：finish_reason 分类兜底文案的特征短语
    "想得太长被截断",
    "敏感内容被过滤",
    "正在查资料",
)


def is_degraded_reply(reply: str) -> bool:
    """检查回复是否是降级/错误/拦截回复。

    这些回复不应写入对话历史和记忆库，否则会污染 agent 后续的检索和回复质量。
    包含：DEGRADED_REPLY、熔断回复、空回复、超时降级回复等。

    Args:
        reply: 待检查的回复文本

    Returns:
        True 表示是降级回复，应跳过记忆写入
    """
    if not reply or not reply.strip():
        return True
    stripped = reply.strip()
    if stripped in _DEGRADED_REPLIES:
        return True
    return (any(stripped.startswith(p) for p in _DEGRADED_PREFIXES)
            or any(p in stripped for p in _DEGRADED_PHRASES))


# ── 请求级 ContextVar (跨协程传递当前请求上下文) ──────────────
_current_request_ctx: ContextVar[RequestContext | None] = ContextVar(
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
    def default_owner() -> UserIdentity:
        """返回默认主人身份 (称谓为 '爸爸')."""
        return UserIdentity(is_owner=True, display_name="爸爸", address_term="爸爸")

    @staticmethod
    def default_guest() -> UserIdentity:
        """返回默认访客身份 (称谓为 '朋友')."""
        return UserIdentity(is_owner=False, display_name="朋友", address_term="朋友")
