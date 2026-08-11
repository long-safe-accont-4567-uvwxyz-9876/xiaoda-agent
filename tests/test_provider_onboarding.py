from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.contracts import (
    AuthDefinition,
    EndpointDefinition,
    ProviderCapabilities,
    ProviderDefinition,
    ProviderProtocol,
)
from llm_gateway.provider_catalog import ProviderCatalog
from llm_gateway.provider_service import ProviderConnectionError, ProviderService
from llm_gateway.transports import CapabilityReport
from web.routers.auth import get_current_user
from web.routers.models import router as models_router
from web.routers.providers import router as providers_router


class MemoryConfig:
    def __init__(self) -> None:
        self.providers: dict[str, dict] = {}
        self.routes = {"chat": {"client": "mimo", "model": "mimo-v2.5"}}

    def get(self, path: str, default=None):
        if path == "models.providers":
            return {key: dict(value) for key, value in self.providers.items()}
        if path == "models.routes":
            return {key: dict(value) for key, value in self.routes.items()}
        if path.startswith("models.providers."):
            value = self.providers.get(path.rsplit(".", 1)[-1])
            return dict(value) if value else default
        return default

    def set(self, path: str, value) -> None:
        if path.startswith("models.providers."):
            self.providers[path.rsplit(".", 1)[-1]] = dict(value)

    def set_many(self, updates: dict) -> None:
        for path, value in updates.items():
            self.set(path, value)

    def delete(self, path: str) -> None:
        self.providers.pop(path.rsplit(".", 1)[-1], None)


class MemoryCredentials:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def read(self, provider_id: str) -> str:
        return self.values.get(provider_id, "")

    def write(self, provider_id: str, value: str) -> None:
        self.values[provider_id] = value

    def delete(self, provider_id: str) -> None:
        self.values.pop(provider_id, None)


class FailingConfig(MemoryConfig):
    def __init__(self, operation: str) -> None:
        super().__init__()
        self.operation = operation
        self.failed = False

    def set(self, path: str, value) -> None:
        if self.operation == "set" and not self.failed:
            self.failed = True
            raise OSError("config write failed")
        super().set(path, value)

    def delete(self, path: str) -> None:
        if self.operation == "delete" and not self.failed:
            self.failed = True
            raise OSError("config delete failed")
        super().delete(path)


@dataclass
class FakeTransport:
    report: CapabilityReport
    closed: bool = False

    async def health_check(self) -> CapabilityReport:
        return self.report

    async def discover_models(self) -> tuple[str, ...]:
        return self.report.models

    async def aclose(self) -> None:
        self.closed = True


def builtin_catalog() -> ProviderCatalog:
    return ProviderCatalog((ProviderDefinition(
        id="mimo",
        protocol=ProviderProtocol.OPENAI_COMPATIBLE,
        endpoint=EndpointDefinition(base_url="https://mimo.example/v1"),
        auth=AuthDefinition(environment_aliases=("MIMO_API_KEY",)),
        capabilities=ProviderCapabilities(tools=True, model_discovery=True),
        builtin=True,
        default_model="mimo-v2.5",
    ),))


def draft(**overrides) -> dict:
    value = {
        "id": "custom",
        "label": "Custom",
        "protocol": "openai_compatible",
        "base_url": "https://example.com/v1",
        "default_model": "custom-chat",
        "enabled": True,
    }
    value.update(overrides)
    return value


def custom_mapping_draft(**overrides) -> dict:
    value = draft(
        id="mapped",
        protocol="custom-map",
        default_model="mapped-chat",
        chat_path="/generate",
        models_path="/catalog",
        auth={"required": False, "header": "X-Key", "scheme": ""},
        headers={"X-Key": "{api_key}"},
        mapping={
            "request": {"messages": "input.messages", "model": "input.model"},
            "response": {"text": "result.text"},
            "stream": {"text": "delta.text"},
            "models": "data.*.id",
        },
    )
    value.update(overrides)
    return value


@pytest.fixture
def service():
    config = MemoryConfig()
    credentials = MemoryCredentials()
    runtime = SimpleNamespace(_custom_clients={})
    reports = {
        "https://example.com/v1": CapabilityReport(
            True,
            ProviderCapabilities(tools=True, streaming=True, model_discovery=True),
            models=("custom-chat", "custom-pro"),
        ),
        "https://example.org/v1": CapabilityReport(
            False,
            ProviderCapabilities(),
            error="health check failed",
        ),
    }

    def transport_factory(definition, credential):
        return FakeTransport(reports[definition.endpoint.base_url])

    result = ProviderService(
        config,
        builtin_catalog(),
        runtime,
        credential_store=credentials,
        transport_factory=transport_factory,
        runtime_client_factory=lambda definition, credential: (definition.id, credential),
    )
    return result, config, credentials, runtime


def test_provider_service_normalizes_custom_map_and_restores_mapping(service):
    provider_service, _, _, _ = service
    definition = provider_service._definition(custom_mapping_draft())

    assert definition.protocol is ProviderProtocol.CUSTOM_MAPPING
    assert definition.metadata["mapping"]["response"]["text"] == "result.text"
    assert definition.auth.required is False
    assert provider_service._record(definition)["chat_path"] == "/generate"


def test_provider_service_restores_persisted_custom_mapping_contract():
    config = MemoryConfig()
    config.providers["mapped"] = ProviderService._record(
        ProviderService._definition(custom_mapping_draft())
    )

    provider_service = ProviderService(
        config,
        builtin_catalog(),
        SimpleNamespace(_custom_clients={}),
        credential_store=MemoryCredentials(),
    )

    restored = provider_service.catalog.get("mapped")
    assert restored.protocol is ProviderProtocol.CUSTOM_MAPPING
    assert restored.endpoint.chat_path == "/generate"
    assert restored.endpoint.models_path == "/catalog"
    assert restored.auth == AuthDefinition(header="X-Key", scheme="", required=False)
    assert restored.metadata["headers"] == {"X-Key": "{api_key}"}
    assert restored.metadata["mapping"] == custom_mapping_draft()["mapping"]


def test_custom_mapping_factory_receives_persisted_contract(service):
    provider_service, _, _, _ = service
    definition = provider_service._definition(custom_mapping_draft())

    transport = provider_service._build_transport(definition, "secret")

    assert transport.__class__.__name__ == "CustomMappingTransport"
    assert transport._base_url == "https://example.com/v1"
    assert transport._chat_path == "/generate"
    assert transport._models_path == "/catalog"
    assert transport._headers == {"X-Key": "secret"}
    assert transport._mapping == custom_mapping_draft()["mapping"]
    assert transport.default_model == "mapped-chat"


@pytest.mark.asyncio
async def test_optional_auth_provider_can_be_tested_without_credentials():
    config = MemoryConfig()
    runtime = SimpleNamespace(_custom_clients={})
    received: list[str] = []

    def transport_factory(definition, credential):
        received.append(credential)
        return FakeTransport(CapabilityReport(True, definition.capabilities, models=(definition.default_model,)))

    provider_service = ProviderService(
        config,
        builtin_catalog(),
        runtime,
        credential_store=MemoryCredentials(),
        transport_factory=transport_factory,
    )

    report = await provider_service.test(custom_mapping_draft())

    assert report.available is True
    assert received == [""]


@pytest.mark.asyncio
async def test_failed_provider_update_preserves_runtime_disk_and_credential(service):
    provider_service, config, credentials, runtime = service
    await provider_service.create(draft(), {"api_key": "old-key"})
    before_config = config.get("models.providers")
    before_runtime = dict(runtime._custom_clients)
    before_credential = credentials.read("custom")

    with pytest.raises(ProviderConnectionError):
        await provider_service.update(
            "custom",
            draft(base_url="https://example.org/v1"),
            {"api_key": "new-key"},
        )

    assert config.get("models.providers") == before_config
    assert runtime._custom_clients == before_runtime
    assert credentials.read("custom") == before_credential


@pytest.mark.asyncio
async def test_create_discover_update_and_delete_are_reflected_everywhere(service):
    provider_service, config, credentials, runtime = service

    report = await provider_service.test(draft(), {"api_key": "secret"})
    created = await provider_service.create(draft(), {"api_key": "secret"})

    assert report.models == ("custom-chat", "custom-pro")
    assert created.id == "custom"
    assert config.get("models.providers.custom")["protocol"] == "openai_compatible"
    assert credentials.read("custom") == "secret"
    assert runtime._custom_clients["custom"] == ("custom", "secret")
    assert await provider_service.discover_models("custom") == ("custom-chat", "custom-pro")

    await provider_service.update("custom", draft(label="Changed"))
    assert config.get("models.providers.custom")["label"] == "Changed"

    await provider_service.delete("custom")
    assert config.get("models.providers.custom") is None
    assert credentials.read("custom") == ""
    assert "custom" not in runtime._custom_clients
    with pytest.raises(KeyError):
        provider_service.catalog.get("custom")


@pytest.mark.asyncio
async def test_failed_create_commit_rolls_back_credential_catalog_and_runtime():
    config = FailingConfig("set")
    credentials = MemoryCredentials()
    runtime = SimpleNamespace(_custom_clients={})
    service = ProviderService(
        config,
        builtin_catalog(),
        runtime,
        credential_store=credentials,
        transport_factory=lambda definition, credential: FakeTransport(CapabilityReport(
            True,
            ProviderCapabilities(),
            models=(definition.default_model,),
        )),
        runtime_client_factory=lambda definition, credential: object(),
    )

    with pytest.raises(OSError, match="config write failed"):
        await service.create(draft(), {"api_key": "secret"})

    assert config.get("models.providers.custom") is None
    assert credentials.read("custom") == ""
    assert runtime._custom_clients == {}
    with pytest.raises(KeyError):
        service.catalog.get("custom")


@pytest.mark.asyncio
async def test_compensation_failure_is_isolated_and_preserves_original_error():
    class FailingDeleteCredentials(MemoryCredentials):
        def delete(self, provider_id):
            raise OSError("credential delete failed")
    config = FailingConfig("set")
    runtime = SimpleNamespace(_custom_clients={})
    service = ProviderService(
        config,
        builtin_catalog(),
        runtime,
        credential_store=FailingDeleteCredentials(),
        transport_factory=lambda definition, credential: FakeTransport(CapabilityReport(
            True,
            ProviderCapabilities(),
            models=(definition.default_model,),
        )),
        runtime_client_factory=lambda definition, credential: object(),
    )

    with pytest.raises(OSError, match="config write failed") as excinfo:
        await service.create(draft(), {"api_key": "secret"})

    with pytest.raises(KeyError):
        service.catalog.get("custom")

    chain = []
    node = excinfo.value
    while node is not None:
        chain.append(str(node))
        node = node.__context__
    assert any("credential delete failed" in text for text in chain)


@pytest.mark.asyncio
async def test_rollback_failures_are_aggregated_in_chain_when_commit_and_rollback_fail():
    """提交与补偿都失败时：原始提交异常保持为主异常，补偿失败聚合到异常链。"""
    class FailingDeleteCredentials(MemoryCredentials):
        def delete(self, provider_id):
            raise OSError("credential delete failed")

    class FailingSetConfig(MemoryConfig):
        def __init__(self):
            super().__init__()
            self.failed = False

        def set(self, path, value):
            if not self.failed:
                self.failed = True
                raise OSError("config set failed")
            super().set(path, value)

    runtime = SimpleNamespace(_custom_clients={})
    service = ProviderService(
        FailingSetConfig(),
        builtin_catalog(),
        runtime,
        credential_store=FailingDeleteCredentials(),
        transport_factory=lambda definition, credential: FakeTransport(CapabilityReport(
            True,
            definition.capabilities,
            models=(definition.default_model,),
        )),
        runtime_client_factory=lambda definition, credential: object(),
    )

    with pytest.raises(OSError, match="config set failed") as excinfo:
        await service.create(draft(), {"api_key": "secret"})

    assert str(excinfo.value) == "config set failed"
    chain = []
    node = excinfo.value
    while node is not None:
        chain.append(str(node))
        node = node.__context__
    assert any("credential delete failed" in text for text in chain)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["credential_write", "config_set", "catalog_register"])
async def test_create_commit_step_failure_matrix(mode):
    """create 提交任一环节失败时，配置/凭证/runtime/catalog 全部回滚干净。"""
    class FailingCreds(MemoryCredentials):
        def write(self, provider_id, value):
            if mode == "credential_write":
                raise OSError("credential write failed")
            super().write(provider_id, value)

    class FailingCfg(MemoryConfig):
        def set(self, path, value):
            if mode == "config_set":
                raise OSError("config set failed")
            super().set(path, value)

    config = FailingCfg()
    credentials = FailingCreds()
    runtime = SimpleNamespace(_custom_clients={})
    service = ProviderService(
        config,
        builtin_catalog(),
        runtime,
        credential_store=credentials,
        transport_factory=lambda definition, credential: FakeTransport(CapabilityReport(
            True,
            definition.capabilities,
            models=(definition.default_model,),
        )),
        runtime_client_factory=lambda definition, credential: object(),
    )
    if mode == "catalog_register":
        def failing_register(definition, replace_existing=False):
            raise OSError("catalog register failed")
        service.catalog.register = failing_register

    with pytest.raises(OSError):
        await service.create(draft(), {"api_key": "secret"})

    assert config.get("models.providers.custom") is None
    assert credentials.read("custom") == ""
    assert runtime._custom_clients == {}
    with pytest.raises(KeyError):
        service.catalog.get("custom")


@pytest.mark.asyncio
async def test_update_commit_failure_restores_all_snapshots():
    """update 提交失败时恢复旧配置/凭证/runtime/catalog/report 快照。"""
    class FailingSetConfig(MemoryConfig):
        def __init__(self):
            super().__init__()
            self.fail_set = False

        def set(self, path, value):
            if self.fail_set:
                raise OSError("config set failed")
            super().set(path, value)

    config = FailingSetConfig()
    credentials = MemoryCredentials()
    runtime = SimpleNamespace(_custom_clients={})
    service = ProviderService(
        config,
        builtin_catalog(),
        runtime,
        credential_store=credentials,
        transport_factory=lambda definition, credential: FakeTransport(CapabilityReport(
            True,
            definition.capabilities,
            models=(definition.default_model,),
        )),
        runtime_client_factory=lambda definition, credential: (definition.id, credential),
    )
    await service.create(draft(), {"api_key": "old-key"})
    service._reports["custom"] = CapabilityReport(True, ProviderCapabilities(), models=("custom-chat",))
    config.fail_set = True

    with pytest.raises(OSError, match="config set failed"):
        await service.update("custom", draft(label="Changed"), {"api_key": "new-key"})

    assert config.get("models.providers.custom")["label"] == "Custom"
    assert credentials.read("custom") == "old-key"
    assert runtime._custom_clients["custom"] == ("custom", "old-key")
    assert service.catalog.get("custom").metadata.get("label") == "Custom"
    assert service._reports["custom"].models == ("custom-chat",)


@pytest.mark.asyncio
async def test_delete_commit_failure_restores_all_snapshots():
    """delete 提交失败时恢复旧配置/凭证/runtime/catalog/report 快照。"""
    class FailingDeleteConfig(MemoryConfig):
        def __init__(self):
            super().__init__()
            self.fail_delete = False

        def delete(self, path):
            if self.fail_delete:
                raise OSError("config delete failed")
            super().delete(path)

    config = FailingDeleteConfig()
    credentials = MemoryCredentials()
    runtime = SimpleNamespace(_custom_clients={})
    service = ProviderService(
        config,
        builtin_catalog(),
        runtime,
        credential_store=credentials,
        transport_factory=lambda definition, credential: FakeTransport(CapabilityReport(
            True,
            definition.capabilities,
            models=(definition.default_model,),
        )),
        runtime_client_factory=lambda definition, credential: (definition.id, credential),
    )
    await service.create(draft(), {"api_key": "secret"})
    service._reports["custom"] = CapabilityReport(True, ProviderCapabilities(), models=("custom-chat",))
    config.fail_delete = True

    with pytest.raises(OSError, match="config delete failed"):
        await service.delete("custom")

    assert config.get("models.providers.custom")["label"] == "Custom"
    assert credentials.read("custom") == "secret"
    assert runtime._custom_clients["custom"] == ("custom", "secret")
    assert service.catalog.get("custom").id == "custom"
    assert service._reports["custom"].models == ("custom-chat",)


def test_config_service_delete_rolls_back_memory_on_save_failure(tmp_path, monkeypatch):
    """ConfigService.delete 的 _save() 失败时必须恢复内存快照（Warning 4/Item 2）。"""
    import json

    from web.config_service import ConfigService

    path = tmp_path / "webui_overrides.json"
    path.write_text(json.dumps({
        "models": {"providers": {"custom": {"label": "Custom"}}},
    }), encoding="utf-8")
    cfg = ConfigService(path=path)

    def failing_save():
        raise OSError("disk full")

    monkeypatch.setattr(cfg, "_save", failing_save)

    with pytest.raises(OSError, match="disk full"):
        cfg.delete("models.providers.custom")

    assert cfg.get("models.providers.custom") == {"label": "Custom"}


def test_provider_credential_store_write_is_atomic_and_roundtrips(tmp_path, monkeypatch):
    """凭证写入走同目录临时文件 + 原子替换，不残留临时文件（Item 2）。"""
    from llm_gateway.provider_service import ProviderCredentialStore
    from web import _provider_keys

    monkeypatch.setattr(_provider_keys, "_get_cred_dir", lambda: tmp_path)
    store = ProviderCredentialStore()

    store.write("custom", "sk-very-secret-value-123")
    assert list(tmp_path.glob(".atomic_*")) == []
    key_file = tmp_path / "provider_custom.key"
    assert key_file.exists()
    assert store.read("custom") == "sk-very-secret-value-123"

    store.write("custom", "sk-replacement-value-456")
    assert list(tmp_path.glob(".atomic_*")) == []
    assert store.read("custom") == "sk-replacement-value-456"

    store.delete("custom")
    assert not key_file.exists()


@pytest.mark.asyncio
async def test_concurrent_create_and_update_are_serialized(service):
    """同 ID 的 create 与 update 并发应串行化：create 成功后 update 生效。"""
    provider_service, config, credentials, runtime = service
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def staged(definition, credential):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return CapabilityReport(True, definition.capabilities, models=(definition.default_model,)), (definition.id, credential)

    provider_service._stage = staged
    first = asyncio.create_task(provider_service.create(draft(), {"api_key": "first"}))
    await entered.wait()
    second = asyncio.create_task(provider_service.update("custom", draft(label="Updated"), {"api_key": "second"}))
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert sum(isinstance(result, ProviderDefinition) for result in results) == 2
    assert config.get("models.providers.custom")["label"] == "Updated"
    assert credentials.read("custom") == "second"


@pytest.mark.asyncio
async def test_concurrent_update_and_delete_are_serialized(service):
    """同 ID 的 update 与 delete 并发应串行化：最终状态一致（provider 被删除）。"""
    provider_service, config, credentials, runtime = service
    await provider_service.create(draft(), {"api_key": "initial"})
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def staged(definition, credential):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return CapabilityReport(True, definition.capabilities, models=(definition.default_model,)), (definition.id, credential)

    provider_service._stage = staged
    first = asyncio.create_task(provider_service.update("custom", draft(label="Changed"), {"api_key": "updated"}))
    await entered.wait()
    second = asyncio.create_task(provider_service.delete("custom"))
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert all(not isinstance(result, Exception) for result in results)
    assert config.get("models.providers.custom") is None
    assert credentials.read("custom") == ""
    assert "custom" not in runtime._custom_clients


def test_non_ollama_rejects_non_loopback_local_hosts(service):
    provider_service, _, _, _ = service
    for base_url in ("http://host.docker.internal:8000/v1", "http://0.0.0.0:8000/v1"):
        with pytest.raises(ValueError, match="safety"):
            provider_service._definition(draft(base_url=base_url))


@pytest.mark.asyncio
async def test_concurrent_create_failure_cannot_rollback_successful_create(service):
    provider_service, config, credentials, runtime = service
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def staged(definition, credential):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return CapabilityReport(True, definition.capabilities, models=(definition.default_model,)), (definition.id, credential)

    provider_service._stage = staged
    first = asyncio.create_task(provider_service.create(draft(), {"api_key": "first"}))
    await entered.wait()
    second = asyncio.create_task(provider_service.create(draft(), {"api_key": "second"}))
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert sum(isinstance(result, ProviderDefinition) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert config.get("models.providers.custom") is not None
    assert credentials.read("custom") in {"first", "second"}
    assert runtime._custom_clients["custom"] == ("custom", credentials.read("custom"))


def test_custom_mapping_rejects_literal_secret_headers(service):
    provider_service, _, _, _ = service

    with pytest.raises(ValueError, match="header"):
        provider_service._definition(custom_mapping_draft(headers={"Authorization": "Bearer real-secret"}))


def test_non_ollama_loopback_localhost_internal_service_is_rejected(service):
    """SSRF 收窄契约：仅 Ollama 允许 loopback；非 Ollama 协议指向 localhost 内部服务应被拒绝。

    旧策略对所有协议统一豁免 localhost，现收窄为仅 Ollama（loopback:11434）例外。
    """
    provider_service, _, _, _ = service
    from security.ssrf_guard import validate_url

    for base_url in (
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
        "http://[::1]:8000/v1",
    ):
        with pytest.raises(ValueError, match="safety"):
            provider_service._definition(draft(base_url=base_url))
        with pytest.raises(ValueError, match="safety"):
            provider_service._definition(custom_mapping_draft(base_url=base_url))
    # Ollama 仍允许 loopback 标准端口
    assert provider_service._definition(
        draft(protocol="ollama", base_url="http://127.0.0.1:11434")
    ).protocol is ProviderProtocol.OLLAMA
    # 公网 URL 仍放行
    allowed, reason = validate_url("https://example.com/v1")
    assert allowed is True
    assert reason == ""
    with pytest.raises(ValueError, match="safety"):
        provider_service._definition(draft(base_url="http://169.254.169.254/latest"))


def test_ollama_local_provider_requires_loopback_and_standard_port(service):
    provider_service, _, _, _ = service

    definition = provider_service._definition(draft(protocol="ollama", base_url="http://127.0.0.1:11434"))
    assert definition.protocol is ProviderProtocol.OLLAMA
    with pytest.raises(ValueError, match="Ollama"):
        provider_service._definition(draft(protocol="ollama", base_url="http://127.0.0.1:8000"))


def test_ollama_allows_only_strict_local_allowlist(service):
    """Item 3：仅允许 http://localhost:11434、http://127.0.0.1:11434、http://[::1]:11434。"""
    provider_service, _, _, _ = service
    for base_url in (
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
    ):
        definition = provider_service._definition(draft(protocol="ollama", base_url=base_url))
        assert definition.protocol is ProviderProtocol.OLLAMA
        assert definition.endpoint.base_url == base_url


def test_ollama_rejects_https_and_noncanonical_local_hosts(service):
    """Item 3：拒绝 HTTPS 本地目标、非规范回环别名、0.0.0.0、容器宿主别名与非 11434 端口。"""
    provider_service, _, _, _ = service
    for base_url in (
        "https://localhost:11434",
        "http://localhost.localdomain:11434",
        "http://ip6-localhost:11434",
        "http://ip6-loopback:11434",
        "http://0.0.0.0:11434",
        "http://host.docker.internal:11434",
        "http://host.minikube.internal:11434",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
    ):
        with pytest.raises(ValueError, match="Ollama"):
            provider_service._definition(draft(protocol="ollama", base_url=base_url))


def test_legacy_provider_create_rejects_localhost_base_url(service, monkeypatch):
    """Item 3：旧 /models/providers 入口对 localhost base_url 直接 400（无通用豁免）。"""
    provider_service, config, _, runtime = service
    app = FastAPI()
    app.state.provider_service = provider_service
    app.state.core = SimpleNamespace(router=runtime)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.include_router(models_router, prefix="/api/v1")
    monkeypatch.setattr("web.routers.models._cfg", lambda request: config)

    for base_url in ("http://localhost:8000/v1", "http://127.0.0.1:11434"):
        response = TestClient(app).post(
            "/api/v1/models/providers",
            json={"id": "custom", "format": "openai", "base_url": base_url, "api_key": "secret"},
        )
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_saved_custom_mapping_runtime_client_is_compat_client():
    from web.custom_providers import CustomMappingCompatClient

    config = MemoryConfig()
    credentials = MemoryCredentials()
    runtime = SimpleNamespace(_custom_clients={})
    provider_service = ProviderService(
        config,
        builtin_catalog(),
        runtime,
        credential_store=credentials,
        transport_factory=lambda definition, credential: FakeTransport(CapabilityReport(
            True,
            definition.capabilities,
            models=(definition.default_model,),
        )),
    )

    await provider_service.create(
        custom_mapping_draft(base_url="https://example.com/v1"),
        {"api_key": "secret"},
    )

    client = runtime._custom_clients["mapped"]
    assert isinstance(client, CustomMappingCompatClient)
    assert callable(client.chat.completions.create)


@pytest.mark.asyncio
async def test_saved_ollama_runtime_client_uses_openai_v1_base_url():
    from openai import AsyncOpenAI

    config = MemoryConfig()
    credentials = MemoryCredentials()
    runtime = SimpleNamespace(_custom_clients={})
    provider_service = ProviderService(
        config,
        builtin_catalog(),
        runtime,
        credential_store=credentials,
        transport_factory=lambda definition, credential: FakeTransport(CapabilityReport(
            True,
            definition.capabilities,
            models=(definition.default_model,),
        )),
    )

    await provider_service.create(
        draft(protocol="ollama", base_url="http://127.0.0.1:11434", id="local-ollama"),
        {"api_key": ""},
    )

    client = runtime._custom_clients["local-ollama"]
    assert isinstance(client, AsyncOpenAI)
    assert str(client.base_url).endswith("/v1/")


def test_restart_rebuilds_runtime_client_from_persisted_config_and_credentials():
    from web.custom_providers import CustomMappingCompatClient

    config = MemoryConfig()
    credentials = MemoryCredentials()
    config.providers["mapped"] = ProviderService._record(
        ProviderService._definition(custom_mapping_draft(base_url="https://example.com/v1"))
    )
    credentials.values["mapped"] = "secret"

    ProviderService(
        config,
        builtin_catalog(),
        SimpleNamespace(_custom_clients={}),
        credential_store=credentials,
    )
    restarted = ProviderService(
        config,
        builtin_catalog(),
        SimpleNamespace(_custom_clients={}),
        credential_store=credentials,
    )

    client = restarted._runtime_clients().get("mapped")
    assert isinstance(client, CustomMappingCompatClient)


def test_restart_skips_provider_missing_required_credential_without_crashing():
    config = MemoryConfig()
    credentials = MemoryCredentials()
    config.providers["custom"] = ProviderService._record(
        ProviderService._definition(draft(id="custom", base_url="https://example.com/v1"))
    )

    restarted = ProviderService(
        config,
        builtin_catalog(),
        SimpleNamespace(_custom_clients={}),
        credential_store=credentials,
    )

    assert restarted.catalog.get("custom").id == "custom"
    assert "custom" not in restarted._runtime_clients()


def test_provider_id_rejects_dots_that_would_collide_in_credential_files(service):
    provider_service, _, _, _ = service

    with pytest.raises(ValueError, match="provider id"):
        provider_service._definition(draft(id="a.b"))
    with pytest.raises(ValueError, match="provider id"):
        provider_service._definition(draft(id="a/b"))

    assert provider_service._definition(draft(id="ab")).id == "ab"


def test_builtin_route_rejects_missing_runtime_client(service):
    provider_service, _, _, _ = service

    assert provider_service.validate_route("mimo", "mimo-v2.5") == "unavailable"


def test_builtin_route_accepts_configured_runtime_client(service):
    provider_service, _, _, runtime = service
    runtime._client = object()
    runtime._is_client_configured = lambda provider: provider == "mimo"

    assert provider_service.validate_route("mimo", "mimo-v2.5") is None


def test_route_update_unknown_provider_returns_404(service, monkeypatch):
    provider_service, config, _, runtime = service
    app = FastAPI()
    app.state.provider_service = provider_service
    app.state.core = SimpleNamespace(router=runtime)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.include_router(models_router, prefix="/api/v1")
    monkeypatch.setattr("web.routers.models._cfg", lambda request: config)

    response = TestClient(app).put(
        "/api/v1/models/routes/chat",
        json={"provider": "ghost", "model": "x"},
    )

    assert response.status_code == 404


def test_builtin_route_unavailable_returns_409(service, monkeypatch):
    provider_service, config, _, runtime = service
    app = FastAPI()
    app.state.provider_service = provider_service
    app.state.core = SimpleNamespace(router=runtime)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.include_router(models_router, prefix="/api/v1")
    monkeypatch.setattr("web.routers.models._cfg", lambda request: config)

    response = TestClient(app).put(
        "/api/v1/models/routes/chat",
        json={"provider": "mimo", "model": "mimo-v2.5"},
    )

    assert response.status_code == 409


def test_route_rejects_disabled_provider(service, monkeypatch):
    provider_service, config, _, runtime = service
    config.providers["disabled"] = draft(id="disabled", enabled=False)
    provider_service.catalog.register(provider_service._definition(config.providers["disabled"]))
    app = FastAPI()
    app.state.provider_service = provider_service
    app.state.core = SimpleNamespace(router=runtime)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.include_router(models_router, prefix="/api/v1")
    monkeypatch.setattr("web.routers.models._cfg", lambda request: config)

    response = TestClient(app).put(
        "/api/v1/models/routes/chat",
        json={"provider": "disabled", "model": "x"},
    )

    assert response.status_code == 409


def test_route_provider_only_update_validates_effective_model(service, monkeypatch):
    provider_service, config, _, runtime = service
    config.providers["custom"] = draft()
    provider_service.catalog.register(provider_service._definition(config.providers["custom"]))
    provider_service._reports["custom"] = CapabilityReport(
        True,
        ProviderCapabilities(),
        models=("mimo-v2.5",),
    )
    runtime._custom_clients["custom"] = object()
    registry = SimpleNamespace(
        get_task_ref=lambda task: {"client": "mimo", "model": "mimo-v2.5"},
        update_route=lambda *args, **kwargs: None,
    )
    runtime._registry = registry
    runtime.TASK_TIMEOUTS = {}
    app = FastAPI()
    app.state.provider_service = provider_service
    app.state.core = SimpleNamespace(router=runtime)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.include_router(models_router, prefix="/api/v1")
    monkeypatch.setattr("web.routers.models._cfg", lambda request: config)
    monkeypatch.setattr("model_router.ROUTE_TABLE", {"chat": {"client": "mimo", "model": "mimo-v2.5"}})

    response = TestClient(app).put(
        "/api/v1/models/routes/chat",
        json={"provider": "custom"},
    )

    assert response.status_code == 200


def test_provider_api_exposes_test_crud_capabilities_and_discovery(service):
    provider_service, _, _, _ = service
    app = FastAPI()
    app.state.provider_service = provider_service
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.include_router(providers_router, prefix="/api/v1")
    client = TestClient(app)

    tested = client.post("/api/v1/providers/test", json={"draft": draft(), "credentials": {"api_key": "secret"}})
    created = client.post("/api/v1/providers", json={"draft": draft(), "credentials": {"api_key": "secret"}})
    capabilities = client.get("/api/v1/providers/custom/capabilities")
    discovered = client.get("/api/v1/providers/custom/models")
    deleted = client.delete("/api/v1/providers/custom", headers={"X-Confirm": "yes"})

    assert tested.status_code == 200
    assert tested.json()["data"]["available"] is True
    assert created.status_code == 200
    assert capabilities.json()["data"]["capabilities"]["tools"] is True
    assert discovered.json()["data"]["models"] == ["custom-chat", "custom-pro"]
    assert deleted.status_code == 200


def test_provider_api_serializes_safe_custom_mapping_contract(service):
    provider_service, _, _, _ = service
    definition = provider_service._definition(custom_mapping_draft())
    provider_service.catalog.register(definition)
    app = FastAPI()
    app.state.provider_service = provider_service
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.include_router(providers_router, prefix="/api/v1")

    response = TestClient(app).get("/api/v1/providers")

    assert response.status_code == 200
    item = next(value for value in response.json()["data"] if value["id"] == "mapped")
    assert item["chat_path"] == "/generate"
    assert item["models_path"] == "/catalog"
    assert item["auth"] == {"required": False, "header": "X-Key", "scheme": ""}
    assert item["mapping"]["response"]["text"] == "result.text"
    assert item["headers"] == {"X-Key": "{api_key}"}
    assert "secret" not in response.text


def test_provider_api_maps_missing_conflict_and_validation_errors(service):
    provider_service, config, _, _ = service
    app = FastAPI()
    app.state.provider_service = provider_service
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.include_router(providers_router, prefix="/api/v1")
    client = TestClient(app)

    invalid = client.post(
        "/api/v1/providers/test",
        json={"draft": custom_mapping_draft(base_url="file:///tmp/model")},
    )
    missing = client.put("/api/v1/providers/missing", json={"draft": draft()})
    config.routes["chat"] = {"client": "custom", "model": "custom-chat"}
    provider_service.catalog.register(provider_service._definition(draft()))
    conflict = client.delete("/api/v1/providers/custom", headers={"X-Confirm": "yes"})

    assert invalid.status_code == 400
    assert missing.status_code == 404
    assert conflict.status_code == 409


def test_provider_api_rejects_private_network_endpoint(service):
    provider_service, _, _, _ = service
    app = FastAPI()
    app.state.provider_service = provider_service
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.include_router(providers_router, prefix="/api/v1")

    response = TestClient(app).post(
        "/api/v1/providers/test",
        json={"draft": draft(base_url="http://169.254.169.254/latest"), "credentials": {"api_key": "secret"}},
    )

    assert response.status_code == 400


def test_legacy_provider_update_uses_atomic_service(service, monkeypatch):
    provider_service, config, credentials, runtime = service
    config.providers["custom"] = draft()
    credentials.values["custom"] = "old-key"
    runtime._custom_clients["custom"] = ("custom", "old-key")
    provider_service.catalog.register(provider_service._definition(config.providers["custom"]))
    app = FastAPI()
    app.state.provider_service = provider_service
    app.state.core = SimpleNamespace(router=runtime)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.include_router(models_router, prefix="/api/v1")
    monkeypatch.setattr("web.routers.models._cfg", lambda request: config)
    monkeypatch.setattr("security.ssrf_guard.is_local_host", lambda url: True)
    before = config.get("models.providers")

    response = TestClient(app).put(
        "/api/v1/models/providers/custom",
        json={"base_url": "https://example.org/v1", "api_key": "new-key"},
    )

    assert response.status_code == 422
    assert config.get("models.providers") == before
    assert credentials.read("custom") == "old-key"
    assert runtime._custom_clients["custom"] == ("custom", "old-key")


def test_legacy_key_update_preserves_state_when_health_check_fails(service, monkeypatch):
    provider_service, config, credentials, runtime = service
    config.providers["custom"] = draft()
    credentials.values["custom"] = "old-key"
    runtime._custom_clients["custom"] = ("custom", "old-key")
    provider_service.catalog.register(provider_service._definition(config.providers["custom"]))
    provider_service._transport_factory = lambda definition, credential: FakeTransport(CapabilityReport(
        False,
        definition.capabilities,
        error="invalid credential",
    ))
    app = FastAPI()
    app.state.provider_service = provider_service
    app.state.core = SimpleNamespace(router=runtime)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.include_router(models_router, prefix="/api/v1")
    monkeypatch.setattr("web.routers.models._cfg", lambda request: config)

    response = TestClient(app).post(
        "/api/v1/models/providers/custom/key",
        json={"api_key": "invalid-key"},
    )

    assert response.status_code == 422
    assert credentials.read("custom") == "old-key"
    assert runtime._custom_clients["custom"] == ("custom", "old-key")
