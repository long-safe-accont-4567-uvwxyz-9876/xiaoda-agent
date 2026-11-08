"""内置 provider（agnes / mimo）启动恢复的回归测试。

回归对象一：provider_service.restore_definition_failed id=agnes / id=mimo
error=base_url must be an http(s) URL（曾每次启动固定两条）。
回归对象二：catalog 内置定义缺 endpoint 时必须单独打
provider_service.restore_missing_endpoint，而不是被吞成 restore_definition_failed。

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
        self.set_calls: list[str] = []

    def get_custom_client(self, provider_id):
        return self._custom_clients.get(provider_id)

    def set_custom_client(self, provider_id, client):
        self.set_calls.append(provider_id)
        self._custom_clients[provider_id] = client

    def remove_custom_client(self, provider_id):
        self._custom_clients.pop(provider_id, None)

    def has_custom_client(self, provider_id):
        return provider_id in self._custom_clients

    def list_custom_clients(self):
        return list(self._custom_clients.items())


class RecordingProviderService(ProviderService):
    """记录恢复流程真正交给 _definition 的草稿。

    断言必须落在草稿上：catalog 本身就是输入，断 catalog 里的 endpoint 等于
    断输入没变，回填逻辑改坏了也不会失败。
    """

    def __init__(self, *args, **kwargs):
        self.definition_drafts: list[dict] = []
        super().__init__(*args, **kwargs)

    def _definition(self, draft):
        self.definition_drafts.append(dict(draft))
        return super()._definition(draft)


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


def test_restore_builtin_providers_backfills_catalog_endpoint_into_draft():
    """内置 provider 恢复时草稿里的端点来自 catalog，用户旧值被覆盖、不挂 runtime client。"""
    config = MemoryConfig()
    config.providers["agnes"] = {
        "id": "agnes",
        "label": "Agnes AI",
        "enabled": True,
        "default_model": "agnes-2.5-flash",
        "base_url": "https://stale.example.invalid/v1",
    }
    config.providers["mimo"] = {
        "id": "mimo",
        "label": "小米 MiMo",
        "enabled": True,
        "default_model": "mimo-v2.5",
    }
    runtime = FakeRuntimeRouter()

    with _capture_warnings() as warnings:
        provider_service = RecordingProviderService(
            config,
            _builtin_agnes_mimo_catalog(),
            runtime,
            credential_store=MemoryCredentials(),
        )

    drafts = {draft["id"]: draft for draft in provider_service.definition_drafts}

    assert [message for message in warnings if "restore_definition_failed" in message] == []
    assert [message for message in warnings if "restore_missing_endpoint" in message] == []

    assert set(drafts) == {"agnes", "mimo"}, drafts
    assert drafts["agnes"]["base_url"] == "https://apihub.agnes-ai.cn/v1"
    assert drafts["mimo"]["base_url"] == "https://api.xiaomimimo.com/v1"
    assert drafts["agnes"]["protocol"] == "openai_compatible"
    assert drafts["agnes"]["chat_path"] == "/chat/completions"
    assert drafts["agnes"]["models_path"] == "/models"
    assert drafts["agnes"]["builtin"] is True
    assert drafts["mimo"]["builtin"] is True

    assert runtime.set_calls == []
    assert runtime.list_custom_clients() == []

    # 恢复过程只读：持久化记录保持原样（含用户旧 base_url），不被清空也不被回写覆盖
    assert config.providers["agnes"] == {
        "id": "agnes",
        "label": "Agnes AI",
        "enabled": True,
        "default_model": "agnes-2.5-flash",
        "base_url": "https://stale.example.invalid/v1",
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


def test_restore_builtin_provider_missing_catalog_endpoint_warns_distinctly():
    """catalog 内置定义缺 endpoint / 缺 base_url 时报 restore_missing_endpoint，不再伪报定义非法。"""
    config = MemoryConfig()
    for provider_id in ("noendpoint", "nobase"):
        config.providers[provider_id] = {
            "id": provider_id,
            "label": provider_id,
            "enabled": True,
            "default_model": "demo-model",
        }

    catalog = ProviderCatalog((
        ProviderDefinition(
            id="noendpoint",
            protocol=ProviderProtocol.OPENAI_COMPATIBLE,
            endpoint=None,
            auth=AuthDefinition(environment_aliases=("NOENDPOINT_API_KEY",)),
            builtin=True,
        ),
        ProviderDefinition(
            id="nobase",
            protocol=ProviderProtocol.OPENAI_COMPATIBLE,
            endpoint=EndpointDefinition(base_url=""),
            auth=AuthDefinition(environment_aliases=("NOBASE_API_KEY",)),
            builtin=True,
        ),
    ))
    runtime = FakeRuntimeRouter()

    with _capture_warnings() as warnings:
        provider_service = RecordingProviderService(
            config, catalog, runtime, credential_store=MemoryCredentials()
        )

    missing = [message for message in warnings if "restore_missing_endpoint" in message]
    assert any("id=noendpoint" in message and "missing=endpoint" in message for message in missing), warnings
    assert any("id=nobase" in message and "missing=endpoint.base_url" in message for message in missing), warnings
    assert [message for message in warnings if "restore_definition_failed" in message] == []

    assert runtime.set_calls == []
    assert provider_service.definition_drafts == []
    assert config.providers["noendpoint"]["default_model"] == "demo-model"
    assert "base_url" not in config.providers["nobase"]
