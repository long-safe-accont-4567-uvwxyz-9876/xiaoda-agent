"""ChatTargetMixin —— Phase 6 拆分自 message_processor.py。

包含聊天目标路由逻辑：_parse_chat_target（解析用户聊天目标）、
get_chat_target / set_chat_target（获取/设置聊天目标子代理）。

说明：_chat_target_lock / _user_chat_target 为实例属性，由 AgentCore.__init__
（agent_core/core.py）初始化，本 mixin 方法内经 self.* 访问，经 MRO 解析。

叶子模块依赖约定：本 mixin 只允许依赖 agent_core._shared 及 config/core 叶子模块，
不得 import agent_core.message_processor（避免循环导入）。
"""
from __future__ import annotations

from loguru import logger


class ChatTargetMixin:
    """聊天目标路由相关方法的 Mixin，由 MessageProcessorMixin 组合使用。"""

    async def _parse_chat_target(self, user_input: str, user_id: str) -> list[str]:
        # INTENT_LLM_CLASSIFY=true 时用 LLM 路由，否则用关键词匹配
        try:
            import config as _cfg
            if getattr(_cfg, "INTENT_LLM_CLASSIFY", False):
                decision = await self._router_engine.decide_with_llm(user_input, user_id)
            else:
                decision = self._router_engine.decide(user_input, user_id)
        except Exception:
            decision = self._router_engine.decide(user_input, user_id)
        if decision.agent_names:
            async with self._chat_target_lock:
                self._user_chat_target[user_id] = decision.agent_names[-1]
        logger.debug("router.decision", agents=decision.agent_names,
                     mode=decision.mode, reason=decision.reasoning)
        return decision.agent_names

    async def get_chat_target(self, user_id: str) -> str:
        """获取用户的聊天目标子代理, 默认返回 'xiaoda'."""
        async with self._chat_target_lock:
            return self._user_chat_target.get(user_id, "xiaoda")

    async def set_chat_target(self, user_id: str, target: str) -> None:
        """设置用户的聊天目标子代理.

        Args:
            user_id: 用户标识
            target: 目标子代理名
        """
        async with self._chat_target_lock:
            self._user_chat_target[user_id] = target
