"""TDD 测试：agnes provider 路由 bug 修复。

Bug 现象：用户在 WebUI 设置 agnes 作为聊天模型，但 LLM 实际回复
自称 "mimo-v2.5"，说明调用仍走 mimo 客户端。

根因：
1. ModelRouter.__init__ 只在启动时初始化 _agnes_client。若启动时
   AGNES_API_KEY 未设置（用户后续通过 WebUI 添加 agnes provider），
   _agnes_client 永远为 None。
2. _select_client_for_provider("agnes") 当 _agnes_client 为 None 时，
   因 elif 条件 `provider not in ("mimo", "agnes")` 把 agnes 排除在外，
   直接回退到 self._client（mimo 客户端），导致 agnes 调用静默走 mimo。
3. _is_client_configured("agnes") 只检查 _agnes_client，不检查
   _custom_clients["agnes"]，导致 fallback 链跳过 agnes。

修复：agnes provider 在 _agnes_client 为 None 时回退到
_custom_clients["agnes"]（用户通过 WebUI 注册的 agnes 客户端），
仍为 None 时抛 LLMError，绝不静默回退到 mimo。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.app_exception import LLMError


def _build_router_with_agnes_in_custom_clients_only():
    """构造一个 ModelRouter 实例，模拟"用户通过 WebUI 添加 agnes provider"场景。

    场景：
    - 启动时未设置 AGNES_API_KEY → _agnes_client = None
    - 用户后续通过 WebUI 添加 agnes → agnes 客户端注册到 _custom_clients["agnes"]
    - mimo 客户端正常存在（self._client）
    """
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._client = MagicMock(name="mimo_client")  # mimo 客户端存在
    router._agnes_client = None  # 启动时未初始化（关键 bug 触发条件）
    router._custom_clients = {"agnes": MagicMock(name="agnes_custom_client")}
    router._credential_locks = {}

    return router


# ─────────────────────────────────────────────────────────────
# Bug #1: _select_client_for_provider("agnes") 错误回退到 mimo
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_select_client_for_agnes_uses_custom_clients_when_agnes_client_is_none():
    """agnes provider 在 _agnes_client 为 None 时应回退到 _custom_clients["agnes"]。

    回归测试：旧实现因 elif 条件 `provider not in ("mimo", "agnes")` 排除了 agnes，
    导致 agnes provider 在 _agnes_client 为 None 时静默回退到 mimo 客户端，
    用户设置 agnes 后实际调用仍是 mimo。
    """
    router = _build_router_with_agnes_in_custom_clients_only()

    client = await router._select_client_for_provider("agnes")

    # 必须使用 _custom_clients["agnes"]，而不是 mimo 客户端
    assert client is router._custom_clients["agnes"], (
        "agnes provider 在 _agnes_client 为 None 时必须回退到 _custom_clients['agnes']，"
        "而不是静默使用 mimo 客户端（会导致用户设置 agnes 后实际走 mimo）"
    )
    assert client is not router._client, (
        "agnes provider 不应回退到 mimo 客户端"
    )


@pytest.mark.asyncio
async def test_select_client_for_agnes_raises_when_no_client_available():
    """agnes provider 在既无 _agnes_client 也无 _custom_clients['agnes'] 时应抛 LLMError。

    回归测试：旧实现会静默回退到 mimo 客户端，让用户误以为 agnes 生效了。
    修复后必须抛 LLMError，明确告知用户 agnes 未配置。
    """
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._client = MagicMock(name="mimo_client")
    router._agnes_client = None
    router._custom_clients = {}  # 没有 agnes 客户端
    router._credential_locks = {}

    with pytest.raises(LLMError) as exc_info:
        await router._select_client_for_provider("agnes")

    # 错误信息应明确指出 agnes 未配置
    assert "agnes" in str(exc_info.value).lower(), (
        "错误信息应明确指出 agnes provider 未配置"
    )


@pytest.mark.asyncio
async def test_select_client_for_agnes_uses_agnes_client_when_available():
    """agnes provider 在 _agnes_client 存在时应优先使用它（不破坏正常场景）。"""
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._client = MagicMock(name="mimo_client")
    agnes_client = MagicMock(name="agnes_client")
    router._agnes_client = agnes_client
    router._custom_clients = {"agnes": MagicMock(name="agnes_custom_client")}
    router._credential_locks = {}

    client = await router._select_client_for_provider("agnes")

    # 优先使用 _agnes_client
    assert client is agnes_client, (
        "_agnes_client 存在时应优先使用它，不应使用 _custom_clients"
    )


@pytest.mark.asyncio
async def test_select_client_for_mimo_unchanged():
    """mimo provider 路由不应受修复影响（不破坏正常场景）。"""
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    mimo_client = MagicMock(name="mimo_client")
    router._client = mimo_client
    router._agnes_client = None
    router._custom_clients = {}
    router._credential_locks = {}

    client = await router._select_client_for_provider("mimo")

    assert client is mimo_client


# ─────────────────────────────────────────────────────────────
# Bug #2: _is_client_configured("agnes") 不检查 _custom_clients
# ─────────────────────────────────────────────────────────────


def test_is_client_configured_agnes_checks_custom_clients():
    """_is_client_configured('agnes') 在 _agnes_client 为 None 但
    _custom_clients['agnes'] 存在时应返回 True。

    回归测试：旧实现只检查 _agnes_client，导致 fallback 链中
    _is_client_configured('agnes') 返回 False，agnes fallback 被跳过。
    """
    router = _build_router_with_agnes_in_custom_clients_only()

    assert router._is_client_configured("agnes") is True, (
        "_is_client_configured('agnes') 应同时检查 _agnes_client 和 _custom_clients['agnes']"
    )


def test_is_client_configured_agnes_returns_false_when_neither_available():
    """agnes 既无 _agnes_client 也无 _custom_clients['agnes'] 时应返回 False。"""
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._client = MagicMock()
    router._agnes_client = None
    router._custom_clients = {}

    assert router._is_client_configured("agnes") is False


def test_is_client_configured_agnes_returns_true_when_agnes_client_exists():
    """_agnes_client 存在时应返回 True（不破坏正常场景）。"""
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._client = MagicMock()
    router._agnes_client = MagicMock()
    router._custom_clients = {}

    assert router._is_client_configured("agnes") is True


# ─────────────────────────────────────────────────────────────
# Bug #3: 端到端验证 - agnes fallback 链不应跳过 _custom_clients['agnes']
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agnes_fallback_chain_uses_custom_clients_agnes():
    """fallback 链在 _agnes_client 为 None 但 _custom_clients['agnes'] 存在时
    应尝试 agnes fallback。

    回归测试：旧实现 _is_client_configured('agnes') 只检查 _agnes_client，
    返回 False 时整个 agnes fallback 分支被跳过。
    """
    router = _build_router_with_agnes_in_custom_clients_only()

    # 验证 _is_client_configured('agnes') 返回 True
    assert router._is_client_configured("agnes") is True
