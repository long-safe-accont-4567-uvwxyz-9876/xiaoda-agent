from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Mapping

import httpx
from loguru import logger

from llm_gateway.contracts import ProviderCapabilities
from llm_gateway.transports.base import (
    CapabilityReport,
    Completion,
    CompletionChunk,
    CompletionRequest,
    ProviderTransport,
    TransportError,
    normalize_finish_reason,
)
from security.ssrf_guard import build_secure_async_client, resolve_and_pin

_PATH = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:\.(?:[A-Za-z][A-Za-z0-9_-]*|[0-9]+|\*))*$")
_HEADER = re.compile(r"^(?:[^{}]|\{(?:api_key|base_url)\})*$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_RESERVED_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def validate_custom_headers(headers: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        raise ValueError("headers must be an object")
    result = {}
    for name, template in headers.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise ValueError("invalid header name")
        if name.lower() in _RESERVED_HEADERS:
            raise ValueError(f"reserved header is not allowed: {name}")
        if not isinstance(template, str) or not _HEADER.fullmatch(template):
            raise ValueError("invalid header template")
        if any(ord(character) < 32 or ord(character) == 127 for character in template):
            raise ValueError("invalid header value")
        result[name] = template
    return result


class CustomMappingTransport(ProviderTransport):
    def __init__(
        self,
        base_url: str,
        *,
        mapping: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        api_key: str = "",
        http_client: Any = None,
        capabilities: ProviderCapabilities | None = None,
        default_model: str = "",
        chat_path: str = "/chat/completions",
        models_path: str = "/models",
    ) -> None:
        super().__init__(capabilities=capabilities, default_model=default_model)
        self._base_url = base_url.rstrip("/")
        self._mapping = self._validate_mapping(mapping)
        self._headers = self._render_headers(headers or {}, api_key)
        self._owns_http = http_client is None
        self._http = http_client if http_client is not None else build_secure_async_client(self._base_url)
        self._chat_path = self._safe_endpoint(chat_path)
        self._models_path = self._safe_endpoint(models_path)
        self._connect_url: str | None = None
        self._host: str = ""

    def _effective_target(self) -> tuple[str, str]:
        """请求期绑定锁定 IP: 返回 (连接URL, Host头)。

        仅当本 transport 自建底层 client（生产路径）时执行请求期 DNS 解析 + 校验并绑定
        锁定 IP；若调用方注入外部 client（测试/调用方自管连接），则按原 base_url 直连。
        """
        if self._connect_url is None:
            if self._owns_http:
                try:
                    self._connect_url, self._host = resolve_and_pin(self._base_url)
                except ValueError as error:
                    raise TransportError(str(error)) from None
            else:
                self._connect_url = self._base_url
                self._host = ""
        return self._connect_url, self._host

    def _headers_with_host(self, host: str) -> dict[str, str]:
        if host:
            headers = dict(self._headers)
            headers["Host"] = host
            return headers
        return self._headers

    @classmethod
    def _validate_mapping(cls, mapping: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(mapping, Mapping):
            raise ValueError("mapping must be an object")
        result = dict(mapping)
        for section in ("request", "response", "stream"):
            values = result.get(section, {})
            if not isinstance(values, Mapping):
                raise ValueError(f"{section} mapping must be an object")
            for path in values.values():
                cls._validate_path(path)
        if "models" in result:
            cls._validate_path(result["models"])
        return result

    @staticmethod
    def _validate_path(path: Any) -> str:
        if not isinstance(path, str) or not _PATH.fullmatch(path) or any(part.startswith("_") for part in path.split(".")):
            raise ValueError("invalid mapping path")
        return path

    def _render_headers(self, headers: Mapping[str, str], api_key: str) -> dict[str, str]:
        return {
            name: template.replace("{api_key}", api_key).replace("{base_url}", self._base_url)
            for name, template in validate_custom_headers(headers).items()
        }

    @staticmethod
    def _safe_endpoint(path: str) -> str:
        if not isinstance(path, str) or not path.startswith("/") or "://" in path:
            raise ValueError("invalid endpoint path")
        return path

    @staticmethod
    def _set_path(payload: dict[str, Any], path: str, value: Any) -> None:
        target = payload
        parts = path.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value

    @staticmethod
    def _get_path(payload: Any, path: str, default: Any = None) -> Any:
        values = [payload]
        for part in path.split("."):
            next_values = []
            for value in values:
                if part == "*" and isinstance(value, list):
                    next_values.extend(value)
                elif part.isdigit() and isinstance(value, list) and int(part) < len(value):
                    next_values.append(value[int(part)])
                elif isinstance(value, Mapping) and part in value:
                    next_values.append(value[part])
            values = next_values
            if not values:
                return default
        return values if "*" in path else values[0]

    def _payload(self, request: CompletionRequest, *, stream: bool) -> dict[str, Any]:
        source = {
            "messages": list(request.messages),
            "model": request.model,
            "stream": stream,
            "tools": list(request.tools),
            "tool_choice": request.tool_choice,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            **dict(request.extra),
        }
        payload: dict[str, Any] = {}
        request_mapping = self._mapping.get("request") or {name: name for name in ("messages", "model", "stream", "tools", "tool_choice", "temperature", "max_tokens")}
        for source_name, target_path in request_mapping.items():
            if source_name in source and source[source_name] is not None:
                self._set_path(payload, target_path, source[source_name])
        return payload

    async def complete(self, request: CompletionRequest) -> Completion:
        try:
            connect_url, host = self._effective_target()
            headers = self._headers_with_host(host)
            response = await self._http.post(f"{connect_url}{self._chat_path}", json=self._payload(request, stream=False), headers=headers)
            response.raise_for_status()
            data = response.json()
            mapping = self._mapping.get("response", {})
            return Completion(
                text=str(self._get_path(data, mapping.get("text", "choices.0.message.content"), "")),
                model=str(self._get_path(data, mapping.get("model", "model"), request.model)),
                finish_reason=normalize_finish_reason(self._get_path(data, mapping.get("finish_reason", "choices.0.finish_reason"))),
                raw=data,
            )
        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException, OSError, ValueError) as error:
            raise TransportError("completion request failed") from error
        except Exception as error:
            logger.exception("transport.custom_mapping.complete_unexpected model={}", request.model)
            raise TransportError("completion request failed") from error

    async def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        try:
            connect_url, host = self._effective_target()
            headers = self._headers_with_host(host)
            async with self._http.stream("POST", f"{connect_url}{self._chat_path}", json=self._payload(request, stream=True), headers=headers) as response:
                response.raise_for_status()
                mapping = self._mapping.get("stream", {})
                async for line in response.aiter_lines():
                    line = line[5:].strip() if line.startswith("data:") else line
                    if not line or line == "[DONE]":
                        continue
                    data = json.loads(line)
                    yield CompletionChunk(
                        text=str(self._get_path(data, mapping.get("text", "choices.0.delta.content"), "")),
                        model=str(self._get_path(data, mapping.get("model", "model"), request.model)),
                        finish_reason=normalize_finish_reason(self._get_path(data, mapping.get("finish_reason", "choices.0.finish_reason"))),
                        raw=data,
                    )
        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException, OSError, ValueError) as error:
            raise TransportError("stream request failed") from error
        except Exception as error:
            logger.exception("transport.custom_mapping.stream_unexpected model={}", request.model)
            raise TransportError("stream request failed") from error

    async def discover_models(self) -> tuple[str, ...]:
        try:
            connect_url, host = self._effective_target()
            headers = self._headers_with_host(host)
            response = await self._http.get(f"{connect_url}{self._models_path}", headers=headers)
            response.raise_for_status()
            values = self._get_path(response.json(), self._mapping.get("models", "data.*.id"), ())
            models = tuple(str(value) for value in values if value)
            return models or await super().discover_models()
        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException, OSError, ValueError):
            return await super().discover_models()
        except Exception:
            logger.exception("transport.custom_mapping.discover_models_unexpected")
            return await super().discover_models()

    async def health_check(self) -> CapabilityReport:
        try:
            connect_url, host = self._effective_target()
            headers = self._headers_with_host(host)
            response = await self._http.get(f"{connect_url}{self._models_path}", headers=headers)
            if response.status_code == 404:
                return CapabilityReport(True, self.capabilities, models=await super().discover_models())
            response.raise_for_status()
            values = self._get_path(response.json(), self._mapping.get("models", "data.*.id"), ())
            models = tuple(str(value) for value in values if value)
            return CapabilityReport(True, self.capabilities, models=models or await super().discover_models())
        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException, OSError):
            return CapabilityReport(False, self.capabilities, error="health check failed")
        except Exception:
            logger.exception("transport.custom_mapping.health_check_unexpected")
            return CapabilityReport(False, self.capabilities, error="health check failed")

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
