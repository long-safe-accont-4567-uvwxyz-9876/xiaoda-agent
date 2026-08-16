"""ModelRouter 的流式执行链 Mixin — 自 model_router.py 拆分（Phase 6）。

内容：route / route_config 之下的调用执行链——chat_stream（流式主循环 +
stall timeout + 截断 ContextVar + usage 记录 + fallback 包装）、
_stream_local_chat（local-ort 流式）、_classify_error、_build_route_kwargs、
_create_completion（客户端选择 → 构造 kwargs → 信号量创建的统一核心）、
_handle_route_response（费用/缓存/凭证/reasoning 处理 + 截断续写）、
_handle_route_exception、_route_with_retry、_route_for_continuation，
以及被 _build_route_kwargs 唯一调用的 _cap_max_tokens（max_tokens 上限裁剪）。
方法体自 model_router.py 逐字节搬移，仅缩进调整（对齐 Phase 3/4/5 先例）。

route / route_config 取舍：留在 ModelRouter 本体。
  - route 是主入口（主 chat 优先/后台让路/指标/降级编排），对搬移块仅单向
    依赖（self._route_with_retry / self._try_fallback_chain，MRO 命中）；
  - 搬移块不回调 route 的新路径（_route_for_continuation 去递归）；仅
    _handle_route_response 的旧兼容路径（TRUNCATION_RETRY_DERECURSE=false）
    经 self.route(...) 回调——同样走 MRO，无反向 import 依赖。

依赖说明：
  - _reasoning_content_var 自 llm_gateway.router_metrics 引入（同一 ContextVar
    对象，CostTrackingMixin.pop_reasoning_content 与 _handle_route_response 共享）；
  - MAX_RETRIES（重试次数常量）随执行链一并搬入本模块；model_router 顶部
    同名 re-export 保持 `from model_router import MAX_RETRIES` 与
    patch("model_router.MAX_RETRIES") 的既有用法可解析（现有测试仅 patch 为
    相同默认值 1，无行为差异；若未来需运行时可变，应仿 fallback_chain 先例
    改为方法内 lazy import，而非依赖模块级 patch 透传）；
  - _cap_max_tokens 随链搬入本 Mixin（web/routers/models.py 经
    ModelRouter._cap_max_tokens 调用，MRO 命中同一实现）。原
    `ModelRouter._cap_max_tokens` 裸引用在 _build_route_kwargs 内改为
    `ExecutionMixin._cap_max_tokens`——这是全部搬移中唯一一行函数体偏差
    （mixin 不得 import model_router，原引用经 MRO 与现引用为同一对象）。

兼容契约（tests/test_execution_mixin.py）：
    - 本模块不得 import model_router（防循环依赖）
    - ModelRouter(ExecutionMixin) 继承保持 self 语义；chat_stream 经
      web/ws_hub、agent_core、chaos 包装等实例调用，_route_with_retry 经
      fallback_chain / route / route_config 的 self.* 调用，均走 MRO 命中本 Mixin
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import openai as _openai_mod  # 用于 openai.APIError 异常捕获
from loguru import logger

from config import DEFAULT_PROVIDER as _CFG_DEFAULT_PROVIDER
from core.app_exception import LLMError
from core.error_codes import ErrorCodeEnum
from llm_gateway.router_metrics import _reasoning_content_var
from llm_gateway.transports import CompletionRequest, TransportError
from model_router_config import (
    _LOCAL_ORT_PROVIDER,
    PROVIDER_MAX_TOKENS_CAP,
    translate_model_for_provider,
)
from utils.common import DEFAULT_MAX_TOKENS
from utils.error_classifier import RecoveryAction
from utils.llm_cleanup import merge_continuation
from utils.metrics import metrics

# 重试次数常量：随执行链一并搬入（model_router 顶部同名 re-export，见模块 docstring）
MAX_RETRIES = 1


class ExecutionMixin:
    """流式执行链：route/route_config 之下的调用执行（ModelRouter 组合此 Mixin）。"""

    @staticmethod
    def _cap_max_tokens(mt: int, provider: str) -> int:
        """P0 修复：按 provider 上限裁剪 max_tokens，避免 agnes 65536 限制触发 500 错误。"""
        cap = PROVIDER_MAX_TOKENS_CAP.get(provider)
        if cap is None:
            return mt
        try:
            _mt = int(mt)
        except (TypeError, ValueError):
            return cap
        return min(_mt, cap) if _mt > 0 else cap

    async def chat_stream(self, messages: list, task_type: str = "chat",
                          temperature: float = 0.7, max_tokens: int = 2000,
                          user_openid: str = "", session_id: str = "",
                          extra_headers: dict | None = None,
                          tools: list[dict] | None = None,
                          tool_choice: str | None = None) -> AsyncIterator[str]:
        """流式调用 LLM，yield 每个 chunk 的 delta content。

        复用 _route_with_retry 的重试/错误分类/凭证轮换逻辑，
        不再独立实现一套调用路径，保证行为一致性。

        P0 修复（截断检测根因）：
        原实现在 async for chunk in stream 循环中只 yield delta.content，
        从不读取 chunk.choices[0].finish_reason，导致：
          1. _stream_finish_reason_var 永远为 None
          2. verification loop 无法检测 finish_reason="length"（max_tokens 截断）
          3. 截断重试机制完全失效（用户反复反馈"截断问题又出现了"根因）
        修复：在流结束时捕获最后一个 chunk 的 finish_reason，写入 ContextVar。

        CR-Major-1 修复（fallback 链缺失 + 流式 usage 漏算）：
        原实现在重试耗尽后直接 raise last_error，不调用 _try_fallback_chain。
        流式调用是用户主要交互方式（QQ/WebUI），主 provider 故障时整条链路断了，
        已配置的 Agnes/自定义 provider 降级完全不会被触发。
        同时原实现不传 stream_options.include_usage，provider 不返回 usage，
        流式调用费用统计漏算（用户反馈"流式调用不计费"根因）。
        修复：
          1. 重试耗尽后调用 _try_fallback_chain；fallback 返回字符串时包装成
             async generator yield 出去，保证调用方语义一致。
          2. 传 stream_options={"include_usage": True}，捕获最后一个 chunk 的 usage
             并调 _record_stream_usage 记录费用。
        """
        config = self._registry.get_task_ref(task_type) or self._registry.get_task_ref("chat") or {}
        model = config["model"]
        mt = max_tokens or config.get("max_tokens", DEFAULT_MAX_TOKENS)
        provider = config.get("client", _CFG_DEFAULT_PROVIDER)
        timeout = self.TASK_TIMEOUTS.get(task_type, 30)

        if provider == _LOCAL_ORT_PROVIDER:
            async for chunk in self._stream_local_chat(
                messages, task_type, model, mt, temperature,
            ):
                yield chunk
            return

        messages = self._apply_prompt_caching(provider, messages)
        extra_headers = self._apply_caching_headers(extra_headers)
        tools = self._filter_tools_for_model(tools, model)

        _start = time.time()
        stream = None
        last_error = None
        _stream_finish_reason: str | None = None
        # CR-Major-1: 在循环外初始化，except 分支才能安全引用（stall timeout 日志需要）
        _stall_timeout = float(os.getenv("LLM_STREAM_STALL_TIMEOUT", "15"))
        _chunk_count = 0
        _stream_usage: Any = None
        _content_yielded = False

        for attempt in range(MAX_RETRIES + 1):
            try:
                _stream_finish_reason = None
                client = await self._select_client_for_provider(provider)
                kwargs = self._build_route_kwargs(
                    model, messages, temperature, mt, True,
                    tools, tool_choice, extra_headers, config, provider,
                )
                # CR-Major-1 修复：stream_options include_usage，让 provider 在最后一个
                # chunk 返回 usage，供 _record_stream_usage 记录费用。
                kwargs["stream_options"] = {"include_usage": True}
                # per-provider 并发信号量：agnes 最多 3 并发，create + stream 消费期间
                # 占用信号量，保证同 provider 并发流不超过 MAX_PROVIDER_CONCURRENCY；
                # 不同 provider 之间不互斥。
                # 注意：不复用 _create_completion，因为其信号量仅覆盖 create；
                #       chat_stream 需在流消费期间也占用信号量（限制并发流数量）。
                async with self._get_provider_call_semaphore(provider):
                    stream = await asyncio.wait_for(
                        client.chat.completions.create(**kwargs),
                        timeout=timeout,
                    )
                    # P0 修复（qq_group 截断根因）：添加 stall timeout 检测死流
                    # 根因：原实现在 async for chunk in stream 中无 stall timeout，
                    # 如果 provider 中途关闭连接且不发送 finish_reason chunk，
                    # 循环会正常结束（无异常），content 被静默截断，
                    # _stream_finish_reason 保持 None → 不触发 length retry → 用户看到截断回复。
                    # 修复：用 asyncio.wait_for 包装每个 chunk 的读取，15 秒无新 chunk → TimeoutError
                    # _stall_timeout 已在循环外初始化（except 分支需引用）
                    _chunk_count = 0
                    _stream_usage = None
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                stream.__anext__(),
                                timeout=_stall_timeout,
                            )
                        except StopAsyncIteration:
                            break  # 流正常结束
                        _chunk_count += 1
                        # CR-Major-1：捕获 usage（最后一个 chunk 才有，include_usage=True 时）
                        _chunk_usage = getattr(chunk, "usage", None)
                        if _chunk_usage is not None:
                            _stream_usage = _chunk_usage
                        try:
                            _choice = chunk.choices[0]
                        except (AttributeError, IndexError):
                            continue
                        # P0 修复：捕获 finish_reason（最后一个 chunk 才有）
                        _chunk_fr = getattr(_choice, "finish_reason", None)
                        if _chunk_fr:
                            _stream_finish_reason = _chunk_fr
                        delta = getattr(_choice.delta, "content", None)
                        if delta:
                            _content_yielded = True
                            yield delta
                await self._finalize_stream(
                    task_type, model, provider, _stream_finish_reason, _stream_usage,
                    _chunk_count, user_openid, session_id, mt, _start)

                # CR-Major-1：流式 usage 记录费用（include_usage=True 时 _stream_usage 非空）
                if _stream_usage is not None:
                    try:
                        await self._record_stream_usage(
                            task_type, model, type("R", (), {"usage": _stream_usage})(),
                            user_openid=user_openid, session_id=session_id,
                            provider=provider,
                        )
                    except (AttributeError, TypeError, OSError) as _ue:
                        logger.debug("router.stream_usage_record_skip: {}", _ue)
                metrics.inc(f"model_route.{task_type}.success")
                metrics.observe(f"model_route.{task_type}.duration", time.time() - _start)
                metrics.maybe_report()
                logger.info("llm.call", event="llm_call", model=model,
                            task=task_type, duration_ms=int((time.time() - _start) * 1000),
                            user_id=user_openid, session_id=session_id, stream=True,
                            finish_reason=_stream_finish_reason)
                return
            except (RuntimeError, OSError, KeyError, ValueError, _openai_mod.APIError,
                    asyncio.TimeoutError, LLMError) as e:
                # CR-Major-1：补 LLMError 捕获（与 route() 对齐）
                # P0 修复：捕获 stall timeout（asyncio.TimeoutError），正确关闭流并走重试
                stream, last_error, should_retry = await self._handle_stream_error(
                    e, provider, task_type, model, attempt, stream,
                    _content_yielded, _stall_timeout, _chunk_count)
                if not should_retry:
                    break


        # CR-Major-1 修复：重试耗尽后调用 fallback 链，而非直接 raise。
        # 流式调用是用户主要交互方式，主 provider 故障时应降级到 Agnes/自定义 provider。
        # fallback 链返回字符串时（非流式降级结果），包装成 async generator yield 出去，
        # 保证调用方 `async for chunk in chat_stream(...)` 语义一致。
        metrics.inc(f"model_route.{task_type}.failure")
        metrics.observe(f"model_route.{task_type}.duration", time.time() - _start)
        metrics.maybe_report()
        if last_error is None:
            # 理论不可达（循环至少跑一次，失败才有 last_error）；防御性兜底
            raise LLMError("流式调用失败：未知错误（last_error 未设置）")
        logger.warning("llm.stream_fallback_attempt", event="llm_stream_fallback",
                       model=model, task=task_type, provider=provider,
                       error=f"{type(last_error).__name__}: {last_error}"[:200])
        try:
            # fallback 链 stream=True 时返回流对象；stream=False 返回字符串
            # 这里传 stream=True 让 fallback 也走流式（若目标 provider 支持）
            fb_result = await self._try_fallback_chain(
                last_error, task_type, messages, temperature, True,
                tools, tool_choice, timeout, user_openid, session_id,
                extra_headers, original_max_tokens=mt,
            )
        except (RuntimeError, OSError, LLMError) as fb_err:
            logger.error("llm.stream_fallback_failed error={}", str(fb_err)[:200])
            fb_result = None
        if fb_result is not None:
            # fallback 返回字符串（_route_with_retry stream=False 路径，或 provider 不支持流式）
            # 包装成 async generator yield 出去，保证调用方语义一致
            if isinstance(fb_result, str):
                yield fb_result
                return
            # fallback 返回流对象（stream=True 路径），透传其 chunks
            if hasattr(fb_result, "__aiter__"):
                async for _fb_chunk in fb_result:
                    _fb_choices = getattr(_fb_chunk, "choices", None)
                    _fb_delta = None
                    if _fb_choices:
                        try:
                            _fb_delta = getattr(_fb_choices[0], "delta", None)
                        except (IndexError, AttributeError):
                            _fb_delta = None
                    if _fb_delta is not None:
                        _fb_content = getattr(_fb_delta, "content", None)
                        if _fb_content:
                            yield _fb_content
                return
            # 其他类型（如 response 对象）直接 yield 字符串形式
            yield str(fb_result)
            return
        # 所有降级目标均不可用，抛出明确异常（与 route() 语义一致）
        raise LLMError(
            f"流式调用所有降级目标均不可用 (task={task_type}): "
            f"{type(last_error).__name__}: {last_error}",
            error_code=ErrorCodeEnum.E_LLM001,
            cause=last_error,
        ) from last_error

    async def _finalize_stream(self, task_type: str, model: str, provider: str,
                               _stream_finish_reason: str | None, _stream_usage: Any,
                               _chunk_count: int, user_openid: str, session_id: str,
                               mt: int, _start: float) -> None:
        """流结束后的后处理：finish_reason 检查 + ContextVar + usage 记录 + metrics。"""
        # P0 修复：流结束后检测是否收到 finish_reason
        # 如果未收到，说明 provider 可能中途关闭连接（死流），content 可能被截断
        if not _stream_finish_reason:
            logger.warning("llm.stream_no_finish_reason",
                           model=model, task=task_type,
                           provider=provider, chunk_count=_chunk_count,
                           hint="provider 可能中途关闭连接，content 可能被截断")
        # P0 修复：流结束后写入 ContextVar，供 verification loop 检测截断
        if _stream_finish_reason:
            try:
                from agent_core._shared import _stream_finish_reason_var
                _stream_finish_reason_var.set(_stream_finish_reason)
            except (ImportError, AttributeError):
                logger.debug("router.stream_finish_reason_var_set_failed", exc_info=True)
            # 截断诊断日志：finish_reason="length" 时记录 mt 和内容长度
            if _stream_finish_reason == "length":
                logger.warning("llm.stream_truncated_by_max_tokens",
                               model=model, task=task_type,
                               max_tokens=mt, provider=provider,
                               finish_reason=_stream_finish_reason)
        # CR-Major-1：流式 usage 记录费用（include_usage=True 时 _stream_usage 非空）
        if _stream_usage is not None:
            try:
                await self._record_stream_usage(
                    task_type, model, type("R", (), {"usage": _stream_usage})(),
                    user_openid=user_openid, session_id=session_id,
                    provider=provider,
                )
            except (AttributeError, TypeError, OSError) as _ue:
                logger.debug("router.stream_usage_record_skip: {}", _ue)
        metrics.inc(f"model_route.{task_type}.success")
        metrics.observe(f"model_route.{task_type}.duration", time.time() - _start)
        metrics.maybe_report()
        logger.info("llm.call", event="llm_call", model=model,
                    task=task_type, duration_ms=int((time.time() - _start) * 1000),
                    user_id=user_openid, session_id=session_id, stream=True,
                    finish_reason=_stream_finish_reason)

    async def _handle_stream_error(self, e: Exception, provider: str, task_type: str,
                                   model: str, attempt: int, stream: Any,
                                   _content_yielded: bool, _stall_timeout: float,
                                   _chunk_count: int) -> tuple[Any | None, Exception | None, bool]:
        """流式异常处理：关闭流 + stall 诊断 + 重试判断。返回 (stream, last_error, should_retry)。"""
        last_error = e
        if stream:
            with contextlib.suppress(AttributeError, OSError):
                await stream.close()
            stream = None
        if _content_yielded:
            raise
        # stall timeout 特殊处理：记录诊断日志
        if isinstance(e, asyncio.TimeoutError):
            logger.warning("llm.stream_stall_timeout",
                           model=model, task=task_type,
                           provider=provider, stall_timeout=_stall_timeout,
                           chunk_count=_chunk_count,
                           hint="流式响应中途停滞，可能 provider 故障")
        should_retry = await self._handle_route_exception(
            e, provider, task_type, model, attempt,
        )
        return stream, last_error, should_retry

    async def _stream_local_chat(
        self,
        messages: list,
        task_type: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        transport = self.get_transport(_LOCAL_ORT_PROVIDER)
        if transport is None:
            raise LLMError(
                "local-ort provider selected but local transport is not configured",
                error_code=ErrorCodeEnum.E_LLM006,
            )
        request = CompletionRequest(
            model=model,
            messages=tuple(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            extra={"route": f"router:{task_type}", "model_id": model},
        )
        try:
            async for chunk in transport.stream(request):
                if chunk.text:
                    yield chunk.text
        except TransportError as error:
            if error.__cause__ is not None:
                from local_ai.integration.reranker import LocalModelUnavailableError

                if isinstance(error.__cause__, LocalModelUnavailableError):
                    raise error.__cause__ from None
            raise

    @staticmethod
    def _classify_error(exc: Exception) -> str:
        """将异常分类为可重试/不可重试错误类型。"""
        exc_msg = str(exc).lower()
        exc_name = type(exc).__name__.lower()
        if isinstance(exc, asyncio.TimeoutError) or 'timeout' in exc_name or 'timeout' in exc_msg:
            return 'timeout'
        if 'rate' in exc_msg or '429' in exc_msg or 'rate_limit' in exc_name:
            return 'rate_limit'
        if 'connection' in exc_name or 'connection' in exc_msg or 'connect' in exc_msg:
            return 'connection_error'
        return 'unknown'

    @staticmethod
    def _build_route_kwargs(model: str, messages: list[dict], temperature: float,
                             max_tokens: int, stream: bool,
                             tools: list[dict] | None, tool_choice: str | None,
                             extra_headers: dict | None,
                             config: dict, provider: str) -> dict:
        """构造非流式/流式路由调用的 kwargs。"""
        # P0 修复：按 provider 上限裁剪 max_tokens（agnes 上限 65536）
        max_tokens = ExecutionMixin._cap_max_tokens(max_tokens, provider)
        # Ollama 模型名翻译：把工作流/云模型名映射为本地实际模型名，
        # 避免请求转发到本地 Ollama 时因模型不存在报错（真实代理的核心配套）。
        _send_model = translate_model_for_provider(provider, model)
        kwargs = {
            "model": _send_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        # ── 防止模型生成退化（repetition degeneration）───
        # 根因：自回归模型的 greedy decoding 无法逃出重复循环，且自增强效应
        # 使重复概率越来越高，最终泄露训练数据中的高频片段。
        # 论文 arXiv:2512.04419 的结论：
        #   - Beam Search + early_stopping=True 是通用方案（但 OpenAI API 不支持）
        #   - presence_penalty 仅对条件模式重复有效，对结构化内容重复无效
        #   - frequency_penalty 论文未测试，作为合理启发式保留
        #   - stop 序列 + 后处理清洗是 API 调用场景下的必要兜底
        fp = config.get("frequency_penalty", 1.0)
        # 优先 WebUI 全局设置（models.frequency_penalty），回退模型配置
        try:
            from config import get_frequency_penalty
            fp = get_frequency_penalty(default=fp)
        except Exception:
            logger.warning("router.frequency_penalty_config_load_failed", exc_info=True)
        if fp:
            kwargs["frequency_penalty"] = fp
        # 论文验证有效值为 1.2，对条件模式重复有效；对结构化重复效果有限但无副作用
        pp = config.get("presence_penalty", 1.0)
        # 优先 WebUI 全局设置（models.presence_penalty），回退模型配置
        try:
            from config import get_presence_penalty
            pp = get_presence_penalty(default=pp)
        except Exception:
            logger.warning("router.presence_penalty_config_load_failed", exc_info=True)
        if pp:
            kwargs["presence_penalty"] = pp
        # 退化兜底停止序列：当模型开始输出工具定义泄露时立即停止
        kwargs["stop"] = ["Never use this AI assistant tool", "\"Never use"]
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
            # 诊断日志：记录发送给 LLM 的工具名称列表
            # P0 修复：loguru 使用 {} 占位符，不是 printf 风格 %s（原写法导致日志显示字面 %s）
            tool_names = [t.get("function", {}).get("name", "?") for t in tools]
            logger.debug("router.tools_sent provider={} model={} count={} names={}",
                         provider, model, len(tools), tool_names)
        if extra_headers:
            kwargs["extra_headers"] = extra_headers

        # 支持 thinking 参数（通用）
        # 关键修复：thinking 关闭时也要传递 enable_thinking: false，否则 agnes 模型使用默认行为
        thinking_config = config.get("thinking")
        # P0 修复：thinking_debug 从 INFO 降为 DEBUG（每次 route 调用都触发，INFO 级别刷屏）
        logger.debug("router.thinking_debug provider={} thinking={}", provider, thinking_config)
        if provider == "agnes":
            # agnes 模型需要明确传递 enable_thinking 参数
            enabled = bool(thinking_config and thinking_config.get("type") == "enabled")
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": enabled}}
        elif thinking_config:
            kwargs["extra_body"] = {"thinking": thinking_config}
        return kwargs

    async def _create_completion(
        self,
        provider: str,
        *,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        stream: bool,
        tools: list[dict] | None,
        tool_choice: str | None,
        extra_headers: dict | None,
        config: dict,
        timeout: int,
        stream_options: dict | None = None,
    ) -> Any:
        """统一「客户端选择 → 构造 kwargs → 加锁创建」的调用核心。

        供 chat_stream / _route_with_retry / _route_for_continuation 复用，
        消除三处重复。stream_options 仅流式路径需要时透传。
        """
        client = await self._select_client_for_provider(provider)
        kwargs = self._build_route_kwargs(
            model, messages, temperature, max_tokens, stream,
            tools, tool_choice, extra_headers, config, provider,
        )
        if stream_options:
            kwargs["stream_options"] = stream_options
        async with self._get_provider_call_semaphore(provider):
            return await asyncio.wait_for(
                client.chat.completions.create(**kwargs),
                timeout=timeout,
            )

    async def _handle_route_response(self, response: Any, task_type: str, model: str,
                                     stream: bool, user_openid: str, session_id: str,
                                     provider: str, tools: list[dict] | None,
                                     messages: list[dict] | None = None,
                                     temperature: float | None = None,
                                     max_tokens: int | None = None,
                                     config: dict | None = None) -> str | object:
        """处理路由成功响应：记录费用、缓存、凭证成功，返回 content 或 response。"""
        if stream:
            # 流式调用：在返回前尝试记录费用（部分 provider 在流结束时提供 usage）
            try:
                await self._record_stream_usage(task_type, model, response,
                                                user_openid, session_id, provider)
            except (AttributeError, TypeError, OSError) as e:
                logger.debug("router.stream_usage_record_failed: %s", e)
            return response

        self._track_cache(response)
        await self._record_usage(task_type, model, response, user_openid, session_id, provider)
        self._check_cache_health()

        # 报告凭证成功
        await self._credential_pool.report_success(provider)

        if tools and response.choices[0].message.tool_calls:
            _reasoning_content_var.set(getattr(response.choices[0].message, "reasoning_content", None) or "")
            return response

        content = response.choices[0].message.content or ""
        rc = getattr(response.choices[0].message, "reasoning_content", None) or ""
        _reasoning_content_var.set(rc)
        # 关键修复：禁止用 reasoning_content 代替 content
        # 根因：agnes-2.0-flash 即使 enable_thinking=False，在 max_tokens 不足或某些边界条件下
        # 仍可能返回 reasoning_content。用思考链代替回复会导致"推理严重泄漏"——
        # LLM 的内部思考过程被当成最终回复发给用户。
        # 正确做法：content 为空时返回降级提示，触发上层 fallback 机制。
        if not content and rc:
            logger.warning("router.reasoning_leak_blocked",
                           model=model, task=task_type,
                           rc_len=len(rc), finish_reason=getattr(response.choices[0], "finish_reason", None))
            content = ""  # 留空，让上层降级/fallback 机制接管

        # usage 诊断日志：记录实际生成 token 数，帮助定位截断根因
        _usage = getattr(response, "usage", None)
        if _usage:
            logger.debug("router.usage", model=model, task=task_type,
                         prompt_tokens=getattr(_usage, "prompt_tokens", 0),
                         completion_tokens=getattr(_usage, "completion_tokens", 0),
                         finish_reason=getattr(response.choices[0], "finish_reason", None),
                         content_len=len(content))

        # 检查 finish_reason：截断重试（assistant-prefill 方式，不污染上下文）
        # P0 重构（用户要求"不许截断" + "重试机制保留"）：
        # 根因 1：原截断重试追加 "请继续完成你的回复" 作为 user message，
        #         LLM 把它当成真实用户输入，在后续轮次回应"继续完成"等元词汇，
        #         造成上下文污染和角色出戏（详见 conversation_logs 2026-07-25 17:46 案例）。
        # 根因 2：max_tokens=32768 对中文长回复过小，频繁触发 length 截断。
        # 修复：
        #   1. WEB_UI_MAX_TOKENS 提升到 131072（匹配模型上下文窗口），从源头消除大部分截断
        #   2. 保留重试机制，但改用 assistant-prefill（不追加 user message），
        #      避免"请继续"prompt 污染上下文
        #   3. 重试使用 _route_for_continuation（去递归化），最多 2 轮
        #   4. 上下文溢出仍由 agent_context.py 的压缩机制处理（"重置机制"）
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        if finish_reason and finish_reason != "stop":
            content_len = len(content)
            if finish_reason == "length":
                content = await self._retry_truncated_content(
                    content, model, task_type, messages, temperature, max_tokens,
                    user_openid, session_id)
            elif finish_reason == "content_filter":
                logger.warning("llm.content_filtered",
                               model=model, task=task_type,
                               content_len=content_len,
                               finish_reason=finish_reason)
                # content_filter 通常是 provider 服务端审查（如 mimo-v2.5 对敏感内容过滤）
                # 抛出异常触发 fallback 链，给 agnes 等其他 provider 一次重试机会
                raise RuntimeError(
                    f"content_filter by {provider}/{model}: 服务端内容审查拦截"
                )
            else:
                logger.info("llm.unusual_finish",
                            model=model, task=task_type,
                            finish_reason=finish_reason,
                            content_len=content_len)

        # 关键修复：空 content 一律抛异常触发 fallback
        # 根因：agnes-2.0-flash 多种异常行为：
        #   1. finish_reason=tool_calls + 空 content（工具调用已在前面的分支处理，到这里 content 为空=异常）
        #   2. finish_reason=stop + 空 content（模型认为不需要回复，但用户会收到空回复）
        # 两种情况都应触发 fallback 重试其他 provider，而不是返回空字符串给用户。
        if not content.strip():
            raise RuntimeError(
                f"empty_content by {provider}/{model}: finish_reason={finish_reason}, "
                f"content 为空（模型未生成有效回复）"
            )

        # 关键：WebUI 设置必须生效，不许泄露思考
        thinking_config = (config or {}).get("thinking", {})
        thinking_disabled = thinking_config.get("type") == "disabled"

        # 1. 清空 reasoning_content（不管 thinking 是否禁用，都不泄露给用户）
        _reasoning_content_var.set("")

        # 2. thinking 禁用时，清理 content 中可能嵌入的推理标记
        if thinking_disabled:
            from utils.text_utils import strip_reasoning
            content = strip_reasoning(content)

        return content

    async def _retry_truncated_content(self, content: str, model: str, task_type: str,
                                       messages: list[dict] | None, temperature: float | None,
                                       max_tokens: int | None, user_openid: str,
                                       session_id: str) -> str:
        """length 截断时用 assistant-prefill 续写（去递归化，最多 2 轮）。

        P0 重构（用户要求"不许截断" + "重试机制保留"）：
        重试不追加 user message（避免"请继续"prompt 污染上下文），改用
        assistant-prefill（追加已有内容让 LLM 续写），走 _route_for_continuation
        去递归化。最多 2 轮。
        """
        content_len = len(content)
        logger.warning("llm.truncated_by_max_tokens",
                       model=model, task=task_type,
                       content_len=content_len,
                       finish_reason="length")
        # Feature flag: TRUNCATION_RETRY_DERECURSE（默认 true）
        _derecurse = os.getenv("TRUNCATION_RETRY_DERECURSE", "true").lower() in ("true", "1", "yes")
        # CodeRabbit #6 修复：加 messages 非空检查（防御性编程）
        if messages and content and len(content) > 10:
            _retry_max_tokens = max_tokens * 2 if max_tokens else None
            for _retry_round in range(2):  # 最多 2 轮重试
                try:
                    retry_messages = messages.copy()
                    # assistant-prefill：追加已有内容，让 LLM 从此处续写
                    retry_messages.append({"role": "assistant", "content": content})
                    # 注意：不追加任何 user message，避免"请继续"prompt 污染上下文
                    if _derecurse:
                        # 新路径：直接调底层，不递归 route()，返回原始 response
                        retry_response = await self._route_for_continuation(
                            task_type, retry_messages, temperature=temperature,
                            max_tokens=_retry_max_tokens,
                            user_openid=user_openid, session_id=session_id,
                        )
                        retry_content = ""
                        _retry_finish = None
                        if retry_response is not None:
                            _choices = getattr(retry_response, "choices", None) or []
                            if _choices:
                                retry_content = getattr(_choices[0].message, "content", "") or ""
                                _retry_finish = getattr(_choices[0], "finish_reason", None)
                    else:
                        # 旧路径（兼容回退）：递归调用 route()
                        retry_result = await self.route(
                            task_type, retry_messages, temperature=temperature,
                            max_tokens=_retry_max_tokens,
                            user_openid=user_openid, session_id=session_id,
                        )
                        retry_content = retry_result if isinstance(retry_result, str) else (retry_result.choices[0].message.content or "")
                        _retry_finish = getattr(retry_result, "choices", [{}])
                        _retry_finish = getattr(_retry_finish[0], "finish_reason", None) if _retry_finish else None
                    if retry_content and len(retry_content) > 5:
                        content, _merge_action = merge_continuation(
                            content, retry_content, assume_tail=True,
                        )
                        logger.info("llm.truncated_retry_success",
                                    final_len=len(content), model=model,
                                    retry_round=_retry_round + 1,
                                    finish_reason=_retry_finish,
                                    derecurse=_derecurse,
                                    merge_action=_merge_action,
                                    method="assistant_prefill")
                        # 检查是否仍然截断（基于真实 finish_reason 判断）
                        if _retry_finish != "length":
                            break  # 不再截断，退出重试
                    else:
                        break  # 无内容，退出
                except Exception as e:
                    logger.warning("llm.truncated_retry_failed", error=str(e), model=model,
                                   retry_round=_retry_round + 1)
                    break
        return content

    async def _handle_route_exception(self, e: Exception, provider: str,
                                      task_type: str, model: str,
                                      attempt: int) -> bool:
        """处理路由异常：分类、报告、轮换凭证。返回 True 表示可重试，False 表示已耗尽。

        对于 ABORT 或不可重试错误，直接 raise 传播给调用方。
        """
        classified = self._error_classifier.classify(e)
        await self._credential_pool.report_error(provider, classified)

        # 根据恢复策略执行不同操作
        if classified.action == RecoveryAction.ROTATE_CREDENTIAL:
            await self._rotate_credential_on_error(provider, classified)

        if classified.action == RecoveryAction.ABORT:
            logger.error("router.call_aborted", task=task_type, model=model,
                         reason=classified.reason.value,
                         error=f"{type(e).__name__}: {e}")
            raise e

        if not classified.is_retryable:
            logger.error("router.call_failed", task=task_type, model=model,
                         attempt=attempt + 1, reason=classified.reason.value,
                         action=classified.action.value,
                         error=f"{type(e).__name__}: {e}")
            raise e

        if attempt < MAX_RETRIES:
            backoff = classified.backoff_seconds if classified.backoff_seconds > 0 else 1 * (attempt + 1)
            # P0 修复（2026-08-05）：loguru extra 字段在当前日志格式下不打印，
            # 导致 router.retry 只显示 event name，看不到 reason/error。
            # 改为 f-string 写入 message，确保 agnes 失败原因可见。
            logger.warning(
                f"router.retry task={task_type} model={model} "
                f"attempt={attempt + 1} reason={classified.reason.value} "
                f"action={classified.action.value} backoff={backoff:.1f}s "
                f"error={type(e).__name__}: {e}")
            await asyncio.sleep(backoff)
            return True
        logger.error("router.retry_exhausted", task=task_type, model=model,
                     attempts=MAX_RETRIES + 1, reason=classified.reason.value,
                     error=f"{type(e).__name__}: {e}")
        return False

    async def _route_with_retry(self, task_type: str, config: dict,
                                messages: list[dict], temperature: float,
                                max_tokens: int, stream: bool,
                                tools: list[dict] | None, tool_choice: str | None,
                                timeout: int, user_openid: str, session_id: str,
                                extra_headers: dict | None = None) -> str | object:
        """带重试的路由调用：客户端选择 → 构建 kwargs → 调用 API → 处理响应/异常。"""
        model = config["model"]
        last_error = None
        provider = config.get("client", _CFG_DEFAULT_PROVIDER)

        if provider == _LOCAL_ORT_PROVIDER:
            chunks = []
            async for chunk in self._stream_local_chat(
                messages, task_type, model, max_tokens, temperature,
            ):
                chunks.append(chunk)
            return "".join(chunks)

        messages = self._apply_prompt_caching(provider, messages)
        # 主路由路径也需过滤工具，防止小模型收到工具定义后输出退化
        tools = self._filter_tools_for_model(tools, model)

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await self._create_completion(
                    provider,
                    model=model, messages=messages, temperature=temperature,
                    max_tokens=max_tokens, stream=stream, tools=tools, tool_choice=tool_choice,
                    extra_headers=extra_headers, config=config, timeout=timeout,
                )
                return await self._handle_route_response(
                    response, task_type, model, stream,
                    user_openid, session_id, provider, tools,
                    messages=messages, temperature=temperature, max_tokens=max_tokens,
                    config=config,
                )

            except (RuntimeError, OSError, KeyError, ValueError,
                    _openai_mod.APIError, LLMError) as e:
                # LLMError：客户端未初始化/无法恢复，重试同一 provider 无意义，
                # 但必须让它作为 last_error 抛出到 route 的降级链（见 route 的注释）
                last_error = e
                should_retry = await self._handle_route_exception(
                    e, provider, task_type, model, attempt,
                )
                if not should_retry:
                    break
        raise last_error

    async def _route_for_continuation(self, task_type: str, messages: list[dict],
                                       temperature: float = 0.7,
                                       max_tokens: int | None = None,
                                       user_openid: str = "",
                                       session_id: str = "") -> Any | None:
        """截断续写专用路由：直接调用 LLM 返回原始 response 对象，不递归触发截断重试。

        P0 修复（Task 1.1+1.2）：替代原 `await self.route(...)` 递归调用。
        - 不进入 `_handle_route_response`，避免再次触发截断重试形成递归风暴
        - 返回原始 response 对象，让调用方正确读取 `finish_reason` 判断是否仍截断
        - 单次调用，无重试（截断续写本身的 2 轮循环由调用方控制）
        - 失败时返回 None（调用方按"无内容"分支处理）

        注意：此方法仅用于截断续写场景，常规路由请使用 route()。
        """
        # 统一走 registry 入口（语义一致；registry._table 即 ROUTE_TABLE，性能无差异）
        config = self._registry.get_task_ref(task_type) or self._registry.get_task_ref("chat") or {}
        model = config["model"]
        provider = config.get("client", _CFG_DEFAULT_PROVIDER)
        mt = max_tokens or config.get("max_tokens", DEFAULT_MAX_TOKENS)
        timeout = self.TASK_TIMEOUTS.get(task_type, 30)

        # 应用 prompt caching（与主路由保持一致）
        messages = self._apply_prompt_caching(provider, messages)

        try:
            response = await self._create_completion(
                provider,
                model=model, messages=messages, temperature=temperature,
                max_tokens=mt, stream=False, tools=None, tool_choice=None,
                extra_headers=None, config=config, timeout=timeout,
            )
            self._track_cache(response)
            logger.info("llm.continuation_call", model=model, task=task_type,
                        user_id=user_openid, session_id=session_id,
                        max_tokens=mt)
            return response
        except (RuntimeError, OSError, KeyError, ValueError, _openai_mod.APIError) as e:
            # 续写失败不影响主流程，调用方按"无内容"分支处理
            logger.warning("llm.continuation_failed",
                           model=model, task=task_type,
                           error=f"{type(e).__name__}: {e}"[:200])
            return None
