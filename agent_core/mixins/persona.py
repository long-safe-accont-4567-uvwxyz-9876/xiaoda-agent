"""PersonaMixin —— Phase 5 拆分自 message_processor.py。

包含人格/心智状态方法：_should_escalate_to_pro（是否升级 chat_pro）、
_update_mental_state_emotion（情绪同步 L/M/S 心理状态模型 S 层）、
_apply_persona_critic（LLM 输出人格一致性检查）、
_run_profile_insight（后台用户认知抽取写入 USER.md）。

叶子模块依赖约定：本 mixin 只允许依赖 agent_core._shared 及 config/core 叶子模块，
不得 import agent_core.message_processor（避免循环导入）。
"""
from __future__ import annotations

from loguru import logger


class PersonaMixin:
    """人格/心智状态相关方法的 Mixin，由 MessageProcessorMixin 组合使用。"""

    def _should_escalate_to_pro(self, user_msg: str, tools: list | None) -> tuple[bool, str]:
        # P0 修复（用户明确要求"取消对话通道分类机制"）：
        # 移除基于关键词/长度的通道分类（PRO_TASK_KEYWORDS）——性价比太低且误判多。
        # 仅保留显式用户意图触发：前端 [Think:] 按钮按下时升级到 chat_pro。
        # 工具调用、长消息、情感内容等不再通过关键词预判升级，
        # 由 LLM 在主路径自行决定推理深度（chat 模型已具备足够能力）。
        if getattr(self, "_think_mode", False):
            return True, "user_think_mode"
        return False, ""

    def _update_mental_state_emotion(self, emotion: dict, user_id: str = "") -> None:
        """将检测到的用户情绪更新到 L/M/S 心理状态模型的 S 层.

        受 MENTAL_STATE_ENABLED 环境变量控制, 默认开启.
        任何异常都被吞掉, 不影响主消息处理流程.
        """
        try:
            from core.mental_state import get_mental_state_manager
            mgr = get_mental_state_manager(user_id=user_id)
            if mgr.enabled:
                mgr.update_short_term(
                    emotion="",
                    user_emotion=emotion.get("primary", ""),
                )
        except Exception as e:
            logger.debug(f"mental_state.update_failed: {e}")

    def _apply_persona_critic(self, reply: str, user_openid: str, user_id: str) -> None:
        """应用 Persona Critic 检查 LLM 输出的人格一致性.

        在 LLM 输出后、发送给用户前调用.
        零质量回退: 任何异常都不影响主流程, 仅记录日志.
        """
        if not reply:
            return
        try:
            from core.persona_coherence import get_persona_critic
            from core.xp_system import get_xp_system

            _uid = user_openid or user_id
            if not _uid:
                return

            critic = get_persona_critic()
            if not critic.enabled:
                return

            xp_sys = get_xp_system()
            xp_state = xp_sys.get_state(_uid)
            check = critic.check(reply, xp_state.level.value)

            if check.needs_rewrite:
                logger.info("persona.rewrite_triggered",
                           score=check.score, issues=check.issues)
                # 实际重写逻辑可由调用方决定, 此处仅记录
            elif check.score < 0.7:
                # 添加案例到 Case Repository 供后续检索学习
                try:
                    critic._case_repo.add_case(reply, check)
                except Exception as e:
                    logger.debug(f"persona.add_case_failed: {e}")
        except Exception as e:
            logger.warning("persona.check_failed", error=str(e))

    async def _run_profile_insight(self, user_id: str, xp_level: int) -> None:
        """后台任务：调用 LLM 抽取用户认知并写入 USER.md。"""
        try:
            from core.user_profile_learner import get_user_profile_learner
            learner = get_user_profile_learner()

            # 从对话上下文获取近期消息
            recent = []
            try:
                recent = self.context.get_last_n(20) or []
            except Exception as e:
                logger.debug("recent_messages_read_failed", error=str(e))

            if not recent:
                return

            prompt = learner.build_insight_prompt(recent, xp_level)
            if not prompt:
                return

            # 轻量级 LLM 调用（使用 flash 路由，低成本）
            response = await self.router.route(
                task_type="memory_encoding",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=512,
                timeout=15,
            )
            if response:
                learner.save_insight(user_id, str(response), xp_level)
        except Exception as e:
            # A5 修复：使用结构化日志添加 error_type，便于排查空错误消息
            logger.warning("profile_learner.insight_failed",
                           error=str(e), error_type=type(e).__name__)
