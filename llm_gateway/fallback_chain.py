"""ModelRouter 的回退链 Mixin — 自 model_router.py 拆分（上帝文件 Phase 4）。

内容：fallback_chat（公开别名）与 _try_fallback_chain（同家兜底档矩阵 →
跨家白名单（开关）→ Agnes → 自定义 provider → 紧急免费车道的多级降级链，
含 timeout 跳链 / 禁止自动跨家切换 / content_filter 跳同 provider /
original_max_tokens 透传 / 降级标记等策略）。

兼容契约（tests/test_fallback_chain_mixin.py）：
    - 本模块不得 import model_router（防循环依赖）；降级策略数据
      FALLBACK_ROUTE 已下沉 model_router_registry，与本契约一致
    - ModelRouter(FallbackChainMixin) 继承保持 self 语义；Phase 0 铺路的
      公开别名 fallback_chat 委托关系不变

2026-11-08（严重-2 + 轻微-15）：
    1. 兜底表从「task 一条链」（FALLBACK_ROUTE）升级为
       task → provider → 同家兜底档 矩阵（model_router_config.FALLBACK_MATRIX，
       自 config/provider_metadata.json 派生）。主模型不是 agnes 时也有自己的
       同家档，不再直接掉进免费车道。
    2. 跨家只在 _CROSS_PROVIDER_MAP 白名单内、且 CROSS_PROVIDER_FALLBACK_POLICY
       显式打开时才允许（默认关闭，保持 2026-08-04「禁止自动跨家切换」）。
    3. 紧急免费车道只在同家档与白名单都不可用时进入，并且一定写下降级标记
       （前端提示 + 落库 model_used）。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import openai as _openai_mod
from loguru import logger

from config import DEFAULT_PROVIDER as _CFG_DEFAULT_PROVIDER
from config import FREE_EMERGENCY_FALLBACK
from core.app_exception import LLMError
from model_router_config import (
    CROSS_PROVIDER_FALLBACK_POLICY,
    get_cross_provider_tier,
    get_fallback_tier,
)
from model_router_registry import FALLBACK_ROUTE
from utils.free_model_backend import DEFAULT_FREE_MODEL

# 降级链统一捕获集。注意 CancelledError 属 BaseException，必须继续向上传播
# （tests/test_fallback_chain_mixin.py::test_cancelled_fallback_call_propagates）。
_CHAIN_ERRORS = (RuntimeError, OSError, KeyError, ValueError, LLMError,
                 _openai_mod.APIError)


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

    def _classify_chain_error(self, err: Exception) -> object:
        """降级链内的错误分类（非致命）。

        分类器缺失或自身抛错都不能中断降级链——生产里 ModelRouter 一定持有
        _error_classifier，但不少单测用 ModelRouter.__new__ 构造最小实例，
        旧实现只在「所有降级目标都被跳过」的路径上才会碰到它。降级到同家兜底档
        后这条路径变成常态，故这里必须容错。
        """
        try:
            return self._error_classifier.classify(err)
        except Exception:
            logger.warning("router.fallback_error_classify_failed", exc_info=True)
            return SimpleNamespace(
                reason=SimpleNamespace(value="unknown"),
                action=SimpleNamespace(value="unknown"),
            )

    # ── 降级标记（严重-2 + 轻微-15，2026-11-08）────────────────────
    # 免费 9B 车道以前是「静默接住」：用户只看到「能用但变差」，前端与
    # conversation_logs.model_used 都没有痕迹。现在每一级降级都会在 router 上
    # 留下结构化标记，由前端提示（/models/routes + 流式事件）与 model_used
    # （get_model_used_for_log）消费。
    def _mark_degraded(self, kind: str, *, original_provider: str, original_model: str,
                       to_provider: str, to_model: str, source: str = "",
                       detail: str = "") -> dict:
        degrade = {
            "degraded": True,
            "kind": kind,
            "from_provider": original_provider,
            "from_model": original_model,
            "to_provider": to_provider,
            "to_model": to_model,
            "source": source,
            "detail": detail,
            "ts": time.time(),
        }
        self._last_degrade = degrade
        logger.warning("router.degraded kind={} from={}/{} to={}/{} source={} detail={}",
                       kind, original_provider, original_model,
                       to_provider, to_model, source, detail)
        return degrade

    def clear_last_degrade(self) -> None:
        """清空降级标记（新一轮请求开始前调用，避免跨轮串味）。"""
        self._last_degrade = None

    def get_last_degrade(self) -> dict | None:
        degrade = getattr(self, "_last_degrade", None)
        return dict(degrade) if isinstance(degrade, dict) else None

    def consume_last_degrade(self) -> dict | None:
        degrade = self.get_last_degrade()
        self.clear_last_degrade()
        return degrade

    def get_model_used_for_log(self) -> str:
        """落库 conversation_logs.model_used 用：降级时记真实模型 + 降级标记。

        未降级时保持旧语义（返回当前主 chat 模型 id），向后兼容。
        """
        degrade = self.consume_last_degrade()
        if degrade:
            return "{}/{} (degraded:{})".format(
                degrade.get("to_provider", ""), degrade.get("to_model", ""),
                degrade.get("kind", ""))
        try:
            return self.get_current_chat_model().get("model_id", "")
        except (AttributeError, KeyError, TypeError):
            return ""

    async def _try_fallback_chain(self, e: Exception, task_type: str,
                                  messages: list[dict], temperature: float,
                                  stream: bool, tools: list[dict] | None,
                                  tool_choice: str | None, timeout: int,
                                  user_openid: str, session_id: str,
                                  extra_headers: dict | None,
                                  original_max_tokens: int | None = None) -> str | object | None:
        """多级 fallback：同家兜底档 → 跨家白名单（开关）→ Agnes → 自定义 provider
        → 紧急免费车道。全部失败返回 None。

        每一级降级前都会检查目标客户端是否已配置（有 API key 且有 base_url），
        未配置的目标会被跳过，避免向未初始化的客户端发起无意义调用。

        P0 修复（Task 1.3）：透传 original_max_tokens，避免 fallback 把 max_tokens 压到 1000。
        根因：原实现 fallback_config.get("max_tokens", 1000) 会把 Web UI 的 32768 压到 1000，
              起点太小 → 截断续写翻倍序列 1000→2000→...→128000 需 7 次递归。
        修复：fallback 时取 max(original_max_tokens, fallback_default)。
        """
        # 治本修复（2026-08-05 用户"治标不治本"反馈）：timeout 错误跳过整个 fallback 链。
        # 根因：服务端强制 thinking 时正常响应就要 6-7s，read timeout 触发后再调同 provider
        #   必然也慢 → 双倍延迟。timeout 意味着服务端慢，同 provider fallback 纯叠加延迟。
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

        _is_content_filter = "content_filter" in str(e) or "content_policy" in str(e)
        _original_provider = ""
        _original_model = ""
        try:
            # Task 6: 通过 registry 快照读取，避免降级链污染全局 ROUTE_TABLE
            _orig_entry = self._registry.get_task(task_type) or {}
            _original_provider = _orig_entry.get("client", _CFG_DEFAULT_PROVIDER)
            _original_model = _orig_entry.get("model", "")
        except Exception:
            logger.warning("router.fallback_original_provider_lookup_failed", exc_info=True)

        _tier_attempted = False

        # ── 1. 同家兜底档（task × 当前 provider，严重-2 核心）────────────
        # 矩阵由 model_router_config.build_fallback_matrix 从 provider_metadata.json
        # 派生：agnes 取路由表里已存在的 chat_agnes（现网行为逐字保留），
        # deepseek / mimo / siliconflow 等取各自的 default_model。
        _tier = get_fallback_tier(task_type, _original_provider)
        if _tier and _tier.get("model"):
            _tier_provider = _tier.get("client") or _original_provider
            _tier_task = _tier.get("task") or f"{task_type}_{_tier_provider}"
            if _is_content_filter and _tier_provider == _original_provider:
                # 同 provider 的兜底档会再次触发 content_filter，浪费一次调用
                logger.warning("router.fallback_skip_same_provider",
                               original_task=task_type, fallback_task=_tier_task,
                               reason="content_filter: same provider will filter again")
            elif self._is_client_configured(_tier_provider):
                _tier_attempted = True
                logger.warning("router.fallback",
                               original_task=task_type, fallback_task=_tier_task,
                               tier_source=_tier.get("source", ""),
                               error=f"{type(e).__name__}: {e}")
                try:
                    _tier_tools = self._filter_tools_for_model(tools, _tier.get("model", ""))
                    # P0 修复：透传 original_max_tokens，避免被档位默认值压缩
                    _tier_max_tokens = _tier.get("max_tokens", 1000)
                    if original_max_tokens:
                        _tier_max_tokens = max(original_max_tokens, _tier_max_tokens)
                    _tier_result = await self._route_with_retry(
                        _tier_task, _tier, messages, temperature,
                        _tier_max_tokens, stream,
                        _tier_tools, tool_choice, timeout, user_openid, session_id,
                        extra_headers=extra_headers,
                    )
                    self._mark_degraded(
                        "same_provider_tier",
                        original_provider=_original_provider,
                        original_model=_original_model,
                        to_provider=_tier_provider,
                        to_model=_tier.get("model", ""),
                        source=_tier.get("source", ""),
                        detail=f"{task_type}->{_tier_task}")
                    return _tier_result
                except _CHAIN_ERRORS as tier_err:
                    classified = self._classify_chain_error(tier_err)
                    logger.error("router.fallback_failed",
                                 fallback_task=_tier_task,
                                 reason=classified.reason.value,
                                 action=classified.action.value,
                                 error=f"{type(tier_err).__name__}: {tier_err}")
            else:
                logger.info("router.fallback_skip_unconfigured",
                            fallback_task=_tier_task, fallback_provider=_tier_provider)

        # ── 1b. 兼容路径：矩阵没有该 task 的同家档时，仍走旧 task 链 ──────
        # FALLBACK_ROUTE 只覆盖声明过降级链的 task（当前仅 chat）；其余 task
        # 保持「原 provider 内 task 降级」的旧行为逐字不变。
        if not _tier:
            fallback_type = FALLBACK_ROUTE.get(task_type)
            while fallback_type:
                # Task 6: 用 registry 快照（深拷贝），降级期间修改不影响全局 ROUTE_TABLE
                fallback_config = self._registry.snapshot_task(fallback_type)
                fallback_provider = (fallback_config.get("client", _CFG_DEFAULT_PROVIDER)
                                     if fallback_config else _CFG_DEFAULT_PROVIDER)
                # 用户硬约束（2026-08-04）：禁止自动切换模型/provider。
                # 跨 provider fallback 只会叠加延迟并违背用户意图，故全部跳过。
                if fallback_provider != _original_provider:
                    logger.info("router.fallback_skip_cross_provider",
                                original_task=task_type, fallback_task=fallback_type,
                                original_provider=_original_provider,
                                fallback_provider=fallback_provider,
                                reason="user_disabled_cross_provider_fallback")
                    fallback_type = FALLBACK_ROUTE.get(fallback_type)
                    continue
                if _is_content_filter and fallback_provider == _original_provider:
                    logger.warning("router.fallback_skip_same_provider",
                                   original_task=task_type, fallback_task=fallback_type,
                                   reason="content_filter: same provider will filter again")
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
                    fallback_tools = self._filter_tools_for_model(
                        tools, fallback_config.get("model", ""))
                    _fallback_max_tokens = fallback_config.get("max_tokens", 1000)
                    if original_max_tokens:
                        _fallback_max_tokens = max(original_max_tokens, _fallback_max_tokens)
                    _legacy_result = await self._route_with_retry(
                        fallback_type, fallback_config, messages, temperature,
                        _fallback_max_tokens, stream,
                        fallback_tools, tool_choice, timeout, user_openid, session_id,
                        extra_headers=extra_headers,
                    )
                    self._mark_degraded(
                        "same_provider_task_chain",
                        original_provider=_original_provider,
                        original_model=_original_model,
                        to_provider=fallback_config.get("client", _CFG_DEFAULT_PROVIDER),
                        to_model=fallback_config.get("model", ""),
                        source="fallback_route",
                        detail=f"{task_type}->{fallback_type}")
                    return _legacy_result
                except _CHAIN_ERRORS as fb_err:
                    classified = self._classify_chain_error(fb_err)
                    logger.error("router.fallback_failed",
                                 fallback_task=fallback_type,
                                 reason=classified.reason.value,
                                 action=classified.action.value,
                                 error=f"{type(fb_err).__name__}: {fb_err}")

        # ── 2. 跨家白名单（仅当 CROSS_PROVIDER_FALLBACK_POLICY 打开）─────
        # 默认关闭：保持 2026-08-04「禁止自动跨家切换」。_CROSS_PROVIDER_MAP 是一张
        # 已声明但默认不生效的映射，开关打开才消费。
        if CROSS_PROVIDER_FALLBACK_POLICY:
            _cross = get_cross_provider_tier(_original_provider)
            if _cross:
                _cross_provider, _cross_model = _cross
                if self._is_client_configured(_cross_provider):
                    _cross_task = f"{task_type}_{_cross_provider}"
                    logger.warning("router.cross_provider_fallback",
                                   original_task=task_type, fallback_task=_cross_task,
                                   original_provider=_original_provider,
                                   fallback_provider=_cross_provider,
                                   reason="cross_provider_fallback_policy_enabled")
                    try:
                        _cross_tools = self._filter_tools_for_model(tools, _cross_model)
                        _cross_max_tokens = original_max_tokens or 1000
                        _cross_cfg = {"model": _cross_model, "client": _cross_provider,
                                      "max_tokens": _cross_max_tokens}
                        _cross_result = await self._route_with_retry(
                            _cross_task, _cross_cfg, messages, temperature,
                            _cross_max_tokens, stream,
                            _cross_tools, tool_choice, timeout, user_openid, session_id,
                            extra_headers=extra_headers,
                        )
                        self._mark_degraded(
                            "cross_provider_whitelist",
                            original_provider=_original_provider,
                            original_model=_original_model,
                            to_provider=_cross_provider, to_model=_cross_model,
                            source="cross_provider_map",
                            detail=f"{task_type}->{_cross_task}")
                        return _cross_result
                    except _CHAIN_ERRORS as cross_err:
                        classified = self._classify_chain_error(cross_err)
                        logger.error("router.cross_provider_fallback_failed",
                                     fallback_provider=_cross_provider,
                                     reason=classified.reason.value,
                                     action=classified.action.value,
                                     error=f"{type(cross_err).__name__}: {cross_err}")
                else:
                    logger.info("router.cross_provider_skip_unconfigured",
                                fallback_provider=_cross_provider)
        else:
            logger.debug("router.cross_provider_fallback_disabled",
                         original_provider=_original_provider,
                         reason="CROSS_PROVIDER_FALLBACK_POLICY=false (default)")

        # ── 3. Agnes 最终兜底（原 provider 本就是 agnes 时的同 provider 降级）──
        # 矩阵已覆盖「chat × agnes」时这一级不会重复执行（_tier_attempted 已为
        # True），仅在矩阵档被 content_filter 跳过的场景保留旧语义。
        if (_original_provider == "agnes" and task_type not in ("chat_agnes",)
                and not _tier_attempted and self._is_client_configured("agnes")):
            try:
                # Task 6: 用 registry 快照读取 chat_agnes，避免污染全局
                agnes_config = self._registry.snapshot_task("chat_agnes")
                if agnes_config:
                    logger.warning("router.agnes_fallback", original_task=task_type)
                    agnes_tools = self._filter_tools_for_model(
                        tools, agnes_config.get("model", ""))
                    # P0 修复：透传 original_max_tokens
                    _agnes_max_tokens = agnes_config.get("max_tokens", 2000)
                    if original_max_tokens:
                        _agnes_max_tokens = max(original_max_tokens, _agnes_max_tokens)
                    _agnes_result = await self._route_with_retry(
                        "chat_agnes", agnes_config, messages, temperature,
                        _agnes_max_tokens, stream,
                        agnes_tools, tool_choice, timeout, user_openid, session_id,
                        extra_headers=extra_headers,
                    )
                    self._mark_degraded(
                        "same_provider_tier",
                        original_provider=_original_provider,
                        original_model=_original_model,
                        to_provider="agnes",
                        to_model=agnes_config.get("model", ""),
                        source="agnes_final", detail=f"{task_type}->chat_agnes")
                    return _agnes_result
            except _CHAIN_ERRORS as agnes_err:
                classified = self._classify_chain_error(agnes_err)
                logger.error("router.agnes_fallback_failed",
                             reason=classified.reason.value,
                             action=classified.action.value,
                             error=f"{type(agnes_err).__name__}: {agnes_err}")

        # ── 4. 已注册的自定义 provider（SiliconFlow/OpenRouter/ModelScope 等）──
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
                    _cp_result = await self._route_with_retry(
                        f"chat_{cp_name}", cp_config, messages, temperature,
                        _cp_max_tokens, stream, cp_tools, tool_choice, timeout,
                        user_openid, session_id,
                        extra_headers=extra_headers,
                    )
                    self._mark_degraded(
                        "same_provider_tier",
                        original_provider=_original_provider,
                        original_model=_original_model,
                        to_provider=cp_name, to_model=cp_model,
                        source="custom_provider",
                        detail=f"{task_type}->chat_{cp_name}")
                    return _cp_result
                except _CHAIN_ERRORS as cp_err:
                    classified = self._classify_chain_error(cp_err)
                    logger.error("router.custom_provider_fallback_failed",
                                 provider=cp_name,
                                 reason=classified.reason.value,
                                 action=classified.action.value,
                                 error=f"{type(cp_err).__name__}: {cp_err}")
                    continue

        # ── 5. 紧急免费车道（2026-09-24 用户决策，env: FREE_EMERGENCY_FALLBACK=false 关闭）──
        # 2026-11-08 收紧（严重-2）：只在**同家兜底档与跨家白名单都不可用/未生效**时
        # 才进入——旧实现是「任何 chat 失败都进」，用户在无痕迹的情况下被静默降质。
        # 命中时必定写降级标记（前端提示 + model_used 都能看到）。
        # 背景：chat → chat_agnes 同为 agnes，agnes 免费档 429 时两条路一起死。
        # 只对 chat 类任务生效；失败被捕获，行为与此前一致（返回 None）。
        if (
            FREE_EMERGENCY_FALLBACK
            and task_type.startswith("chat")
            and self._is_client_configured("siliconflow")
        ):
            try:
                # 9B 小模型 + 32K 窗口：输出预算夹在 [1024, 4096]，
                # 避免把主力路由的大 max_tokens（65535）原样喂给小模型爆窗
                _free_max_tokens = min(max(original_max_tokens or 0, 1024), 4096)
                free_config = {
                    "model": DEFAULT_FREE_MODEL,
                    "max_tokens": _free_max_tokens,
                    "client": "siliconflow",
                }
                logger.warning(
                    "router.emergency_free_fallback",
                    original_task=task_type,
                    original_provider=_original_provider,
                    model=DEFAULT_FREE_MODEL,
                    max_tokens=_free_max_tokens,
                    note="same-provider tier and cross-provider whitelist both "
                         "unavailable; using free model as last resort",
                )
                free_tools = self._filter_tools_for_model(tools, DEFAULT_FREE_MODEL)
                _free_result = await self._route_with_retry(
                    "chat_free", free_config, messages, temperature,
                    _free_max_tokens, stream, free_tools, tool_choice, timeout,
                    user_openid, session_id, extra_headers=extra_headers,
                )
                self._mark_degraded(
                    "free_lane",
                    original_provider=_original_provider,
                    original_model=_original_model,
                    to_provider="siliconflow",
                    to_model=DEFAULT_FREE_MODEL,
                    source="free_emergency_fallback",
                    detail="same-provider tier and cross-provider whitelist unavailable")
                return _free_result
            except _CHAIN_ERRORS as free_err:
                classified = self._classify_chain_error(free_err)
                logger.error("router.emergency_free_fallback_failed",
                             reason=classified.reason.value,
                             action=classified.action.value,
                             error=f"{type(free_err).__name__}: {free_err}")
        return None
