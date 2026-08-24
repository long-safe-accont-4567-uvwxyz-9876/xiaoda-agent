"""MainPathMixin —— Phase 3 拆分自 message_processor.py。

包含主处理路径相关方法：_run_main_process_path、_setup_main_emotion_and_memory、
_run_emotion_llm_background、_build_main_messages、_finalize_main_reply、
_dynamic_emotion_threshold、_retrieve_main_memories、_inject_image_description、
_prepare_sticker_and_tools、_resolve_task_and_circuit、_call_main_llm_with_verification。

叶子模块依赖约定：本 mixin 只允许依赖 agent_core._shared、agent_core.mixins.verification、
agent_core.mixins.voice 及 config/core 叶子模块，不得 import agent_core.message_processor
（避免循环导入）。
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import tempfile
import time
from typing import Any

from loguru import logger

from agent_core._shared import DEGRADED_REPLY, ProcessResult, is_degraded_reply
from agent_core.mixins.verification import _system_context_var
from agent_core.mixins.voice import _get_temperature
from config import (
    AGENT_CONFIG,
    STREAM_TEXT_PUSH,
    STRUCTURED_STREAM_EVENTS,
    build_safe_system_prompt,
    get_reply_dedup_enabled,
)
from core.background_tasks import _spawn
from core.circuit_breaker import CircuitState
from core.degradation_strategy import get_degradation_strategy
from emotion.emotion_enum import CN_TO_EN, ensure_emotion_tag, is_unified
from emotion.emotion_simple import build_emotion_hint, detect_emotion
from tool_engine.tool_registry import to_openai_tools
from utils.common import DEFAULT_MAX_TOKENS
from utils.common import safe_int as _safe_int
from utils.text_utils import encode_image_to_base64


class MainPathMixin:
    """主处理路径相关方法的 Mixin，由 MessageProcessorMixin 组合使用。"""

    async def _run_main_process_path(self, ctx: Any, user_input: Any, clean_input: Any, user_id: Any, source: Any,
                                      user_openid: Any, session_id: Any, status_callback: Any, image_data: Any,
                                      is_master: Any, force_voice: Any, trace: Any) -> Any:
        """主处理路径：完整记忆检索 + LLM 调用 + 后处理。"""
        _pipeline_t0 = time.time()
        _proc_id = f"{user_id[:12]}@{int(_pipeline_t0 * 1000) % 100000}"

        # 记忆检索阶段
        _mp_t0 = time.time()
        logger.info("pipeline.memory.start proc_id={} user_id={}", _proc_id, user_id[:20])
        emotion, emotion_label = await self._setup_main_emotion_and_memory(
            user_input, is_master, ctx)
        _mp_memory_ms = int((time.time() - _mp_t0) * 1000)
        logger.info("pipeline.memory.done proc_id={} elapsed_ms={} emotion={}",
                    _proc_id, _mp_memory_ms, emotion_label)
        if _mp_memory_ms > 3000:
            logger.warning("agent.stage_slow stage=memory_retrieval elapsed_ms={}", _mp_memory_ms)

        # 消息构建阶段
        _mp_t1 = time.time()
        logger.info("pipeline.build_msg.start proc_id={}", _proc_id)
        messages, _pre_picked_sticker, tools = await self._build_main_messages(
            user_input, is_master, image_data, clean_input, emotion, user_id, source)
        _mp_build_ms = int((time.time() - _mp_t1) * 1000)
        logger.info("pipeline.build_msg.done proc_id={} elapsed_ms={} msg_count={} tool_count={}",
                    _proc_id, _mp_build_ms, len(messages), len(tools) if tools else 0)
        if _mp_build_ms > 2000:
            logger.warning("agent.stage_slow stage=build_messages elapsed_ms={}", _mp_build_ms)

        # 任务类型解析与熔断器检查
        _mp_t1b = time.time()
        early_result, task_type, _cb_max_tokens, circuit_state, _model_cfg = \
            self._resolve_task_and_circuit(user_input, tools, messages, trace, source=source)
        logger.info("pipeline.resolve_task.done proc_id={} elapsed_ms={} task_type={} circuit={}",
                    _proc_id, int((time.time() - _mp_t1b) * 1000), task_type, circuit_state)
        if early_result is not None:
            logger.info("pipeline.early_return proc_id={} reason=circuit_or_task", _proc_id)
            return early_result

        # 主 LLM 调用 + 验收循环
        _mp_t2 = time.time()
        logger.info("pipeline.llm_verify.start proc_id={} task_type={} max_tokens={}",
                    _proc_id, task_type, _cb_max_tokens)
        principal = getattr(ctx, "principal", None)
        is_owner = principal.is_owner if principal is not None else ctx.is_master
        reply, tool_results = await self._call_main_llm_with_verification(
            messages, tools, task_type, _model_cfg, _cb_max_tokens, circuit_state,
            status_callback, user_openid, session_id, trace, ctx, user_input, is_owner)
        _mp_llm_ms = int((time.time() - _mp_t2) * 1000)
        logger.info("pipeline.llm_verify.done proc_id={} elapsed_ms={} reply_len={} tool_results={}",
                    _proc_id, _mp_llm_ms, len(reply) if reply else 0,
                    len(tool_results) if tool_results else 0)
        if _mp_llm_ms > 5000:
            logger.warning("agent.stage_slow stage=llm_verify elapsed_ms={} memory_ms={} build_ms={}", _mp_llm_ms, _mp_memory_ms, _mp_build_ms)

        # 后处理阶段（含媒体提取与隐私扫描）
        _mp_t3 = time.time()
        logger.info("pipeline.finalize.start proc_id={}", _proc_id)
        _result = await self._finalize_main_reply(
            reply, tool_results, user_input, user_id, source, emotion,
            emotion_label, ctx, user_openid, is_master, _pre_picked_sticker, force_voice, trace,
            session_id)
        logger.info("pipeline.finalize.done proc_id={} elapsed_ms={} total_ms={}",
                    _proc_id, int((time.time() - _mp_t3) * 1000),
                    int((time.time() - _pipeline_t0) * 1000))
        return _result

    async def _setup_main_emotion_and_memory(self, user_input: Any,
                                               is_master: Any,
                                               ctx: Any) -> tuple:
        """主路径阶段1：情绪检测 + 记忆检索。返回 (emotion, emotion_label)。"""
        emotion = detect_emotion(user_input)
        # emotion_llm 后台 fire-and-forget（不阻塞主流程，结果异步更新 mental_state）
        try:
            from config import ENABLE_EMOTION_LLM
            if ENABLE_EMOTION_LLM:
                _spawn(self._run_emotion_llm_background(
                    user_input, getattr(ctx, "user_id", "")), timeout=2.0)
        except Exception:
            logger.debug("emotion.llm_spawn_failed", exc_info=True)
        emotion_hint = build_emotion_hint(emotion)
        ctx.last_user_emotion = emotion.get("primary", "")
        self._update_mental_state_emotion(emotion, user_id=getattr(ctx, "user_id", ""))

        token = getattr(ctx, "user_context_token", None)
        if token is None:
            token = self.context.get_user_context_token()
        if token is not None:
            await self.context.commit_user_context(token, emotion_hint=emotion_hint)

        # 记忆检索与 notebook 上下文加载并行化
        memories = await self._retrieve_main_memories(
            user_input,
            is_master,
            emotion,
            user_token=token,
        )
        if token is not None:
            await self.context.commit_user_context(
                token,
                memory_retrieval=memories if memories else None,
            )

        emotion_label = emotion.get("primary", "")
        return emotion, emotion_label

    async def _run_emotion_llm_background(self, user_input: str, user_id: str) -> None:
        """emotion_llm 后台深度情绪分析（fire-and-forget）。

        不阻塞主流程，LLM 结果异步写入 mental_state（primary + PAD + needs），
        使后续请求的情绪引导提示使用更精准的 LLM 情绪信息。任何异常均吞掉。
        """
        try:
            from emotion.emotion_llm import detect_emotion_llm
            llm_emotion = await detect_emotion_llm(
                user_input, router=getattr(self, "router", None))
            if not llm_emotion or not llm_emotion.get("primary"):
                return
            from core.mental_state import get_mental_state_manager_if_exists
            mgr = get_mental_state_manager_if_exists(user_id=user_id)
            if mgr is not None and mgr.enabled:
                _pad = None
                if all(k in llm_emotion for k in ("P", "A", "D")):
                    _pad = {"P": llm_emotion["P"], "A": llm_emotion["A"],
                            "D": llm_emotion["D"]}
                _needs = llm_emotion.get("needs") or None
                mgr.update_short_term(
                    emotion="",
                    user_emotion=llm_emotion.get("primary", ""),
                    user_pad=_pad,
                    user_needs=_needs,
                )
                logger.debug("emotion.llm_background_updated",
                             primary=llm_emotion.get("primary"))
        except Exception:
            logger.debug("emotion.llm_background_failed", exc_info=True)

    async def _build_main_messages(self, user_input: Any, is_master: Any, image_data: Any,
                                     clean_input: Any, emotion: Any,
                                     user_id: Any, source: Any = None) -> tuple:
        """主路径阶段2：构建消息 + 图片描述注入 + 表情包/工具准备。返回 (messages, _pre_picked_sticker, tools)。"""
        # 构建消息
        effective_input = user_input
        if not is_master:
            safe_prompt = build_safe_system_prompt(
                address_term=self.context.current_address_term)
            messages = [{"role": "system", "content": safe_prompt}]
            messages.append({"role": "user", "content": effective_input})
        else:
            messages = await self.context.build_messages(effective_input, source=source or "")

        # P0 新增：system_context 注入（主动问候等内部场景）
        # 根因：nudge_engine/greeting_scheduler 原先把场景提示作为 user_input 传入，
        #       导致 conversation_logs.user_message 出现"（场景：现在早上...）"等系统提示，
        #       污染历史记录 + LLM 在后续轮次回应这些元提示。
        # 修复：场景提示走 system message，user_input 保持中性占位符（如"（主动问候）"），
        #       仅 LLM 可见，不写入 DB。
        # P0-2 修复：从 ContextVar 读取（Task 级隔离），而非实例属性（单例并发覆写）
        _sys_ctx = _system_context_var.get() or ""
        if _sys_ctx:
            # 插入到消息列表的开头（system prompt 之后、history 之前）
            _sys_msg = {"role": "system", "content": _sys_ctx}
            # 找到第一个非 system 消息的位置，插入到其前面
            _insert_idx = 0
            for i, m in enumerate(messages):
                if m.get("role") != "system":
                    _insert_idx = i
                    break
                _insert_idx = i + 1
            messages.insert(_insert_idx, _sys_msg)
            logger.debug("agent.system_context_injected",
                         ctx_len=len(_sys_ctx), insert_pos=_insert_idx)

        # 图片描述注入
        messages = await self._inject_image_description(messages, user_input, image_data)

        # 表情包意图与工具准备
        _pre_picked_sticker, tools = self._prepare_sticker_and_tools(
            messages, clean_input, emotion, is_master, user_id, user_input, image_data,
            source=source or "")

        return messages, _pre_picked_sticker, tools

    async def _finalize_main_reply(self, reply: str, tool_results: Any, user_input: Any,
                                     user_id: Any, source: Any, emotion: Any,
                                     emotion_label: str, ctx: Any, user_openid: Any,
                                     is_master: Any, _pre_picked_sticker: Any,
                                     force_voice: Any, trace: Any, session_id: Any) -> ProcessResult:
        """主路径阶段4+5：媒体提取、隐私扫描、人格校验、上下文记录、情绪标签、语音构建。返回 ProcessResult。"""
        # 媒体提取与隐私扫描
        media_image_paths, media_video_path, reply = await self._extract_media_from_tool_results(
            tool_results, reply)
        # 兜底：提取 LLM 伪造的图片 URL（未调 agnes_image_generate 而在回复里写 markdown 图/裸 URL）
        fab_image_paths, reply = await self._extract_fabricated_images_from_reply(reply)
        media_image_paths.extend(fab_image_paths)
        if not is_master and reply:
            safe, alt_reply, _ = self.security.check_output_privacy(reply)
            if not safe:
                logger.warning("agent.privacy_leak_blocked", user_id=user_id, reply_preview=reply[:100])
                reply = alt_reply

        # Persona Critic: 检查 LLM 输出人格一致性（LLM 输出后、发送给用户前）
        self._apply_persona_critic(reply, user_openid, user_id)

        # 仅主人群聊消息（及非群聊场景）记入记忆
        _should_remember = is_master or source != "qq_group"
        if _should_remember:
            if not ctx.handled_by_tool_call:
                # 降级/错误回复既不入记忆库也不入对话历史，
                # 否则 build_messages() 会让 LLM 在后续轮次看到系统内部状态，
                # 导致 LLM 模仿降级语气或基于假历史编造上下文。
                # 同时跳过 user 消息，避免留下未配对的 user 消息造成上下文断档。
                if is_degraded_reply(reply):
                    logger.info("agent.skip_degraded_reply_not_in_history", source=source, reply_preview=reply[:60])
                else:
                    await self.context.add_message("user", user_input)
                    rc = self.router.pop_reasoning_content()
                    # strip emotion tags before storing to memory
                    _clean_for_memory = self.sticker_manager.strip_emotion_tag(reply)
                    await self.context.add_message("assistant", _clean_for_memory, reasoning_content=rc)
                    # L5 修复: 捕获本次回复使用的模型名，透传到 conversation_logs.model_used
                    _model_used = self.router.get_current_chat_model().get("model_id", "")
                    self._bg_task_manager.run_background_tasks(
                        user_input, _clean_for_memory, user_id, source, emotion, tool_results,
                        session_id=session_id, model_used=_model_used,
                        request_context=getattr(ctx, "group_context_metadata", None),
                        user_context_token=getattr(ctx, "user_context_token", None),
                    )
        elif not is_degraded_reply(reply):
            _model_used = self.router.get_current_chat_model().get("model_id", "")
            self._bg_task_manager.log_conversation_only(
                user_input, reply, user_id, source, emotion,
                session_id=session_id, model_used=_model_used,
                request_context=getattr(ctx, "group_context_metadata", None),
            )
        # 群聊非主人不进入个人偏好、画像或记忆管线。
        if source != "qq_group" or is_master:
            try:
                from core.preference_pipeline import get_preference_pipeline
                _spawn(get_preference_pipeline().process_correction(
                    user_input, reply, self._bg_task_manager.learning_manager))
            except Exception as e:
                logger.debug("msg.preference_pipeline_spawn_failed", error=str(e))
        try:
            _spawn(self.router.flush_costs())
        except Exception as e:
            logger.error("费用统计刷新失败，可能丢失费用数据: {}", str(e))

        trace.info("agent.process.done", reply_preview=reply[:100],
                   reply_len=len(reply))

        # 情绪标签
        if is_unified():
            reply, ensured_emotion = ensure_emotion_tag(reply)
            if ensured_emotion.value != emotion_label:
                emotion_label = ensured_emotion.value

        if _pre_picked_sticker:
            clean_reply = self._finalize_reply(reply, strip_emotion=True, style="xiaoda")
            sticker_path = _pre_picked_sticker
        else:
            clean_reply, sticker_path = self.get_sticker_info(reply, ctx.last_user_emotion)
            # 统一清洗出口（get_sticker_info 已剥 emotion tag，故 strip_emotion=False）
            clean_reply = self._clean_reply_full(clean_reply, style="xiaoda", strip_emotion=False)

        audio_path, tts_pending, tts_text = await self._build_voice_result(
            clean_reply, emotion_label, force_voice)
        if audio_path:
            clean_reply = clean_reply + "\n\n🎙️ 语音消息已发送～"

        _spawn(self._hook_engine.fire_post_response())

        # 更新持续情绪状态（让 agent 有情绪惯性）
        try:
            from emotion.emotion_state import get_emotion_state
            _intensity_map = {
                "happy": 0.6, "excited": 0.8, "love": 0.7,
                "shy": 0.5, "sad": 0.7, "angry": 0.8,
                "surprised": 0.7, "confused": 0.4, "thinking": 0.3,
                "playful": 0.6, "moved": 0.7, "anxious": 0.6,
                "fear": 0.8, "pout": 0.5, "neutral": 0.2,
                "curious": 0.4, "greeting": 0.3,
            }
            get_emotion_state(getattr(ctx, "user_id", "")).update(
                emotion_label, _intensity_map.get(emotion_label, 0.5)
            )
        except Exception as e:
            logger.debug("emotion_state.update_failed", error=str(e))

        return ProcessResult(reply=clean_reply, emotion=emotion_label, sticker_path=sticker_path,
                             audio_path=audio_path, tool_results=tool_results, image_paths=media_image_paths,
                             video_path=media_video_path, tts_pending=tts_pending, tts_text=tts_text)

    def _dynamic_emotion_threshold(self, user_input: str, emotion: dict, base: float = 0.5) -> float:
        """根据对话情景动态调整情绪触发阈值。

        自适应策略:
          1. 情绪强度高 → 降低阈值 (更容易触发安慰记忆)
          2. 用户表达情感关键词多 → 降低阈值
          3. 对话深入 (长输入) → 降低阈值
          4. 短/无情感输入 → 保持或提高阈值 (避免误触发)

        最终阈值 clamp 在 [0.2, 0.8] 范围内, 防止极端值。
        """
        threshold = base
        intensity = float(emotion.get("intensity", 0.0))

        # 因子 1: 情绪强度越高, 阈值越低
        # intensity 0.8 → threshold -= 0.15; intensity 0.3 → threshold += 0.05
        if intensity >= 0.7:
            threshold -= 0.15
        elif intensity >= 0.5:
            threshold -= 0.05
        elif intensity <= 0.2:
            threshold += 0.05

        # 因子 2: 情感关键词密度
        emotional_words = (
            "难过", "伤心", "哭", "痛", "累", "烦", "压力", "焦虑",
            "害怕", "孤独", "想你", "分手", "吵架", "遗憾", "后悔",
            "开心", "喜欢", "幸福", "感恩", "想", "心情", "感觉",
        )
        query_lower = user_input.lower() if isinstance(user_input, str) else ""
        emo_count = sum(1 for w in emotional_words if w in query_lower)
        if emo_count >= 3:
            threshold -= 0.1   # 密集情感表达 → 大幅降低
        elif emo_count >= 1:
            threshold -= 0.05  # 有情感词 → 小幅降低

        # 因子 3: 输入长度 (深入对话)
        effective_len = sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in query_lower)
        if effective_len > 40:
            threshold -= 0.05  # 长输入: 用户在认真倾诉

        return max(0.2, min(0.8, threshold))

    async def _retrieve_main_memories(
        self,
        user_input: Any,
        is_master: Any,
        emotion: Any,
        user_token: Any | None = None,
    ) -> Any:
        """主路径记忆检索（含情绪触发的安抚记忆）与 notebook 加载并行。"""
        _retrieve_start = time.time()
        if user_token is not None:
            await self.context.commit_user_context(
                user_token, evidence_bundle=None
            )
        # 记忆检索超时（秒）在此统一解析一次，内层检索与外层 gather 复用同一值，
        # 避免配置 MEMORY_RETRIEVE_TIMEOUT > 8 时被外层写死的 8s 硬顶（日志口径不一致）。
        import config as _cfg
        _mem_timeout = float(getattr(_cfg, "MEMORY_RETRIEVE_TIMEOUT", 8.0))

        async def _retrieve_memories() -> Any:
            # 降级检查: L2+ 关闭记忆检索, 跳过以减少负载
            if not get_degradation_strategy().is_feature_available("memory_search"):
                return None
            if self.memory and is_master:
                self.memory.signal_new_message()
                _t0 = time.time()
                logger.info("pipeline.memory.retrieve.start")
                try:
                    _k = self.memory._suggest_k(user_input, default_k=8)
                    logger.info("pipeline.memory.retrieve.call start k={}", _k)
                    # 治本（2026-08-05）：单次记忆检索超时 2→5s。
                    # 根因：2s 对 embed/reranker/检索链路过短，网络波动即误砍，
                    #       导致 memory.retrieve_timeout_single 频繁 → 记忆注入为空 → 回复短。
                    # 记忆检索已与 notebook/constraint 解耦并独立执行。
                    # (2026-08-06) 超时改为 config.MEMORY_RETRIEVE_TIMEOUT（默认 8s）：
                    #   USB 盘慢时 5s 仍频繁误砍（今日 66 次 retrieve_timeout_single），
                    #   8s 给慢速存储足够余量，同时控制最坏延迟（LLM 前串行 await）。
                    from memory.scope import Scope, current_scope
                    memory_scope = current_scope()
                    # 群回复始终使用 conversation boundary；P0 隐私边界不可由配置绕过。
                    _session_id = str(getattr(memory_scope, "session_id", ""))
                    _is_group_scope = _session_id.startswith("qq_group:")
                    if _is_group_scope:
                        memory_scope = Scope.group(
                            user_id=memory_scope.user_id,
                            group_id=str(memory_scope.session_id)[len("qq_group:"):],
                            agent_id=memory_scope.agent_id,
                            request_id=memory_scope.request_id,
                        )
                    traced_retrieve = getattr(
                        self.memory, "retrieve_memories_with_trace", None
                    )
                    if inspect.iscoroutinefunction(traced_retrieve):
                        outcome = await asyncio.wait_for(
                            traced_retrieve(
                                user_input,
                                k=_k,
                                scope=memory_scope,
                                conv_user_id=memory_scope.user_id,
                            ),
                            timeout=_mem_timeout,
                        )
                        results = list(outcome.results)
                        outcome_degraded = outcome.degraded_components
                        outcome_dropped = outcome.dropped
                    else:
                        results = await asyncio.wait_for(
                            self.memory.retrieve_memories(
                                user_input,
                                k=_k,
                                scope=memory_scope,
                                conv_user_id=memory_scope.user_id,
                            ),
                            timeout=_mem_timeout,
                        )
                        outcome_degraded = ()
                        outcome_dropped = ()
                    if results and _is_group_scope:
                        _raw_count = len(results)
                        results = [m for m in results if memory_scope.matches_record(m)]
                        if len(results) != _raw_count:
                            logger.info("privacy.group_personal_memory_filtered",
                                        dropped=_raw_count - len(results),
                                        kept=len(results))
                    if user_token is not None:
                        try:
                            from memory.evidence import EvidenceBundle, RetrievalPlan
                            from memory.retrieval.trace import (
                                read_retrieval_dropped,
                                read_retrieval_trace,
                            )
                            channels = tuple(dict.fromkeys(
                                str(channel)
                                for item in (results or [])
                                for channel in (
                                    item.get("channels")
                                    or [item.get("source_channel") or item.get("source")]
                                )
                                if channel
                            ))
                            plan = RetrievalPlan.from_query(
                                str(user_input), scope=memory_scope, top_k=_k,
                                enabled_channels=channels,
                                budget_ms=int(_mem_timeout * 1000),
                            )
                            degraded_components = tuple(dict.fromkeys(
                                (*outcome_degraded, *read_retrieval_trace())
                            ))
                            upstream_dropped = tuple(dict.fromkeys(
                                (*outcome_dropped, *read_retrieval_dropped())
                            ))
                            evidence_bundle = EvidenceBundle.from_results(
                                plan, results or [],
                                degraded_components=degraded_components,
                                upstream_dropped=upstream_dropped,
                            ).apply_budget(
                                int(getattr(_cfg, "MEMORY_EVIDENCE_TOKEN_BUDGET", 3000))
                            )
                            committed = await self.context.commit_user_context(
                                user_token, evidence_bundle=evidence_bundle
                            )
                            logger.debug(
                                "memory.evidence_shadow_built committed={} retrieved={} injected={} dropped={}",
                                committed,
                                len(evidence_bundle.evidence) + len(evidence_bundle.dropped),
                                len(evidence_bundle.evidence),
                                len(evidence_bundle.dropped),
                            )
                        except Exception as evidence_error:
                            logger.warning(
                                "memory.evidence_shadow_failed error={}", str(evidence_error)
                            )
                    _retrieve_ms = int((time.time() - _t0) * 1000)
                    logger.info("pipeline.memory.retrieve.done elapsed_ms={} result_count={}",
                                _retrieve_ms, len(results) if results else 0)
                except asyncio.TimeoutError:
                    _delay = (time.time() - _t0) - _mem_timeout
                    logger.warning("memory.retrieve_timeout_single",
                                   hint=f"单次记忆检索超时 {_mem_timeout}s，跳过本次记忆",
                                   cancel_delay_ms=int(_delay * 1000),
                                   query_preview=user_input[:50])
                    results = None
                except Exception as e:
                    from local_ai.integration.reranker import is_structured_local_unavailable

                    if is_structured_local_unavailable(e):
                        raise
                    logger.warning("memory.retrieve_failed", error=str(e))
                    results = None
                if results is not None:
                    # 动态情绪阈值: 根据对话情景自适应调整
                    _base_threshold = 0.5
                    try:
                        import config as _emotion_cfg
                        _base_threshold = float(getattr(_emotion_cfg, "EMOTION_TRIGGER_THRESHOLD", 0.5))
                    except (ImportError, ValueError, TypeError):
                        logger.debug("main_path.emotion_threshold_config_fallback")
                    _emo_threshold = self._dynamic_emotion_threshold(
                        user_input, emotion, _base_threshold
                    )
                    # 注：comfort_memories 不再追加到 results
                    # 根因：retrieve_comfort_memories 只按情绪标签+重要性+时间排序，
                    # 与当前 query 零语义相关，会污染记忆检索结果，导致"回忆不准"。
                    # 情绪安抚应由模型基于真实相关记忆自行组织语言，而非注入无关"开心记忆"。
                    return results
                return None
            return None

        async def _load_notebook() -> None:
            _t0 = time.time()
            logger.info("pipeline.memory.notebook.start")
            try:
                await self._load_notebook_context(user_token=user_token)
                _elapsed = int((time.time() - _t0) * 1000)
                logger.info("pipeline.memory.notebook.done elapsed_ms={}", _elapsed)
                if _elapsed > 500:
                    logger.warning("memory.notebook_load_slow",
                                   elapsed_ms=_elapsed)
            except Exception as e:
                logger.warning("notebook.load_failed", error=str(e))

        # 治本（2026-08-05）：核心记忆检索与次要上下文解耦，杜绝"记忆被整段跳过"。
        # 根因：原实现 asyncio.gather(记忆检索, notebook, constraint) 整体被 3s wait_for
        #       包裹。notebook 加载慢（USB 盘 IO，实测 8s）时，3s 超时取消整个 gather，
        #       连正在进行的核心记忆检索也一并取消 → memory.retrieve_global_timeout →
        #       记忆被跳过 → 注入"没有找到相关记忆" → 回复短而敷衍。
        # 修复（根治，非缩短超时掩盖）：
        #   1) 核心记忆检索独立执行，给足超时（8s），确保能完整执行并注入上下文；
        #   2) notebook 是次要信息（当前关注点/待办），转后台异步，慢不阻塞记忆检索；
        #   3) constraint_lessons 结果从未被消费（jianjia 子串匹配粗糙，见下方注释），
        #      整体移除该空转慢环节。
        _gather_start = time.time()
        memories_task = asyncio.create_task(_retrieve_memories())
        if is_master:
            _spawn(_load_notebook())  # 全局单主人 notebook 仅 owner 可加载
        try:
            memories = await asyncio.wait_for(memories_task, timeout=_mem_timeout)
        except asyncio.TimeoutError:
            _cancel_delay = (time.time() - _gather_start) - _mem_timeout
            logger.warning("memory.retrieve_global_timeout",
                           hint=f"记忆检索整体超时 {_mem_timeout}s，跳过记忆继续生成回复",
                           cancel_delay_ms=int(_cancel_delay * 1000),
                           query_preview=user_input[:50])
            memories = None
        logger.info("memory.retrieve_stage",
                    stage="gather_done",
                    elapsed_ms=int((time.time() - _gather_start) * 1000),
                    has_memories=bool(memories))

        # 注：constraint_lessons 不再追加到 memories
        # 根因：jieba 子串匹配粗糙，单关键词命中即入选，极易注入无关经验，
        # 且以 "[经验] xxx" 格式混入 memory_retrieval，污染记忆检索结果。
        # 经验教训应通过独立通道（如 volatile 层）注入，不混入"相关记忆"。

        # ContextNest A2: 审计本次响应消费了哪些记忆版本 (point-in-time 重建支持)
        if memories and hasattr(self.memory, "audit_retrieval"):
            try:
                from memory.context_governance import ContextGovernance
                _response_id = ContextGovernance.new_response_id()
                # 治本修复（2026-08-05）：audit_retrieval 改 fire-and-forget。
                # 根因：audit_retrieval 写 DB（USB 盘），await 阻塞主流程 9s
                # （日志 11:37:11→11:37:20 memory.audited 9s 铁证）。
                # 审计是后台数据操作，不影响回复生成，无需阻塞用户等待。
                _spawn(self.memory.audit_retrieval(_response_id, memories))
            except Exception as e:
                logger.debug("memory.audit_call_failed", error=str(e))

        return memories

    async def _inject_image_description(self, messages: Any, user_input: Any, image_data: Any) -> Any:
        """向 messages 注入图片描述（直接传入图片或从用户输入提取路径）。"""
        if image_data:
            logger.info("agent.vision_start", image_count=len(image_data),
                        total_b64_size=sum(len(img.get('data', '')) for img in image_data))
            image_description = await self._describe_images(image_data)
            if image_description:
                messages.append({
                    "role": "system",
                    "content": f"用户发送了一张图片，图片内容识别结果如下：\n{image_description}\n\n请用你自己的语气和人格风格，自然地向用户描述你看到了什么，不要直接复述识别结果，不要提及视觉模型或识别工具。"
                })
            else:
                messages.append({
                    "role": "system",
                    "content": "用户发送了一张图片，但视觉识别未能成功识别图片内容。请诚实地告诉用户你暂时看不清这张图片，不要编造图片内容，可以请用户描述一下图片里是什么。"
                })
        elif "[图片:" in user_input and "已保存到" in user_input:
            img_path_match = re.search(r'已保存到\s+([^\s，。]+)', user_input)
            if img_path_match:
                img_path = img_path_match.group(1)
                # 路径安全检查：仅允许项目目录和临时目录
                _allowed_prefixes = (
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    os.path.expanduser("~"),
                    "/tmp",
                    tempfile.gettempdir(),
                )
                _resolved = os.path.realpath(os.path.abspath(img_path))
                if not any(_resolved.startswith(p) for p in _allowed_prefixes):
                    logger.warning("chat.image_path_traversal_blocked", path=img_path)
                    messages.append({
                        "role": "system",
                        "content": "[系统提示] 用户发送了一张图片，但图片路径不合法。请告诉用户你暂时无法查看这张图片。"
                    })
                else:
                    try:
                        mime, img_b64 = encode_image_to_base64(img_path)
                        image_description = await self._describe_images([{"mimeType": mime, "data": img_b64}])
                        if image_description:
                            messages.append({
                                "role": "system",
                                "content": f"用户发送了一张图片，图片内容识别结果如下：\n{image_description}\n\n请用你自己的语气和人格风格，自然地向用户描述你看到了什么，不要直接复述识别结果，不要提及视觉模型或识别工具。"
                            })
                        else:
                            messages.append({
                                "role": "system",
                                "content": "用户发送了一张图片，但视觉识别未能成功识别图片内容。请诚实地告诉用户你暂时看不清这张图片，不要编造图片内容，可以请用户描述一下图片里是什么。"
                            })
                    except (FileNotFoundError, ValueError):
                        messages.append({
                            "role": "system",
                            "content": "[系统提示] 用户发送了一张图片，但图片文件无法读取。请告诉用户你暂时无法查看这张图片。"
                        })
                    except Exception as e:
                        logger.warning("agent.image_load_failed", error=str(e))
                        messages.append({
                            "role": "system",
                            "content": "[系统提示] 用户发送了一张图片，但图片加载失败。请告诉用户你暂时无法查看这张图片。"
                        })
        return messages

    def _prepare_sticker_and_tools(self, messages: Any, clean_input: Any, emotion: Any, is_master: Any,
                                    user_id: Any, user_input: Any, image_data: Any,
                                    source: str = "") -> tuple:
        """准备表情包提示（注入 messages）与工具列表。返回 (_pre_picked_sticker, tools)。"""
        _sticker_keywords = ["表情包", "表情", "贴纸", "sticker", "贴图"]
        _sticker_intent = any(kw in clean_input for kw in _sticker_keywords)
        _pre_picked_sticker = None
        if (_sticker_intent and self.sticker_manager.available
                and get_degradation_strategy().is_feature_available("emotion")):
            _detected_e = self.sticker_manager.detect_emotion(clean_input)
            if not _detected_e:
                _detected_e = CN_TO_EN.get(emotion.get("primary", ""), "happy")
            _pre_picked_sticker = self.sticker_manager.pick(_detected_e)
            if _pre_picked_sticker:
                _sticker_desc = _pre_picked_sticker.stem.split("_", 1)[-1].replace("_", " ").replace("-", " ")
                _sticker_cat = _detected_e
                messages.append({
                    "role": "system",
                    "content": f"[系统提示] 你正在给用户发送一张表情包图片。图片描述：「{_sticker_desc}」，情绪分类：「{_sticker_cat}」。请在回复中自然地提到这张表情包的内容，让用户感受到你真的知道发了什么图。不要说'这是一张图片'之类的机械描述，要用你的风格自然表达。"
                })

        _tools_list = to_openai_tools()
        tools = _tools_list if _tools_list else None
        # 有图片时也保留完整工具列表——用户可能发参考图+要求生成图片（图生图场景），
        # 禁用工具会导致 agnes_image_generate 无法调用，LLM 只能用文字"假装"已生成。
        # 让 LLM 自行决定是否调用工具，符合下方"统一保留完整工具列表"的原则。

        # P0 修复（用户明确要求"取消对话通道分类机制"）：
        # 移除 filter_tools_for_simple_task 调用——通道分类性价比太低，
        # 误判会导致工具被错误过滤（如天气查询被当简单闲聊→工具被移除→瞎扯）。
        # 所有消息统一保留完整工具列表，由 LLM 自行决定是否调用。
        # 表情包意图时硬移除 delegate_task（CLAUDE.md 规范）
        # 表情包由主体流程自动附带，委托出去会丢失预选表情包和 system message
        if _sticker_intent and tools:
            tools = [t for t in tools if t.get("function", {}).get("name") != "delegate_task"]
            if not tools:
                tools = None
        # 非主人消息：白名单制，只保留允许的工具
        if not is_master and tools:
            tools = [t for t in tools if t.get("function", {}).get("name") in self.ALLOWED_NON_MASTER_TOOLS]
            if not tools:
                tools = None
            logger.info("agent.tools_filtered_for_non_master",
                        user_id=user_id, source=source,
                        allowed=list(self.ALLOWED_NON_MASTER_TOOLS))
        return _pre_picked_sticker, tools

    def _resolve_task_and_circuit(self, user_input: Any, tools: Any, messages: Any, trace: Any,
                                    source: str = "qq") -> tuple:
        """任务类型解析与熔断器检查。返回 (early_result, task_type, _cb_max_tokens, circuit_state, _model_cfg)。

        early_result 非 None 时表示熔断器 RED 状态，应直接返回。
        Web UI (source="web") 使用更高的 max_tokens 以支持近似 Hermes 的长回复流式输出；
        QQ 通道保持 ROUTE_TABLE 默认值（平台消息长度有限制）。
        """
        should_escalate, reason = self._should_escalate_to_pro(user_input, tools)
        # chat_pro 已合并进 chat（agnes 不支持 thinking，升级无意义）
        base_task = "chat"
        task_type = self.router.resolve_task_type(base_task)
        if should_escalate:
            trace.info("chat.escalate_skipped_merged", reason=reason,
                       hint="chat_pro merged into chat, agnes disables thinking")

        _model_cfg = AGENT_CONFIG.get("model", {})
        circuit_state = self._circuit_breaker.check(self._cognitive_state)
        if circuit_state == CircuitState.RED:
            logger.warning("agent.circuit_breaker_red")
            return ProcessResult(reply="系统需要休息一下，请稍后再试吧～"), \
                task_type, None, circuit_state, _model_cfg
        if circuit_state == CircuitState.HALF_OPEN:
            logger.info("agent.circuit_breaker_half_open_probe")

        _cb_max_tokens = None
        # Web UI 近似 Hermes 无限流式输出：使用 131072 tokens 上限（匹配模型上下文窗口）
        # P0 重构（用户明确要求"不许截断"）：
        # 根因：32768 对中文长回复过小，频繁触发 finish_reason="length"，
        #       原"请继续完成你的回复"重试 prompt 会污染上下文（LLM 回应"继续完成"等元词汇）。
        # 修复：提升到 131072（mimo/agnes 上下文窗口 128K），从源头消除截断。
        #       即使模型偶尔生成超长回复，也由 agent_context 压缩机制处理，不再截断重试。
        # QQ 通道保持 None → 走 ROUTE_TABLE 默认值（1500），避免超长回复被 QQ 平台截断
        _web_max_tokens = _safe_int(os.getenv("WEB_UI_MAX_TOKENS", "131072"), 131072)
        if source == "web":
            _cb_max_tokens = _web_max_tokens
        if circuit_state == CircuitState.YELLOW:
            messages.append({
                "role": "system",
                "content": "[系统警告] 当前认知状态不佳，请简化回复。"
            })
            _base_mt = _cb_max_tokens if _cb_max_tokens else _model_cfg.get("max_tokens", DEFAULT_MAX_TOKENS)
            _cb_max_tokens = int(_base_mt * 0.8)
        return None, task_type, _cb_max_tokens, circuit_state, _model_cfg

    async def _call_main_llm_with_verification(self, messages: Any, tools: Any, task_type: Any, _model_cfg: Any,
                                                _cb_max_tokens: Any, circuit_state: Any, status_callback: Any,
                                                user_openid: Any, session_id: Any, trace: Any, ctx: Any, user_input: Any, is_owner: Any) -> tuple:
        """主 LLM 调用 + 验收循环 + 熔断器状态更新。返回 (reply, tool_results)。"""
        reply = ""
        tool_results = []
        try:
            _llm_t0 = time.time()
            if STREAM_TEXT_PUSH and status_callback and (not tools or STRUCTURED_STREAM_EVENTS):
                logger.info("pipeline.llm_call.start mode=stream task_type={}", task_type)
                if tools and STRUCTURED_STREAM_EVENTS:
                    result = await self._stream_llm_turn(
                        messages, status_callback=status_callback, task_type=task_type,
                        turn=0,
                        temperature=_get_temperature(_model_cfg),
                        max_tokens=_cb_max_tokens,
                        tools=tools,
                        tool_choice="auto",
                        user_openid=user_openid, session_id=session_id,
                    )
                else:
                    result = await self._stream_llm_response(
                        messages, status_callback=status_callback, task_type=task_type,
                        temperature=_get_temperature(_model_cfg),
                        max_tokens=_cb_max_tokens,
                        user_openid=user_openid, session_id=session_id,
                    )
            else:
                logger.info("pipeline.llm_call.start mode=route task_type={} timeout={}",
                            task_type, self.LLM_CALL_TIMEOUT)
                result = await asyncio.wait_for(self.router.route(
                    task_type, messages,
                    temperature=_get_temperature(_model_cfg),
                    max_tokens=_cb_max_tokens,
                    tools=tools,
                    tool_choice="auto" if tools else None,
                    user_openid=user_openid, session_id=session_id,
                ), timeout=self.LLM_CALL_TIMEOUT)
            logger.info("pipeline.llm_call.done elapsed_ms={} result_type={}",
                        int((time.time() - _llm_t0) * 1000),
                        "str" if isinstance(result, str) else "tool_calls")

            # Harness 验收循环
            _verify_t0 = time.time()
            reply, tool_results = await self._run_verification_loop(
                result, messages, tools, trace,
                task_type=task_type,
                temperature=_get_temperature(_model_cfg),
                max_tokens=_cb_max_tokens,
                user_openid=user_openid, session_id=session_id,
                is_owner=is_owner, ctx=ctx, user_input=user_input,
            )
            logger.info("pipeline.verification.done elapsed_ms={} reply_len={} tool_count={}",
                        int((time.time() - _verify_t0) * 1000),
                        len(reply) if reply else 0, len(tool_results) if tool_results else 0)
            if tool_results:
                ctx.handled_by_tool_call = True
            # 最终防线：如果 verification loop 返回空回复，触发 fallback
            if not reply or not reply.strip():
                logger.warning("agent.empty_reply_guard", tool_count=len(tool_results))
                raise RuntimeError("empty_reply_guard: verification loop 返回空回复")
            logger.info("agent.got_reply", length=len(reply), preview=reply[:80],
                        tool_count=len(tool_results))
            # ── 跨对话回复去重（模型层面去重机制） ──
            # 检测新回复与最近 N 条回复的相似度，超阈值则重试一次
            # P0 修复：移除 `not tool_results` 门控条件
            # 根因：诊断日志证实"在吗"类问候回复 tool_cnt=1（情绪/贴纸工具）被跳过去重，
            #   而数据库里"在的呢～""像被电流贯穿"等重复恰是问候/角色扮演回复——
            #   这类最易重复的回复却被排除在去重之外，导致去重对最严重场景完全失效。
            # reply 是 LLM 最终回复文本，与是否调用工具无关，应统一参与去重。
            if reply and len(reply) > 20:
                # WebUI 开关：models.reply_dedup_enabled（默认开），关闭则跳过去重
                if not get_reply_dedup_enabled():
                    trace.info("reply.dedup_skipped", reason="webui_switch_off")
                else:
                    _dedup_t0 = time.time()
                    logger.info("pipeline.dedup.start reply_len={}", len(reply))
                    reply = await self._dedup_reply_against_recent(
                        reply, messages, task_type, _model_cfg,
                        _cb_max_tokens, user_openid, session_id, trace,
                    )
                    logger.info("pipeline.dedup.done elapsed_ms={} reply_len={}",
                                int((time.time() - _dedup_t0) * 1000), len(reply))
            if circuit_state == CircuitState.HALF_OPEN:
                self._circuit_breaker.on_half_open_success(self._cognitive_state)
            else:
                self._circuit_breaker.on_success(self._cognitive_state)
        except Exception as e:
            trace.error("agent.model_error", error=str(e))
            if circuit_state == CircuitState.HALF_OPEN:
                self._circuit_breaker.on_half_open_failure(self._cognitive_state)
            else:
                self._circuit_breaker.on_failure(self._cognitive_state)
            if self._error_handler:
                try:
                    error_reply = await self._error_handler.handle_error_with_intelligence(
                        error=e, user_query=user_input, context="主处理流程模型调用错误"
                    )
                    reply = error_reply if error_reply and len(error_reply) > 50 else DEGRADED_REPLY
                except Exception as e:
                    logger.debug("agent.error_handler_fallback: {}", e)
                    reply = DEGRADED_REPLY
            else:
                try:
                    result = await self.router.route(
                        "chat", messages, temperature=0.7,
                        user_openid=user_openid, session_id=session_id,
                    )
                    reply = self._clean_reply(result) if isinstance(result, str) else DEGRADED_REPLY
                except Exception as e:
                    logger.debug("agent.flash_fallback: {}", e)
                    reply = DEGRADED_REPLY
        return reply, tool_results
