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


# ─────────────────────────────────────────────────────────────
# Bug #4: set_chat_model("agnes") 后 chat_flash 被错误重置成 mimo，
#         导致 agnes 主路由失败时 fallback 链跳到 mimo
# ─────────────────────────────────────────────────────────────


def test_set_chat_model_agnes_should_not_reset_chat_flash_to_mimo(monkeypatch):
    """set_chat_model('agnes', ...) 后 chat_flash 应跟随主 provider (agnes)，
    不应被 _CROSS_PROVIDER_MAP 重置成 mimo。

    Bug 链：
    1. 用户设置 agnes → set_chat_model 把 chat_pro/chat_flash 同步成 agnes
    2. 但 _CROSS_PROVIDER_MAP 又把 chat_flash 重置成 mimo（"跨 provider 降级"）
    3. 用户发长消息 → task_type='chat_pro' (agnes)
    4. agnes 返回空 content（agnes-2.0-flash 已知问题）→ 抛 RuntimeError
    5. fallback chain: chat_pro → chat_flash (mimo) → 返回 mimo 回复
    6. 用户看到 LLM 自称 mimo-v2.5

    修复：chat_flash 应跟随主 provider，跨 provider 降级作为最后手段。
    """
    from model_router import ModelRouter, ROUTE_TABLE

    # 备份 ROUTE_TABLE 以便测试后恢复
    _backup = {k: dict(v) for k, v in ROUTE_TABLE.items()}
    try:
        router = ModelRouter.__new__(ModelRouter)
        router._client = MagicMock(name="mimo_client")
        router._agnes_client = MagicMock(name="agnes_client")
        router._custom_clients = {}
        router._credential_locks = {}
        router._current_chat_model = None
        router.TASK_TIMEOUTS = {"chat": 30}

        # mock config_service 避免真实写入
        _fake_cfg = MagicMock()
        monkeypatch.setattr(
            "web.config_service.get_config_service",
            lambda: _fake_cfg,
        )

        # 模拟 set_chat_model("agnes", "agnes-2.0-flash")
        router.set_chat_model("agnes", "agnes-2.0-flash")

        # chat_pro 应该是 agnes
        assert ROUTE_TABLE["chat_pro"]["client"] == "agnes", \
            "chat_pro 应跟随主 provider (agnes)"
        # chat_flash 也应该是 agnes，不应被 _CROSS_PROVIDER_MAP 重置成 mimo
        assert ROUTE_TABLE["chat_flash"]["client"] == "agnes", \
            "chat_flash 应跟随主 provider (agnes)，不应被重置成 mimo"
        assert ROUTE_TABLE["chat_flash"]["model"] == "agnes-2.0-flash", \
            "chat_flash 的 model 应跟随主 provider"
    finally:
        # 恢复 ROUTE_TABLE
        for k, v in _backup.items():
            ROUTE_TABLE[k] = v


def test_set_chat_model_mimo_should_not_reset_chat_flash_to_agnes(monkeypatch):
    """对称测试：set_chat_model('mimo', ...) 后 chat_flash 应跟随 mimo。

    避免 _CROSS_PROVIDER_MAP 把 chat_flash 重置成 agnes。
    """
    from model_router import ModelRouter, ROUTE_TABLE

    _backup = {k: dict(v) for k, v in ROUTE_TABLE.items()}
    try:
        router = ModelRouter.__new__(ModelRouter)
        router._client = MagicMock(name="mimo_client")
        router._agnes_client = MagicMock(name="agnes_client")
        router._custom_clients = {}
        router._credential_locks = {}
        router._current_chat_model = None
        router.TASK_TIMEOUTS = {"chat": 30}

        _fake_cfg = MagicMock()
        monkeypatch.setattr(
            "web.config_service.get_config_service",
            lambda: _fake_cfg,
        )

        router.set_chat_model("mimo", "mimo-v2.5")

        assert ROUTE_TABLE["chat_pro"]["client"] == "mimo"
        assert ROUTE_TABLE["chat_flash"]["client"] == "mimo", \
            "chat_flash 应跟随主 provider (mimo)，不应被重置成 agnes"
    finally:
        for k, v in _backup.items():
            ROUTE_TABLE[k] = v
