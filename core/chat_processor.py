"""ChatProcessor — 单轮对话主流程编排。

从 AgentCore._process_impl 中提取的核心对话处理逻辑。
当前作为编排层存在，引用 AgentCore 的组件而非完全独立，
后续可逐步将状态迁移到本模块。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_core._shared import ProcessResult


class ChatProcessor:
    """单轮对话主流程处理器。

    职责：
    1. 路由决策（子代理 / TaskGraph / xiaoda 直聊）
    2. 上下文构建（记忆/笔记/情绪/视觉）
    3. 模型调用与工具循环
    4. 后处理（媒体提取/情绪标签/表情包/语音）
    """

    def __init__(self, core: Any) -> None:
        """初始化 ChatProcessor。

        Args:
            core: AgentCore 实例（门面引用）
        """
        self._core = core

    async def process(self, ctx: Any, user_input: str, user_id: str,
                      source: str, user_openid: str, session_id: str,
                      status_callback: Any, image_data: list[dict] | None) -> ProcessResult:
        """处理单轮对话的主入口。

        当前实现直接委托回 AgentCore._process_impl，
        后续逐步将逻辑迁移到本方法中。
        """
        return await self._core._process_impl(
            ctx, user_input, user_id, source, user_openid, session_id,
            status_callback, image_data,
        )

    # ── 以下为逐步迁移的辅助方法 ──────────────────────────────────

    @staticmethod
    def should_use_task_graph(chat_targets: list[str], task_graph: Any, user_input: str,
                               user_id: str, force_voice: bool,
                               image_data: Any, clean_input: str,
                               is_manual_target_fn: Any, is_simple_task_fn: Any) -> bool:
        """判断是否应使用 TaskGraph 路由。"""
        return (
            "xiaoda" in chat_targets
            and task_graph is not None
            and not is_manual_target_fn(user_input, user_id)
            and not is_simple_task_fn(clean_input)
            and not force_voice
            and not image_data
            and not ("[图片:" in user_input and "已保存到" in user_input)
        )

    @staticmethod
    def filter_tools_for_simple_task(tools: list[dict] | None,
                                      clean_input: str,
                                      is_simple_fn: Any) -> list[dict] | None:
        """简单任务时过滤掉系统级工具。"""
        if not tools or not is_simple_fn(clean_input):
            return tools

        hidden = {"hardware_status", "gpio_control", "i2c_comm",
                  "service_manage", "network_diag", "dev_assist"}
        filtered = [t for t in tools if t.get("function", {}).get("name") not in hidden]
        return filtered if filtered else None

    @staticmethod
    def clean_mention_from_input(user_input: str) -> str:
        """移除 @mention 标记，返回纯文本输入。"""
        from config import get_agent_display_name
        chars: set[str] = set()
        for name in ("xiaoda", "xiaoli", "xiaolang", "xiaolian", "xiaoke"):
            chars.update(get_agent_display_name(name))
        pat = r'@[' + re.escape(''.join(sorted(chars))) + r']+'
        return re.sub(pat, '', user_input).strip()
