"""子代理管理 Mixin —— 拆分自原 agent_core.py 的 AgentCore 类。

包含单/并行子代理调度、通用委托、小莉委托、子代理上下文构建、
小妲转述、状态通知、手动目标判断、小莉反向委托等子代理管理相关方法。
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from loguru import logger

from agent_core._shared import (
    PARALLEL_WALL_TIMEOUT_S,
    SUB_AGENT_DISPATCH_TIMEOUT_S,
    TIRED_MSG,
    ProcessResult,
    RequestContext,
    _current_request_ctx,
    is_degraded_reply,
)

# TTS 时机控制 v2：统一触发决策（避免子 agent 路径漏守卫导致 voice_mode 开启后"失控"）
from agent_core.message_processor import _decide_tts_trigger
from config import TTS_ASYNC_MODE, build_system_prompt, get_agent_display_name
from core.cancel_token import CancellationError, CancelToken
from core.degradation_strategy import get_degradation_strategy
from core.event_bus import AgentEvent, AgentEventType, event_bus, gen_task_id
from emotion.emotion_enum import CN_TO_EN, EMOTION_TAG_GUIDE, VALID_EMOTION_TAGS
from emotion.emotion_simple import detect_emotion
from utils.text_utils import humanize, strip_dsml, strip_reasoning


# ── 子 Agent @ 对话模式专用：情绪标签规则（注入 system prompt）──────────────
# 仅在 _dispatch_single_sub_agent（用户 @ 子 Agent 直接对话）时注入，
# 正常 delegate_task 工具调用不注入（子 Agent 人格文件保留"不要加情绪标签"）。
# 标签会被 _finalize_reply 的 strip_emotion_tag 剥离，不会泄露给用户，
# 但会触发子 Agent 专属表情包系统（pick strict 模式：无对应分类不发）。
# 文案从 emotion_enum.EMOTION_TAG_GUIDE 动态生成——枚举即唯一事实源，
# 避免再出现提示词列表与表情包分类脱节的漂移（2026-08 review #1）。
def _render_emotion_rule(tags: dict[str, str]) -> str:
    """按给定的 标签→中文说明 表渲染情绪规则块。"""
    return (
        "## 情绪标签（必须遵守）\n"
        "\n"
        "⚠️ 这是一条硬性规则，每条回复都必须遵守：\n"
        "\n"
        "在每条回复的最末尾，附上一个情绪标签，格式为 [emotion:xxx]。xxx 为以下之一：\n"
        + "\n".join(f"- {tag} — {desc}" for tag, desc in tags.items())
        + "\n"
        "\n"
        "规则：\n"
        "1. 每条回复必须有且仅有一个情绪标签\n"
        "2. 标签放在回复文本的最末尾\n"
        "3. 这个标签不会显示给用户，但会用来选择合适的表情包，没有标签就无法发送表情包\n"
        "4. 不要用文字画表情包，表情包会根据情绪标签自动发送"
    )


# 全量规则（枚举全集）；实际注入时经 _sub_agent_emotion_rule 按代理表情包目录裁剪
_SUB_AGENT_EMOTION_RULE = _render_emotion_rule(EMOTION_TAG_GUIDE)





class SubAgentManagerMixin:
    """子代理管理相关方法的 Mixin，由 AgentCore 组合使用。"""

    def _sub_agent_emotion_rule(self, target: str) -> str:
        """按子代理专属表情包实际拥有的情绪分类生成标签规则。

        strict pick 模式下，提示词教了目录里没有的分类只会白白降低出图率
        （如 xiaoli 包仅有 6/17 类）。因此规则中的标签清单 =
        该代理物理目录 ∩ 核心枚举；目录缺失/为空时退回全量枚举，
        保证规则永远非空（2026-08 review 二轮 Fix C）。
        """
        mgr = self.get_sticker_manager(target)
        allowed = (mgr.available_categories & VALID_EMOTION_TAGS) if mgr.available else set()
        if not allowed:
            return _SUB_AGENT_EMOTION_RULE
        return _render_emotion_rule(
            {t: d for t, d in EMOTION_TAG_GUIDE.items() if t in allowed})

    async def _notify_sub_outcome(self, agent: str, event_type: Any,
                                  task_id: str, data: dict,
                                  belief_success: bool | None) -> None:
        """发射子代理生命周期事件 + BeliefRouter 反馈的统一入口。

        原先本文件 8 处"emit + belief try/except"复制粘贴块（且已出现语义
        漂移：超时路径有的更新 belief 有的不更新）收口至此。belief_success
        传 None 表示该事件无成败语义（STARTED/CANCELLED 等）或按原行为跳过反馈。
        """
        await event_bus.emit(AgentEvent(
            type=event_type, agent=agent, task_id=task_id, data=data))
        if belief_success is None:
            return
        _br = getattr(self.context, "belief_router", None)
        if _br:
            try:
                await _br.update_belief(agent, belief_success)
            except Exception as e:
                logger.debug("belief_router.update_failed agent={} error={}", agent, str(e)[:100])

    @staticmethod
    def _can_use_personal_context(source: str, ctx: RequestContext | None) -> bool:
        if source == "qq_group":
            principal = getattr(ctx, "principal", None) if ctx else None
            return (
                getattr(principal, "is_owner", False) is True
                or getattr(ctx, "is_master", False) is True
            )
        return True

    def _persist_sub_agent_reply(
        self,
        *,
        user_input: str,
        reply: str,
        user_id: str,
        source: str,
        emotion: dict,
        session_id: str,
        model_used: str,
        ctx: RequestContext | None,
    ) -> None:
        request_context = getattr(ctx, "group_context_metadata", None) if ctx else None
        if self._can_use_personal_context(source, ctx):
            self._bg_task_manager.run_background_tasks(
                user_input,
                reply,
                user_id,
                source,
                emotion,
                [],
                session_id=session_id,
                model_used=model_used,
                request_context=request_context,
                user_context_token=getattr(ctx, "user_context_token", None),
            )
            return
        self._bg_task_manager.log_conversation_only(
            user_input,
            reply,
            user_id,
            source,
            emotion,
            session_id=session_id,
            model_used=model_used,
            request_context=request_context,
        )

    async def _dispatch_single_sub_agent(self, target: str, clean_input: str,
                                          user_id: str, source: str, session_id: str, trace: Any,
                                          force_voice: bool = False,
                                          ctx: RequestContext | None = None) -> ProcessResult:
        _ctx = ctx or _current_request_ctx.get()
        sub_agent = self.dispatcher.get_agent(target)
        if not sub_agent or not sub_agent.available:
            await self._notify_sub_outcome(
                target, AgentEventType.SUB_FAILED, gen_task_id(target),
                {"error": f"agent unavailable: {target}"}, None)
            return ProcessResult(reply=f"{sub_agent.config.display_name if sub_agent else target}{TIRED_MSG}")

        display_name = sub_agent.config.display_name
        task_id = gen_task_id(target)
        await self._notify_sub_outcome(
            target, AgentEventType.SUB_STARTED, task_id,
            {"display_name": display_name, "input_preview": clean_input[:50]}, None)
        trace.info("agent.chat_target_sub", target=target, input_preview=clean_input[:50])
        allow_personal_context = self._can_use_personal_context(source, _ctx)
        context_str = self._build_sub_agent_context(
            include_personal=allow_personal_context,
        )
        # 审计 Fix7：本次调用专属的工具结果收集器（调用方创建并持有），经
        # dispatch → chat → _exec_one_tool_call 下传写入；取代原实例级
        # _last_tool_results（并发 dispatch 打到同一子代理会互相串媒体）。
        sub_tool_results: list = []
        sub_reply = await self._dispatch_sub_agent_with_events(
            target, clean_input, display_name, context_str, _ctx, task_id,
            tool_results_sink=sub_tool_results)

        # 审计 Fix6：直接 dispatch 路径补齐媒体提取——子代理生图/生视频的工具结果
        # 此前在 ProcessResult 中丢失（无 tool_results/image_paths/video_path），
        # WebUI/QQ 收不到子代理生成的图片。复用主路径 _extract_media_from_tool_results
        # （AgentCore 组合了 ToolExecutorMixin，生产环境必有；缺失或无工具结果时跳过，
        # 兼容仅实现部分方法的测试桩）。dispatch 契约（返回 str）保持不变。
        media_image_paths: list = []
        media_video_path = None
        if sub_tool_results and hasattr(self, "_extract_media_from_tool_results"):
            media_image_paths, media_video_path, sub_reply = \
                await self._extract_media_from_tool_results(sub_tool_results, sub_reply)

        emotion = detect_emotion(clean_input)
        if _ctx:
            _ctx.last_user_emotion = emotion.get("primary", "")
        # 子代理对话也写入主体历史：切回小妲或追问时上下文不断档
        # 降级/错误回复不入 history 也不入记忆库（与主对话路径一致），
        # 同时跳过 user 消息避免未配对断档
        if is_degraded_reply(sub_reply):
            logger.info("sub_agent.skip_degraded_reply_not_in_history", reply_preview=sub_reply[:60])
        else:
            if allow_personal_context:
                await self.context.add_message("user", clean_input)
                await self.context.add_message("assistant", sub_reply, agent=target)
            self._persist_sub_agent_reply(
                user_input=clean_input,
                reply=sub_reply,
                user_id=user_id,
                source=source,
                emotion=emotion,
                session_id=session_id,
                model_used=self.router.get_current_chat_model().get("model_id", ""),
                ctx=_ctx,
            )

        emotion_label = emotion.get("primary", "")
        sticker_path = self._detect_sub_sticker(target, sub_reply)

        # 剥离情绪标签（检测完成后才剥离，避免标签泄露给用户）
        clean_sub_reply = self._finalize_reply(sub_reply, style=target)

        # 子代理回复隐私扫描（与主 Agent 路径一致）
        principal = getattr(_ctx, "principal", None) if _ctx else None
        is_master = (
            getattr(principal, "is_owner", False) is True
            if principal is not None
            else (self.security.is_owner(user_id) if user_id else False)
        )
        if not is_master and clean_sub_reply:
            safe, alt_reply, _ = self.security.check_output_privacy(clean_sub_reply)
            if not safe:
                logger.warning("agent.sub_agent_privacy_leak_blocked",
                               target=target, user_id=user_id,
                               reply_preview=clean_sub_reply[:100])
                clean_sub_reply = alt_reply or f"{display_name}不方便回答这个问题呢～"

        sub_audio_path, sub_tts_pending, sub_tts_text = await self._synthesize_sub_tts(
            target, sub_agent, clean_sub_reply, emotion_label, force_voice)

        if sub_audio_path:
            clean_sub_reply = clean_sub_reply + "\n\n🎙️ 语音消息已发送～"

        return ProcessResult(reply=clean_sub_reply, emotion=emotion_label, sticker_path=sticker_path,
                             audio_path=sub_audio_path, tool_results=sub_tool_results,
                             image_paths=media_image_paths, video_path=media_video_path,
                             tts_pending=sub_tts_pending, tts_text=sub_tts_text)

    async def _dispatch_sub_agent_with_events(self, target: str, clean_input: str,
                                              display_name: str, context_str: str,
                                              _ctx: Any, task_id: str,
                                              tool_results_sink: list | None = None) -> str:
        """dispatch 子代理 + 事件 emit + BeliefRouter 反馈 + 异常降级；sink 原样透传（审计 Fix7）。"""
        # 注入情绪标签规则：@ 直接对话模式下，子 Agent 回复需带 [emotion:xxx] 标签
        # 以触发专属表情包系统（delegate_task 工具调用不注入，保持"不加标签"）
        # CancelToken 仅用于主动取消（timeout=None 不创建后台 timer task），
        # 真正超时保护交给 asyncio.wait_for —— 避免 dispatch 卡住时超时假象。
        token = CancelToken(timeout=None)
        try:
            token.check()  # 检查是否已被主动取消
            sub_reply = await asyncio.wait_for(
                self.dispatcher.dispatch(target, clean_input, context=context_str, status_callback=_ctx.status_callback if _ctx else None, address_term=self.context.current_address_term, extra_system_prompt=self._sub_agent_emotion_rule(target), tool_results_sink=tool_results_sink),
                timeout=SUB_AGENT_DISPATCH_TIMEOUT_S,
            )
            token.check()  # 检查是否在 dispatch 期间被主动取消
            await self._notify_sub_outcome(
                target, AgentEventType.SUB_COMPLETED, task_id,
                {"reply_preview": (sub_reply or "")[:100]},
                bool(sub_reply and sub_reply.strip()))
        except CancellationError:
            # 主动取消
            await self._notify_sub_outcome(
                target, AgentEventType.SUB_CANCELLED, task_id,
                {"error": "cancelled"}, None)
            sub_reply = f"{display_name}被取消了"
        except TimeoutError:
            # asyncio.wait_for 超时——真正中断 dispatch
            token.cancel("timeout")
            await self._notify_sub_outcome(
                target, AgentEventType.SUB_FAILED, task_id,
                {"error": "timeout"}, None)
            sub_reply = f"{display_name}处理超时了...等会儿再来吧！💤"
        except Exception as dispatch_err:
            # 其他 dispatch 异常——发射 SUB_FAILED 并降级
            await self._notify_sub_outcome(
                target, AgentEventType.SUB_FAILED, task_id,
                {"error": str(dispatch_err)[:200]}, False)
            sub_reply = None
        finally:
            token.cleanup()
        if sub_reply is None:
            sub_reply = f"{display_name}{TIRED_MSG}"
        return sub_reply

    def _detect_sub_sticker(self, target: str, sub_reply: str) -> str | None:
        """子 Agent 表情包：用原始回复检测情绪（含 [emotion:xxx] 标签），再选择表情包。

        剥离后 [emotion:xxx] 标签已消失，detect_emotion 只能靠关键词，不可靠，
        所以必须在 _finalize_reply 剥离标签前检测。
        """
        sub_sticker_mgr = self.get_sticker_manager(target)
        if not sub_sticker_mgr.available:
            return None
        # 1. 使用 sticker_manager 对子Agent的原始回复进行情绪检测（含 [emotion:xxx] 标签）
        detected = sub_sticker_mgr.detect_emotion(sub_reply)
        # 2. 如果 sticker_manager 未检测到，使用 emotion_simple 对原始回复进行情绪检测
        if not detected:
            sub_reply_emotion = detect_emotion(sub_reply)
            sub_reply_emotion_label = sub_reply_emotion.get("primary", "")
            if sub_reply_emotion_label:
                detected = CN_TO_EN.get(sub_reply_emotion_label, "")
        # 3. 检测到情绪且 should_send() 返回 True，则 pick() 选择表情包
        #    strict 模式：专属表情包目录无对应情绪分类就不发送（不 fallback 到全部随机）
        if detected and sub_sticker_mgr.should_send(sub_reply, detected_emotion=detected):
            return sub_sticker_mgr.pick(detected, strict=True)
        return None

    async def _synthesize_sub_tts(self, target: str, sub_agent: Any, clean_sub_reply: str,
                                  emotion_label: str, force_voice: bool) -> tuple[Any, bool, str]:
        """子代理 TTS（异步/同步双路径）。返回 (audio_path, tts_pending, tts_text)。

        TTS 时机控制 v2：统一过 _decide_tts_trigger（原串行路径完全无守卫，是子 agent
        回复代码/URL 也发语音的根因）。补齐内容守卫 + 降级守卫，与主路径一致。
        """
        sub_audio_path = None
        sub_tts_pending = False
        sub_tts_text = ""
        if _decide_tts_trigger(
                clean_sub_reply, force_voice=force_voice, voice_mode=self._voice_mode,
                tts_available=self.tts.available,
                tts_enabled=get_degradation_strategy().is_feature_available("tts")):
            if TTS_ASYNC_MODE:
                # Task 6: 异步 TTS
                sub_tts_pending = True
                sub_tts_text = self._clean_reply(clean_sub_reply)
            else:
                try:
                    sub_audio_path = await sub_agent.synthesize(self._clean_reply(clean_sub_reply), emotion=emotion_label)
                except Exception as e:
                    # 使用 ErrorClassifier 统一分类 TTS 异常，记录 reason/action 便于排查
                    classified = self._error_classifier.classify(e)
                    logger.warning("agent.sub_tts_failed", target=target,
                                   reason=classified.reason.value,
                                   action=classified.action.value,
                                   retryable=classified.is_retryable,
                                   error=str(e))
        return sub_audio_path, sub_tts_pending, sub_tts_text

    async def parallel_dispatch(
        self,
        targets_inputs: list[tuple[str, str]],
        user_id: str,
        source: str,
        session_id: str,
        trace: Any,
        ctx: RequestContext | None = None,
    ) -> list[ProcessResult]:
        """并行调度多个子代理，用于无依赖任务并发执行。

        例如：用户问"分别让小莉和小狼回答"，可同时调用两个子代理。

        所有传入任务视为无依赖，用 ``asyncio.gather`` 并发执行（Windows Proactor
        上 ``asyncio.create_task`` 存在已知问题，``gather`` 更兼容）。未来若需依赖
        检测，可在此处接入 ``core/parallel_dag.py`` 的 ToolDAG 构建 DAG。

        :param targets_inputs: [(target_name, input_text), ...]
        :returns: 每个 target 的 ProcessResult 列表（顺序与输入一致）
        """
        if not targets_inputs:
            return []

        # 单任务直接走串行路径，避免 gather 的额外开销
        if len(targets_inputs) == 1:
            target, input_text = targets_inputs[0]
            result = await self._dispatch_single_sub_agent(
                target, input_text, user_id, source, session_id, trace, ctx=ctx
            )
            return [result]

        # 多任务并行：return_exceptions 避免单个失败影响整体
        tasks = [
            self._dispatch_single_sub_agent(
                target, input_text, user_id, source, session_id, trace, ctx=ctx
            )
            for target, input_text in targets_inputs
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 异常归一化为 ProcessResult，保证顺序与输入一致且不阻塞其他任务
        final_results: list[ProcessResult] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                target = targets_inputs[i][0]
                logger.error("agent.parallel_dispatch_failed",
                             target=target, error=str(result))
                final_results.append(ProcessResult(
                    reply=f"{target}暂时无法响应，请稍后再试。",
                    error=str(result),
                ))
            else:
                final_results.append(result)
        return final_results

    async def _dispatch_parallel_sub_agents(self, targets: list[str], clean_input: str,
                                            user_id: str, source: str, session_id: str, trace: Any,
                                            force_voice: bool = False,
                                            ctx: RequestContext | None = None) -> ProcessResult:
        _ctx = ctx or _current_request_ctx.get()
        trace.info("agent.parallel_dispatch", targets=targets, input_preview=clean_input[:50])

        if _ctx and _ctx.status_callback:
            try:
                await _ctx.status_callback(f"⚡ 并行调度中，同时启动 {len(targets)} 个Agent...")
            except Exception as e:
                logger.warning("并行调度状态回调失败: {}", str(e))

        # 构建子代理任务上下文与子任务列表
        agent_configs = self._agent_route_configs
        sub_context = self._build_sub_agent_context(
            include_personal=self._can_use_personal_context(source, _ctx),
        )
        sub_tasks: dict[str, str] = {}
        for t in targets:
            desc = agent_configs.get(t, {}).get("route_description", t)
            sub_tasks[t] = f"关于「{clean_input}」中属于{desc or t}范畴的部分，请进行专业分析和处理。"
        # A2A 共享黑板：父代理在汇总时可读取黑板中已有子代理产出，避免重复计算
        bb = getattr(self.context, "shared_blackboard", None)

        # 并行执行所有子代理任务（return_exceptions 避免单个失败影响整体）
        # 墙钟超时保护：整体并行调度不超过 200 秒（层级定义见 _shared.py 截止时间区）
        _PARALLEL_WALL_TIMEOUT = PARALLEL_WALL_TIMEOUT_S
        try:
            raw_results = await asyncio.wait_for(
                asyncio.gather(
                    *[self._parallel_run_one(t, sub_tasks, sub_context, bb, clean_input, user_id) for t in targets],
                    return_exceptions=True,
                ),
                timeout=_PARALLEL_WALL_TIMEOUT,
            )
        except TimeoutError:
            logger.error("agent.parallel_dispatch_wall_timeout",
                         targets=targets, timeout=_PARALLEL_WALL_TIMEOUT)
            # 超时时返回已完成的子代理结果 + 超时提示
            raw_results = [
                {"agent": t, "display_name": t, "reply": f"{t}处理超时", "error": True}
                for t in targets
            ]
        # 聚合结果：异常归一化为 dict，便于统一展示
        intermediate: list[dict] = []
        for r in raw_results:
            if isinstance(r, Exception):
                intermediate.append({"agent": "unknown", "display_name": "未知",
                                     "reply": f"执行异常: {r}", "error": True})
            elif isinstance(r, dict):
                intermediate.append(r)

        all_replies = "\n\n".join(
            [f"【{r['display_name']}】\n{r['reply']}" for r in intermediate]
        )
        # model_used 由调用方传入（依赖注入）：此处已持有 self.router
        _parallel_model_used = self.router.get_current_chat_model().get("model_id", "")
        return await self._finalize_parallel_reply(
            all_replies, clean_input, user_id, source, session_id, force_voice, _ctx,
            intermediate=intermediate, model_used=_parallel_model_used,
        )

    async def _parallel_run_one(self, t: str, sub_tasks: dict[str, str], sub_context: str,
                                 bb: Any, clean_input: str, user_id: str = "") -> dict:
        """并行调度单个子代理：黑板缓存读写 + 超时控制 + 异常归一化。

        成功返回 dict(agent/display_name/reply)；失败时 reply 字段为降级文案，
        error=True 便于上层聚合时识别。
        """
        agent = self.dispatcher.get_agent(t)
        display_name = agent.config.display_name if agent else t
        if not agent or not agent.available:
            return {"agent": t, "display_name": display_name,
                    "reply": f"{display_name}暂时不可用", "error": True}
        sub_task = sub_tasks.get(t, clean_input)
        # 20.1/20.3: 委托前读取黑板中该子代理对同一任务的已有产出
        task_key = self._bb_task_key(t, sub_task, user_id=user_id)
        if bb is not None:
            try:
                cached = await bb.get(task_key)
                if cached is not None:
                    logger.debug("blackboard.parallel_hit key={} agent={}", task_key, t)
                    return {"agent": t, "display_name": display_name, "reply": cached}
            except Exception as e:
                logger.debug("blackboard.get_failed key={} error={}", task_key, e)
        task_id = gen_task_id(t)
        await self._notify_sub_outcome(
            t, AgentEventType.SUB_STARTED, task_id,
            {"display_name": display_name, "input_preview": sub_task[:50]}, None)
        try:
            reply = await asyncio.wait_for(
                self.dispatcher.dispatch(t, sub_task, context=sub_context, status_callback=None, address_term=self.context.current_address_term),
                timeout=SUB_AGENT_DISPATCH_TIMEOUT_S,
            )
            if reply is None:
                # 降级回复不缓存（与 delegate_to_agent / delegate_to_xiaoli 行为一致），
                # 避免后续 10 分钟内对同一任务持续返回降级文案
                # BeliefRouter 反馈回路：None 视为失败
                await self._notify_sub_outcome(
                    t, AgentEventType.SUB_COMPLETED, task_id,
                    {"reply_preview": ""}, False)
                return {"agent": t, "display_name": display_name,
                        "reply": f"{display_name}{TIRED_MSG}"}
            # 20.2: 子代理完成后将结果写入共享黑板，供父代理汇总或其他流程复用
            if bb is not None and reply:
                try:
                    await bb.put(task_key, reply, agent_name=t)
                except Exception as e:
                    logger.debug("blackboard.put_failed key={} error={}", task_key, e)
            await self._notify_sub_outcome(
                t, AgentEventType.SUB_COMPLETED, task_id,
                {"reply_preview": reply[:100]},
                bool(reply and reply.strip()))
            return {"agent": t, "display_name": display_name, "reply": reply}
        except TimeoutError:
            await self._notify_sub_outcome(
                t, AgentEventType.SUB_FAILED, task_id,
                {"error": "timeout"}, False)
            return {"agent": t, "display_name": display_name,
                    "reply": f"{display_name}处理超时", "error": True}
        except Exception as e:
            # 使用 ErrorClassifier 统一分类子代理委托异常
            # 根据 RecoveryAction 决定恢复策略：此处无既有重试逻辑，统一降级返回错误信息
            classified = self._error_classifier.classify(e)
            logger.warning("agent.parallel_sub_agent_failed", agent=t,
                           reason=classified.reason.value,
                           action=classified.action.value,
                           retryable=classified.is_retryable,
                           backoff=f"{classified.backoff_seconds:.1f}s",
                           error=str(e))
            await self._notify_sub_outcome(
                t, AgentEventType.SUB_FAILED, task_id,
                {"error": str(e)[:200]}, False)
            return {"agent": t, "display_name": display_name,
                    "reply": f"处理出错: {e}", "error": True}

    async def _finalize_parallel_reply(self, all_replies: str, clean_input: str,
                                        user_id: str, source: str, session_id: str,
                                        force_voice: bool, _ctx: Any,
                                        intermediate: list[dict] | None = None,
                                        model_used: str = "") -> ProcessResult:
        """并行子代理结果收尾：情绪检测、表情包选择、TTS 语音合成。

        并行结果直接使用，跳过小妲重新总结（SynthesisNode 已负责综合）。
        表情包优先使用子代理专属表情包管理器，降级到小妲的。

        model_used 由调用方传入（依赖注入）：调用方 _dispatch_parallel_sub_agents
        已持有 self.router，避免本方法直接够取 router 导致单测桩需额外 mock。
        """
        emotion = detect_emotion(clean_input)
        if _ctx:
            _ctx.last_user_emotion = emotion.get("primary", "")
        self._persist_sub_agent_reply(
            user_input=clean_input,
            reply=all_replies,
            user_id=user_id,
            source=source,
            emotion=emotion,
            session_id=session_id,
            model_used=model_used,
            ctx=_ctx,
        )

        emotion_label = emotion.get("primary", "")

        # 表情包选择：优先使用子代理专属表情包管理器
        sticker_path = None
        clean_reply = all_replies
        if intermediate:
            for item in intermediate:
                agent_name = item.get("agent", "")
                reply_text = item.get("reply", "")
                sub_sticker_mgr = self.get_sticker_manager(agent_name)
                if sub_sticker_mgr.available:
                    detected = sub_sticker_mgr.detect_emotion(reply_text)
                    if not detected:
                        sub_reply_emotion = detect_emotion(reply_text)
                        sub_reply_emotion_label = sub_reply_emotion.get("primary", "")
                        if sub_reply_emotion_label:
                            detected = CN_TO_EN.get(sub_reply_emotion_label, "")
                    if detected and sub_sticker_mgr.should_send(reply_text, detected_emotion=detected):
                        sticker_path = sub_sticker_mgr.pick(detected, strict=True)
                        if sticker_path:
                            break

        # 降级：子代理无表情包时使用小妲的
        if not sticker_path:
            clean_reply, sticker_path = self.get_sticker_info(
                all_replies, _ctx.last_user_emotion if _ctx else ""
            )
        else:
            # 剥离情绪标签
            clean_reply = self.sticker_manager.strip_emotion_tag(all_replies)

        # 清理推理内容和 DSML 标签（防止泄露给用户）
        clean_reply = strip_dsml(clean_reply)
        clean_reply = strip_reasoning(clean_reply)
        # humanize（与主小妲路径一致）
        clean_reply = humanize(clean_reply, style="xiaoda")

        audio_path = None
        tts_pending = False
        tts_text = ""
        # TTS 时机控制 v2：统一过 _decide_tts_trigger（原并行路径缺内容守卫，
        # voice_mode 开启后子 agent 代码/URL 回复也强制发语音）。与主路径一致。
        if _decide_tts_trigger(
                clean_reply, force_voice=force_voice, voice_mode=self._voice_mode,
                tts_available=self.tts.available,
                tts_enabled=get_degradation_strategy().is_feature_available("tts")):
            if TTS_ASYNC_MODE:
                # Task 6: 异步 TTS
                tts_pending = True
                tts_text = self._clean_reply(clean_reply)
            else:
                try:
                    audio_path = await self.tts.synthesize_xiaoda(
                        self._clean_reply(clean_reply), emotion=emotion_label
                    )
                except Exception as e:
                    # 使用 ErrorClassifier 统一分类 TTS 异常，记录 reason/action 便于排查
                    classified = self._error_classifier.classify(e)
                    logger.warning("agent.parallel_tts_failed",
                                   reason=classified.reason.value,
                                   action=classified.action.value,
                                   retryable=classified.is_retryable,
                                   error=str(e))

        if audio_path:
            clean_reply = clean_reply + "\n\n🎙️ 语音消息已发送～"

        return ProcessResult(
            reply=clean_reply, emotion=emotion_label, sticker_path=sticker_path,
            audio_path=audio_path, tts_pending=tts_pending, tts_text=tts_text,
        )

    async def delegate_to_agent(self, name: str, task: str,
                                 mode: str = "single", verifier: str = "") -> str:
        """通用子代理委托（delegate_task 工具的执行端）。

        Args:
            name: 目标子代理标识名（pipe 模式下用逗号分隔多个，如 "xiaolian,xiaoke"）
            task: 任务描述
            mode: 操作模式 — single(默认) / generate_verify(生成+验证) / pipe(顺序管道)
            verifier: 当 mode=generate_verify 时，指定验证子代理名
        """
        # pipe 模式：顺序管道，前一个的输出作为后一个的输入
        if mode == "pipe" and "," in name:
            agents = [a.strip().lower() for a in name.split(",") if a.strip()]
            if len(agents) >= 2:
                return await self._sequential_pipe(agents, task)

        # I8: 3 种新协作模式
        if "," in name:
            agents = [a.strip().lower() for a in name.split(",") if a.strip()]
            if len(agents) >= 2:
                if mode == "ensemble":
                    return await self._ensemble_agents(agents, task)
                if mode == "retry_fallback":
                    return await self._retry_fallback(agents, task)
                if mode == "debate":
                    return await self._debate_agents(agents, verifier, task)

        if name == "xiaoli":
            return await self.delegate_to_xiaoli(task)
        _ctx = _current_request_ctx.get()
        agent = self.dispatcher.get_agent(name)
        if not agent:
            return f"（找不到名为 {name} 的子代理）"
        # A2A 共享黑板：委托前读取已有产出，避免重复工作（黑板为 None 时跳过）
        bb = getattr(self.context, "shared_blackboard", None)
        task_key = self._bb_task_key(name, task, user_id=_ctx.user_id if _ctx else "")
        cached = await self._read_blackboard_cache(name, task, bb, task_key)
        if cached is not None:
            return cached
        context = self._build_sub_agent_context(task_hint=task)
        result, _duration = await self._dispatch_and_record(name, task, context, _ctx, agent)
        # I7: 记录子 Agent 工作履历 (供路由器智能调度)
        try:
            from core.agent_work_record import get_work_recorder
            get_work_recorder().record(
                name, task_type=mode, success=result is not None,
                duration=_duration)
        except Exception as e:
            logger.debug("sub_agent.work_record_failed", error=str(e))
        if result is None:
            return f"{agent.config.display_name}{TIRED_MSG}"

        result = await self._verify_result(name, task, result, mode, verifier)
        await self._write_blackboard_cache(bb, task_key, result, name)
        return result

    async def _read_blackboard_cache(self, name: str, task: str, bb: Any, task_key: str) -> str | None:
        """A2A 共享黑板读取已有产出；命中返回缓存，未命中/失败返回 None。"""
        if bb is None:
            return None
        try:
            cached = await bb.get(task_key)
            if cached is not None:
                logger.debug("blackboard.delegate_hit key={} agent={}", task_key, name)
                return cached
        except Exception as e:
            logger.debug("blackboard.get_failed key={} error={}", task_key, e)
        return None

    async def _dispatch_and_record(self, name: str, task: str, context: Any,
                                   _ctx: Any, agent: Any,
                                   timeout_s: float | None = SUB_AGENT_DISPATCH_TIMEOUT_S,
                                   interjections: list | None = None
                                   ) -> tuple[str | None, float]:
        """dispatch 子代理 + 事件 emit + BeliefRouter 反馈；失败重新抛出。

        timeout_s=None 表示不设外层超时（后台委托模式：长任务跑到底，
        不再"超时即取消丢工作"；卡死防护由 dispatcher 内部 LLM 超时承担）。
        """
        import time as _time_mod
        _t0 = _time_mod.time()
        task_id = gen_task_id(name)
        await self._notify_sub_outcome(
            name, AgentEventType.SUB_STARTED, task_id,
            {"display_name": agent.config.display_name, "input_preview": task[:50]}, None)
        try:
            result = await asyncio.wait_for(self.dispatcher.dispatch(
                name, task, context=context,
                status_callback=_ctx.status_callback if _ctx else None, address_term=self.context.current_address_term,
                interjections=interjections), timeout=timeout_s)
            _duration = _time_mod.time() - _t0
            await self._notify_sub_outcome(
                name, AgentEventType.SUB_COMPLETED, task_id,
                {"reply_preview": (result or "")[:100]},
                bool(result and result.strip()))
        except Exception as dispatch_err:
            _duration = _time_mod.time() - _t0
            await self._notify_sub_outcome(
                name, AgentEventType.SUB_FAILED, task_id,
                {"error": str(dispatch_err)[:200]}, False)
            raise
        return result, _duration

    async def run_background_delegation(self, job: Any) -> None:
        """后台委托执行体（delegate_task background=true 的落地端）。

        与同步路径的差异：
        - 无 wait_for 外层超时——长任务跑到底，修复"超时即取消丢工作"；
        - 完成后结果交回主代理：经 router 转述成主代理口吻，走普通消息
          通道主动发出（无专用帧、无机械回执，对用户就是主代理又说了话）；
        - 用户/主代理可通过 sub_agent_control 查看进度、终止（cancel）、
          插话（job.interjections 由 _chat_loop 每轮消费）。
        """
        from core import async_delegation as ad

        agent = self.dispatcher.get_agent(job.agent)
        if agent is None:
            ad.mark_done(job.task_id, ok=False,
                         preview=f"unknown agent {job.agent}")
            await ad.deliver_text(
                job, f"找不到名为 {job.agent} 的子代理，这项任务没法安排下去。", failed=True)
            return

        _ctx = _current_request_ctx.get()
        context = self._build_sub_agent_context(task_hint=job.task_text)

        # 进度采集：包一层回调写进 job.last_progress，供 sub_agent_control 查询
        base_cb = _ctx.status_callback if _ctx else None

        async def progress_cb(msg: Any) -> None:
            ad.note_progress(job, str(msg))
            if base_cb is not None:
                try:
                    await base_cb(msg)
                except Exception:  # noqa: BLE001 —— 原通道展示失败不影响执行
                    pass

        class _CtxShim:
            """透传原 ctx 关键字段 + 替换 status_callback 的轻量壳。"""

            def __init__(self, inner: Any, cb: Any) -> None:
                self._inner = inner
                self.status_callback = cb

            def __getattr__(self, item: str) -> Any:
                return getattr(self._inner, item)

        try:
            result, _duration = await self._dispatch_and_record(
                job.agent, job.task_text, context,
                _CtxShim(_ctx, progress_cb) if _ctx else None,
                agent, timeout_s=None, interjections=job.interjections)
        except asyncio.CancelledError:
            ad.mark_cancelled(job.task_id)
            await ad.deliver_text(
                job, f"收到收到～{job.display_name}的那个任务已经按你说的停下来了。")
            return
        except Exception as e:  # noqa: BLE001 —— 失败也要主动告知用户
            logger.warning("async_delegation.dispatch_failed agent={} error={}",
                           job.agent, str(e)[:150])
            ad.mark_done(job.task_id, ok=False, preview=str(e)[:120])
            await ad.compose_and_deliver(self, job, f"执行出错了：{str(e)[:300]}",
                                         failed=True)
            return

        if not (result and result.strip()):
            ad.mark_done(job.task_id, ok=False, preview="empty reply")
            await ad.compose_and_deliver(
                self, job, "子代理已执行，但没有返回有效内容。", failed=True)
            return

        try:
            from core.agent_work_record import get_work_recorder
            get_work_recorder().record(
                job.agent, task_type=job.mode, success=True, duration=_duration)
        except Exception as e:  # noqa: BLE001
            logger.debug("sub_agent.work_record_failed error={}", str(e))

        result = await self._verify_result(
            job.agent, job.task_text, result, job.mode, job.verifier)

        # 黑板缓存：与同步路径同键规则，供后续委托复用（黑板为 None 时内部跳过）
        try:
            bb = getattr(getattr(self, "context", None), "shared_blackboard", None)
            key = self._bb_task_key(
                job.agent, job.task_text,
                user_id=_ctx.user_id if _ctx else "")
            await self._write_blackboard_cache(bb, key, result, job.agent)
        except Exception as e:  # noqa: BLE001
            logger.debug("async_delegation.blackboard_write_failed error={}", str(e)[:120])

        ad.mark_done(job.task_id, ok=True, preview=result[:120])
        await ad.compose_and_deliver(self, job, result)

    async def _verify_result(self, name: str, task: str, result: str,
                             mode: str, verifier: str) -> str:
        """generate_verify / single 模式的交叉验证（single 模式按风险自动触发）。"""
        if mode == "generate_verify" and verifier:
            return await self._cross_verify(name, verifier, task, result)
        if mode == "single":
            # 自动验证：检测输出是否包含关键操作痕迹，自动触发交叉验证
            from core.risk_classifier import OutputRiskDetector
            is_critical, suggested_verifier = OutputRiskDetector.detect(result)
            if is_critical and suggested_verifier and suggested_verifier != name:
                logger.info("agent.auto_verify_triggered generator={} verifier={}",
                            name, suggested_verifier)
                return await self._cross_verify(name, suggested_verifier, task, result)
        return result

    async def _write_blackboard_cache(self, bb: Any, task_key: str, result: str, name: str) -> None:
        """A2A 共享黑板写入产出（黑板为 None 时跳过）。"""
        if bb is None:
            return
        try:
            await bb.put(task_key, result, agent_name=name)
        except Exception as e:
            logger.debug("blackboard.put_failed key={} error={}", task_key, e)

    async def _cross_verify(self, generator: str, verifier: str,
                             task: str, generated: str) -> str:
        """子代理交叉验证（借鉴 Trae Code Review Step 5.5）。

        验证子代理独立审查生成结果，发现问题则附加修正建议。
        """
        verify_prompt = (
            f"请审查以下任务执行结果，判断是否存在错误或遗漏。\n\n"
            f"任务：{task}\n"
            f"执行者：{generator}\n"
            f"执行结果：{generated}\n\n"
            f"请返回：1.是否存在明显错误（是/否）2.严重程度（高/中/低/无）3.理由及修正建议"
        )
        _ctx = _current_request_ctx.get()
        context = self._build_sub_agent_context(task_hint=verify_prompt)
        verify_result = await self.dispatcher.dispatch(
            verifier, verify_prompt, context=context,
            status_callback=_ctx.status_callback if _ctx else None, address_term=self.context.current_address_term)
        if verify_result is None:
            return generated  # 验证子代理不可用，退化为原结果
        # 检测验证结果是否发现问题
        if any(kw in verify_result for kw in ("是，存在", "是，存在明显", "严重程度：高", "严重程度:高")):
            logger.info("agent.cross_verify_issue_found generator={} verifier={}",
                        generator, verifier)
            return f"{generated}\n\n【{verifier}审查反馈】{verify_result}"
        return generated

    async def _sequential_pipe(self, agents: list[str], task: str) -> str:
        """顺序管道：前一个子代理的输出作为后一个的输入（借鉴 Trae Pattern 2）。

        agents[0] 的输入是 task，agents[1] 的输入是 task + agents[0] 的输出，
        依此类推。最终返回最后一个子代理的输出。
        """
        current_input = task
        for i, agent_name in enumerate(agents):
            if i == 0:
                pipe_task = task
            else:
                pipe_task = (
                    f"基于以下前置分析结果，继续完成任务：\n\n"
                    f"原始任务：{task}\n\n"
                    f"前置结果：{current_input}\n\n"
                    f"请基于以上信息继续分析并给出你的专业判断。"
                )
            result = await self.delegate_to_agent(agent_name, pipe_task, mode="single")
            current_input = result
            logger.debug("agent.pipe_step step={} agent={} result_len={}",
                         i + 1, agent_name, len(result))
        return current_input

    # ============================================================
    # I8: 3 种新协作模式
    # ============================================================

    async def _ensemble_agents(self, agents: list[str], task: str) -> str:
        """集成模式：多 agent 并行解决同一任务，选最全面的结果。

        借鉴 Trae Pattern 4 (ensemble) — 多个 agent 独立尝试，取最优。
        适用于创意任务、问题解决等有多种有效路径的场景。
        """
        tasks = [self.delegate_to_agent(a, task, mode="single") for a in agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid = [r for r in results if isinstance(r, str) and len(r) > 20]
        if not valid:
            return "（所有子代理都无法完成任务）"
        # 启发式：选最长的结果（通常最全面），无需额外 LLM 调用
        best = max(valid, key=len)
        logger.info("agent.ensemble_done agents={} best_len={}",
                    len(agents), len(best))
        return best

    async def _retry_fallback(self, agents: list[str], task: str) -> str:
        """重试降级：按优先级依次尝试，失败/空结果则降级到下一个。

        适用于可靠性要求高的任务 — 主 agent 不可用或失败时自动降级。
        """
        for _i, agent_name in enumerate(agents):
            try:
                result = await self.delegate_to_agent(agent_name, task, mode="single")
                if result and len(result) > 20:
                    return result
                logger.info("agent.retry_fallback_step agent={} result_short",
                            agent_name)
            except Exception as e:
                logger.warning("agent.retry_fallback_failed agent={} error={}",
                               agent_name, str(e)[:100])
        return "（所有子代理都未能完成任务）"

    async def _debate_agents(self, agents: list[str], synthesizer: str,
                                task: str) -> str:
        """辩论模式：两个 agent 持对立立场，综合者合并观点。

        借鉴 Trae Pattern 3 (debate) — 正反方独立论证，第三方综合。
        适用于分析、决策、评估等需要多角度思考的任务。
        """
        if len(agents) < 2:
            return await self.delegate_to_agent(
                agents[0] if agents else "xiaoda", task, mode="single")

        pro_prompt = f"请从正面/支持角度分析以下问题，给出你的论点和论据：\n{task}"
        con_prompt = f"请从反面/质疑角度分析以下问题，给出你的论点和论据：\n{task}"

        pro_task = self.delegate_to_agent(agents[0], pro_prompt, mode="single")
        con_task = self.delegate_to_agent(agents[1], con_prompt, mode="single")
        pro_result, con_result = await asyncio.gather(
            pro_task, con_task, return_exceptions=True)

        # 异常降级：如果某一方失败，用另一方的结果
        if not isinstance(pro_result, str) or len(pro_result) < 10:
            pro_result = "（正方无法给出观点）"
        if not isinstance(con_result, str) or len(con_result) < 10:
            con_result = "（反方无法给出观点）"

        synth_name = synthesizer or "xiaoda"
        synth_prompt = (
            f"以下是关于「{task}」的正反两方观点，请综合分析并给出平衡的结论：\n\n"
            f"【正方观点】\n{pro_result}\n\n"
            f"【反方观点】\n{con_result}\n\n"
            f"请综合以上观点，给出你的判断和建议。"
        )
        logger.info("agent.debate_done pro={} con={} synth={}",
                    agents[0], agents[1], synth_name)
        return await self.delegate_to_agent(synth_name, synth_prompt, mode="single")

    async def delegate_to_xiaoli(self, task: str, factual: bool = False) -> str:
        """将任务委托给小莉子代理完成并返回结果.

        Args:
            task: 任务描述文本
            factual: 是否要求仅返回事实数据 (不进行角色扮演), 默认 False

        Returns:
            子代理的回复文本
        """
        _ctx = _current_request_ctx.get()
        # A2A 共享黑板：委托前读取已有产出（factual 与非 factual 结果不同，需区分 key）
        bb = getattr(self.context, "shared_blackboard", None)
        task_key = self._bb_task_key("xiaoli", task, suffix="factual" if factual else "", user_id=_ctx.user_id if _ctx else "")
        cached = await self._read_blackboard_cache("xiaoli", task, bb, task_key)
        if cached is not None:
            return cached
        if factual:
            context = "这是小妲委托的查询任务。请直接返回查询结果，不要加任何个人风格、感叹号或角色扮演，只报告事实数据。"
        else:
            _xiaoda_dn = get_agent_display_name('xiaoda')
            _xiaoli_dn = get_agent_display_name('xiaoli')
            context = f"{_xiaoda_dn}委托{_xiaoli_dn}的任务。{_xiaoda_dn}温柔聪慧，{_xiaoli_dn}叫她'{_xiaoda_dn}姐姐'。{self.context.current_address_term}是{_xiaoda_dn}最亲近的人，也是{_xiaoli_dn}的大哥哥/大姐姐。"
        result = await self.dispatcher.dispatch("xiaoli", task, context=context, status_callback=_ctx.status_callback if _ctx else None, address_term=self.context.current_address_term)
        if result is None:
            return f"{get_agent_display_name('xiaoli')}{TIRED_MSG}"
        await self._write_blackboard_cache(bb, task_key, result, "xiaoli")
        return result

    @staticmethod
    def _bb_task_key(agent_name: str, task: str, suffix: str = "", user_id: str = "") -> str:
        """计算共享黑板中子代理委托结果的稳定 key。

        基于 user_id + agent_name + task 内容的 md5 摘要，保证：
        - 同一用户对同一子代理的相同任务命中缓存；
        - 不同用户即使提交相同任务也不会命中彼此的缓存（隐私隔离）。
        user_id 为空时退化为旧格式，保持向后兼容。
        """
        raw = task if not user_id else f"{user_id}\x00{task}"
        h = hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        key = f"bb:delegate:{agent_name}:{h}"
        if suffix:
            key += f":{suffix}"
        return key

    def _build_sub_agent_context(
        self,
        task_hint: str = "",
        *,
        include_personal: bool = True,
    ) -> str:
        parts = []
        recent = self.context.get_last_n(12) if include_personal else []
        if recent:
            conv_lines = []
            for m in recent:
                role = m.get("role", "")
                content = m.get("content", "")
                if not content or role == "tool":
                    continue
                prefix = {"user": f"{self.context.current_address_term}:", "assistant": f"{get_agent_display_name('xiaoda')}:"}.get(role, f"{role}:")
                conv_lines.append(f"{prefix} {content[:120]}")
            if conv_lines:
                parts.append("[对话历史]\n" + "\n".join(conv_lines))

        if task_hint:
            parts.append(f"[当前任务]\n{task_hint}")

        partner_lines = []
        configs = getattr(self, "_agent_route_configs", {}) or {}
        if configs:
            for _name, cfg in configs.items():
                if not isinstance(cfg, dict):
                    continue
                display_name = cfg.get("display_name", _name)
                route_desc = cfg.get("route_description", "")
                if route_desc:
                    partner_lines.append(f"{display_name}：{route_desc}")
                else:
                    partner_lines.append(f"{display_name}")
        else:
            partner_lines = [
                "小莉：擅长搜索、查资料、活泼的小帮手",
                "小狼：擅长代码、技术分析、黑客思维",
            ]
        if partner_lines:
            parts.append("[可用的伙伴]\n" + "\n".join(partner_lines) + "\n需要时可以通过 delegate_task 工具向她们求助")

        if include_personal and self.context.compressed_summary:
            parts.append(f"[早期对话摘要]\n{self.context.compressed_summary[:300]}")

        portrait = self.context.user_portrait if include_personal else None
        if portrait:
            parts.append(f"[{self.context.current_address_term}画像]\n{portrait[:200]}")

        return "\n\n".join(parts) if parts else ""

    async def _rephrase_as_xiaoda(self, user_input: str, xiaoli_result: str) -> str:
        try:
            prompt = (
                f"{self.context.current_address_term}问：{user_input}\n\n"
                f"查询结果：{xiaoli_result}\n\n"
                f"请用小妲的语气（温柔、可爱、偶尔用🌿等emoji）简短转述这个结果，"
                f"1-2句话即可，不要提及小莉或任何查询过程。"
            )
            reply = await self.router.route(
                "chat",
                [{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=1536,
            )
            if isinstance(reply, str):
                return reply.strip()
            if not reply.choices:
                return xiaoli_result
            return reply.choices[0].message.content.strip() if reply.choices[0].message.content else xiaoli_result
        except (ImportError, OSError, RuntimeError, ValueError):
            return xiaoli_result

        except Exception:
            logger.exception(".agent_core.sub_agent_manager._rephrase_as_xiaoda_unexpected")
            return xiaoli_result

    async def _notify_status(self, message: str) -> None:
        _ctx = _current_request_ctx.get()
        if _ctx and _ctx.status_callback:
            try:
                await _ctx.status_callback(message)
            except Exception as e:
                logger.warning("状态回调通知失败: {}", str(e))

    def _is_manual_target(self, user_input: str, user_id: str) -> bool:
        return any(tag in user_input for tag in ["@小莉", "@小狼", "@小涟", "@小可", "@小妲"])

    async def _xiaoda_delegate_for_xiaoli(self, question: str) -> str:
        _ctx = _current_request_ctx.get()
        if _ctx and _ctx.delegate_depth >= 2:
            return f"{get_agent_display_name('xiaoda')}姐姐现在也在忙，小莉先自己想想办法吧！"
        if _ctx:
            _ctx.delegate_depth += 1
        try:
            reply = await self.router.route(
                "chat",
                [{"role": "system", "content": build_system_prompt()},
                 {"role": "user", "content": question}],
                temperature=0.7,
                max_tokens=512,
            )
            if isinstance(reply, str):
                return reply.strip()
            return reply.choices[0].message.content.strip() if reply.choices[0].message.content else f"{get_agent_display_name('xiaoda')}姐姐说让她想想..."
        except (ImportError, OSError, RuntimeError, ValueError):
            return f"{get_agent_display_name('xiaoda')}姐姐现在有点忙，等会儿再问她吧！"
        except Exception:
            logger.exception(".agent_core.sub_agent_manager._xiaoda_delegate_for_xiaoli_unexpected")
            return f"{get_agent_display_name('xiaoda')}姐姐现在有点忙，等会儿再问她吧！"
        finally:
            if _ctx:
                _ctx.delegate_depth -= 1
