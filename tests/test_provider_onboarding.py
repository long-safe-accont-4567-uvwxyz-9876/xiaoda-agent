from __future__ import annotations

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
