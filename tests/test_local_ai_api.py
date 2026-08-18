from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers import local_deploy
from web.routers.auth import get_current_user
from web.routers.local_ai import (
    attach_local_ai_services,
    initialize_local_ai_services,
    local_ai_event_sink,
)
from web.routers.local_ai import (
    router as local_ai_router,
)
from web.routers.local_ai_storage import router as local_ai_storage_router
from web.routers.local_deploy import router as local_deploy_router
from web.ws_hub import local_ai_event


class Record:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class FakeDevices:
    def __init__(self) -> None:
        self.items = [
            Record(
                id="cpu:0",
                name="Test CPU",
                kind="cpu",
                architecture="x86_64",
                state="available",
                memory_total=16_000,
                memory_available=12_000,
                backends=[],
                system={},
                evidence={},
            )
        ]

    def scan(self, force: bool = False) -> list[Record]:
        return self.items


class FakeCatalog:
    def __init__(self) -> None:
        self.items = [Record(id="catalog:qwen", purpose="chat", download_size=4)]

    def filter(self, purpose: str | None, max_download_bytes: int | None, advanced: bool) -> list[Record]:
        return self.items


class FakeModels:
    def __init__(self) -> None:
        self.items = [Record(id="installed:qwen", removable=True, directory="/tmp/models/installed:qwen")]
        self.removed: list[str] = []

    async def list(self) -> list[Record]:
        return self.items

    async def get(self, model_id: str) -> Record | None:
        return next((item for item in self.items if item.id == model_id), None)

    async def remove(self, model_id: str) -> None:
        self.removed.append(model_id)


class FakeDownloads:
    def __init__(self) -> None:
        self.items: list[Record] = []
        self.started: list[str] = []

    def list(self) -> list[Record]:
        return self.items

    def active_for_model(self, model_id: str) -> list[Record]:
        terminal = {"completed", "failed", "cancelled", "quarantined"}
        return [
            item
            for item in self.items
            if item.model_id == model_id and item.state not in terminal
        ]

    def create(self, model: Record, destination: str) -> Record:
        task = Record(id="download:one", model_id=model.id, destination=destination, state="pending")
        self.items.append(task)
        return task

    async def start(self, task_id: str) -> Record:
        self.started.append(task_id)
        return self.items[0]

    async def pause(self, task_id: str) -> Record:
        return self.items[0]

    async def resume(self, task_id: str) -> Record:
        return self.items[0]

    async def cancel(self, task_id: str, discard_partials: bool = False) -> Record:
        return self.items[0]


class FakeInstances:
    def __init__(self) -> None:
        self.items: list[Record] = []

    def list(self) -> list[Record]:
        return self.items

    async def start(self, model_id: str, backend_override: str | None = None) -> Record:
        instance = Record(id="instance:one", model_id=model_id, device_id=backend_override or "cpu:0", state="running")
        self.items.append(instance)
        return instance

    async def stop(self, instance_id: str) -> None:
        self.items = [item for item in self.items if item.id != instance_id]

    def model_in_use(self, model_id: str) -> bool:
        return any(item.model_id == model_id for item in self.items)

    def get(self, instance_id: str) -> Record | None:
        return next((item for item in self.items if item.id == instance_id), None)


class FakeStoragePolicy:
    def __init__(self) -> None:
        self.validations: list[tuple[str, int]] = []
        self.result: Record | None = None

    def validate_destination(self, path: str, required_bytes: int) -> Record:
        self.validations.append((path, required_bytes))
        return self.result or Record(path=path, writable=True, error=None, reason=None)


@pytest.fixture
def services() -> SimpleNamespace:
    events: list[dict[str, Any]] = []

    async def broadcast(event: dict[str, Any]) -> None:
        events.append(event)

    return SimpleNamespace(
        devices=FakeDevices(),
        catalog=FakeCatalog(),
        models=FakeModels(),
        downloads=FakeDownloads(),
        instances=FakeInstances(),
        storage_policy=FakeStoragePolicy(),
        broadcast=broadcast,
        events=events,
    )


@pytest.fixture
def app(services: SimpleNamespace) -> FastAPI:
    api = FastAPI()
    api.state.local_ai = services
    api.include_router(local_ai_router, prefix="/api/v1")
    api.include_router(local_deploy_router, prefix="/api/v1")
    api.include_router(local_ai_storage_router, prefix="/api/v1")
    return api


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    return TestClient(app)


def data(response) -> Any:
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_all_local_ai_resources_require_auth(app: FastAPI) -> None:
    client = TestClient(app)
    for path in (
        "/api/v1/local-ai/devices",
        "/api/v1/local-ai/catalog",
        "/api/v1/local-ai/models",
        "/api/v1/local-ai/downloads",
        "/api/v1/local-ai/instances",
        "/api/v1/local-ai/storage",
    ):
        assert client.get(path).status_code == 401


def test_resource_collections_are_exposed(client: TestClient) -> None:
    assert data(client.get("/api/v1/local-ai/devices"))[0]["id"] == "cpu:0"
    assert data(client.get("/api/v1/local-ai/catalog"))[0]["id"] == "catalog:qwen"
    assert data(client.get("/api/v1/local-ai/models"))[0]["id"] == "installed:qwen"
    assert data(client.get("/api/v1/local-ai/downloads")) == []
    assert data(client.get("/api/v1/local-ai/instances")) == []


def test_main_app_mounts_all_local_ai_resource_routes() -> None:
    from web.server import create_app

    # FastAPI 新版 app.routes 含 _IncludedRouter 包装（内部才持有具体路由），
    # 需递归展开 original_router 取 path。
    # 不同 FastAPI 版本下 _IncludedRouter.original_router.routes 暴露的 path
    # 可能带 /api/v1 前缀（0.115/0.136）也可能不带（0.137+），统一剥离前缀后比对，
    # 避免跨版本脆弱。
    def _iter_paths(routes):
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                yield from _iter_paths(inner.routes)
            elif hasattr(route, "path"):
                path = route.path
                if path.startswith("/api/v1/"):
                    path = path[len("/api/v1"):]
                yield path

    paths = set(_iter_paths(create_app().routes))
    assert {
        "/local-ai/devices",
        "/local-ai/catalog",
        "/local-ai/models",
        "/local-ai/downloads",
        "/local-ai/instances",
        "/local-ai/storage",
    } <= paths


def test_local_ai_services_attach_to_application_state(tmp_path) -> None:
    from unittest.mock import Mock

    api = FastAPI()
    core = SimpleNamespace(db=SimpleNamespace(local_ai=Mock()))

    async def broadcast(event: dict[str, Any]) -> None:
        pass

    services = attach_local_ai_services(api, core, broadcast, tmp_path / "downloads.json")
    assert api.state.local_ai is services
    assert services.downloads._state_path == tmp_path / "downloads.json"


def test_local_ai_services_reuse_core_instance_manager(tmp_path) -> None:
    from unittest.mock import Mock

    api = FastAPI()
    shared = Mock()
    core = SimpleNamespace(
        db=SimpleNamespace(local_ai=Mock()),
        local_ai_instances=shared,
    )

    services = attach_local_ai_services(api, core, object(), tmp_path / "downloads.json")

    assert services.instances is shared


def test_local_ai_service_initialization_recovers_downloads(monkeypatch, tmp_path) -> None:
    recovered: list[bool] = []

    class Downloads:
        async def recover(self) -> None:
            recovered.append(True)

    services = SimpleNamespace(downloads=Downloads())
    monkeypatch.setattr(
        "web.routers.local_ai.attach_local_ai_services",
        lambda app, core, broadcast, state_path: services,
    )
    result = asyncio.run(
        initialize_local_ai_services(FastAPI(), object(), object(), tmp_path / "downloads.json")
    )
    assert result is services
    assert recovered == [True]


def test_websocket_local_ai_events_have_canonical_resource_keys() -> None:
    assert local_ai_event("device", Record(id="cpu:0")) == {
        "type": "local_ai_device_updated",
        "device": {"id": "cpu:0"},
    }
    assert local_ai_event("download", Record(id="download:one")) == {
        "type": "local_ai_download_updated",
        "download": {"id": "download:one"},
    }
    assert local_ai_event("instance", Record(id="instance:one")) == {
        "type": "local_ai_instance_updated",
        "instance": {"id": "instance:one"},
    }
    with pytest.raises(ValueError, match="unsupported Local AI resource"):
        local_ai_event("arbitrary", Record(id="unsafe"))


def test_download_event_sink_translates_task_to_canonical_download_key() -> None:
    events: list[dict[str, Any]] = []

    async def broadcast(event: dict[str, Any]) -> None:
        events.append(event)

    sink = local_ai_event_sink(broadcast)
    asyncio.run(sink({"type": "local_ai_download_updated", "task": {"id": "download:one"}}))
    assert events == [{
        "type": "local_ai_download_updated",
        "download": {"id": "download:one"},
    }]


def test_download_create_requires_destination_and_is_idempotent_by_request_id(
    client: TestClient,
    services: SimpleNamespace,
) -> None:
    assert client.post(
        "/api/v1/local-ai/downloads",
        json={"model_id": "catalog:qwen", "request_id": "request:one"},
    ).status_code == 422
    payload = {"model_id": "catalog:qwen", "destination": "/models", "request_id": "request:one"}
    first = client.post("/api/v1/local-ai/downloads", json=payload)
    second = client.post("/api/v1/local-ai/downloads", json=payload)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["data"]["task"]["id"] == "download:one"
    assert second.json()["data"]["task"]["id"] == "download:one"
    assert len(services.downloads.items) == 1


def test_request_id_is_generated_when_omitted(client: TestClient) -> None:
    download = client.post(
        "/api/v1/local-ai/downloads",
        json={"model_id": "catalog:qwen", "destination": "/models"},
    )
    instance = client.post(
        "/api/v1/local-ai/instances",
        json={"model_id": "installed:qwen", "device_id": "cpu:0"},
    )

    assert download.status_code == 202
    assert instance.status_code == 202
    assert instance.json()["data"]["task_id"]


def test_download_uses_storage_policy_before_creating_task(
    client: TestClient,
    services: SimpleNamespace,
) -> None:
    services.storage_policy.result = Record(
        path="/models",
        writable=False,
        error="insufficient free space",
        reason="insufficient free space",
    )

    response = client.post(
        "/api/v1/local-ai/downloads",
        json={"model_id": "catalog:qwen", "destination": "/models"},
    )

    assert response.status_code == 422
    assert services.storage_policy.validations == [("/models", 4)]
    assert services.downloads.items == []


def test_reusing_download_request_id_with_different_input_is_rejected(
    client: TestClient,
    services: SimpleNamespace,
) -> None:
    first = client.post(
        "/api/v1/local-ai/downloads",
        json={"model_id": "catalog:qwen", "destination": "/models", "request_id": "request:conflict"},
    )
    second = client.post(
        "/api/v1/local-ai/downloads",
        json={"model_id": "catalog:qwen", "destination": "/other", "request_id": "request:conflict"},
    )
    assert first.status_code == 202
    assert second.status_code == 409
    assert len(services.downloads.items) == 1


def test_remove_model_requires_confirmation(
    client: TestClient,
    services: SimpleNamespace,
) -> None:
    path = "/api/v1/local-ai/models/installed:qwen"
    assert client.delete(path).status_code == 400
    assert services.models.removed == []
    assert client.delete(path, headers={"X-Confirm": "yes"}).status_code == 204
    assert services.models.removed == ["installed:qwen"]


def test_remove_model_rejects_unknown_model(
    client: TestClient,
) -> None:
    response = client.delete(
        "/api/v1/local-ai/models/installed:missing",
        headers={"X-Confirm": "yes"},
    )
    assert response.status_code == 404


def test_remove_model_rejects_running_instance(
    client: TestClient,
    services: SimpleNamespace,
) -> None:
    await_instance = asyncio.run(services.instances.start("installed:qwen", "cpu:0"))
    assert await_instance.id == "instance:one"
    response = client.delete(
        "/api/v1/local-ai/models/installed:qwen",
        headers={"X-Confirm": "yes"},
    )
    assert response.status_code == 409
    assert services.models.removed == []


def test_remove_model_rejects_active_download(
    client: TestClient,
    services: SimpleNamespace,
) -> None:
    task = services.downloads.create(services.catalog.items[0], "/models")
    assert task.id == "download:one"
    task.model_id = "installed:qwen"
    response = client.delete(
        "/api/v1/local-ai/models/installed:qwen",
        headers={"X-Confirm": "yes"},
    )
    assert response.status_code == 409
    assert services.models.removed == []


@pytest.mark.skipif(sys.platform == "win32", reason="model id contains ':' which is invalid in Windows filesystem")
def test_remove_model_cleans_disk_directory(
    client: TestClient,
    services: SimpleNamespace,
    tmp_path,
) -> None:
    model_dir = tmp_path / "installed:qwen"
    model_dir.mkdir(parents=True)
    (model_dir / "model.onnx").write_text("fake")
    services.models.items[0].directory = str(model_dir)
    response = client.delete(
        "/api/v1/local-ai/models/installed:qwen",
        headers={"X-Confirm": "yes"},
    )
    assert response.status_code == 204
    assert not model_dir.exists()


def test_spawn_reports_background_task_failure(
    services: SimpleNamespace,
    caplog,
) -> None:
    from web.routers.local_ai import LocalAIServices

    ai = LocalAIServices(
        devices=services.devices,
        catalog=services.catalog,
        models=services.models,
        downloads=services.downloads,
        instances=services.instances,
        broadcast=services.broadcast,
        storage_policy=services.storage_policy,
    )

    async def failing() -> None:
        raise RuntimeError("boom")

    async def run() -> None:
        ai.spawn(failing())
        await asyncio.sleep(0.05)
        assert not ai.background_tasks

    asyncio.run(run())
    assert "background task failed" in caplog.text
    assert "boom" in caplog.text


def test_request_id_idempotency_is_scoped_by_resource(
    client: TestClient,
    services: SimpleNamespace,
) -> None:
    request_id = "shared:one"
    download = client.post(
        "/api/v1/local-ai/downloads",
        json={
            "model_id": "catalog:qwen",
            "destination": "/models",
            "request_id": request_id,
        },
    )
    instance = client.post(
        "/api/v1/local-ai/instances",
        json={"model_id": "installed:qwen", "device_id": "cpu:0", "request_id": request_id},
    )
    assert download.status_code == 202
    assert instance.status_code == 202
    assert instance.json()["data"]["task_id"] == request_id
    for _ in range(20):
        if services.instances.items:
            break
        asyncio.run(asyncio.sleep(0))
    assert services.instances.items[0].model_id == "installed:qwen"


def test_rescan_and_instance_lifecycle_publish_websocket_events(
    client: TestClient,
    services: SimpleNamespace,
) -> None:
    assert data(client.post("/api/v1/local-ai/devices/rescan"))[0]["id"] == "cpu:0"
    response = client.post(
        "/api/v1/local-ai/instances",
        json={"model_id": "installed:qwen", "device_id": "cpu:0", "request_id": "start:one"},
    )
    assert response.status_code == 202
    assert response.json()["data"]["task_id"] == "start:one"
    for _ in range(20):
        if any(event["type"] == "local_ai_instance_updated" for event in services.events):
            break
        asyncio.run(asyncio.sleep(0))
    assert any(event["type"] == "local_ai_device_updated" for event in services.events)
    assert any(event["type"] == "local_ai_instance_updated" for event in services.events)


def test_instance_start_failure_publishes_retryable_websocket_event(
    client: TestClient,
    services: SimpleNamespace,
) -> None:
    async def fail_start(model_id: str, backend_override: str | None = None) -> Record:
        raise RuntimeError("runtime unavailable")

    services.instances.start = fail_start
    response = client.post(
        "/api/v1/local-ai/instances",
        json={"model_id": "installed:qwen", "device_id": "cpu:0", "request_id": "start:failed"},
    )
    assert response.status_code == 202
    for _ in range(20):
        if services.events:
            break
        asyncio.run(asyncio.sleep(0))
    assert services.events[-1] == {
        "type": "local_ai_instance_updated",
        "request_id": "start:failed",
        "model_id": "installed:qwen",
        "operation": "start",
        "status": "failed",
        "error": {
            "code": "instance_start_failed",
            "message": "runtime unavailable",
            "retryable": True,
        },
    }


def test_instance_start_task_can_be_queried(client: TestClient, services: SimpleNamespace) -> None:
    services.request_results = {}
    services.request_results[("instance", "start:pending")] = "start:pending"
    services.request_results[("instance", "start:completed")] = Record(id="instance:one")
    services.request_results[("instance", "start:failed")] = RuntimeError("runtime unavailable")

    pending = data(client.get("/api/v1/local-ai/instances/tasks/start:pending"))
    completed = data(client.get("/api/v1/local-ai/instances/tasks/start:completed"))
    failed = data(client.get("/api/v1/local-ai/instances/tasks/start:failed"))
    missing = client.get("/api/v1/local-ai/instances/tasks/start:missing")

    assert pending == {"task_id": "start:pending", "status": "pending"}
    assert completed == {
        "task_id": "start:completed",
        "status": "completed",
        "instance": {"id": "instance:one"},
    }
    assert failed == {
        "task_id": "start:failed",
        "status": "failed",
        "error": {
            "code": "instance_start_failed",
            "message": "runtime unavailable",
            "retryable": True,
        },
    }
    assert missing.status_code == 404


def test_legacy_devices_endpoint_translates_authoritative_devices(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        local_deploy,
        "get_config_service",
        lambda: SimpleNamespace(get=lambda *args: "cpu"),
    )
    response = client.get("/api/v1/local-deploy/devices")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["devices"][0]["id"] == "cpu:0"
    assert payload["devices"][0]["model"] == "Test CPU"
    assert payload["current"] == "cpu:0"
    assert payload["devices"][0]["active"] is True
    assert "3 TOPS INT8" not in response.text


def test_legacy_device_fallback_does_not_invent_npu_model(monkeypatch) -> None:
    npu_available = True
    monkeypatch.setattr("memory.npu_embed.probe_npu", lambda: npu_available)
    monkeypatch.setattr(local_deploy, "_detect_cpu_model", lambda: "Test CPU")
    monkeypatch.setattr(local_deploy, "_detect_gpu_model", lambda: "")
    monkeypatch.setattr(local_deploy, "get_config_service", lambda: SimpleNamespace(get=lambda *args: ""))
    local_deploy._DEVICE_CACHE["data"] = None

    available_payload = local_deploy._detect_devices()
    npu_available = False
    local_deploy._DEVICE_CACHE["data"] = None
    unavailable_payload = local_deploy._detect_devices()

    available_npu = next(device for device in available_payload["devices"] if device["id"] == "npu")
    unavailable_npu = next(device for device in unavailable_payload["devices"] if device["id"] == "npu")
    assert available_npu["model"] == "NPU"
    assert unavailable_npu["model"] == "未检测到可用 NPU"
    assert "VIP9000" not in str(unavailable_npu)
