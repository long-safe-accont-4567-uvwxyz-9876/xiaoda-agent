"""消息处理 Mixin —— 拆分自原 agent_core.py 的 AgentCore 类。

包含主处理流程 _process_impl 及消息分类、语音意图识别、图片描述、
聊天目标路由等消息处理相关方法。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

from loguru import logger

from config import (MIMO_MODEL, AGENT_CONFIG, build_safe_system_prompt,
                    SIMPLE_TASK_KEYWORDS, PRO_TASK_KEYWORDS, TTS_ASYNC_MODE,
                    SIMPLE_CHAT_FASTPATH, STREAM_TEXT_PUSH)
from prompt_builder import build_scene_aware_prompt
from core.chat_processor import ChatProcessor
from core.circuit_breaker import CircuitState
from core.background_tasks import _spawn
from emotion.emotion_simple import detect_emotion, build_emotion_hint
from emotion.emotion_enum import CN_TO_EN, is_unified, ensure_emotion_tag
from task_orchestrator import run_task_graph
from tool_engine.tool_registry import to_openai_tools
from utils.text_utils import (has_dsml_tool_calls, parse_dsml_tool_calls,
                              humanize, encode_image_to_base64)

from agent_core.core import DEGRADED_REPLY, ProcessResult

if TYPE_CHECKING:
    from agent_core.core import RequestContext


class MessageProcessorMixin:
    """消息处理相关方法的 Mixin，由 AgentCore 组合使用。"""

    async def _process_impl(self, ctx: RequestContext, user_input: str, user_id: str,
                             source: str, user_openid: str, session_id: str,
                             status_callback, image_data: list[dict] | None,
                             is_master: bool = True) -> ProcessResult:
        if self._tool_call_handler:
            self._tool_call_handler._tool_repair.clear_storm_window()

        _trace_id = f"{int(time.time()*1000)%1000000:06d}"
        trace = logger.bind(trace_id=_trace_id)
        trace.info("agent.process.start", source=source, user_id=user_id,
                    msg_preview=user_input[:80])

        # 尽早发送"收到，正在想..."提示，让用户 <1 秒看到响应
        # （restore_from_db 读外置硬盘数据库需要 1-2 秒，提前发送避免用户等待）
        if status_callback:
            try:
                await status_callback("收到，正在想...")
            except Exception:
                pass

        allowed, reason = self.security.is_allowed(user_id)
        if not allowed:
            trace.warning("agent.blocked", reason=reason)
            return ProcessResult(reply="")

        # 群聊 session 按用户隔离：不同用户使用不同 session_id
        if source == "qq_group" and user_openid:
            session_id = f"qq_group:{user_openid}"

        # 按当前用户重新恢复历史摘要（群聊场景下不同用户历史不混合）
        # 使用 user_openid 优先（QQ 群聊稳定标识），其次 user_id
        _restore_id = user_openid or user_id
        # 群聊多用户上下文隔离：切换到当前用户的 history 和压缩摘要
        # 避免不同用户共享单例 context 导致串话和隐私泄露
        if _restore_id:
            try:
                await self.context.switch_user_context(_restore_id)
            except Exception as e:
                logger.warning("agent.switch_user_context_failed", error=str(e))
        if _restore_id and self.db:
            try:
                await self.context.restore_from_db(self.db, user_id=_restore_id,
                                                    address_term=self.context.current_address_term)
            except Exception as e:
                logger.warning("agent.restore_failed", error=str(e))

        if self.slash_handler and self.slash_handler.is_slash_command(user_input):
            slash_reply = await self.slash_handler.handle(user_input, user_id)
            return ProcessResult(reply=slash_reply)

        chat_targets = await self._parse_chat_target(user_input, user_id)
        clean_input = ChatProcessor.clean_mention_from_input(user_input)

        voice_intent = self._detect_voice_intent(clean_input)
        force_voice = voice_intent and not self._voice_mode

        if not clean_input:
            target_name = "可莉" if chat_targets and chat_targets[0] == "keli" else "纳西妲"
            confirm_msg = f"好～现在跟{target_name}说话啦！有什么想聊的呀？"
            trace.info("agent.chat_target_switch", target=chat_targets)
            return ProcessResult(reply=confirm_msg, emotion="greeting")

        non_nahida_targets = [t for t in chat_targets if t != "nahida"]
        if non_nahida_targets:
            if len(non_nahida_targets) == 1:
                return await self._dispatch_single_sub_agent(
                    non_nahida_targets[0], clean_input, user_id, source, session_id, trace,
                    force_voice=force_voice, ctx=ctx,
                )
            else:
                return await self._dispatch_parallel_sub_agents(
                    non_nahida_targets, clean_input, user_id, source, session_id, trace,
                    force_voice=force_voice, ctx=ctx,
                )

        # Task 9: 简单对话快速路径（方案 E）—— 跳过记忆检索，使用最小上下文
        if SIMPLE_CHAT_FASTPATH and self._is_simple_chat(clean_input) \
                and not image_data and not ("[图片:" in user_input and "已保存到" in user_input):
            trace.info("chat.fast_path", input_preview=clean_input[:50])

            emotion = detect_emotion(user_input)
            emotion_hint = build_emotion_hint(emotion)
            self.context.emotion_hint = emotion_hint
            ctx.last_user_emotion = emotion.get("primary", "")
            emotion_label = emotion.get("primary", "")

            # 构建最小上下文：系统提示 + 最近 3 轮历史 + 用户输入
            # 非主人使用安全化 prompt（剥离所有隐私信息），主人使用完整 prompt
            if is_master:
                # 场景感知动态排序：根据用户输入调整 MD 模块顺序，
                # 让最相关的模块靠近用户输入（LLM 注意力最强的位置）
                system_prompt = build_scene_aware_prompt(user_input, self.context.current_address_term)
            else:
                system_prompt = build_safe_system_prompt()
            messages = [{"role": "system", "content": system_prompt}]

            # Context 层注入（动态提示：用户画像 + 学习规则，已缓存 ~0ms）
            # 这是"味道"的来源 —— 没有这层，回复会变成无个性的模板
            if is_master:
                _dynamic = self.context._build_dynamic_prompt()
                if _dynamic:
                    messages.append({"role": "system", "content": _dynamic})

            # Volatile 层：时间感知 + 情绪（延迟注入到用户输入前，确保 LLM 注意力最强）
            _volatile = self.context._build_time_context()
            if emotion_hint:
                _addr = self.context.current_address_term if is_master else "你"
                _volatile += f"\n[感知到{_addr}的情绪：{emotion_hint}]"

            # 轻量 FTS 记忆检索（毫秒级，让 fast_path 也能"记住"之前的内容）
            # 只检索 2 条摘要，避免 prompt 过长拖慢 LLM
            # 问候类跳过：FTS 会检索到含时间词的旧记忆（如"今天晚上过得开心吗"），
            # 污染 LLM 的时间感知，让它 echo 用户的时间词
            _is_greeting = bool(re.match(
                r'^(早[上安]?|中午|下午|晚上|晚安|你好|哈喽|hi|hello|hey)[好呀～~！!。.\s]*$',
                clean_input.strip(), re.IGNORECASE))

            if is_master and not _is_greeting and self.memory and getattr(self.memory, 'memory', None):
                try:
                    _fts_mems = await self.memory.memory.search_memories_fts(user_input, limit=2)
                    if _fts_mems:
                        # 注入时间戳，让 LLM 知道每条记忆发生的时间
                        _mem_lines = []
                        for m in _fts_mems:
                            _s = m.get("summary", "")
                            if not _s:
                                continue
                            # 跳过问候类记忆，避免旧问候污染时间感知
                            # （记忆里的"爸爸晚上好呀～"会让 LLM 模仿，即使当前是中午）
                            if re.search(r'(晚上好|早上好|中午好|下午好|晚安|早安)', _s):
                                continue
                            _ts = m.get("timestamp", 0)
                            if _ts:
                                try:
                                    _d = time.strftime("%m-%d %H:%M", time.localtime(float(_ts)))
                                    _mem_lines.append(f"[{_d}] {_s[:120]}")
                                except (ValueError, TypeError, OSError):
                                    _mem_lines.append(_s[:120])
                            else:
                                _mem_lines.append(_s[:120])
                        if _mem_lines:
                            messages.append({
                                "role": "system",
                                "content": "[相关长期记忆]\n" + "\n---\n".join(_mem_lines)
                            })
                except Exception as e:
                    logger.debug(f"fast_path.fts_failed: {e}")
            # 主动检索 C：情绪触发（轻量版，仅强负面情绪时检索安抚记忆）
            if is_master and self.memory and emotion.get("valence") == "negative" \
                    and float(emotion.get("intensity", 0.0)) >= 0.5:
                try:
                    _comfort = await self.memory.retrieve_comfort_memories(limit=1)
                    if _comfort:
                        _c_lines = []
                        for m in _comfort:
                            _s = m.get("summary", "")
                            if _s:
                                _c_lines.append(_s[:120])
                        if _c_lines:
                            _addr = self.context.current_address_term or "你"
                            messages.append({
                                "role": "system",
                                "content": f"[曾经让{_addr}开心的回忆（温柔陪伴时可以提起）]\n" + "\n".join(_c_lines)
                            })
                except Exception as e:
                    logger.debug(f"fast_path.comfort_failed: {e}")
            # 非主人不加载历史对话（防止看到主人的聊天内容）
            if is_master:
                for msg in self.context.get_last_n(6):
                    m = {"role": msg["role"], "content": str(msg.get("content", "")) if msg.get("content") is not None else ""}
                    messages.append(m)
            # Volatile 层：时间感知紧贴用户输入注入，LLM 对最近内容注意力最强
            messages.append({"role": "system", "content": _volatile})
            messages.append({"role": "user", "content": user_input})

            _model_cfg = AGENT_CONFIG.get("model", {})
            reply = ""
            try:
                # Task 7: 流式状态推送 —— 快速路径 LLM 调用前通知
                await self._notify_status("正在思考回复...")
                result = await self.router.route(
                    "chat", messages,
                    temperature=_model_cfg.get("temperature", 0.7),
                    user_openid=user_openid, session_id=session_id,
                )
                if isinstance(result, str):
                    reply = self._clean_reply(result)
                else:
                    reply = self._clean_reply(result.choices[0].message.content or "")
            except Exception as e:
                logger.warning("agent.fast_path_failed", error=str(e))
                reply = DEGRADED_REPLY

            # 非主人输出侧隐私扫描
            if not is_master and reply:
                safe, alt_reply, _ = self.security.check_output_privacy(reply)
                if not safe:
                    logger.warning("agent.privacy_leak_blocked", user_id=user_id, reply_preview=reply[:100])
                    reply = alt_reply

            await self.context.add_message("user", user_input)
            await self.context.add_message("assistant", reply)

            self._bg_task_manager.run_background_tasks(
                user_input, reply, user_id, source, emotion, [],
                session_id=session_id,
            )

            try:
                _spawn(self.router.flush_costs())
            except Exception as e:
                logger.error(f"费用统计刷新失败: {e}")

            # 确保情绪标签存在且合法
            if is_unified():
                reply, ensured_emotion = ensure_emotion_tag(reply)
                if ensured_emotion.value != emotion_label:
                    emotion_label = ensured_emotion.value

            clean_reply, sticker_path = self.get_sticker_info(reply, ctx.last_user_emotion)
            clean_reply = humanize(clean_reply, style="nahida")

            audio_path = None
            tts_pending = False
            tts_text = ""
            should_generate_voice = self._voice_mode or force_voice
            if should_generate_voice and self.tts.available and len(clean_reply) > 2:
                if TTS_ASYNC_MODE:
                    tts_pending = True
                    tts_text = self._clean_reply(clean_reply)
                else:
                    try:
                        audio_path = await self.tts.synthesize_nahida(self._clean_reply(clean_reply), emotion=emotion_label)
                    except Exception as e:
                        logger.warning("agent.tts_failed", error=str(e))

            if audio_path:
                clean_reply = clean_reply + "\n\n🎙️ 语音消息已发送～"

            _spawn(self._hook_engine.fire_post_response())
            trace.info("agent.fast_path.done", reply_preview=clean_reply[:100])
            return ProcessResult(reply=clean_reply, emotion=emotion_label, sticker_path=sticker_path,
                                 audio_path=audio_path, tts_pending=tts_pending, tts_text=tts_text)

        if "nahida" in chat_targets and self._task_graph and not self._is_manual_target(user_input, user_id) and not self._is_simple_task(clean_input) and not force_voice and not image_data and not ("[图片:" in user_input and "已保存到" in user_input):
            try:
                graph_result = await run_task_graph(
                    graph=self._task_graph,
                    user_input=clean_input,
                    user_id=user_id,
                    session_id=session_id,
                    status_callback=status_callback,
                    agent_configs=self._agent_route_configs,
                    dispatcher=self.dispatcher,
                )
                if graph_result.final_output:
                    emotion = detect_emotion(clean_input)
                    ctx.last_user_emotion = emotion.get("primary", "")
                    # 关键：写入对话历史，否则下一轮上下文丢失（"葡萄牙呢"类追问失忆 bug）
                    await self.context.add_message("user", clean_input)
                    await self.context.add_message("assistant", graph_result.final_output)
                    self._bg_task_manager.run_background_tasks(
                        clean_input, graph_result.final_output, user_id, source, emotion, [],
                        session_id=session_id,
                    )
                    emotion_label = emotion.get("primary", "")
                    clean_reply = self._finalize_reply(graph_result.final_output, style="nahida")
                    sticker_path = None
                    audio_path = None
                    tts_pending = False
                    tts_text = ""
                    should_generate_voice = self._voice_mode or force_voice
                    if should_generate_voice and len(clean_reply) > 2:
                        if TTS_ASYNC_MODE:
                            # Task 6: 异步 TTS
                            tts_pending = True
                            tts_text = self._clean_reply(clean_reply)
                        else:
                            try:
                                target_agent = self.dispatcher.get_agent(graph_result.route_target)
                                if target_agent:
                                    audio_path = await target_agent.synthesize(self._clean_reply(clean_reply), emotion=emotion_label)
                            except Exception as e:
                                logger.warning("agent.routed_tts_failed", error=str(e))
                    if audio_path:
                        clean_reply = clean_reply + "\n\n🎙️ 语音消息已发送～"
                    return ProcessResult(reply=clean_reply, emotion=emotion_label, sticker_path=sticker_path, audio_path=audio_path, tts_pending=tts_pending, tts_text=tts_text)
            except Exception as e:
                logger.warning("agent.task_graph_failed", error=str(e))
                return ProcessResult(reply=DEGRADED_REPLY)

        if "可莉" in user_input and "nahida" in chat_targets:
            klee_reply = await self.delegate_to_klee(clean_input, factual=True)
            self.context.klee_context = klee_reply
        else:
            self.context.klee_context = None

        emotion = detect_emotion(user_input)
        emotion_hint = build_emotion_hint(emotion)
        self.context.emotion_hint = emotion_hint
        ctx.last_user_emotion = emotion.get("primary", "")

        # 记忆检索与 notebook 上下文加载并行化（asyncio.gather）
        # Task 7: 流式状态推送 —— 记忆检索前通知
        await self._notify_status("正在回忆相关记忆...")

        async def _retrieve_memories():
            if self.memory and is_master:
                self.memory.signal_new_message()
                try:
                    results = await self.memory.retrieve_memories(user_input, k=3)
                except Exception as e:
                    logger.warning("memory.retrieve_failed", error=str(e))
                    results = None

                # 主动检索 C：情绪触发
                # 检测到用户负面情绪（valence=negative）且强度超阈值时，
                # 并行检索"安抚性记忆"（带正面情绪标签的历史记忆），
                # 合并到主检索结果中，让纳西妲能回忆起"曾经让用户开心的事"来温柔陪伴。
                if results is not None:
                    try:
                        import config as _cfg
                        _emo_threshold = float(getattr(_cfg, "EMOTION_TRIGGER_THRESHOLD", 0.5))
                    except Exception:
                        _emo_threshold = 0.5

                    if (emotion.get("valence") == "negative"
                            and float(emotion.get("intensity", 0.0)) >= _emo_threshold):
                        try:
                            comfort = await self.memory.retrieve_comfort_memories(limit=2)
                            if comfort:
                                _existing_ids = {str(r.get("id", "")) for r in results}
                                for _r in comfort:
                                    _rid = str(_r.get("id", ""))
                                    if _rid and _rid not in _existing_ids:
                                        _existing_ids.add(_rid)
                                        results.append(_r)
                                logger.debug("memory.emotion_trigger_activated",
                                             valence=emotion.get("valence"),
                                             intensity=emotion.get("intensity"),
                                             comfort_added=len(comfort))
                        except Exception as e:
                            logger.debug("memory.emotion_trigger_failed", error=str(e))
                    return results
                return None
            return None

        async def _load_notebook():
            try:
                await self._load_notebook_context()
            except Exception as e:
                logger.warning("notebook.load_failed", error=str(e))

        memories, _ = await asyncio.gather(_retrieve_memories(), _load_notebook())
        self.context.memory_retrieval = memories if memories else None

        effective_input = user_input
        # 非主人使用安全化 prompt 构建消息（剥离隐私、不加载历史）
        if not is_master:
            safe_prompt = build_safe_system_prompt()
            messages = [{"role": "system", "content": safe_prompt}]
            messages.append({"role": "user", "content": effective_input})
        else:
            messages = self.context.build_messages(effective_input)

        if image_data:
            logger.info("agent.vision_start", image_count=len(image_data), total_b64_size=sum(len(img.get('data', '')) for img in image_data))
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

        _sticker_keywords = ["表情包", "表情", "贴纸", "sticker", "贴图"]
        _sticker_intent = any(kw in clean_input for kw in _sticker_keywords)
        _pre_picked_sticker = None
        if _sticker_intent and self.sticker_manager.available:
            _detected_e = self.sticker_manager.detect_emotion(clean_input)
            if not _detected_e:
                _detected_e = CN_TO_EN.get(emotion.get("primary", ""), "happy")
            _pre_picked_sticker = self.sticker_manager.pick(_detected_e)
            if _pre_picked_sticker:
                _sticker_desc = _pre_picked_sticker.stem.replace("_", " ").replace("-", " ")
                _sticker_cat = _pre_picked_sticker.parent.name
                messages.append({
                    "role": "system",
                    "content": f"[系统提示] 你正在给用户发送一张表情包图片。图片描述：「{_sticker_desc}」，分类：「{_sticker_cat}」。请在回复中自然地提到这张表情包的内容，让用户感受到你真的知道发了什么图。不要说'这是一张图片'之类的机械描述，要用你的风格自然表达。"
                })

        tools = to_openai_tools() if to_openai_tools() else None

        has_image = image_data or ("[图片:" in user_input and "已保存到" in user_input)
        if has_image and tools:
            tools = None

        # 简单任务（问候/闲聊）时过滤掉系统级工具，避免模型自作主张查硬件等
        # 但保留天气、搜索等用户可能期望模型主动使用的工具
        tools = ChatProcessor.filter_tools_for_simple_task(tools, clean_input, self._is_simple_task)

        # 表情包意图时禁用所有工具：sticker 由系统自动附带，模型只需回复文字
        if _sticker_intent and tools:
            tools = None

        # 非主人消息：过滤敏感工具，仅保留聊天能力
        if not is_master and tools:
            _SAFE_TOOLS_FOR_STRANGER = set()  # 外人不可使用任何工具，纯聊天
            tools = None
            logger.info("agent.tools_filtered_for_non_master", user_id=user_id)

        should_escalate, reason = self._should_escalate_to_pro(user_input, tools)
        base_task = "chat_pro" if should_escalate else "chat"
        task_type = self.router.resolve_task_type(base_task)

        if should_escalate:
            trace.info("chat.escalated_to_pro", reason=reason)

        reply = ""
        tool_results = []

        _model_cfg = AGENT_CONFIG.get("model", {})
        is_owner = self.security.is_owner(user_id)

        # 熔断器检查
        circuit_state = self._circuit_breaker.check(self._cognitive_state)
        if circuit_state == CircuitState.RED:
            logger.warning("agent.circuit_breaker_red")
            return ProcessResult(reply="系统需要休息一下，请稍后再试吧～")
        elif circuit_state == CircuitState.HALF_OPEN:
            logger.info("agent.circuit_breaker_half_open_probe")

        # YELLOW 状态处理：注入警告并降低 max_tokens 20%
        _cb_max_tokens = None
        if circuit_state == CircuitState.YELLOW:
            messages.append({
                "role": "system",
                "content": "[系统警告] 当前认知状态不佳，请简化回复。"
            })
            _cb_max_tokens = int(_model_cfg.get("max_tokens", 1500) * 0.8)

        try:
            # Task 7: 流式状态推送 —— 主 LLM 调用前通知
            await self._notify_status("正在思考回复...")
            if STREAM_TEXT_PUSH and status_callback and not tools:
                # P0: 流式文本推送 —— 仅在无工具调用时启用，内部失败自动降级到同步
                result = await self._stream_llm_response(
                    messages, status_callback=status_callback, task_type=task_type,
                    temperature=_model_cfg.get("temperature", 0.7),
                    max_tokens=_cb_max_tokens,
                    user_openid=user_openid, session_id=session_id,
                )
            else:
                result = await self.router.route(
                    task_type,
                    messages,
                    temperature=_model_cfg.get("temperature", 0.7),
                    max_tokens=_cb_max_tokens,
                    tools=tools,
                    tool_choice="auto" if tools else None,
                    user_openid=user_openid,
                    session_id=session_id,
                )

            if isinstance(result, str):
                if has_dsml_tool_calls(result) and tools:
                    dsml_calls = parse_dsml_tool_calls(result, self.tool_repair._allowed_tools)
                    if dsml_calls:
                        logger.info("agent.dsml_in_content", count=len(dsml_calls))
                        for dc in dsml_calls:
                            fn = dc.get("function", {})
                            logger.info("agent.dsml_tool_call", tool=fn.get("name",""), args=str(fn.get("arguments",""))[:200])
                        dsml_reasoning = self.router.pop_reasoning_content()
                        reply, tool_results = await self._handle_tool_calls(
                            dsml_calls, messages, trace,
                            assistant_content=result,
                            reasoning_content=dsml_reasoning,
                            user_openid=user_openid, session_id=session_id,
                            safe_mode=not is_owner, ctx=ctx,
                        )
                        ctx.handled_by_tool_call = True
                        logger.info("agent.got_dsml_tool_reply", length=len(reply), preview=reply[:80])
                    else:
                        reply = self._clean_reply(result)
                        logger.info("agent.got_string_reply", length=len(reply), preview=reply[:80])
                else:
                    # 即使 tools 为 None（如 has_image=True），也通过 _clean_reply -> strip_dsml 清理非标准 [TOOL_CALL] 格式文本
                    reply = self._clean_reply(result)
                    logger.info("agent.got_string_reply", length=len(reply), preview=reply[:80])
            else:
                msg = result.choices[0].message
                if msg.tool_calls:
                    tc_list = [
                        {"id": str(tc.id), "type": "function", "function": {"name": tc.function.name, "arguments": str(tc.function.arguments) if tc.function.arguments else "{}"}}
                        for tc in msg.tool_calls
                    ]
                    reasoning = getattr(msg, "reasoning_content", None)
                    self.router.pop_reasoning_content()  # clear stale value since we extract directly
                    reply, tool_results = await self._handle_tool_calls(
                        tc_list, messages, trace,
                        assistant_content=msg.content or "",
                        reasoning_content=reasoning,
                        user_openid=user_openid, session_id=session_id,
                        safe_mode=not is_owner, ctx=ctx,
                    )
                    ctx.handled_by_tool_call = True
                    logger.info("agent.got_tool_reply", length=len(reply), preview=reply[:80])
                else:
                    reply = self._clean_reply(msg.content or "")
                    logger.info("agent.got_string_reply", length=len(reply), preview=reply[:80])
            # LLM 调用成功后更新认知状态
            if circuit_state == CircuitState.HALF_OPEN:
                self._circuit_breaker.on_half_open_success(self._cognitive_state)
            else:
                self._circuit_breaker.on_success(self._cognitive_state)
        except Exception as e:
            trace.error("agent.model_error", error=str(e))
            # LLM 调用失败后更新认知状态
            if circuit_state == CircuitState.HALF_OPEN:
                self._circuit_breaker.on_half_open_failure(self._cognitive_state)
            else:
                self._circuit_breaker.on_failure(self._cognitive_state)
            if self._error_handler:
                try:
                    error_reply = await self._error_handler.handle_error_with_intelligence(
                        error=e, user_query=user_input, context="主处理流程模型调用错误"
                    )
                    if error_reply and len(error_reply) > 50:
                        reply = error_reply
                    else:
                        reply = DEGRADED_REPLY
                except Exception as e:
                    logger.debug(f"agent.error_handler_fallback: {e}")
                    reply = DEGRADED_REPLY
            else:
                try:
                    result = await self.router.route(
                        "chat_flash", messages, temperature=0.7,
                        user_openid=user_openid, session_id=session_id,
                    )
                    reply = self._clean_reply(result) if isinstance(result, str) else DEGRADED_REPLY
                except Exception as e:
                    logger.debug(f"agent.flash_fallback: {e}")
                    reply = DEGRADED_REPLY

        # 从工具结果中提取媒体路径，并清理回复中的冗余路径描述
        media_image_paths, media_video_path, reply = await self._extract_media_from_tool_results(tool_results, reply)

        # 非主人输出侧隐私扫描（主处理路径）
        if not is_master and reply:
            safe, alt_reply, _ = self.security.check_output_privacy(reply)
            if not safe:
                logger.warning("agent.privacy_leak_blocked", user_id=user_id, reply_preview=reply[:100])
                reply = alt_reply

        if not ctx.handled_by_tool_call:
            await self.context.add_message("user", user_input)
            rc = self.router.pop_reasoning_content()
            await self.context.add_message("assistant", reply, reasoning_content=rc)

        self._bg_task_manager.run_background_tasks(
            user_input, reply, user_id, source, emotion, tool_results,
            session_id=session_id,
        )

        try:
            _spawn(self.router.flush_costs())
        except Exception as e:
            logger.error(f"费用统计刷新失败，可能丢失费用数据: {e}")

        trace.info("agent.process.done", reply_preview=reply[:100])

        emotion_label = emotion.get("primary", "")

        # 确保情绪标签存在且合法（LLM 可能漏标或标错）
        if is_unified():
            reply, ensured_emotion = ensure_emotion_tag(reply)
            if ensured_emotion.value != emotion_label:
                emotion_label = ensured_emotion.value

        if _pre_picked_sticker:
            clean_reply = self._finalize_reply(reply, strip_emotion=True, style="nahida")
            sticker_path = _pre_picked_sticker
        else:
            clean_reply, sticker_path = self.get_sticker_info(reply, ctx.last_user_emotion)
            # get_sticker_info 已做 strip_emotion_tag，这里补 humanize
            clean_reply = humanize(clean_reply, style="nahida")

        audio_path = None
        tts_pending = False
        tts_text = ""
        should_generate_voice = self._voice_mode or force_voice
        if should_generate_voice and self.tts.available and len(clean_reply) > 2:
            if TTS_ASYNC_MODE:
                # Task 6: 异步 TTS —— 跳过同步合成，标记 pending 供调用方后台处理
                tts_pending = True
                tts_text = self._clean_reply(clean_reply)
            else:
                try:
                    audio_path = await self.tts.synthesize_nahida(self._clean_reply(clean_reply), emotion=emotion_label)
                except Exception as e:
                    logger.warning("agent.tts_failed", error=str(e))

        if audio_path:
            clean_reply = clean_reply + "\n\n🎙️ 语音消息已发送～"

        # PostResponse 钩子（批量后处理）
        _spawn(self._hook_engine.fire_post_response())

        return ProcessResult(reply=clean_reply, emotion=emotion_label, sticker_path=sticker_path, audio_path=audio_path, tool_results=tool_results, image_paths=media_image_paths, video_path=media_video_path, tts_pending=tts_pending, tts_text=tts_text)

    async def _stream_llm_response(self, messages: list, status_callback=None,
                                    task_type: str = "chat", **kwargs) -> str:
        """流式调用 LLM，逐 token 推送给前端。

        当 STREAM_TEXT_PUSH=true 时使用此方法。
        失败时降级到原有同步调用。
        """
        if not STREAM_TEXT_PUSH:
            return await self.router.route(task_type, messages, **kwargs)

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
            # 降级到同步调用（丢弃已积累的部分流式内容）
            return await self.router.route(task_type, messages, **kwargs)
        return "".join(full_response)

    def _should_escalate_to_pro(self, user_msg: str, tools: list | None) -> tuple[bool, str]:
        tool_keywords = PRO_TASK_KEYWORDS["tool"]
        if tools and any(kw in user_msg for kw in tool_keywords):
            return True, "tool_likely_query"

        negative = PRO_TASK_KEYWORDS["negative"]
        if any(kw in user_msg for kw in negative) and len(user_msg) > 30:
            return True, "deep_emotional_content"

        if len(user_msg) > 300:
            return True, "long_complex_message"

        return False, ""

    def _is_simple_task(self, user_input: str) -> bool:
        # 否定指令（告诉助手不要做某事）优先判断，属于简单对话，不应路由到子Agent
        negative_patterns = [
            r"(?:不|别|不要|不用|不需要|没必要)\s*(?:要|用|调用|查|检查|执行|运行|搜索|搜|找|看)",
            r"(?:不需要|不用|别)\s*(?:调用|使用)\s*(?:这个|那个|任何)?\s*(?:工具|功能)",
        ]
        if any(re.search(pat, user_input) for pat in negative_patterns):
            return True

        complex_keywords = SIMPLE_TASK_KEYWORDS["complex"]
        if any(kw in user_input for kw in complex_keywords):
            return False

        # 对话性消息关键词 — 这些是日常聊天，不是复杂任务
        chat_keywords = SIMPLE_TASK_KEYWORDS["chat"]
        if any(kw in user_input for kw in chat_keywords):
            return True

        cn_chars = sum(1 for c in user_input if '\u4e00' <= c <= '\u9fff')
        effective_len = cn_chars * 2 + len(user_input) - cn_chars
        if effective_len <= 20:
            return True
        simple_tool_patterns = ["天气", "气温", "时间", "几点", "日期", "星期", "翻译"]
        if effective_len <= 25 and any(kw in user_input for kw in simple_tool_patterns):
            return True
        return False

    def _is_simple_chat(self, query: str) -> bool:
        """Task 9: 判断是否为简单闲聊，可走快速路径（跳过记忆检索）。

        判定规则（满足任一即返回 True）：
        1. 命中 SIMPLE_TASK_KEYWORDS["chat"] 中的闲聊关键词
        2. 有效长度（中文×2 + 其他×1）≤ 10
        但若命中 complex 关键词则返回 False，避免误判。
        """
        # 复杂任务关键词优先排除
        complex_keywords = SIMPLE_TASK_KEYWORDS["complex"]
        if any(kw in query for kw in complex_keywords):
            return False
        # 闲聊关键词命中
        chat_keywords = SIMPLE_TASK_KEYWORDS["chat"]
        if any(kw in query for kw in chat_keywords):
            return True
        # 有效长度 ≤ 10 视为简单闲聊
        cn_chars = sum(1 for c in query if '\u4e00' <= c <= '\u9fff')
        effective_len = cn_chars * 2 + len(query) - cn_chars
        if effective_len <= 10:
            return True
        return False

    def _detect_voice_intent(self, user_input: str) -> bool:
        voice_keywords = [
            "语音", "声音", "说话", "朗读", "念给我", "读给我",
            "用声音", "听你", "听听你", "发语音", "生成语音",
            "语音回复", "语音消息", "说给我听", "念出来",
            "tts", "voice",
        ]
        q = user_input.lower()
        return any(kw in q for kw in voice_keywords)

    async def _describe_images(self, image_data: list[dict]) -> str:
        """使用 MiMo Vision API 识别图片内容"""
        try:
            if not self.router or not self.router._client:
                logger.warning("agent.vision_no_client")
                return ""

            vision_parts = [{"type": "text", "text": "请详细描述这张图片的内容。如果有文字，请完整转录。如果是题目，请给出题目内容。"}]
            for i, img in enumerate(image_data):
                b64_data = img.get('data', '')
                mime = img.get('mimeType', 'image/jpeg')
                logger.info("agent.vision_image", index=i, mime=mime, b64_len=len(b64_data))
                if not b64_data:
                    logger.warning("agent.vision_empty_data", index=i)
                    continue
                vision_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{b64_data}"
                    }
                })

            if len(vision_parts) <= 1:
                logger.warning("agent.vision_no_valid_images")
                return ""

            response = await self.router._client.chat.completions.create(
                model=MIMO_MODEL,
                messages=[{"role": "user", "content": vision_parts}],
                max_tokens=800,
            )
            description = response.choices[0].message.content.strip()
            logger.info("agent.image_described", length=len(description))
            return description
        except Exception as e:
            logger.warning("agent.image_describe_failed", error=str(e))
            return ""

    async def _nahida_synthesis_chat(self, prompt: str) -> str:
        try:
            result = await self.router.route(
                "chat",
                [
                    {"role": "system", "content": """你是纳西妲，须弥的草神。你的任务是整理团队成员的工作结果，向用户汇报。

重要规则：
1. 必须输出具体的事实信息和关键要点，不要只说空洞的比喻或感想
2. 如果搜索到了新闻/资料，必须列出具体的标题、摘要和关键数据
3. 如果是代码/技术结果，列出核心代码和结论
4. 用简洁清晰的语言组织，可以带一点你的风格但内容必须充实
5. 不要编造信息，只基于提供的内容整理
6. 格式：先一句话总结，然后分点列出具体信息"""},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2048,
                temperature=0.5,
            )
            if isinstance(result, str):
                return result.strip()
            return result.choices[0].message.content.strip()
        except Exception as e:
            logger.warning("agent.nahida_synthesis_failed", error=str(e))
            return prompt

    async def _parse_chat_target(self, user_input: str, user_id: str) -> list[str]:
        decision = self._router_engine.decide(user_input, user_id)
        if decision.agent_names:
            async with self._chat_target_lock:
                self._user_chat_target[user_id] = decision.agent_names[-1]
        logger.debug("router.decision", agents=decision.agent_names,
                     mode=decision.mode, reason=decision.reasoning)
        return decision.agent_names

    async def get_chat_target(self, user_id: str) -> str:
        async with self._chat_target_lock:
            return self._user_chat_target.get(user_id, "nahida")

    async def set_chat_target(self, user_id: str, target: str):
        async with self._chat_target_lock:
            self._user_chat_target[user_id] = target
