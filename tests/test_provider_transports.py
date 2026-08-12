from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from llm_gateway.contracts import ProviderCapabilities
from llm_gateway.transports import (
    AnthropicTransport,
    CompletionRequest,
    CustomMappingTransport,
    LocalOrtTransport,
    OllamaTransport,
    OpenAICompatibleTransport,
    TransportError,
)


class OpenAIClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.models = SimpleNamespace(list=self.list_models)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if kwargs.get("stream"):
            async def chunks():
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="hel", tool_calls=None), finish_reason=None)],
                    model=kwargs["model"],
                )
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="lo", tool_calls=None), finish_reason="stop")],
                    model=kwargs["model"],
                )

            return chunks()
        function = SimpleNamespace(name="weather", arguments='{"city":"北京"}')
        tool_call = SimpleNamespace(id="call_1", function=function)
        message = SimpleNamespace(content="hello", tool_calls=[tool_call])
        usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
            model=kwargs["model"],
            usage=usage,
        )

    async def list_models(self) -> Any:
        return SimpleNamespace(data=[SimpleNamespace(id="remote-model")])


class JsonResponse:
    def __init__(self, data: Any, status_code: int = 200, text: str = "") -> None:
        self._data = data
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class HttpClient:
    def __init__(self, protocol: str) -> None:
        self.protocol = protocol
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> JsonResponse:
        self.requests.append(("POST", url, kwargs))
        if self.protocol == "anthropic":
            return JsonResponse({
                "model": "claude-test",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "tool_use", "id": "toolu_1", "name": "weather", "input": {"city": "北京"}},
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 3, "output_tokens": 2},
            })
        if self.protocol == "ollama":
            return JsonResponse({"model": "qwen", "message": {"content": "hello"}, "done": True, "done_reason": "stop"})
        return JsonResponse({"result": {"answer": "hello", "end": "stop", "model": "custom-model"}})

    async def get(self, url: str, **kwargs: Any) -> JsonResponse:
        self.requests.append(("GET", url, kwargs))
        if self.protocol == "ollama":
            return JsonResponse({"models": [{"name": "qwen"}]})
        return JsonResponse({"data": [{"id": "custom-model"}]})

    def stream(self, method: str, url: str, **kwargs: Any):
        self.requests.append((method, url, kwargs))
        protocol = self.protocol

        class StreamResponse:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def raise_for_status(self) -> None:
                return None

            async def aiter_lines(self):
                if protocol == "anthropic":
                    yield 'event: content_block_delta'
                    yield 'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hel"}}'
                    yield 'event: content_block_delta'
                    yield 'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"lo"}}'
                    yield 'event: message_delta'
                    yield 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}'
                    return
                if protocol == "ollama":
                    yield '{"message":{"content":"hel"},"done":false}'
                    yield '{"message":{"content":"lo"},"done":true,"done_reason":"stop"}'
                    return
                yield '{"delta":{"text":"hel"}}'
                yield '{"delta":{"text":"lo"},"finish":"stop"}'

        return StreamResponse()


class BrokenHttpClient:
    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code

    async def get(self, url: str, **kwargs: Any) -> JsonResponse:
        if self.status_code is None:
            raise RuntimeError("offline")
        return JsonResponse({}, status_code=self.status_code)


class CancelToken:
    is_cancelled = False

    def check(self) -> None:
        return None


class OrtRuntime:
    def health(self) -> bool:
        return True

    async def stream(self, messages, options, cancel_token):
        assert messages[-1]["content"] == "hi"
        assert options["max_tokens"] == 20
        assert isinstance(cancel_token, CancelToken)
        yield "hel"
        yield "lo"


def sample_request() -> CompletionRequest:
    return CompletionRequest(
        model="test-model",
        messages=({"role": "system", "content": "system"}, {"role": "user", "content": "hi"}),
        tools=({
            "type": "function",
            "function": {
                "name": "weather",
                "description": "weather",
                "parameters": {"type": "object"},
            },
        },),
        tool_choice="auto",
        temperature=0.2,
        max_tokens=20,
    )


@pytest.fixture(params=["openai", "anthropic", "ollama", "custom", "local_ort"])
def transport(request):
    capabilities = ProviderCapabilities(tools=True, model_discovery=True)
    if request.param == "openai":
        return OpenAICompatibleTransport(OpenAIClient(), capabilities=capabilities)
    if request.param == "anthropic":
        return AnthropicTransport("key", "https://anthropic.test", http_client=HttpClient("anthropic"), capabilities=capabilities)
    if request.param == "ollama":
        return OllamaTransport("http://ollama.test", http_client=HttpClient("ollama"), capabilities=capabilities)
    if request.param == "custom":
        return CustomMappingTransport(
            "https://custom.test",
            http_client=HttpClient("custom"),
            mapping={
                "request": {"messages": "input.messages", "model": "input.model", "stream": "input.stream"},
                "response": {"text": "result.answer", "finish_reason": "result.end", "model": "result.model"},
                "stream": {"text": "delta.text", "finish_reason": "finish"},
                "models": "data.*.id",
            },
            headers={"Authorization": "Bearer {api_key}"},
            api_key="secret",
            capabilities=capabilities,
        )
    return LocalOrtTransport(OrtRuntime(), "local-model", cancel_token_factory=CancelToken)


@pytest.mark.asyncio
async def test_transport_stream_contract(transport):
    chunks = [chunk async for chunk in transport.stream(sample_request())]

    assert "".join(chunk.text for chunk in chunks) == "hello"
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_transport_complete_contract(transport):
    completion = await transport.complete(sample_request())

    assert completion.text == "hello"
    assert completion.model
    assert completion.finish_reason in {"stop", "tool_calls"}


@pytest.mark.asyncio
async def test_transport_health_contract(transport):
    report = await transport.health_check()

    assert report.available is True
    assert report.capabilities.streaming is True


@pytest.mark.asyncio
async def test_openai_transport_normalizes_tools_usage_and_discovery():
    client = OpenAIClient()
    transport = OpenAICompatibleTransport(client, capabilities=ProviderCapabilities(tools=True, model_discovery=True))

    completion = await transport.complete(sample_request())

    assert completion.tool_calls[0].name == "weather"
    assert completion.tool_calls[0].arguments == {"city": "北京"}
    assert completion.usage.total_tokens == 5
    assert await transport.discover_models() == ("remote-model",)
    assert client.requests[0]["tools"] == list(sample_request().tools)


@pytest.mark.asyncio
async def test_discovery_falls_back_to_configured_model():
    class BrokenModels(OpenAIClient):
        async def list_models(self):
            raise RuntimeError("not supported")

    transport = OpenAICompatibleTransport(BrokenModels(), default_model="fallback-model")

    assert await transport.discover_models() == ("fallback-model",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport",
    [
        AnthropicTransport("key", "https://anthropic.test", http_client=BrokenHttpClient(), default_model="configured"),
        OllamaTransport("http://ollama.test", http_client=BrokenHttpClient(), default_model="configured"),
        CustomMappingTransport("https://custom.test", mapping={}, http_client=BrokenHttpClient(), default_model="configured"),
    ],
)
async def test_http_transport_health_reports_unreachable_upstream(transport):
    report = await transport.health_check()

    assert report.available is False
    assert report.models == ()
    assert report.error == "health check failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_http_transport_health_reports_authentication_failure(status_code):
    transport = AnthropicTransport(
        "key",
        "https://anthropic.test",
        http_client=BrokenHttpClient(status_code),
        default_model="configured",
    )

    report = await transport.health_check()

    assert report.available is False
    assert report.models == ()


@pytest.mark.asyncio
async def test_http_transport_health_accepts_unsupported_model_discovery():
    transport = AnthropicTransport(
        "key",
        "https://anthropic.test",
        http_client=BrokenHttpClient(404),
        default_model="configured",
    )

    report = await transport.health_check()

    assert report.available is True
    assert report.models == ("configured",)
    assert report.error is None


@pytest.mark.asyncio
async def test_anthropic_transport_uses_native_streaming_and_converts_tools():
    client = HttpClient("anthropic")
    transport = AnthropicTransport("key", "https://anthropic.test/v1", http_client=client)

    chunks = [chunk async for chunk in transport.stream(sample_request())]

    method, url, kwargs = client.requests[0]
    assert method == "POST"
    assert url == "https://anthropic.test/v1/messages"
    assert kwargs["json"]["system"] == "system"
    assert kwargs["json"]["tools"][0]["input_schema"] == {"type": "object"}
    assert "".join(chunk.text for chunk in chunks) == "hello"


@pytest.mark.asyncio
async def test_anthropic_stream_normalizes_tool_use_events():
    class AnthropicToolClient(HttpClient):
        def stream(self, method: str, url: str, **kwargs: Any):
            class StreamResponse:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return None

                def raise_for_status(self) -> None:
                    return None

                async def aiter_lines(self):
                    yield 'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"weather","input":{}}}'
                    yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":"}}'
                    yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\\"北京\\"}"}}'
                    yield 'data: {"type":"content_block_stop","index":0}'
                    yield 'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}'

            return StreamResponse()

    transport = AnthropicTransport("key", "https://anthropic.test", http_client=AnthropicToolClient("anthropic"))

    chunks = [chunk async for chunk in transport.stream(sample_request())]
    calls = [call for chunk in chunks for call in chunk.tool_calls]

    assert calls[-1].id == "toolu_1"
    assert calls[-1].name == "weather"
    assert calls[-1].arguments == {"city": "北京"}
    assert chunks[-1].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_ollama_normalizes_complete_and_stream_tool_calls():
    class OllamaToolClient(HttpClient):
        async def post(self, url: str, **kwargs: Any) -> JsonResponse:
            return JsonResponse({
                "model": "qwen",
                "message": {"content": "", "tool_calls": [{"function": {"name": "weather", "arguments": {"city": "北京"}}}]},
                "done": True,
            })

        def stream(self, method: str, url: str, **kwargs: Any):
            class StreamResponse:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return None

                def raise_for_status(self) -> None:
                    return None

                async def aiter_lines(self):
                    yield '{"model":"qwen","message":{"content":"","tool_calls":[{"function":{"name":"weather","arguments":{"city":"北京"}}}]},"done":true}'

            return StreamResponse()

    transport = OllamaTransport("http://ollama.test", http_client=OllamaToolClient("ollama"))

    completion = await transport.complete(sample_request())
    chunks = [chunk async for chunk in transport.stream(sample_request())]

    assert completion.tool_calls[0].name == "weather"
    assert completion.tool_calls[0].arguments == {"city": "北京"}
    assert completion.finish_reason == "tool_calls"
    assert chunks[0].tool_calls[0].name == "weather"
    assert chunks[0].finish_reason == "tool_calls"


def test_custom_mapping_rejects_executable_or_unsafe_templates():
    with pytest.raises(ValueError, match="mapping path"):
        CustomMappingTransport("https://custom.test", mapping={"response": {"text": "__class__.__mro__"}})
    with pytest.raises(ValueError, match="header template"):
        CustomMappingTransport("https://custom.test", mapping={}, headers={"X-Key": "{api_key.__class__}"})


@pytest.mark.parametrize(
    "headers",
    [
        {"": "{api_key}"},
        {"Bad Header": "{api_key}"},
        {"X-Test\nInjected": "{api_key}"},
        {"X-Test": "{api_key}\r\nInjected: yes"},
        {"Host": "{base_url}"},
        {"content-length": "{api_key}"},
        {"Transfer-Encoding": "{api_key}"},
        {"CONNECTION": "{api_key}"},
    ],
)
def test_custom_mapping_rejects_unsafe_header_names_values_and_reserved_headers(headers):
    with pytest.raises(ValueError, match="header"):
        CustomMappingTransport("https://custom.test", mapping={}, headers=headers)


class RecordingHttpClient:
    """记录发起请求的 URL/headers 的内存 httpx 替身。"""

    def __init__(self) -> None:
        self.recorded: list[tuple[str, str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> JsonResponse:
        self.recorded.append(("POST", url, kwargs))
        return JsonResponse({"result": {"answer": "hello"}})

    async def get(self, url: str, **kwargs: Any) -> JsonResponse:
        self.recorded.append(("GET", url, kwargs))
        return JsonResponse({"data": [{"id": "custom-model"}]})


@pytest.mark.asyncio
async def test_custom_mapping_connects_to_pinned_ip(monkeypatch):
    """SSRF: 请求期应把连接目标绑定为锁定 IP，并携带原始 Host 头（关闭 DNS rebinding）。"""
    transport = CustomMappingTransport(
        "https://attacker.example/v1",
        mapping={"response": {"text": "result.answer"}},
        api_key="k",
    )
    client = RecordingHttpClient()
    transport._http = client  # 保留 _owns_http=True，仅替换底层 client
    monkeypatch.setattr(
        "llm_gateway.transports.custom_mapping.resolve_and_pin",
        lambda url: ("http://10.0.0.1/v1", "attacker.example"),
    )

    completion = await transport.complete(sample_request())

    method, url, kwargs = client.recorded[0]
    assert method == "POST"
    assert url == "http://10.0.0.1/v1/chat/completions"
    assert kwargs["headers"]["Host"] == "attacker.example"
    assert completion.text == "hello"


@pytest.mark.asyncio
async def test_custom_mapping_blocks_request_time_dns_rebinding(monkeypatch):
    """SSRF: 请求期解析到危险地址应被拦截（而非按 hostname 直连）。"""
    transport = CustomMappingTransport("https://attacker.example/v1", mapping={}, api_key="k")

    def unsafe(url):
        raise ValueError("SSRF 校验失败: 目标 attacker.example 解析到危险 IP 10.0.0.1")

    monkeypatch.setattr("llm_gateway.transports.custom_mapping.resolve_and_pin", unsafe)

    with pytest.raises(TransportError):
        await transport.complete(sample_request())


@pytest.mark.asyncio
async def test_custom_mapping_applies_declarative_paths_and_header_templates():
    client = HttpClient("custom")
    transport = CustomMappingTransport(
        "https://custom.test",
        http_client=client,
        mapping={
            "request": {"messages": "payload.chat", "model": "payload.engine", "stream": "payload.streaming"},
            "response": {"text": "result.answer", "finish_reason": "result.end", "model": "result.model"},
        },
        headers={"X-API-Key": "{api_key}"},
        api_key="secret",
    )

    completion = await transport.complete(sample_request())

    _, _, kwargs = client.requests[0]
    assert kwargs["json"]["payload"]["engine"] == "test-model"
    assert kwargs["json"]["payload"]["chat"][-1]["content"] == "hi"
    assert kwargs["headers"] == {"X-API-Key": "secret"}
    assert completion.text == "hello"


@pytest.mark.asyncio
async def test_custom_mapping_supports_array_indices_in_default_openai_paths():
    class OpenAIHttpClient(HttpClient):
        async def post(self, url: str, **kwargs: Any) -> JsonResponse:
            self.requests.append(("POST", url, kwargs))
            return JsonResponse({
                "model": "custom-model",
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            })

    transport = CustomMappingTransport(
        "https://custom.test",
        http_client=OpenAIHttpClient("custom"),
        mapping={},
    )

    completion = await transport.complete(sample_request())

    assert completion.text == "hello"
    assert completion.finish_reason == "stop"


@pytest.mark.asyncio
async def test_transport_normalizes_protocol_errors():
    class BrokenClient(OpenAIClient):
        async def create(self, **kwargs):
            raise RuntimeError("secret upstream detail")

    transport = OpenAICompatibleTransport(BrokenClient())

    with pytest.raises(TransportError, match="completion request failed"):
        await transport.complete(sample_request())


@pytest.mark.asyncio
async def test_ollama_transport_maps_http_status_without_leaking_response_body():
    request = httpx.Request("POST", "http://ollama.test/api/chat")
    response = httpx.Response(401, request=request, text="secret upstream response")

    class UnauthorizedClient(HttpClient):
        async def post(self, url: str, **kwargs: Any) -> JsonResponse:
            raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    transport = OllamaTransport("http://ollama.test", http_client=UnauthorizedClient("ollama"))

    with pytest.raises(TransportError, match="provider authentication failed") as captured:
        await transport.complete(sample_request())

    assert "secret upstream response" not in str(captured.value)


@pytest.mark.asyncio
async def test_anthropic_compat_client_supports_streaming(monkeypatch):
    from web.custom_providers import AnthropicCompatClient

    transport = AnthropicTransport("key", "https://anthropic.test", http_client=HttpClient("anthropic"))
    monkeypatch.setattr("web.custom_providers.AnthropicTransport", lambda *args, **kwargs: transport)
    monkeypatch.setattr("web.custom_providers.resolve_and_pin", lambda url: (url, ""))
    client = AnthropicCompatClient("key", "https://anthropic.test")

    stream = await client._create(model="claude-test", messages=[{"role": "user", "content": "hi"}], stream=True)
    chunks = [chunk async for chunk in stream]

    assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks) == "hello"
    assert chunks[-1].choices[0].finish_reason == "stop"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "factory", "patch_target"),
    [
        (
            "llm_gateway.transports.anthropic",
            lambda: AnthropicTransport("key"),
            "llm_gateway.transports.anthropic.build_secure_async_client",
        ),
        (
            "llm_gateway.transports.ollama",
            lambda: OllamaTransport("http://ollama.test"),
            "llm_gateway.transports.ollama.httpx.AsyncClient",
        ),
        (
            "llm_gateway.transports.custom_mapping",
            lambda: CustomMappingTransport("https://custom.test", mapping={}),
            "llm_gateway.transports.custom_mapping.build_secure_async_client",
        ),
    ],
)
async def test_http_transport_closes_only_owned_client(monkeypatch, module_name, factory, patch_target):
    class CloseableClient:
        def __init__(self, **kwargs: Any) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    owned = CloseableClient()
    monkeypatch.setattr(patch_target, lambda *args, **kwargs: owned)
    transport = factory()

    await transport.aclose()

    assert owned.closed is True

    external = CloseableClient()
    if module_name.endswith("anthropic"):
        transport = AnthropicTransport("key", http_client=external)
    elif module_name.endswith("ollama"):
        transport = OllamaTransport("http://ollama.test", http_client=external)
    else:
        transport = CustomMappingTransport("https://custom.test", mapping={}, http_client=external)

    await transport.aclose()

    assert external.closed is False


@pytest.mark.asyncio
async def test_anthropic_compat_stream_closes_transport_when_consumer_stops(monkeypatch):
    from web.custom_providers import AnthropicCompatClient

    class TemporaryTransport:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.closed = False

        async def stream(self, request):
            yield SimpleNamespace(text="first", model=request.model, finish_reason=None, tool_calls=())
            yield SimpleNamespace(text="second", model=request.model, finish_reason="stop", tool_calls=())

        async def aclose(self) -> None:
            self.closed = True

    transport = TemporaryTransport()
    monkeypatch.setattr("web.custom_providers.AnthropicTransport", lambda *args, **kwargs: transport)
    monkeypatch.setattr("web.custom_providers.resolve_and_pin", lambda url: (url, ""))
    client = AnthropicCompatClient("key", "https://anthropic.test")

    stream = await client._create(model="claude-test", messages=[{"role": "user", "content": "hi"}], stream=True)
    await anext(stream)
    await stream.aclose()

    assert transport.closed is True


@pytest.mark.asyncio
async def test_anthropic_runtime_client_blocks_request_time_dns_rebinding(monkeypatch):
    """SSRF: 运行时 anthropic chat 路径请求期 rebinding 到内网/元数据应被拦截。"""
    from web.custom_providers import AnthropicCompatClient

    def unsafe(url):
        raise ValueError("SSRF 校验失败: 目标 attacker.example 解析到危险 IP 10.0.0.1")

    monkeypatch.setattr("web.custom_providers.resolve_and_pin", unsafe)
    client = AnthropicCompatClient("key", "https://attacker.example")

    with pytest.raises(ValueError):
        await client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )


@pytest.mark.asyncio
async def test_openai_runtime_pinning_transport_rewrites_to_pinned_ip(monkeypatch):
    """SSRF: openai 运行时传输层把连接目标改写为锁定 IP 并携带原始 Host 头。"""
    import httpx

    from web.custom_providers import PinningAsyncTransport

    class Recording:
        async def handle_async_request(self, request):
            self.url = str(request.url)
            self.host = request.headers.get("Host")
            return httpx.Response(200, request=request, json={}, headers={"content-type": "application/json"})

    recorder = Recording()
    transport = PinningAsyncTransport("https://attacker.example/v1", http_transport=recorder)
    monkeypatch.setattr(
        "security.ssrf_guard.resolve_and_pin",
        lambda url: ("https://10.0.0.1/v1", "attacker.example"),
    )

    await transport.handle_async_request(
        httpx.Request("POST", "https://attacker.example/v1/chat/completions")
    )

    assert recorder.url == "https://10.0.0.1/v1/chat/completions"
    assert recorder.host == "attacker.example"


def test_openai_runtime_client_uses_pinning_transport():
    """SSRF: build_client("openai") 产出的运行时 client 应经 pinning 传输层建连。"""
    from web.custom_providers import PinningAsyncTransport, build_client

    client = build_client("openai", "https://attacker.example/v1", "k")

    assert isinstance(client._client._transport, PinningAsyncTransport)


@pytest.mark.asyncio
async def test_openai_runtime_client_blocks_request_time_dns_rebinding(monkeypatch):
    """SSRF: 运行时 openai chat 路径请求期 rebinding 到内网/元数据应被拦截。"""
    import openai

    from web.custom_providers import build_client

    def unsafe(url):
        raise ValueError("SSRF 校验失败: 目标 attacker.example 解析到危险 IP 169.254.169.254")

    monkeypatch.setattr("web.custom_providers.resolve_and_pin", unsafe)
    client = build_client("openai", "https://attacker.example/v1", "k")

    with pytest.raises(openai.APIConnectionError):
        await client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )


@pytest.mark.asyncio
async def test_secure_transport_rewrites_to_pinned_ip_and_preserves_host_and_sni(monkeypatch):
    """Item 4: 统一安全传输层把连接目标改写为锁定 IP，保留原始 Host 头与 HTTPS SNI。"""
    import httpx

    from security.ssrf_guard import SecureAsyncTransport

    class Recording:
        async def handle_async_request(self, request):
            self.url = str(request.url)
            self.host = request.headers.get("Host")
            self.sni = request.extensions.get("sni_hostname")
            return httpx.Response(200, request=request, json={},
                                  headers={"content-type": "application/json"})

    recorder = Recording()
    transport = SecureAsyncTransport("https://attacker.example/v1", http_transport=recorder)
    monkeypatch.setattr(
        "security.ssrf_guard.resolve_and_pin",
        lambda url: ("https://10.0.0.1/v1", "attacker.example"),
    )

    await transport.handle_async_request(
        httpx.Request("POST", "https://attacker.example/v1/chat/completions")
    )

    assert recorder.url == "https://10.0.0.1/v1/chat/completions"
    assert recorder.host == "attacker.example"
    assert recorder.sni == "attacker.example"


@pytest.mark.asyncio
async def test_secure_transport_blocks_request_time_dns_rebinding(monkeypatch):
    """Item 4: 统一安全传输层请求期解析到危险地址应被拦截。"""
    import httpx

    from security.ssrf_guard import SecureAsyncTransport

    def unsafe(url):
        raise ValueError("SSRF 校验失败: 目标 attacker.example 解析到危险 IP 10.0.0.1")

    monkeypatch.setattr("security.ssrf_guard.resolve_and_pin", unsafe)
    transport = SecureAsyncTransport("https://attacker.example/v1")

    with pytest.raises(ValueError):
        await transport.handle_async_request(
            httpx.Request("GET", "https://attacker.example/v1/models")
        )


def test_build_secure_async_client_disables_redirect_following(monkeypatch):
    """Item 4: 统一安全 client 禁止自动跟随重定向。"""
    from security.ssrf_guard import build_secure_async_client

    monkeypatch.setattr("security.ssrf_guard.resolve_and_pin", lambda url: (url, ""))
    client = build_secure_async_client("https://api.example.com")

    assert client.follow_redirects is False


@pytest.mark.asyncio
async def test_secure_client_does_not_follow_redirect_to_metadata(monkeypatch):
    """Item 4: 上游返回 302 指向云元数据时统一安全 client 也不自动跟随。"""
    import httpx

    from security.ssrf_guard import build_secure_async_client

    class Redirecting:
        def __init__(self) -> None:
            self.requests = 0

        async def handle_async_request(self, request):
            self.requests += 1
            return httpx.Response(
                302,
                request=request,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
            )

    recorder = Redirecting()
    monkeypatch.setattr(
        "security.ssrf_guard.resolve_and_pin",
        lambda url: ("https://attacker.example/v1", "attacker.example"),
    )
    client = build_secure_async_client("https://attacker.example/v1", http_transport=recorder)
    resp = await client.get("https://attacker.example/v1/models")

    assert resp.status_code == 302
    assert recorder.requests == 1


def test_anthropic_transport_default_client_uses_secure_transport():
    from llm_gateway.transports import AnthropicTransport
    from security.ssrf_guard import SecureAsyncTransport

    transport = AnthropicTransport("key", "https://api.anthropic.com")

    assert isinstance(transport._http._transport, SecureAsyncTransport)


def test_custom_mapping_transport_default_client_uses_secure_transport():
    from llm_gateway.transports import CustomMappingTransport
    from security.ssrf_guard import SecureAsyncTransport

    transport = CustomMappingTransport("https://custom.test", mapping={}, api_key="k")

    assert isinstance(transport._http._transport, SecureAsyncTransport)


def _openai_compatible_definition(base_url: str):
    from llm_gateway.contracts import (
        AuthDefinition,
        EndpointDefinition,
        ProviderCapabilities,
        ProviderDefinition,
        ProviderProtocol,
    )

    return ProviderDefinition(
        id="remote-openai",
        protocol=ProviderProtocol.OPENAI_COMPATIBLE,
        endpoint=EndpointDefinition(base_url=base_url, chat_path="/chat/completions", models_path="/models"),
        auth=AuthDefinition(environment_aliases=(), header="Authorization", scheme="Bearer", required=True),
        capabilities=ProviderCapabilities(tools=True, model_discovery=True),
        default_model="gpt-test",
        max_tokens_cap=None,
        metadata={"label": "Remote", "enabled": True, "order": 1, "headers": {}, "mapping": {}},
    )


def test_provider_transport_openai_injects_secure_http_client():
    """Item 4: ProviderService 探活 transport 的 OpenAI client 应经统一安全传输层建连。"""
    from llm_gateway.provider_service import ProviderService
    from security.ssrf_guard import SecureAsyncTransport

    transport = ProviderService._build_transport(
        _openai_compatible_definition("https://attacker.example/v1"), "key"
    )

    assert isinstance(transport._client._client._transport, SecureAsyncTransport)


def test_runtime_openai_client_uses_secure_transport():
    """Item 4: 运行时 OpenAI client（build_client）应经统一安全传输层建连。"""
    from llm_gateway.provider_service import ProviderService
    from security.ssrf_guard import SecureAsyncTransport

    client = ProviderService._build_runtime_client(
        _openai_compatible_definition("https://attacker.example/v1"), "key"
    )

    assert isinstance(client._client._transport, SecureAsyncTransport)
