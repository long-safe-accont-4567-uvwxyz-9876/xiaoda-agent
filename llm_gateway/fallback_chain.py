"""ModelRouter 的回退链 Mixin — 自 model_router.py 拆分（上帝文件 Phase 4）。

内容：fallback_chat（公开别名）与 _try_fallback_chain（FALLBACK_ROUTE →
Agnes → 自定义 provider 的多级降级链，含 timeout 跳链 / 禁止跨 provider
切换 / content_filter 跳同 provider / original_max_tokens 透传等策略）。
方法体自 model_router.py 逐字节搬移，仅缩进调整。

兼容契约（tests/test_fallback_chain_mixin.py）：
    - 本模块不得 import model_router（防循环依赖）；降级策略数据
      FALLBACK_ROUTE 已下沉 model_router_registry，与本契约一致
    - ModelRouter(FallbackChainMixin) 继承保持 self 语义；Phase 0 铺路的
      公开别名 fallback_chat 委托关系不变
"""
from __future__ import annotations

from loguru import logger

from config import DEFAULT_PROVIDER as _CFG_DEFAULT_PROVIDER
from core.app_exception import LLMError
from model_router_registry import FALLBACK_ROUTE


class FallbackChainMixin:
    """多级回退链（ModelRouter 组合此 Mixin）。"""

    async def fallback_chat(self, e: Exception, task_type: str,
                            messages: list[dict], temperature: float,
                            stream: bool, tools: list[dict] | None,
                            tool_choice: str | None, timeout: int,
                            user_openid: str, session_id: str,
                            extra_headers: dict | None,
                            original_max_tokens: int | None = None) -> str | object | None:
        """公开别名：跨模块调用 _try_fallback_chain 的稳定入口（替代私有方法白盒依赖）。"""
        return await self._try_fallback_chain(
            e, task_type, messages, temperature, stream, tools, tool_choice,
            timeout, user_openid, session_id, extra_headers, original_max_tokens)

    async def _try_fallback_chain(self, e: Exception, task_type: str,
                                  messages: list[dict], temperature: float,
                                  stream: bool, tools: list[dict] | None,
                                  tool_choice: str | None, timeout: int,
                                  user_openid: str, session_id: str,
                                  extra_headers: dict | None,
                                  original_max_tokens: int | None = None) -> str | object | None:
        """多级 fallback：FALLBACK_ROUTE → Agnes → 自定义 provider。全部失败返回 None。

        每一级降级前都会检查目标客户端是否已配置（有 API key 且有 base_url），
        未配置的目标会被跳过，避免向未初始化的客户端发起无意义调用。

        P0 修复（Task 1.3）：透传 original_max_tokens，避免 fallback 把 max_tokens 压到 1000。
        根因：原实现 fallback_config.get("max_tokens", 1000) 会把 Web UI 的 32768 压到 1000，
              起点太小 → 截断续写翻倍序列 1000→2000→...→128000 需 7 次递归。
        修复：fallback 时取 max(original_max_tokens, fallback_default)。
        """
        # 治本修复（2026-08-05 用户"治标不治本"反馈）：timeout 错误跳过整个 fallback 链。
        # 根因：agnes-2.0-flash 服务端强制 thinking，正常响应 6-7s（实测铁证）。
        #   read timeout 触发后，fallback 链会再调同 provider 的 agnes（不同 task_type），
        #   agnes 慢时 fallback 也慢 → 8s+8s=16s 双倍延迟（日志 llm_verify=9012ms 铁证）。
        #   timeout 意味着服务端慢，同 provider fallback 再调一次必然也慢，纯叠加延迟。
        # 修复：timeout 错误直接返回 None，不执行 fallback，由上层降级返回提示。
        #   避免双倍等待，最坏单次 timeout 即降级，而非 timeout×2。
        try:
            _classified_for_fb = self._error_classifier.classify(e)
            if _classified_for_fb.reason.value == "timeout":
                logger.warning("router.fallback_skip_timeout",
                               task=task_type,
                               reason="timeout: same provider fallback would double latency",
                               error=f"{type(e).__name__}: {e}")
                return None
        except Exception:
            logger.warning("router.fallback_error_classify_failed", exc_info=True)

        # 1. 降级到更便宜的模型（FALLBACK_ROUTE 自 model_router_registry，数据下沉后无反向依赖）
        fallback_type = FALLBACK_ROUTE.get(task_type)
        # P0 修复：content_filter 触发时跳过同 provider 的 fallback 目标
        # 根因：同 provider 的 fallback 目标会再次触发 content_filter
        # 浪费一次调用 + 触发 verification retry，导致 14 秒延迟
        # 修复：content_filter 时跳过同 provider 的 fallback，直接到不同 provider（如 agnes）
        _is_content_filter = "content_filter" in str(e) or "content_policy" in str(e)
        _original_provider = ""
        try:
            # Task 6: 通过 registry 快照读取，避免降级链污染全局 ROUTE_TABLE
            _orig_entry = self._registry.get_task(task_type) or {}
            _original_provider = _orig_entry.get("client", _CFG_DEFAULT_PROVIDER)
        except Exception:
            logger.warning("router.fallback_original_provider_lookup_failed", exc_info=True)
        # 用户硬约束（2026-08-04）：禁止自动切换模型/provider。
        # 用户在 WebUI 切换到哪个 provider，就一直用该 provider，失败也不跨 provider 兜底。
        # 同 provider 内的重试（_route_with_retry）保留，不算"切换"。
        # 跨 provider fallback 只会叠加延迟（再调一次别的 API）并违背用户意图，故全部跳过。
        while fallback_type:
            # Task 6: 用 registry 快照（深拷贝），降级期间修改不影响全局 ROUTE_TABLE
            fallback_config = self._registry.snapshot_task(fallback_type)
            fallback_provider = fallback_config.get("client", _CFG_DEFAULT_PROVIDER) if fallback_config else _CFG_DEFAULT_PROVIDER
            # 禁止跨 provider 切换：fallback 目标 provider 必须与原 provider 一致
            if fallback_provider != _original_provider:
                logger.info("router.fallback_skip_cross_provider",
                            original_task=task_type, fallback_task=fallback_type,
                            original_provider=_original_provider,
                            fallback_provider=fallback_provider,
                            reason="user_disabled_cross_provider_fallback")
                fallback_type = FALLBACK_ROUTE.get(fallback_type)
                continue
            # content_filter 时跳过同 provider（同样的过滤模型会再次拦截）
            if _is_content_filter and fallback_provider == _original_provider:
                logger.warning("router.fallback_skip_same_provider",
                               original_task=task_type, fallback_task=fallback_type,
                               reason="content_filter: same provider will filter again")
                # 跳到下一级 fallback
                fallback_type = FALLBACK_ROUTE.get(fallback_type)
                continue
            # D12: 降级前检查目标客户端是否已配置，未配置则跳过该降级目标
            if fallback_config and self._is_client_configured(fallback_provider):
                break
            fallback_type = FALLBACK_ROUTE.get(fallback_type)
        if fallback_type:
            logger.warning("router.fallback",
                           original_task=task_type, fallback_task=fallback_type,
                           error=f"{type(e).__name__}: {e}")
            try:
                fallback_tools = self._filter_tools_for_model(tools, fallback_config.get("model", ""))
                # P0 修复：透传 original_max_tokens，避免被 fallback_config 默认值压缩
                _fallback_max_tokens = fallback_config.get("max_tokens", 1000)
                if original_max_tokens:
                    _fallback_max_tokens = max(original_max_tokens, _fallback_max_tokens)
                return await self._route_with_retry(
                    fallback_type, fallback_config, messages, temperature,
                    _fallback_max_tokens, stream,
                    fallback_tools, tool_choice, timeout, user_openid, session_id,
                    extra_headers=extra_headers,
                )
            except (RuntimeError, OSError, KeyError, ValueError, LLMError) as fb_err:
                logger.error("router.fallback_failed",
                             fallback_task=fallback_type,
                             error=f"{type(fb_err).__name__}: {fb_err}")

        # 2. 尝试 Agnes 作为最终降级
        # 用户硬约束：禁止跨 provider 切换。仅当原 provider 本就是 agnes 时才允许
        # 走 agnes 内部的 chat_agnes task（同 provider，不算切换）。
        if _original_provider == "agnes" and task_type not in ("chat_agnes",) and self._is_client_configured("agnes"):
            try:
                # Task 6: 用 registry 快照读取 chat_agnes，避免污染全局
                agnes_config = self._registry.snapshot_task("chat_agnes")
                if agnes_config:
                    logger.warning("router.agnes_fallback", original_task=task_type)
                    agnes_tools = self._filter_tools_for_model(tools, agnes_config.get("model", ""))
                    # P0 修复：透传 original_max_tokens
                    _agnes_max_tokens = agnes_config.get("max_tokens", 2000)
                    if original_max_tokens:
                        _agnes_max_tokens = max(original_max_tokens, _agnes_max_tokens)
                    return await self._route_with_retry(
                        "chat_agnes", agnes_config, messages, temperature,
                        _agnes_max_tokens, stream,
                        agnes_tools, tool_choice, timeout, user_openid, session_id,
                        extra_headers=extra_headers,
                    )
            except (RuntimeError, OSError, KeyError, ValueError, LLMError) as agnes_err:
                logger.error("router.agnes_fallback_failed", error=str(agnes_err))

        # 3. 尝试已注册的自定义 provider（SiliconFlow/OpenRouter/ModelScope 等）
        # 用户硬约束：禁止跨 provider 切换。仅当原 provider 本就是该自定义 provider 时才执行。
        if task_type.startswith("chat"):
            for cp_name, _cp_client in self.list_custom_clients():
                # 跨 provider 切换一律跳过
                if cp_name != _original_provider:
                    continue
                try:
                    cp_model = self._get_custom_provider_default_model(cp_name)
                    if not cp_model:
                        continue
                    cp_config = {"model": cp_model, "max_tokens": 1000, "client": cp_name}
                    logger.warning("router.custom_provider_fallback",
                                   original_task=task_type, provider=cp_name, model=cp_model)
                    cp_tools = self._filter_tools_for_model(tools, cp_model)
                    # P0 修复：透传 original_max_tokens，避免硬编码 1000 压缩
                    _cp_max_tokens = 1000
                    if original_max_tokens:
                        _cp_max_tokens = max(original_max_tokens, 1000)
                    return await self._route_with_retry(
                        f"chat_{cp_name}", cp_config, messages, temperature,
                        _cp_max_tokens, stream, cp_tools, tool_choice, timeout,
                        user_openid, session_id,
                        extra_headers=extra_headers,
                    )
                except (RuntimeError, OSError, KeyError, ValueError, LLMError) as cp_err:
                    # CR-Major-2 修复：补 LLMError 捕获。
                    # _route_with_retry 内部 _select_client_for_provider 在 client 未初始化时
                    # 抛 LLMError（继承 AppException，不属于 RuntimeError/OSError/ValueError），
                    # 原捕获集合漏掉它 → 自定义 provider fallback 链提前终止，异常逃逸到 route()。
                    logger.error("router.custom_provider_fallback_failed",
                                 provider=cp_name, error=str(cp_err))
                    continue
        return None
