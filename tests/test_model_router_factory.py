"""model_router Phase 0 回归：get_model_router 单例工厂 + fallback_chat 公开别名。

背景：core/dream_engine_v2 与 emotion/emotion_llm 引用不存在的
get_model_router()（潜伏 ImportError 死路径）；message_processor 此前白盒调用
router._try_fallback_chain（私有跨模块依赖）。Phase 0 补齐工厂并公开别名。
"""
import pytest

from model_router import get_model_router, ModelRouter


def test_get_model_router_returns_singleton():
    """get_model_router 必须返回同一实例（懒加载单例）。"""
    a = get_model_router()
    b = get_model_router()
    assert isinstance(a, ModelRouter)
    assert a is b


def test_importable_from_dead_path_call_sites():
    """dream_engine_v2 / emotion_llm 的 import 路径不再 ImportError。"""
    import core.dream_engine_v2
    import emotion.emotion_llm
    assert callable(getattr(core.dream_engine_v2, "_get_global_router", None))
    assert callable(getattr(emotion.emotion_llm, "_get_global_router", None))


@pytest.mark.asyncio
async def test_fallback_chat_delegates_to_private_impl():
    """公开别名 fallback_chat 委托 _try_fallback_chain（参数透传）。"""
    router = ModelRouter.__new__(ModelRouter)
    captured = {}

    async def _fake(e, task_type, messages, temperature, stream, tools,
                    tool_choice, timeout, user_openid, session_id,
                    extra_headers, original_max_tokens=None):
        captured.update(
            e=e, task_type=task_type, stream=stream, timeout=timeout,
            original_max_tokens=original_max_tokens)
        return "fb-ok"

    router._try_fallback_chain = _fake
    result = await router.fallback_chat(
        ValueError("x"), "chat", [], 0.7, False, None, None, 30,
        "u1", "s1", None, original_max_tokens=2048)
    assert result == "fb-ok"
    assert captured["task_type"] == "chat"
    assert captured["stream"] is False
    assert captured["timeout"] == 30
    assert captured["original_max_tokens"] == 2048
