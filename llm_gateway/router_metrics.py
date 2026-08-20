"""ModelRouter 的成本统计/缓存统计 Mixin — 自 model_router.py 拆分（Phase 5）。

内容：_calc_cost / _record_usage / _record_stream_usage / _flush_cost_buffer /
flush_costs / close（成本记录与客户端关闭），以及 _track_cache /
_check_cache_health / get_cache_stats / _is_small_model /
_filter_tools_for_model / pop_reasoning_content（缓存统计与模型工具过滤）。
方法体自 model_router.py 逐字节搬移，仅缩进调整。

兼容契约（tests/test_cost_tracking_mixin.py）：
    - 本模块不得 import model_router（防循环依赖）
    - ModelRouter(CostTrackingMixin) 继承保持 self 语义，
      `router.flush_costs()` / `router.close()` / `router.get_cache_stats()` /
      `router.pop_reasoning_content()` 等 agent_core/slash_commands 调用方
      不受影响；close 内对 list_custom_clients/clear_custom_clients 的
      self.* 调用经 MRO 解析回 ModelRouter 原方法
"""
from __future__ import annotations

import contextvars
import time
from typing import Any

import openai as _openai_mod  # 用于 openai.APIError 异常捕获
from loguru import logger

from model_router_config import MIMO_PRICING, PROVIDER_PRICING
from transports.agnes_transport import close_agnes_shared_client

# 请求级隔离的 reasoning_content，避免并发请求间共享状态
_reasoning_content_var = contextvars.ContextVar('reasoning_content', default='')


class CostTrackingMixin:
    """成本统计与缓存统计（ModelRouter 组合此 Mixin）。"""

    def _calc_cost(self, prompt_tokens: int, completion_tokens: int,
                   cache_hit_tokens: int = 0, cache_miss_tokens: int = 0,
                   model: str = "", provider: str = "") -> float:
        cache_miss = cache_miss_tokens if cache_miss_tokens > 0 else (prompt_tokens - cache_hit_tokens)
        if cache_miss < 0:
            cache_miss = prompt_tokens
        # 按 provider 查定价表
        if provider == "mimo":
            pricing = MIMO_PRICING.get("pro") if "pro" in model else MIMO_PRICING.get("standard")
        else:
            pricing = PROVIDER_PRICING.get(provider, PROVIDER_PRICING["default"])
        input_cost = (cache_miss / 1_000_000) * pricing["input_per_m"]
        cache_cost = (cache_hit_tokens / 1_000_000) * pricing["cache_hit_per_m"]
        output_cost = (completion_tokens / 1_000_000) * pricing["output_per_m"]
        return input_cost + cache_cost + output_cost

    async def _record_usage(self, task_type: str, model: str, response: Any,
                             user_openid: str = "", session_id: str = "",
                             provider: str = "") -> None:
        try:
            usage = response.usage
            if not usage:
                return
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            cache_hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
            cache_miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
            cost = self._calc_cost(prompt_tokens, completion_tokens, cache_hit, cache_miss, model, provider)

            record = {
                "user_openid": user_openid,
                "session_id": session_id,
                "model": model,
                "task_type": task_type,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cache_hit_tokens": cache_hit,
                "cache_miss_tokens": cache_miss,
                "cost_usd": cost,
                "created_at": time.time(),
            }

            if self._analytics:
                self._cost_buffer.append(record)
                if len(self._cost_buffer) >= self._cost_flush_threshold:
                    await self._flush_cost_buffer()
            else:
                logger.debug("router.usage_no_db", task=task_type, cost=f"${cost:.6f}")
        except (OSError, KeyError, ValueError, TypeError) as e:
            logger.warning("router.usage_record_failed", error=str(e))

    async def _record_stream_usage(self, task_type: str, model: str, stream_response: Any,
                                    user_openid: str = "", session_id: str = "",
                                    provider: str = "") -> None:
        """流式调用结束后记录费用：聚合 chunk 的 usage（OpenAI 在最后一个 chunk 提供）。"""
        try:
            usage = getattr(stream_response, "usage", None)
            if not usage:
                # 部分 SDK 需要消费完流才能拿到 usage，这里尝试读取已关闭流的属性
                return
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            cache_hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
            cache_miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
            cost = self._calc_cost(prompt_tokens, completion_tokens, cache_hit, cache_miss, model, provider)
            record = {
                "user_openid": user_openid,
                "session_id": session_id,
                "model": model,
                "task_type": task_type,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cache_hit_tokens": cache_hit,
                "cache_miss_tokens": cache_miss,
                "cost_usd": cost,
                "created_at": time.time(),
            }
            if self._analytics:
                self._cost_buffer.append(record)
                if len(self._cost_buffer) >= self._cost_flush_threshold:
                    await self._flush_cost_buffer()
            else:
                logger.debug("router.stream_usage_no_db", task=task_type, cost=f"${cost:.6f}")
        except (OSError, KeyError, ValueError, TypeError) as e:
            logger.warning("router.stream_usage_record_failed", error=str(e))

    async def _flush_cost_buffer(self) -> None:
        if not self._cost_buffer or not self._analytics:
            return
        try:
            await self._analytics.batch_insert_api_usage(self._cost_buffer)
            count = len(self._cost_buffer)
            self._cost_buffer.clear()
            logger.debug("router.cost_flushed", count=count)
        except (OSError, KeyError, ValueError) as e:
            logger.warning("router.cost_flush_failed", error=str(e))

    async def flush_costs(self) -> None:
        await self._flush_cost_buffer()

    async def close(self) -> None:
        """关闭所有 AsyncOpenAI 客户端, 释放 TCP 连接.

        CodeRabbit 修复：注入共享 httpx client 的 agnes wrapper 不调用 ``.close()`` ——
        ``.close()`` 会连带关闭共享 httpx client，影响其他复用该 client 的实例。
        共享 agnes client 由 ``close_agnes_shared_client()`` 统一关闭；MiMo 与非 agnes
        自定义 provider 客户端（未注入共享 httpx，SDK 自建 client）独立 close。
        """
        if self._client is not None:
            try:
                await self._client.close()
            except (RuntimeError, OSError, _openai_mod.APIError):
                logger.debug("model_router.close_client_error", exc_info=True)
        self._client = None
        # 关闭自定义 provider 客户端（跳过 agnes：它复用共享 httpx client，由下方统一关闭）
        if hasattr(self, "_custom_clients"):
            for cp_name, cp_client in self.list_custom_clients():
                if cp_name == "agnes":
                    continue
                close_fn = getattr(cp_client, "close", None)
                if close_fn is None:
                    continue
                try:
                    await close_fn()
                except (RuntimeError, OSError, _openai_mod.APIError):
                    logger.debug("model_router.close_custom_client_error", exc_info=True)
            self.clear_custom_clients()
        # agnes 共享 httpx client：统一关闭一次（应用退出时调用，此时无在途请求）
        try:
            await close_agnes_shared_client()
        except (RuntimeError, OSError):
            logger.debug("model_router.close_agnes_shared_client_error", exc_info=True)
        self._agnes_client = None

    def _track_cache(self, response: Any) -> None:
        try:
            usage = response.usage
            if not usage:
                return
            # MiMo 格式：prompt_cache_hit_tokens / prompt_cache_miss_tokens
            mimo_hit = 0
            if hasattr(usage, "prompt_cache_hit_tokens"):
                mimo_hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
                self._cache_stats["hit_tokens"] += mimo_hit
                self._cache_stats["miss_tokens"] += getattr(usage, "prompt_cache_miss_tokens", 0) or 0

            # P6 Task 27.1: OpenAI 兼容格式 cached_tokens
            # 优先 prompt_tokens_details.cached_tokens（标准 OpenAI 协议），
            # 仅当其为 0 或缺失时才回退到顶层 cached_tokens（部分 provider 简化字段），
            # 避免同一缓存命中值被两个字段同时累加导致统计翻倍。
            cached_from_details = 0
            prompt_details = getattr(usage, "prompt_tokens_details", None)
            if prompt_details is not None:
                cached_from_details = getattr(prompt_details, "cached_tokens", 0) or 0
            cached_top = getattr(usage, "cached_tokens", 0) or 0
            cached_tokens = cached_from_details if cached_from_details > 0 else cached_top

            # 去重：若 MiMo 的 prompt_cache_hit_tokens 与 OpenAI 的 cached_tokens 同时存在，
            # 只累加一次（避免 hit_tokens 重复计数）。
            if cached_tokens > 0 and mimo_hit == 0:
                self._cache_stats["hit_tokens"] += cached_tokens
            # _cached_tokens_total 只累加一次（已通过 cached_tokens 去重）
            if cached_tokens > 0:
                self._cached_tokens_total += cached_tokens

            # P6 Task 27.2: 每 100 次请求输出一次缓存命中统计
            self._request_count += 1
            if self._request_count % 100 == 0:
                logger.info("prompt_cache.stats requests={} cached_tokens={}",
                            self._request_count, self._cached_tokens_total)
        except (KeyError, ValueError, OSError) as e:
            logger.debug("缓存统计追踪失败: {}", e)

    def _check_cache_health(self) -> None:
        now = time.time()
        if now - self._last_cache_warning < 300:
            return
        total = self._cache_stats["hit_tokens"] + self._cache_stats["miss_tokens"]
        if total > 10000:
            ratio = self._cache_stats["hit_tokens"] / total
            if ratio < 0.5:
                self._last_cache_warning = now
                logger.warning("router.cache_hit_low",
                               hit_ratio=f"{ratio:.1%}",
                               suggestion="考虑固定系统 prompt 前缀以提高缓存命中率")

    def get_cache_stats(self) -> dict:
        total = self._cache_stats["total_calls"]
        hit = self._cache_stats["hit_tokens"]
        miss = self._cache_stats["miss_tokens"]
        total_tokens = hit + miss
        return {
            "total_calls": total,
            "hit_tokens": hit,
            "miss_tokens": miss,
            "hit_ratio": round(hit / total_tokens, 3) if total_tokens > 0 else 0.0,
        }

    # 参数量 <= 14B 的小模型在接收大量工具定义时容易输出退化（乱码/JSON循环）
    _SMALL_MODEL_PATTERNS = (
        "7b", "8b", "4b", "3b", "1.5b", "1.8b", "0.5b",
        "mini", "tiny", "small",
    )

    def _is_small_model(self, model: str) -> bool:
        """判断是否为小模型（参数量 <= 14B），小模型不适合接收大量工具定义。"""
        model_lower = model.lower()
        # 明确的大模型标记
        for big in ("72b", "70b", "67b", "104b", "236b", "pro", "max", "plus", "large"):
            if big in model_lower:
                return False
        return any(small in model_lower for small in self._SMALL_MODEL_PATTERNS)

    def _filter_tools_for_model(self, tools: list[dict] | None, model: str) -> list[dict] | None:
        """检查工具列表与目标模型的兼容性，对小模型移除工具定义防止输出退化。

        根因：Qwen2.5-7B 等小模型在接收 30+ 个工具定义时，输出严重退化
        （循环输出 JSON 片段乱码），导致对话不可用。
        """
        if not tools:
            return tools

        # P0 修复：移除 agnes tools_may_not_be_supported 误告警
        # 根因：agnes-2.0-flash 实际支持工具调用（日志中 tool.calls_selected 多次成功），
        #       原告警每次 route 调用都触发，造成日志噪声 + 误导排查方向。
        #       工具兼容性实际由 _is_small_model + 工具调用结果兜底，无需提前告警。
        # 如需诊断工具发送情况，查看 router.tools_sent DEBUG 日志即可。

        # 小模型不发送工具定义，防止输出退化
        if self._is_small_model(model):
            logger.warning("router.tools_stripped_for_small_model model={} tool_count={}", model, len(tools))
            return None

        return tools

    def pop_reasoning_content(self) -> str | None:
        rc = _reasoning_content_var.get("")
        _reasoning_content_var.set("")
        return rc if rc else None