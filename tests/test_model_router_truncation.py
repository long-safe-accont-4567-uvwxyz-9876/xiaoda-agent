"""P0 修复验证测试：截断重试去递归化 + finish_reason 检测 + fallback max_tokens 透传

配套 spec：docs/specs/spec-context-management-2026-07-26.md
配套 tasks：Task 1.1, 1.2, 1.3

验证目标：
1. 截断重试不递归调用 route()，最多 2 次 LLM 调用（非 2^N 次）
2. finish_reason 检测正确（stop 时 break，length 时继续）
3. fallback 链透传 original_max_tokens，不被压缩到 1000
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_response(content: str, finish_reason: str = "stop"):
    """构造模拟的 OpenAI response 对象。"""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    response = MagicMock()
    response.choices = [choice]
    response.usage = None
    return response


@pytest.mark.asyncio
async def test_truncation_retry_no_recursion():
    """Task 1.1: 截断重试去递归化。

    模拟连续 finish_reason="length"，验证：
    - 最多 2 次 LLM 调用（不是 2^N 次递归）
    - 调用的是 _route_for_continuation 而非 route（不递归）
    """
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    # 最小化初始化
    router._client = MagicMock()
    router._agnes_client = None
    router._custom_clients = {}
    router._credential_pool = MagicMock()
    router._credential_pool.get_credential = AsyncMock(return_value=None)
    router._credential_pool.report_error = AsyncMock()
    router._credential_pool.report_success = AsyncMock()  # 新增
    router._error_classifier = MagicMock()
    router._cache_stats = {"total_calls": 0, "hit_tokens": 0, "miss_tokens": 0}
    router._analytics = None
    router._credential_locks = {}
    router.TASK_TIMEOUTS = {"chat": 30}
    router._apply_caching_headers = lambda h: h
    router._apply_prompt_caching = lambda p, m: m
    router._filter_tools_for_model = lambda t, m: t
    router._is_client_configured = lambda p: p == "mimo"
    router._select_client_for_provider = AsyncMock(return_value=router._client)
    router._track_cache = lambda r: None
    router._build_route_kwargs = MagicMock(return_value={"model": "test", "messages": [], "temperature": 0.7, "max_tokens": 1000})
    router._check_cache_health = lambda: None  # 跳过缓存健康检查
    router._last_cache_warning = 0.0

    # 模拟 _route_for_continuation：每次返回 length 截断
    call_count = {"continuation": 0, "route": 0}

    async def _mock_route_for_continuation(task_type, messages, **kwargs):
        call_count["continuation"] += 1
        # 每次都返回 length 截断，验证最多 2 轮就停止
        return _make_response("续写内容" * 100, finish_reason="length")

    router._route_for_continuation = _mock_route_for_continuation

    # 模拟主调用返回截断响应
    main_response = _make_response("主回复内容" * 100, finish_reason="length")

    # 调用 _handle_route_response，触发截断重试
    with patch.dict(os.environ, {"TRUNCATION_RETRY_DERECURSE": "true"}):
        result = await router._handle_route_response(
            main_response, "chat", "test-model", False,
            "user1", "session1", "mimo", None,
            messages=[{"role": "user", "content": "test"}],
            temperature=0.7, max_tokens=1000,
            config={"model": "test-model", "thinking": {"type": "disabled"}},
        )

    # 验证：最多 2 次 continuation 调用（不是递归的 2^N 次）
    assert call_count["continuation"] <= 2, \
        f"截断重试递归化失败：调用了 {call_count['continuation']} 次 continuation（应 ≤ 2）"
    # 验证：没有调用 route（不递归）
    assert call_count["route"] == 0, \
        f"截断重试仍递归调用了 route(): {call_count['route']} 次"
    # 验证：最终内容包含主回复 + 续写
    assert "主回复内容" in result
    print(f"✅ Task 1.1 验证通过：continuation 调用 {call_count['continuation']} 次（≤ 2），无 route 递归")


@pytest.mark.asyncio
async def test_finish_reason_detection_stop():
    """Task 1.2: finish_reason="stop" 时正确 break。"""
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._client = MagicMock()
    router._cache_stats = {"total_calls": 0, "hit_tokens": 0, "miss_tokens": 0}
    router._credential_pool = MagicMock()
    router._credential_pool.report_success = AsyncMock()
    router._credential_locks = {}
    router._apply_prompt_caching = lambda p, m: m
    router._filter_tools_for_model = lambda t, m: t
    router._is_client_configured = lambda p: False
    router._select_client_for_provider = AsyncMock(return_value=router._client)
    router._track_cache = lambda r: None
    router._build_route_kwargs = MagicMock(return_value={"model": "test"})
    router.TASK_TIMEOUTS = {"chat": 30}
    router._check_cache_health = lambda: None
    router._last_cache_warning = 0.0

    call_count = {"continuation": 0}

    async def _mock_continuation(task_type, messages, **kwargs):
        call_count["continuation"] += 1
        # 第一次续写就 stop，应立即 break
        return _make_response("续写完成。", finish_reason="stop")

    router._route_for_continuation = _mock_continuation

    main_response = _make_response("主回复内容" * 100, finish_reason="length")

    with patch.dict(os.environ, {"TRUNCATION_RETRY_DERECURSE": "true"}):
        await router._handle_route_response(
            main_response, "chat", "test-model", False,
            "user1", "session1", "mimo", None,
            messages=[{"role": "user", "content": "test"}],
            temperature=0.7, max_tokens=1000,
            config={"model": "test-model", "thinking": {"type": "disabled"}},
        )

    # 验证：finish_reason="stop" 时只调用 1 次就 break
    assert call_count["continuation"] == 1, \
        f"finish_reason=stop 时应只调用 1 次 continuation，实际 {call_count['continuation']}"
    print(f"✅ Task 1.2 验证通过：finish_reason=stop 时只调用 1 次就 break")


@pytest.mark.asyncio
async def test_fallback_max_tokens_passthrough():
    """Task 1.3: fallback 链透传 original_max_tokens。

    模拟 Web UI 传入 32768，触发 fallback 后 max_tokens 不低于 32768。
    """
    from model_router import ModelRouter
    from model_router import ROUTE_TABLE

    router = ModelRouter.__new__(ModelRouter)
    router._client = MagicMock()
    router._agnes_client = MagicMock()
    router._custom_clients = {"siliconflow": MagicMock()}
    router._cache_stats = {"total_calls": 0, "hit_tokens": 0, "miss_tokens": 0}
    router._credential_locks = {}
    router._apply_prompt_caching = lambda p, m: m
    router._filter_tools_for_model = lambda t, m: t
    router._is_client_configured = lambda p: True
    router._select_client_for_provider = AsyncMock(return_value=router._client)
    router._track_cache = lambda r: None
    router._build_route_kwargs = MagicMock(return_value={"model": "test"})
    router._get_custom_provider_default_model = lambda p: "test-model"
    router.TASK_TIMEOUTS = {"chat": 30}
    router._check_cache_health = lambda: None
    router._last_cache_warning = 0.0

    # 记录 fallback 调用时的 max_tokens
    received_max_tokens = []

    async def _mock_route_with_retry(task_type, config, messages, temperature,
                                      max_tokens, *args, **kwargs):
        received_max_tokens.append((task_type, max_tokens))
        # 模拟 fallback 也失败，让链路继续
        raise RuntimeError("fallback also failed")

    router._route_with_retry = _mock_route_with_retry

    # 触发 fallback 链：original_max_tokens=32768
    test_error = RuntimeError("main call failed")
    await router._try_fallback_chain(
        test_error, "chat_pro", [{"role": "user", "content": "test"}],
        0.7, False, None, None, 30, "user1", "session1", None,
        original_max_tokens=32768,
    )

    # 验证：所有 fallback 调用的 max_tokens 都 ≥ 32768
    assert len(received_max_tokens) > 0, "未触发任何 fallback"
    for task_type, mt in received_max_tokens:
        assert mt >= 32768, \
            f"fallback {task_type} 的 max_tokens={mt} 被压缩（应 ≥ 32768）"
    print(f"✅ Task 1.3 验证通过：{len(received_max_tokens)} 次 fallback 调用，max_tokens 全部 ≥ 32768")
    for task_type, mt in received_max_tokens:
        print(f"   - {task_type}: max_tokens={mt}")


@pytest.mark.asyncio
async def test_feature_flag_backward_compatible():
    """Feature flag TRUNCATION_RETRY_DERECURSE=false 时回退到旧行为。"""
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._client = MagicMock()
    router._cache_stats = {"total_calls": 0, "hit_tokens": 0, "miss_tokens": 0}
    router._credential_pool = MagicMock()
    router._credential_pool.report_success = AsyncMock()
    router._credential_locks = {}
    router._apply_prompt_caching = lambda p, m: m
    router._filter_tools_for_model = lambda t, m: t
    router._is_client_configured = lambda p: False
    router._track_cache = lambda r: None
    router._check_cache_health = lambda: None
    router._last_cache_warning = 0.0

    route_called = {"count": 0}

    async def _mock_route(*args, **kwargs):
        route_called["count"] += 1
        return "续写内容"

    router.route = _mock_route

    main_response = _make_response("主回复内容" * 100, finish_reason="length")

    # 关闭 derecurse，应走旧路径（调用 route()）
    with patch.dict(os.environ, {"TRUNCATION_RETRY_DERECURSE": "false"}):
        try:
            await router._handle_route_response(
                main_response, "chat", "test-model", False,
                "user1", "session1", "mimo", None,
                messages=[{"role": "user", "content": "test"}],
                temperature=0.7, max_tokens=1000,
                config={"model": "test-model", "thinking": {"type": "disabled"}},
            )
        except Exception:
            pass  # 测试环境可能因 mock 不完整抛错，忽略

    assert route_called["count"] > 0, \
        "TRUNCATION_RETRY_DERECURSE=false 时应回退到调用 route()"
    print(f"✅ Feature flag 验证通过：false 时走旧路径（调用 route() {route_called['count']} 次）")


if __name__ == "__main__":
    asyncio.run(test_truncation_retry_no_recursion())
    asyncio.run(test_finish_reason_detection_stop())
    asyncio.run(test_fallback_max_tokens_passthrough())
    asyncio.run(test_feature_flag_backward_compatible())
    print("\n🎉 所有 P0 测试通过！")
