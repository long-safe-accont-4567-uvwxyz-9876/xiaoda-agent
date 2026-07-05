"""子代理管理 Mixin —— 拆分自原 agent_core.py 的 AgentCore 类。

包含单/并行子代理调度、通用委托、小莉委托、子代理上下文构建、
小妲转述、状态通知、手动目标判断、小莉反向委托等子代理管理相关方法。
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from loguru import logger

from config import TTS_ASYNC_MODE, build_system_prompt, get_agent_display_name
from emotion.emotion_simple import detect_emotion
from emotion.emotion_enum import CN_TO_EN
from utils.text_utils import humanize
from core.degradation_strategy import get_degradation_strategy

from agent_core._shared import ProcessResult, _current_request_ctx, RequestContext


class SubAgentManagerMixin:
    """子代理管理相关方法的 Mixin，由 AgentCore 组合使用。"""

    async def _dispatch_single_sub_agent(self, target: str, clean_input: str,
                                          user_id: str, source: str, session_id: str, trace: Any,
                                          force_voice: bool = False,
                                          ctx: RequestContext | None = None) -> ProcessResult:
        _ctx = ctx or _current_request_ctx.get()
        sub_agent = self.dispatcher.get_agent(target)
        if not sub_agent or not sub_agent.available:
            return ProcessResult(reply=f"{sub_agent.config.display_name if sub_agent else target}现在有点累了...等会儿再来吧！💤")

        display_name = sub_agent.config.display_name
        trace.info("agent.chat_target_sub", target=target, input_preview=clean_input[:50])
        context_str = self._build_sub_agent_context()
        sub_reply = await self.dispatcher.dispatch(target, clean_input, context=context_str, status_callback=_ctx.status_callback if _ctx else None, address_term=self.context.current_address_term)
        if sub_reply is None:
            sub_reply = f"{display_name}现在有点累了...等会儿再来吧！💤"

        emotion = detect_emotion(clean_input)
        if _ctx:
            _ctx.last_user_emotion = emotion.get("primary", "")
        # 子代理对话也写入主体历史：切回小妲或追问时上下文不断档
        await self.context.add_message("user", clean_input)
        await self.context.add_message("assistant", f"[{display_name}] {sub_reply}")
        self._bg_task_manager.run_background_tasks(
            clean_input, sub_reply, user_id, source, emotion, [],
            session_id=session_id,
        )

        clean_sub_reply = self._finalize_reply(sub_reply, style=target)

        # 子代理回复隐私扫描（与主 Agent 路径一致）
        is_master = self.security.is_owner(user_id) if user_id else False
        if not is_master and clean_sub_reply:
            safe, alt_reply, _ = self.security.check_output_privacy(clean_sub_reply)
            if not safe:
                logger.warning("agent.sub_agent_privacy_leak_blocked",
                               target=target, user_id=user_id,
                               reply_preview=clean_sub_reply[:100])
                clean_sub_reply = alt_reply or f"{display_name}不方便回答这个问题呢～"

        emotion_label = emotion.get("primary", "")
        sticker_path = None

        # 子 Agent 表情包：动态获取对应智能体的 sticker_manager
        sub_sticker_mgr = self.get_sticker_manager(target)
        if sub_sticker_mgr.available:
            # 1. 使用 sticker_manager 对子Agent的回复文本进行情绪检测
            detected = sub_sticker_mgr.detect_emotion(clean_sub_reply)
            # 2. 如果 sticker_manager 未检测到，使用 emotion_simple 对子Agent回复进行情绪检测
            if not detected:
                sub_reply_emotion = detect_emotion(clean_sub_reply)
                sub_reply_emotion_label = sub_reply_emotion.get("primary", "")
                if sub_reply_emotion_label:
                    detected = CN_TO_EN.get(sub_reply_emotion_label, "")
            # 3. 如果检测到情绪且 should_send() 返回 True，则 pick() 选择表情包
            if sub_sticker_mgr.should_send(clean_sub_reply, detected_emotion=detected):
                sticker_path = sub_sticker_mgr.pick(detected)

        sub_audio_path = None
        sub_tts_pending = False
        sub_tts_text = ""
        should_generate_voice = self._voice_mode or force_voice
        if should_generate_voice and len(clean_sub_reply) > 2:
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

        if sub_audio_path:
            clean_sub_reply = clean_sub_reply + "\n\n🎙️ 语音消息已发送～"

        return ProcessResult(reply=clean_sub_reply, emotion=emotion_label, sticker_path=sticker_path, audio_path=sub_audio_path, tts_pending=sub_tts_pending, tts_text=sub_tts_text)

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
                logger.warning(f"并行调度状态回调失败: {e}")

        # 构建子代理任务上下文与子任务列表
        agent_configs = self._agent_route_configs
        sub_context = self._build_sub_agent_context()
        sub_tasks: dict[str, str] = {}
        for t in targets:
            desc = agent_configs.get(t, {}).get("route_description", t)
            sub_tasks[t] = f"关于「{clean_input}」中属于{desc or t}范畴的部分，请进行专业分析和处理。"
        # A2A 共享黑板：父代理在汇总时可读取黑板中已有子代理产出，避免重复计算
        bb = getattr(self.context, "shared_blackboard", None)

        # 并行执行所有子代理任务（return_exceptions 避免单个失败影响整体）
        raw_results = await asyncio.gather(
            *[self._parallel_run_one(t, sub_tasks, sub_context, bb, clean_input) for t in targets],
            return_exceptions=True,
        )
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
        return await self._finalize_parallel_reply(
            all_replies, clean_input, user_id, source, session_id, force_voice, _ctx
        )

    async def _parallel_run_one(self, t: str, sub_tasks: dict[str, str], sub_context: str,
                                 bb: Any, clean_input: str) -> dict:
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
        task_key = self._bb_task_key(t, sub_task)
        if bb is not None:
            try:
                cached = await bb.get(task_key)
                if cached is not None:
                    logger.debug("blackboard.parallel_hit key={} agent={}", task_key, t)
                    return {"agent": t, "display_name": display_name, "reply": cached}
            except Exception as e:
                logger.debug("blackboard.get_failed key={} error={}", task_key, e)
        try:
            reply = await asyncio.wait_for(
                self.dispatcher.dispatch(t, sub_task, context=sub_context, status_callback=None, address_term=self.context.current_address_term),
                timeout=180,
            )
            if reply is None:
                # 降级回复不缓存（与 delegate_to_agent / delegate_to_xiaoli 行为一致），
                # 避免后续 10 分钟内对同一任务持续返回降级文案
                return {"agent": t, "display_name": display_name,
                        "reply": f"{display_name}现在有点累了...等会儿再来吧！💤"}
            # 20.2: 子代理完成后将结果写入共享黑板，供父代理汇总或其他流程复用
            if bb is not None and reply:
                try:
                    await bb.put(task_key, reply, agent_name=t)
                except Exception as e:
                    logger.debug("blackboard.put_failed key={} error={}", task_key, e)
            return {"agent": t, "display_name": display_name, "reply": reply}
        except asyncio.TimeoutError:
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
            return {"agent": t, "display_name": display_name,
                    "reply": f"处理出错: {e}", "error": True}

    async def _finalize_parallel_reply(self, all_replies: str, clean_input: str,
                                        user_id: str, source: str, session_id: str,
                                        force_voice: bool, _ctx: Any) -> ProcessResult:
        """并行子代理结果收尾：情绪检测、表情包选择、TTS 语音合成。

        并行结果直接使用，跳过小妲重新总结（SynthesisNode 已负责综合）。
        """
        emotion = detect_emotion(clean_input)
        if _ctx:
            _ctx.last_user_emotion = emotion.get("primary", "")
        self._bg_task_manager.run_background_tasks(
            clean_input, all_replies, user_id, source, emotion, [],
            session_id=session_id,
        )

        emotion_label = emotion.get("primary", "")
        clean_reply, sticker_path = self.get_sticker_info(
            all_replies, _ctx.last_user_emotion if _ctx else ""
        )
        # get_sticker_info 已做 strip_emotion_tag，这里补 humanize（与主小妲路径一致）
        clean_reply = humanize(clean_reply, style="xiaoda")

        audio_path = None
        tts_pending = False
        tts_text = ""
        should_generate_voice = self._voice_mode or force_voice
        if (should_generate_voice and self.tts.available and len(clean_reply) > 2
                and get_degradation_strategy().is_feature_available("tts")):
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

        if name in ("xiaoli", "xiaoli"):
            return await self.delegate_to_xiaoli(task)
        _ctx = _current_request_ctx.get()
        agent = self.dispatcher.get_agent(name)
        if not agent:
            return f"（找不到名为 {name} 的子代理）"
        # A2A 共享黑板：委托前读取已有产出，避免重复工作（黑板为 None 时跳过）
        bb = getattr(self.context, "shared_blackboard", None)
        task_key = self._bb_task_key(name, task)
        if bb is not None:
            try:
                cached = await bb.get(task_key)
                if cached is not None:
                    logger.debug("blackboard.delegate_hit key={} agent={}", task_key, name)
                    return cached
            except Exception as e:
                logger.debug("blackboard.get_failed key={} error={}", task_key, e)
        context = self._build_sub_agent_context(task_hint=task)
        import time as _time_mod
        _t0 = _time_mod.time()
        result = await asyncio.wait_for(self.dispatcher.dispatch(
            name, task, context=context,
            status_callback=_ctx.status_callback if _ctx else None, address_term=self.context.current_address_term), timeout=180)
        _duration = _time_mod.time() - _t0
        # I7: 记录子 Agent 工作履历 (供路由器智能调度)
        try:
            from core.agent_work_record import get_work_recorder
            get_work_recorder().record(
                name, task_type=mode, success=result is not None,
                duration=_duration)
        except Exception as e:
            logger.debug("sub_agent.work_record_failed", error=str(e))
        if result is None:
            return f"{agent.config.display_name}现在有点累了...等会儿再试吧💤"

        # generate_verify 模式：委托验证子代理审查结果（借鉴 Trae Code Review Step 5.5）
        if mode == "generate_verify" and verifier:
            result = await self._cross_verify(name, verifier, task, result)
        elif mode == "single":
            # 自动验证：检测输出是否包含关键操作痕迹，自动触发交叉验证
            from core.risk_classifier import OutputRiskDetector
            is_critical, suggested_verifier = OutputRiskDetector.detect(result)
            if is_critical and suggested_verifier and suggested_verifier != name:
                logger.info("agent.auto_verify_triggered generator={} verifier={}",
                            name, suggested_verifier)
                result = await self._cross_verify(name, suggested_verifier, task, result)
        # A2A 共享黑板：委托完成后写入产出，供父代理汇总或其他子代理复用
        if bb is not None:
            try:
                await bb.put(task_key, result, agent_name=name)
            except Exception as e:
                logger.debug("blackboard.put_failed key={} error={}", task_key, e)
        return result

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
        for i, agent_name in enumerate(agents):
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
        task_key = self._bb_task_key("xiaoli", task, suffix="factual" if factual else "")
        if bb is not None:
            try:
                cached = await bb.get(task_key)
                if cached is not None:
                    logger.debug("blackboard.delegate_hit key={} agent=xiaoli", task_key)
                    return cached
            except Exception as e:
                logger.debug("blackboard.get_failed key={} error={}", task_key, e)
        if factual:
            context = "这是小妲委托的查询任务。请直接返回查询结果，不要加任何个人风格、感叹号或角色扮演，只报告事实数据。"
        else:
            _xiaoda_dn = get_agent_display_name('xiaoda')
            _xiaoli_dn = get_agent_display_name('xiaoli')
            context = f"{_xiaoda_dn}委托{_xiaoli_dn}的任务。{_xiaoda_dn}温柔聪慧，{_xiaoli_dn}叫她'{_xiaoda_dn}姐姐'。{self.context.current_address_term}是{_xiaoda_dn}最亲近的人，也是{_xiaoli_dn}的大哥哥/大姐姐。"
        result = await self.dispatcher.dispatch("xiaoli", task, context=context, status_callback=_ctx.status_callback if _ctx else None, address_term=self.context.current_address_term)
        if result is None:
            return "小莉现在有点累了...等会儿再来找大哥哥玩吧！蹦蹦...💤"
        # A2A 共享黑板：委托完成后写入产出
        if bb is not None:
            try:
                await bb.put(task_key, result, agent_name="xiaoli")
            except Exception as e:
                logger.debug("blackboard.put_failed key={} error={}", task_key, e)
        return result

    @staticmethod
    def _bb_task_key(agent_name: str, task: str, suffix: str = "") -> str:
        """计算共享黑板中子代理委托结果的稳定 key。

        基于 agent_name + task 内容的 md5 摘要，保证相同任务命中缓存。
        """
        h = hashlib.md5(task.encode("utf-8")).hexdigest()[:16]
        key = f"bb:delegate:{agent_name}:{h}"
        if suffix:
            key += f":{suffix}"
        return key

    def _build_sub_agent_context(self, task_hint: str = "") -> str:
        parts = []
        recent = self.context.get_last_n(12)
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

        if self.context._compressed_summary:
            parts.append(f"[早期对话摘要]\n{self.context._compressed_summary[:300]}")

        portrait = self.context.user_portrait
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
                "chat_flash",
                [{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=1024,
            )
            if isinstance(reply, str):
                return reply.strip()
            return reply.choices[0].message.content.strip() if reply.choices[0].message.content else xiaoli_result
        except Exception:
            return xiaoli_result

    async def _notify_status(self, message: str) -> None:
        _ctx = _current_request_ctx.get()
        if _ctx and _ctx.status_callback:
            try:
                await _ctx.status_callback(message)
            except Exception as e:
                logger.warning(f"状态回调通知失败: {e}")

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
                "chat_flash",
                [{"role": "system", "content": build_system_prompt()},
                 {"role": "user", "content": question}],
                temperature=0.7,
                max_tokens=300,
            )
            if isinstance(reply, str):
                return reply.strip()
            return reply.choices[0].message.content.strip() if reply.choices[0].message.content else f"{get_agent_display_name('xiaoda')}姐姐说让她想想..."
        except Exception:
            return f"{get_agent_display_name('xiaoda')}姐姐现在有点忙，等会儿再问她吧！"
        finally:
            if _ctx:
                _ctx.delegate_depth -= 1
