"""内置 provider（agnes / mimo）启动恢复的回归测试。

回归对象：provider_service.restore_definition_failed id=agnes / id=mimo
error=base_url must be an http(s) URL（曾每次启动固定两条）。

单独成文件而不是追加进 tests/test_provider_onboarding.py：后者已顶到
test_giant_file_ratchet 的行数基线，任何追加都会触发热点文件不得增长。
"""

from __future__ import annotations

import contextlib

import pytest

from llm_gateway.contracts import (
    AuthDefinition,
    EndpointDefinition,
    ProviderCapabilities,
    ProviderDefinition,
    ProviderProtocol,
)
from llm_gateway.provider_catalog import ProviderCatalog
from llm_gateway.provider_service import ProviderService


class MemoryConfig:
    def __init__(self) -> None:
        self.providers: dict[str, dict] = {}

    def get(self, path: str, default=None):
        if path == "models.providers":
            return {key: dict(value) for key, value in self.providers.items()}
        if path.startswith("models.providers."):
            value = self.providers.get(path.rsplit(".", 1)[-1])
            return dict(value) if value else default
        return default

    def set(self, path: str, value) -> None:
        if path.startswith("models.providers."):
            self.providers[path.rsplit(".", 1)[-1]] = dict(value)


class MemoryCredentials:
    def read(self, provider_id: str) -> str:
        return ""

    def write(self, provider_id: str, value: str) -> None:
        return None

    def delete(self, provider_id: str) -> None:
        return None


class FakeRuntimeRouter:
    def __init__(self) -> None:
        self._custom_clients: dict[str, object] = {}

    def get_custom_client(self, provider_id):
        return self._custom_clients.get(provider_id)

    def set_custom_client(self, provider_id, client):
        self._custom_clients[provider_id] = client

    def remove_custom_client(self, provider_id):
        self._custom_clients.pop(provider_id, None)

    def has_custom_client(self, provider_id):
        return provider_id in self._custom_clients

    def list_custom_clients(self):
        return list(self._custom_clients.items())


def _definition(provider_id: str, base_url: str, env: str, model: str) -> ProviderDefinition:
    return ProviderDefinition(
        id=provider_id,
        protocol=ProviderProtocol.OPENAI_COMPATIBLE,
        endpoint=EndpointDefinition(base_url=base_url),
        auth=AuthDefinition(environment_aliases=(env,)),
        capabilities=ProviderCapabilities(tools=True, model_discovery=True),
        builtin=True,
        default_model=model,
    )


def _builtin_agnes_mimo_catalog() -> ProviderCatalog:
    return ProviderCatalog((
        _definition("agnes", "https://apihub.agnes-ai.cn/v1", "AGNES_API_KEY", "agnes-2.5-flash"),
        _definition("mimo", "https://api.xiaomimimo.com/v1", "MIMO_API_KEY", "mimo-v2.5"),
    ))


@contextlib.contextmanager
def _capture_warnings():
    """捕获 loguru 的 WARNING 及以上日志（服务用 loguru，不走 stdlib logging）。"""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(
        lambda message: messages.append(message.record["message"]), level="WARNING"
    )
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def test_restore_builtin_providers_backfills_base_url_from_catalog():
    """agnes / mimo 恢复不再失败，端点取自 catalog，且不挂自定义运行期客户端。"""
    config = MemoryConfig()
    config.providers["agnes"] = {
        "id": "agnes",
        "label": "Agnes AI",
        "enabled": True,
        "default_model": "agnes-2.5-flash",
    }
    config.providers["mimo"] = {
        "id": "mimo",
        "label": "小米 MiMo",
        "enabled": True,
        "default_model": "mimo-v2.5",
    }
    runtime = FakeRuntimeRouter()

    with _capture_warnings() as warnings:
        provider_service = ProviderService(
            config,
            _builtin_agnes_mimo_catalog(),
            runtime,
            credential_store=MemoryCredentials(),
        )

    assert [message for message in warnings if "restore_definition_failed" in message] == []
    assert (
        provider_service.catalog.get("agnes").endpoint.base_url
        == "https://apihub.agnes-ai.cn/v1"
    )
    assert (
        provider_service.catalog.get("mimo").endpoint.base_url
        == "https://api.xiaomimimo.com/v1"
    )
    assert runtime.list_custom_clients() == []
    # 恢复过程只读：持久化记录保持原样，不被清空也不被回写覆盖
    assert config.providers["agnes"] == {
        "id": "agnes",
        "label": "Agnes AI",
        "enabled": True,
        "default_model": "agnes-2.5-flash",
    }
    assert "base_url" not in config.providers["mimo"]


def test_restore_custom_provider_without_base_url_is_still_rejected():
    """自定义 provider 缺 base_url 仍被拒，校验强度不因内置回填而降低。"""
    config = MemoryConfig()
    config.providers["broken"] = {
        "id": "broken",
        "label": "Broken",
        "protocol": "openai_compatible",
        "enabled": True,
    }

    with _capture_warnings() as warnings:
        provider_service = ProviderService(
            config,
            ProviderCatalog(()),
            FakeRuntimeRouter(),
            credential_store=MemoryCredentials(),
        )

    assert any(
        "restore_definition_failed id=broken" in message
        and "base_url must be an http(s) URL" in message
        for message in warnings
    ), warnings
    with pytest.raises(KeyError):
        provider_service.catalog.get("broken")
