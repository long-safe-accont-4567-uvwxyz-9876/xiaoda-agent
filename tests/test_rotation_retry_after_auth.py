"""认证轮换后单次重试行为测试（2026-08-29 审计修复）。

修复点：MiMo 401 等认证类错误被 ErrorClassifier 判定 non-retryable，
原实现在 _handle_route_exception 轮换凭证后立即 rethrow——换了健康 key
但当次请求仍降级，下个请求才受益。现允许紧接一次用新客户端的重试：
    - 仅当错误为认证类（AUTH_ERROR）且本轮确实装入了新凭证；
    - 有界：只一次、不递归（retry_state 由调用方持有，重试自身再失败
      时 used 标记已置位，不会触发第二次轮换重试）；
    - 重试仍失败按原失败路径 raise。
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
import openai
import pytest

import llm_gateway.client_lifecycle as client_lifecycle_module
from llm_gateway.client_lifecycle import ClientLifecycleMixin
from llm_gateway.router_execution import ExecutionMixin
from utils.error_classifier import ErrorClassifier


def _auth_error() -> openai.AuthenticationError:
    """构造 openai.AuthenticationError（401），与线上 MiMo 401 同型"""
    return openai.AuthenticationError(
        message="Error code: 401 - invalid api key",
        response=httpx.Response(
            401, request=httpx.Request("POST", "https://api.mimo.example/v1")),
        body=None,
    )


def _chunk(content: str | None = None, finish_reason: str | None = None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=content),
            finish_reason=finish_reason,
        )],
        usage=None,
    )


class _Stream:
    def __init__(self, chunks) -> None:
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration

    async def close(self):
        return None


class _FakePool:
    """每次 get_credential 依次返回预置凭证（模拟池中有健康 key）"""

    def __init__(self, keys: list[str]) -> None:
        self._keys = list(keys)

    async def get_credential(self, provider: str):
        return SimpleNamespace(
            api_key=self._keys.pop(0) if self._keys else "",
            base_url="https://api.mimo.example/v1",
        )

    async def report_error(self, provider, classified, api_key=""):
        return None


class _RetryRouter(ClientLifecycleMixin, ExecutionMixin):
    """最小 harness：真实 ErrorClassifier + 真实凭证轮换 + 假客户端"""

    TASK_TIMEOUTS = {"chat": 1}

    def __init__(self, client_factory, pool) -> None:
        self._registry = SimpleNamespace(get_task_ref=lambda _task: {
            "model": "test-model", "client": "mimo", "max_tokens": 32,
        })
        self._error_classifier = ErrorClassifier()
        self._credential_pool = pool
        self._credential_locks = {}
        self._client = None
        self._agnes_client = None
        self._client_factory = client_factory
        self._try_fallback_chain = AsyncMock(return_value=None)
        self._record_stream_usage = AsyncMock()

    async def _select_client_for_provider(self, provider):
        # 始终返回当前 _client：轮换成功后这里拿到的是换入的新客户端
        return self._client

    def _apply_prompt_caching(self, _provider, messages):
        return messages

    def _apply_caching_headers(self, headers):
        return headers

    def _filter_tools_for_model(self, tools, _model):
        return tools

    @asynccontextmanager
    async def _get_provider_call_semaphore(self, _provider):
        yield


def _patch_openai(monkeypatch, client_factory) -> None:
    """把 client_lifecycle 的 AsyncOpenAI 构造替换为测试可控工厂。

    轮换路径用 AsyncOpenAI(api_key=..., base_url=...) 换入新客户端，
    这里让它走测试工厂（返回带预编程 create 行为的假客户端）。
    """

    def _construct(*_args, **kwargs):
        return client_factory(kwargs.get("api_key", ""), kwargs.get("base_url", ""))

    monkeypatch.setattr(client_lifecycle_module, "AsyncOpenAI", _construct)


def _make_client(api_key: str, create_side_effect) -> MagicMock:
    client = MagicMock()
    client.api_key = api_key
    client.chat.completions.create = AsyncMock(side_effect=create_side_effect)
    return client


@pytest.mark.asyncio
async def test_auth_rotation_retries_once_with_new_key_and_succeeds(monkeypatch) -> None:
    """key A 401、池中有 B：当次请求用 B 成功返回，总尝试 = 2"""
    def factory(api_key, _base_url):
        if api_key == "sk-b":
            # 注意：create() 需"返回"流对象，用 lambda 包装（side_effect 直传
            # 会被 AsyncMock 当作可迭代对象逐值消费）
            return _make_client(api_key, lambda *_a, **_k: _Stream([
                _chunk("用新 key 的回答"), _chunk(finish_reason="stop"),
                SimpleNamespace(choices=[], usage=SimpleNamespace(total_tokens=3)),
            ]))
        return _make_client(api_key, None)

    _patch_openai(monkeypatch, factory)
    old_client = _make_client("sk-a", openai.AuthenticationError(
        message="Error code: 401",
        response=httpx.Response(
            401, request=httpx.Request("POST", "https://api.mimo.example/v1")),
        body=None,
    ))
    router = _RetryRouter(factory, _FakePool(["sk-b"]))
    router._client = old_client

    parts = [part async for part in router.chat_stream(
        [{"role": "user", "content": "你好"}],
    )]

    assert parts == ["用新 key 的回答"]
    # 原 key 客户端一次失败；新 key 客户端一次成功（当次请求受益）
    assert old_client.chat.completions.create.await_count == 1
    assert router._client.api_key == "sk-b"
    assert router._client.chat.completions.create.await_count == 1
    assert router._record_stream_usage.await_count == 1


@pytest.mark.asyncio
async def test_auth_rotation_retry_exhausted_follows_original_failure_path(monkeypatch) -> None:
    """新 key 仍 401：按原失败路径 raise，总尝试有界（≤2）。

    池中再换 C 也不再触发第二次轮换重试（retry_state 已用），
    但轮换本身仍会装入 C 供下个请求使用。
    """
    clients: dict[str, MagicMock] = {}

    def factory(api_key, _base_url):
        clients[api_key] = _make_client(api_key, _auth_error())
        return clients[api_key]

    _patch_openai(monkeypatch, factory)
    old_client = _make_client("sk-a", _auth_error())
    router = _RetryRouter(factory, _FakePool(["sk-b", "sk-c"]))
    router._client = old_client

    with pytest.raises(openai.AuthenticationError):
        async for _chunk in router.chat_stream([{"role": "user", "content": "你好"}]):
            pass

    # 有界：原 key 一次 + 轮换后新 key 一次 = 2（不会用 C 再试第三次）
    assert old_client.chat.completions.create.await_count == 1
    assert clients["sk-b"].chat.completions.create.await_count == 1
    assert clients["sk-c"].chat.completions.create.await_count == 0
    # 轮换本身仍会装入 C 供下个请求使用
    assert router._client.api_key == "sk-c"
    # 失败路径不记 usage/success
    assert router._record_stream_usage.await_count == 0


@pytest.mark.asyncio
async def test_rotation_retry_granted_only_once_per_call():
    """单次调用内轮换重试只授予一次：used 标记置位后不再授予（不递归）"""
    router = _RetryRouter(
        lambda api_key, _base: _make_client(api_key, None), _FakePool([]))
    router._rotate_credential_on_error = AsyncMock(return_value=True)
    router._active_api_key = lambda _provider: ""
    error = _auth_error()
    state: dict = {}

    granted = await router._handle_route_exception(
        error, "mimo", "chat", "m", 0, retry_state=state)
    assert granted is True
    assert state == {"rotation_retry_used": True}

    # 同一次调用的重试再失败：即使又装入了新凭证也不再授予
    with pytest.raises(openai.AuthenticationError):
        await router._handle_route_exception(
            error, "mimo", "chat", "m", 1, retry_state=state)


@pytest.mark.asyncio
async def test_no_retry_without_freshly_installed_credential():
    """未装入新凭证（池无新 key / 同 key）时保持原行为：立即 raise"""
    router = _RetryRouter(
        lambda api_key, _base: _make_client(api_key, None), _FakePool([]))
    router._rotate_credential_on_error = AsyncMock(return_value=False)
    router._active_api_key = lambda _provider: ""

    with pytest.raises(openai.AuthenticationError):
        await router._handle_route_exception(
            _auth_error(), "mimo", "chat", "m", 0, retry_state={})


@pytest.mark.asyncio
async def test_non_auth_non_retryable_errors_keep_original_path():
    """非认证类不可重试错误行为不变：ABORT 直接 raise，不做轮换重试"""
    router = _RetryRouter(
        lambda api_key, _base: _make_client(api_key, None), _FakePool([]))
    router._rotate_credential_on_error = AsyncMock(return_value=True)
    router._active_api_key = lambda _provider: ""

    # empty_content → EMPTY_REPLY → ABORT：轮换不触发、直接 raise
    with pytest.raises(RuntimeError, match="empty_content"):
        await router._handle_route_exception(
            RuntimeError("empty_content by mimo: finish_reason=stop"),
            "mimo", "chat", "m", 0, retry_state={})
    router._rotate_credential_on_error.assert_not_called()
