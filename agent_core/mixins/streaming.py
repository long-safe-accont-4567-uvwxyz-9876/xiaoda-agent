"""StreamingMixin — 流式 LLM 响应（逐 token 推送 + 失败降级）。

自 agent_core/message_processor.py 拆分（上帝文件 Phase 7）：函数体逐字节搬移，
仅缩进调整。依赖 self.router（chat_stream / route / fallback_chat）经 MRO 组合。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from agent_core._shared import _stream_finish_reason_var
from config import STREAM_TEXT_PUSH


class StreamingMixin:
    async def _stream_llm_response(self, messages: list, status_callback: Any=None,
                                    task_type: str = "chat", **kwargs: Any) -> str:
        """流式调用 LLM，逐 token 推送给前端。

        当 STREAM_TEXT_PUSH=true 时使用此方法。
        失败时降级到原有同步调用。
        """
        if not STREAM_TEXT_PUSH:
            return await self.router.route(task_type, messages, **kwargs)

        # 重置流式 finish_reason，避免上次调用的残留值干扰截断检测
        # CodeRabbit 复审修复 #6：改为 ContextVar 重置（每个 Task 有独立 context）
        _stream_finish_reason_var.set(None)
        full_response = []
        try:
            async for delta in self.router.chat_stream(messages, task_type=task_type, **kwargs):
                if delta:
                    full_response.append(delta)
                    if status_callback:
                        try:
                            await status_callback({
                                "type": "stream_text",
                                "delta": delta,
                                "accumulated": "".join(full_response),
                            })
                        except Exception as cb_err:
                            logger.debug("agent.stream_callback_failed: {}", str(cb_err)[:100])
        except Exception as e:
            logger.warning("message_processor.stream_llm_failed: {}", str(e)[:200])
            accumulated = "".join(full_response)
            # 根因：流式失败时原实现直接回落到 route()，route() 虽内部也走 fallback 链，
            # 但走的是「重新选主 provider 再失败再 fallback」的完整路径，多一次主调用开销；
            # 且 stream 路径的 e 已经是真实失败原因，直接喂给 _try_fallback_chain 跳过主重试更高效。
            # 保留 accumulated 非空时返回部分内容的现有行为，避免重复内容（已推送的 delta 不能撤回）。
            if accumulated:
                logger.info("message_processor.stream_partial_return len={}", len(accumulated))
                return accumulated + "\n\n[⚠️ 内容生成中断，以上为已生成的部分]"
            # 取舍：降级时 stream=False，把流式退化为一次性返回。
            # 原因：此处再消费一个 fallback provider 的流对象需要重复 stall timeout/finish_reason
            # 检测逻辑，复杂且易错；非流式返回用户感知仅是「这次没有逐字效果」，可靠性优先。
            fb_result = await self.router.fallback_chat(
                e, task_type, messages,
                kwargs.get("temperature", 0.7),
                False,
                kwargs.get("tools"),
                kwargs.get("tool_choice"),
                kwargs.get("timeout", 60),
                kwargs.get("user_openid", ""),
                kwargs.get("session_id", ""),
                kwargs.get("extra_headers"),
                original_max_tokens=kwargs.get("max_tokens"),
            )
            # 降级返回 str 直接用；返回 None（所有降级目标不可用）才回落到 route() 兜底。
            if isinstance(fb_result, str) and fb_result:
                return fb_result
            return await self.router.route(task_type, messages, **kwargs)
        return "".join(full_response)
