"""验证 ModelRouter.route 已把 LLMError 纳入降级异常集合。

背景：_select_client_for_provider 在客户端无法恢复时抛 LLMError
（继承 AppException，不属于 RuntimeError/OSError/ValueError）。
原 route 的 except 集合漏掉 LLMError，导致主 provider 客户端未初始化时
异常直接抛给上层，已配置的 Agnes/自定义 provider 降级链完全不会被触发。
本测试补回归保护：确保 LLMError 能进入 except 分支并触发 _try_fallback_chain。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.app_exception import LLMError
from model_router import ModelRouter


def _make_router() -> ModelRouter:
    """构造一个跳过 __init__ 的 ModelRouter 实例。

    __init__ 会读环境变量、做 SSRF 校验、注册 credential pool 等，
    依赖外部状态且与本测试无关。route() 仅访问 _cache_stats 与
    _apply_caching_headers（静态方法），故只补这两个即可。
    """
    from model_router import ModelRouteRegistry, ROUTE_TABLE
    router = ModelRouter.__new__(ModelRouter)
    router._cache_stats = {"total_calls": 0, "hit_tokens": 0, "miss_tokens": 0}
    # route() 走 registry.get_task_ref，需要 _registry 已初始化
    router._registry = ModelRouteRegistry(ROUTE_TABLE)
    return router


async def test_llm_error_triggers_fallback_chain():
    """_route_with_retry 抛 LLMError 时应触发 _try_fallback_chain 并返回其结果。

    这是核心回归点：若 route 的 except 集合漏掉 LLMError，
    异常会直接穿透 route → 调用方收不到降级结果。
    """
    router = _make_router()

    # 模拟主 provider 客户端未初始化场景
    router._route_with_retry = AsyncMock(side_effect=LLMError("客户端未初始化"))
    # 降级链命中 Agnes/自定义 provider
    router._try_fallback_chain = AsyncMock(return_value="fb")

    # 显式传 timeout 避免 route 访问 self.TASK_TIMEOUTS（__init__ 跳过未初始化）
    result = await router.route(
        "chat", [{"role": "user", "content": "hi"}], timeout=30
    )

    assert result == "fb"
    # 降级链应被调用且仅一次
    router._try_fallback_chain.assert_called_once()
    # 触发降级的原始异常必须是 LLMError，证明它被纳入捕获集合
    assert isinstance(router._try_fallback_chain.call_args.args[0], LLMError)


async def test_llm_error_fallback_exhausted_raises_llm_error():
    """所有降级目标均不可用时 route 应抛 LLMError 且消息明确。

    D12 约束：兜底耗尽时抛明确异常而非裸 re-raise，
    避免上层因原始错误信息不明确而无法判断降级已耗尽。
    """
    router = _make_router()

    router._route_with_retry = AsyncMock(side_effect=LLMError("客户端未初始化"))
    # 所有降级目标不可用 → 返回 None
    router._try_fallback_chain = AsyncMock(return_value=None)

    with pytest.raises(LLMError) as exc_info:
        await router.route(
            "chat", [{"role": "user", "content": "hi"}], timeout=30
        )

    assert "所有降级目标均不可用" in str(exc_info.value)
