"""model_router Phase 4（回退链 Mixin 抽出）结构契约测试。

背景：fallback_chat / _try_fallback_chain（FALLBACK_ROUTE → Agnes →
自定义 provider 的多级降级链）抽为 llm_gateway/fallback_chain.
FallbackChainMixin，方法体逐字节搬移。Phase 0 已为此铺路
（公开别名 fallback_chat + message_processor 走公开入口）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import model_router
from llm_gateway.fallback_chain import FallbackChainMixin


def test_mixin_imports_standalone():
    import importlib
    mod = importlib.import_module("llm_gateway.fallback_chain")
    assert hasattr(mod.FallbackChainMixin, "_try_fallback_chain")
    assert hasattr(mod.FallbackChainMixin, "fallback_chat")


def test_model_router_inherits_mixin():
    assert issubclass(model_router.ModelRouter, FallbackChainMixin)
    # 未在 ModelRouter 体内重写：MRO 直接命中 Mixin 实现
    assert model_router.ModelRouter.fallback_chat is FallbackChainMixin.fallback_chat
    assert (model_router.ModelRouter._try_fallback_chain
            is FallbackChainMixin._try_fallback_chain)


def test_mixin_does_not_import_model_router():
    import llm_gateway.fallback_chain as mod
    assert "model_router" not in getattr(mod, "__dict__", {})


@pytest.mark.asyncio
async def test_fallback_chat_delegates_to_chain():
    """公开别名 fallback_chat 透传 _try_fallback_chain（Phase 0 契约保持）"""
    calls: list[tuple] = []

    class FakeRouter(FallbackChainMixin):
        async def _try_fallback_chain(self, e, task_type, messages, temperature,
                                      stream, tools, tool_choice, timeout,
                                      user_openid, session_id, extra_headers,
                                      original_max_tokens=None):
            calls.append((task_type, original_max_tokens))
            return "fb-result"

    r = FakeRouter()
    out = await r.fallback_chat(ValueError("x"), "chat", [], 0.7, False,
                                None, None, 30, "u", "s", None,
                                original_max_tokens=32768)
    assert out == "fb-result"
    assert calls == [("chat", 32768)]


@pytest.mark.asyncio
async def test_timeout_error_skips_chain():
    """timeout 错误直接返回 None（不做同 provider 双倍等待）——搬移后行为不变"""

    class FakeClassifier:
        def __init__(self, exc):
            self._exc = exc

        def classify(self, e):
            reason = type("R", (), {"value": "timeout"})()
            return type("C", (), {"reason": reason})()

    class FakeRouter(FallbackChainMixin):
        def __init__(self, exc):
            self._error_classifier = FakeClassifier(exc)
            self._registry = None

    router = FakeRouter(ValueError("read timeout"))
    result = await router._try_fallback_chain(
        ValueError("read timeout"), "chat", [], 0.7, False,
        None, None, 30, "u", "s", None)
    assert result is None
