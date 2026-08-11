from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone

import pytest

from local_ai.contracts import (
    CatalogFile,
    CatalogModel,
    ComputeDevice,
    InstalledModel,
    ModelPurpose,
    RuntimeProfile,
)
from local_ai.instances.manager import InstanceInUseError, InstanceManager
from local_ai.runtimes.base import RuntimeValidationError
from local_ai.runtimes.registry import RuntimeRegistry

NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)


class FakeRuntime:
    def __init__(self, events: list[str], name: str) -> None:
        self.events = events
        self.name = name
        self.profile = None
        self.running = False
        self.healthy = True

    def start(self, profile: RuntimeProfile) -> bool:
        self.events.append(f"start:{self.name}")
        self.profile = profile
        self.running = True
        return True

    def stop(self) -> None:
        self.events.append(f"stop:{self.name}")
        self.running = False

    def health(self) -> bool:
        return self.running and self.healthy


class FailingStartRuntime(FakeRuntime):
    def start(self, profile: RuntimeProfile) -> bool:
        self.events.append(f"start:{self.name}")
        self.profile = profile
        return False


class RaisingStartRuntime(FakeRuntime):
    def start(self, profile: RuntimeProfile) -> bool:
        self.events.append(f"start:{self.name}")
        self.profile = profile
        raise RuntimeError(f"cannot start {self.name}")


class FailingStopRuntime(FakeRuntime):
    def stop(self) -> None:
        super().stop()
        raise RuntimeError(f"cannot stop {self.name}")


class RetryableStopRuntime(FakeRuntime):
    def __init__(self, events: list[str], name: str, stop_failures: int = 1) -> None:
        super().__init__(events, name)
        self.stop_failures = stop_failures
        self.stop_attempts = 0

    def stop(self) -> None:
        self.stop_attempts += 1
        self.events.append(f"stop:{self.name}:{self.stop_attempts}")
        if self.stop_attempts <= self.stop_failures:
            raise RuntimeError(f"cannot stop {self.name}")
        self.running = False


class BlockingStartRuntime(FakeRuntime):
    def __init__(
        self,
        events: list[str],
        name: str,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        super().__init__(events, name)
        self.entered = entered
        self.release = release

    def start(self, profile: RuntimeProfile) -> bool:
        self.entered.set()
        self.release.wait()
        return super().start(profile)


class BlockingStartRetryableStopRuntime(RetryableStopRuntime):
    def __init__(
        self,
        events: list[str],
        name: str,
        entered: threading.Event,
        release: threading.Event,
        stop_failures: int = 1,
    ) -> None:
        super().__init__(events, name, stop_failures)
        self.entered = entered
        self.release = release

    def start(self, profile: RuntimeProfile) -> bool:
        self.entered.set()
        self.release.wait()
        return super().start(profile)


class BlockingHealthRuntime(FakeRuntime):
    def __init__(
        self,
        events: list[str],
        name: str,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        super().__init__(events, name)
        self.entered = entered
        self.release = release

    def health(self) -> bool:
        self.events.append("health:enter")
        self.entered.set()
        self.release.wait()
        result = super().health()
        self.events.append("health:exit")
        return result


class BlockingStopRuntime(FakeRuntime):
    def __init__(
        self,
        events: list[str],
        name: str,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        super().__init__(events, name)
        self.entered = entered
        self.release = release

    def stop(self) -> None:
        self.entered.set()
        self.release.wait()
        super().stop()


class FakeModelRegistry:
    def __init__(self, models: dict[str, InstalledModel]) -> None:
        self.models = models

    async def get(self, model_id: str) -> InstalledModel | None:
        return self.models.get(model_id)


class FakeDeviceRegistry:
    def __init__(self) -> None:
        self.devices = {
            "cpu:0": ComputeDevice(
                id="cpu:0",
                name="CPU",
                kind="cpu",
                architecture="x86_64",
                state="available",
                memory_total=16_000,
                memory_available=12_000,
            ),
            "gpu:0": ComputeDevice(
                id="gpu:0",
                name="GPU",
                kind="gpu",
                architecture="x86_64",
                state="available",
                memory_total=8_000,
                memory_available=6_000,
            ),
        }
        self.overrides: list[str | None] = []

    def recommend(self, model: CatalogModel, override: str | None = None) -> RuntimeProfile:
        self.overrides.append(override)
        device_id = override or "cpu:0"
        runtime = "ort_genai" if model.purpose is ModelPurpose.CHAT else "ort"
        provider = "CUDAExecutionProvider" if device_id == "gpu:0" else "CPUExecutionProvider"
        return RuntimeProfile(runtime=runtime, device_id=device_id, provider=provider)

    def scan(self, force: bool = False) -> list[ComputeDevice]:
        return list(self.devices.values())

    def remove(self, device_id: str) -> None:
        self.devices.pop(device_id)


class FakeDatabase:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = False

    async def close(self) -> None:
        self.events.append("database:close")
        self.closed = True


class RecordingInstances(dict):
    def __init__(self, manager: InstanceManager, values: dict) -> None:
        super().__init__(values)
        self.manager = manager
        self.writes_during_shutdown = 0

    def __setitem__(self, key, value) -> None:
        if self.manager._shutting_down:
            self.writes_during_shutdown += 1
        super().__setitem__(key, value)


def installed_model(model_id: str, purpose: ModelPurpose) -> InstalledModel:
    return InstalledModel(
        id=model_id,
        catalog_id=f"catalog:{model_id}",
        revision="abcdef0",
        purpose=purpose,
        directory=f"/models/{model_id.replace(':', '-')}",
        manifest_checksum="sha256:test",
        validation_state="validated",
        ownership="user",
        installed_at=NOW,
        metadata={
            "compatibility": {
                "runtimes": ["ort_genai" if purpose is ModelPurpose.CHAT else "ort"],
                "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
            },
            "runtime_requirements": {"minimum_ram": 1},
        },
    )


def catalog_model(purpose: ModelPurpose) -> CatalogModel:
    return CatalogModel(
        id="catalog:test",
        source="test",
        repository="test/model",
        revision="abcdef0",
        purpose=purpose,
        files=(CatalogFile(path="model.onnx", size=1, sha256="0" * 64),),
        download_size=1,
    )


def profile_model_name(profile: RuntimeProfile) -> str:
    return "local:chat" if profile.runtime.value == "ort_genai" else "local:embedding"


@pytest.fixture
def setup_manager():
    events: list[str] = []
    models = {
        "local:chat": installed_model("local:chat", ModelPurpose.CHAT),
        "local:embedding": installed_model("local:embedding", ModelPurpose.EMBEDDING),
    }
    runtimes: list[FakeRuntime] = []

    def factory(profile: RuntimeProfile) -> FakeRuntime:
        runtime = FakeRuntime(events, profile_model_name(profile))
        runtimes.append(runtime)
        return runtime

    runtime_registry = RuntimeRegistry(
        {"ort": factory, "ort_genai": factory}
    )
    device_registry = FakeDeviceRegistry()
    database = FakeDatabase(events)
    manager = InstanceManager(
        FakeModelRegistry(models),
        device_registry,
        runtime_registry,
        database=database,
        owns_database=True,
    )
    return manager, device_registry, database, runtimes, events


def test_runtime_registry_creates_adapter_for_profile():
    installed = installed_model("local:embedding", ModelPurpose.EMBEDDING)
    profile = RuntimeProfile(
        runtime="ort",
        device_id="cpu:0",
        provider="CPUExecutionProvider",
    )
    adapter = RuntimeRegistry().create(profile, installed_model=installed)
    assert type(adapter).__name__ == "EmbeddingRuntime"


def test_runtime_registry_rejects_unsupported_runtime():
    profile = RuntimeProfile(
        runtime="vip",
        device_id="npu:0",
        provider="VIPExecutionProvider",
    )
    with pytest.raises(RuntimeValidationError):
        RuntimeRegistry().create(profile)


@pytest.mark.parametrize(
    ("runtime", "purpose"),
    [("ort", "chat"), ("ort_genai", "embedding"), ("ort_genai", "reranker")],
)
def test_runtime_registry_rejects_unknown_runtime_purpose_combination(runtime, purpose):
    installed = installed_model("local:test", ModelPurpose(purpose))
    profile = RuntimeProfile(
        runtime=runtime,
        device_id="cpu:0",
        provider="CPUExecutionProvider",
    )
    with pytest.raises(RuntimeValidationError, match="unsupported runtime and purpose"):
        RuntimeRegistry().create(profile, installed_model=installed)


def test_runtime_registry_requires_explicit_installed_model():
    profile = RuntimeProfile(
        runtime="ort",
        device_id="cpu:0",
        provider="CPUExecutionProvider",
    )
    with pytest.raises(RuntimeValidationError, match="installed_model"):
        RuntimeRegistry().create(profile)


@pytest.mark.asyncio
async def test_start_selects_profile_and_honors_backend_override(setup_manager):
    manager, devices, _, runtimes, _ = setup_manager
    instance = await manager.start("local:chat", backend_override="gpu:0")
    assert devices.overrides == ["gpu:0"]
    assert instance.device_id == "gpu:0"
    assert instance.runtime.value == "ort_genai"
    assert dict(runtimes[0].profile.options) == {}


@pytest.mark.asyncio
async def test_concurrent_start_for_same_model_is_serialized(setup_manager):
    manager, _, _, runtimes, events = setup_manager
    first, second = await asyncio.gather(
        manager.start("local:embedding"),
        manager.start("local:embedding"),
    )
    assert first.id == second.id
    assert len(runtimes) == 1
    assert events.count("start:local:embedding") == 1


@pytest.mark.asyncio
async def test_different_models_can_start_independently(setup_manager):
    manager, _, _, _, _ = setup_manager
    first, second = await asyncio.gather(
        manager.start("local:chat"),
        manager.start("local:embedding"),
    )
    assert first.model_id != second.model_id
    assert manager.active_count == 2


@pytest.mark.asyncio
async def test_stop_releases_runtime_and_removes_instance(setup_manager):
    manager, _, _, runtimes, _ = setup_manager
    instance = await manager.start("local:embedding")
    await manager.stop(instance.id)
    assert runtimes[0].running is False
    assert manager.get(instance.id) is None
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_stop_is_idempotent(setup_manager):
    manager, _, _, runtimes, events = setup_manager
    instance = await manager.start("local:embedding")
    await manager.stop(instance.id)
    await manager.stop(instance.id)
    assert runtimes[0].running is False
    assert events.count("stop:local:embedding") == 1


@pytest.mark.asyncio
async def test_failed_runtime_start_does_not_register_instance(setup_manager):
    manager, _, _, _, events = setup_manager
    manager._runtime_registry = RuntimeRegistry(
        {"ort": lambda profile: FailingStartRuntime(events, profile_model_name(profile))}
    )
    with pytest.raises(RuntimeError, match="failed to start"):
        await manager.start("local:embedding")
    assert manager.active_count == 0
    assert events[-2:] == ["start:local:embedding", "stop:local:embedding"]


@pytest.mark.asyncio
async def test_raising_runtime_start_rolls_back_runtime_and_instance_state(setup_manager):
    manager, _, _, _, events = setup_manager
    manager._runtime_registry = RuntimeRegistry(
        {"ort": lambda profile: RaisingStartRuntime(events, profile_model_name(profile))}
    )
    with pytest.raises(RuntimeError, match="cannot start"):
        await manager.start("local:embedding")
    assert manager.list() == []
    assert events[-2:] == ["start:local:embedding", "stop:local:embedding"]


@pytest.mark.asyncio
async def test_list_and_get_return_serializable_instances(setup_manager):
    manager, _, _, _, _ = setup_manager
    instance = await manager.start("local:embedding")
    payload = manager.get(instance.id).to_dict()
    json.dumps(payload, allow_nan=False)
    assert manager.list() == [manager.get(instance.id)]
    assert payload["state"] == "running"
    assert payload["health"] == "healthy"


@pytest.mark.asyncio
async def test_unhealthy_runtime_is_marked_degraded(setup_manager):
    manager, _, _, runtimes, _ = setup_manager
    instance = await manager.start("local:embedding")
    runtimes[0].healthy = False
    await manager.refresh_health()
    assert manager.get(instance.id).state == "degraded"
    assert manager.get(instance.id).health == "unhealthy"


@pytest.mark.asyncio
async def test_device_loss_marks_instance_degraded(setup_manager):
    manager, device_registry, _, _, _ = setup_manager
    instance = await manager.start("local:chat")
    device_registry.remove(instance.device_id)
    await manager.refresh_health()
    assert manager.get(instance.id).state == "degraded"
    assert manager.get(instance.id).health == "device_unavailable"


@pytest.mark.asyncio
async def test_health_refresh_restores_runtime_state(setup_manager):
    manager, _, _, runtimes, _ = setup_manager
    instance = await manager.start("local:embedding")
    runtimes[0].healthy = False
    await manager.refresh_health()
    runtimes[0].healthy = True
    await manager.refresh_health()
    assert manager.get(instance.id).state == "running"
    assert manager.get(instance.id).health == "healthy"


@pytest.mark.asyncio
async def test_route_dependency_is_serialized_and_blocks_stop(setup_manager):
    manager, _, _, _, _ = setup_manager
    instance = await manager.start("local:chat")
    bound = manager.bind_route(instance.id, "chat")
    assert bound.active_routes == ("chat",)
    with pytest.raises(InstanceInUseError):
        await manager.stop(instance.id)
    unbound = manager.unbind_route(instance.id, "chat")
    assert unbound.active_routes == ()
    await manager.stop(instance.id)


@pytest.mark.asyncio
async def test_shutdown_stops_all_runtimes_before_database_close(setup_manager):
    manager, _, database, _, events = setup_manager
    await manager.start("local:embedding")
    await manager.start("local:chat")
    await manager.shutdown()
    assert manager.active_count == 0
    assert database.closed is True
    close_index = events.index("database:close")
    assert all(index < close_index for index, event in enumerate(events) if event.startswith("stop:"))


@pytest.mark.asyncio
async def test_shutdown_ignores_route_dependencies(setup_manager):
    manager, _, _, _, _ = setup_manager
    instance = await manager.start("local:chat")
    manager.bind_route(instance.id, "chat")
    await manager.shutdown()
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_shutdown_closes_database_after_stop_failure(setup_manager):
    manager, _, database, _, events = setup_manager
    manager._runtime_registry = RuntimeRegistry(
        {
            "ort": lambda profile: FailingStopRuntime(events, profile_model_name(profile)),
            "ort_genai": lambda profile: FakeRuntime(events, profile_model_name(profile)),
        }
    )
    await manager.start("local:embedding")
    await manager.start("local:chat")
    with pytest.raises(ExceptionGroup, match="shutdown failed"):
        await manager.shutdown()
    with pytest.raises(ExceptionGroup, match="shutdown failed"):
        await manager.shutdown()
    assert manager.active_count == 0
    assert database.closed is True
    assert events[-1] == "database:close"
    assert events.count("database:close") == 1


@pytest.mark.asyncio
async def test_shutdown_is_idempotent(setup_manager):
    manager, _, _, _, events = setup_manager
    await manager.start("local:embedding")
    await manager.shutdown()
    await manager.shutdown()
    assert events.count("database:close") == 1


@pytest.mark.asyncio
async def test_shutdown_waits_for_inflight_start_before_database_close(setup_manager):
    manager, _, database, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    manager._runtime_registry = RuntimeRegistry(
        {
            "ort": lambda profile: BlockingStartRuntime(
                events,
                profile_model_name(profile),
                entered,
                release,
            )
        }
    )
    start_task = asyncio.create_task(manager.start("local:embedding"))
    await asyncio.to_thread(entered.wait)
    shutdown_task = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    release.set()
    await start_task
    await shutdown_task
    assert manager.active_count == 0
    assert database.closed is True
    assert events[-2:] == ["stop:local:embedding", "database:close"]


@pytest.mark.asyncio
async def test_cancelled_start_is_stopped_before_shutdown_closes_database(setup_manager):
    manager, _, database, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    runtime = BlockingStartRuntime(
        events,
        "local:embedding",
        entered,
        release,
    )
    manager._runtime_registry = RuntimeRegistry(
        {"ort": lambda profile: runtime}
    )
    start_task = asyncio.create_task(manager.start("local:embedding"))
    await asyncio.to_thread(entered.wait)
    start_task.cancel()
    shutdown_task = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await start_task
    await shutdown_task
    assert manager.active_count == 0
    assert database.closed is True
    assert runtime.running is False
    assert events[-2:] == ["stop:local:embedding", "database:close"]


@pytest.mark.asyncio
async def test_repeated_cancel_during_start_cleanup_finishes_before_shutdown(setup_manager):
    manager, _, database, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    runtime = BlockingStartRuntime(
        events,
        "local:embedding",
        entered,
        release,
    )
    manager._runtime_registry = RuntimeRegistry({"ort": lambda profile: runtime})
    start_task = asyncio.create_task(manager.start("local:embedding"))
    await asyncio.to_thread(entered.wait)
    start_task.cancel()
    await asyncio.sleep(0)
    start_task.cancel()
    assert len(manager._lifecycle_tasks) == 1
    shutdown_task = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await start_task
    await shutdown_task
    assert manager.active_count == 0
    assert database.closed is True
    assert runtime.running is False
    assert manager._lifecycle_tasks == set()
    assert events[-2:] == ["stop:local:embedding", "database:close"]


@pytest.mark.asyncio
async def test_cancelled_start_rollback_failure_is_owned_until_shutdown_retry_succeeds(
    setup_manager,
):
    manager, _, database, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    runtime = BlockingStartRetryableStopRuntime(
        events,
        "local:embedding",
        entered,
        release,
    )
    manager._runtime_registry = RuntimeRegistry({"ort": lambda profile: runtime})
    start_task = asyncio.create_task(manager.start("local:embedding"))
    await asyncio.to_thread(entered.wait)
    start_task.cancel()
    release.set()
    with pytest.raises(BaseExceptionGroup) as captured:
        await start_task
    assert captured.value.subgroup(asyncio.CancelledError) is not None
    assert captured.value.subgroup(RuntimeError) is not None
    assert runtime.running is True
    assert manager.active_count == 0
    assert len(manager._pending_cleanup) == 1
    assert database.closed is False
    await manager.shutdown()
    assert runtime.stop_attempts == 2
    assert runtime.running is False
    assert manager._pending_cleanup == {}
    assert database.closed is True
    assert events[-2:] == ["stop:local:embedding:2", "database:close"]


@pytest.mark.asyncio
async def test_shutdown_aggregates_persistent_pending_cleanup_failure(setup_manager):
    manager, _, database, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    runtime = BlockingStartRetryableStopRuntime(
        events,
        "local:embedding",
        entered,
        release,
        stop_failures=3,
    )
    manager._runtime_registry = RuntimeRegistry({"ort": lambda profile: runtime})
    start_task = asyncio.create_task(manager.start("local:embedding"))
    await asyncio.to_thread(entered.wait)
    start_task.cancel()
    release.set()
    with pytest.raises(BaseExceptionGroup):
        await start_task
    with pytest.raises(ExceptionGroup, match="shutdown failed") as first_shutdown:
        await manager.shutdown()
    assert first_shutdown.value.subgroup(RuntimeError) is not None
    assert runtime.running is True
    assert len(manager._pending_cleanup) == 1
    assert database.closed is True
    with pytest.raises(ExceptionGroup, match="shutdown failed"):
        await manager.shutdown()
    assert runtime.stop_attempts == 3
    assert len(manager._pending_cleanup) == 1
    assert events.count("database:close") == 1


@pytest.mark.asyncio
async def test_health_refresh_does_not_restore_instance_stopped_while_health_blocks(setup_manager):
    manager, _, _, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    manager._runtime_registry = RuntimeRegistry(
        {
            "ort": lambda profile: BlockingHealthRuntime(
                events,
                profile_model_name(profile),
                entered,
                release,
            )
        }
    )
    instance = await manager.start("local:embedding")
    refresh_task = asyncio.create_task(manager.refresh_health())
    await asyncio.to_thread(entered.wait)
    stop_task = asyncio.create_task(manager.stop(instance.id))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(refresh_task, stop_task)
    assert manager.get(instance.id) is None
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_cancelled_health_keeps_model_lock_until_worker_finishes(setup_manager):
    manager, _, _, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    manager._runtime_registry = RuntimeRegistry(
        {
            "ort": lambda profile: BlockingHealthRuntime(
                events,
                profile_model_name(profile),
                entered,
                release,
            )
        }
    )
    instance = await manager.start("local:embedding")
    health_task = asyncio.create_task(manager.refresh_health())
    await asyncio.to_thread(entered.wait)
    health_task.cancel()
    stop_task = asyncio.create_task(manager.stop(instance.id))
    await asyncio.sleep(0)
    assert manager.get(instance.id) is not None
    assert len(manager._lifecycle_tasks) == 1
    assert "stop:local:embedding" not in events
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await health_task
    await stop_task
    assert manager._lifecycle_tasks == set()
    assert events[-3:] == [
        "health:enter",
        "health:exit",
        "stop:local:embedding",
    ]


@pytest.mark.asyncio
async def test_manager_tracks_inflight_runtime_work_per_model(setup_manager):
    manager, _, _, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    manager._runtime_registry = RuntimeRegistry(
        {
            "ort": lambda profile: BlockingHealthRuntime(
                events,
                profile_model_name(profile),
                entered,
                release,
            )
        }
    )
    await manager.start("local:embedding")
    health_task = asyncio.create_task(manager.refresh_health())
    await asyncio.to_thread(entered.wait)
    assert len(manager._model_tasks["local:embedding"]) == 1
    assert "local:chat" not in manager._model_tasks
    release.set()
    await health_task
    assert manager._model_tasks == {}


@pytest.mark.asyncio
async def test_health_does_not_write_back_after_shutdown_starts(setup_manager):
    manager, _, _, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    manager._runtime_registry = RuntimeRegistry(
        {
            "ort": lambda profile: BlockingHealthRuntime(
                events,
                profile_model_name(profile),
                entered,
                release,
            )
        }
    )
    await manager.start("local:embedding")
    instances = RecordingInstances(manager, manager._instances)
    manager._instances = instances
    health_task = asyncio.create_task(manager.refresh_health())
    await asyncio.to_thread(entered.wait)
    shutdown_task = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(health_task, shutdown_task)
    assert instances.writes_during_shutdown == 0


@pytest.mark.asyncio
async def test_route_cannot_bind_after_stop_has_started(setup_manager):
    manager, _, _, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    manager._runtime_registry = RuntimeRegistry(
        {
            "ort_genai": lambda profile: BlockingStopRuntime(
                events,
                profile_model_name(profile),
                entered,
                release,
            )
        }
    )
    instance = await manager.start("local:chat")
    stop_task = asyncio.create_task(manager.stop(instance.id))
    await asyncio.to_thread(entered.wait)
    with pytest.raises(InstanceInUseError, match="stopping"):
        manager.bind_route(instance.id, "chat")
    release.set()
    await stop_task
    assert manager.get(instance.id) is None


@pytest.mark.asyncio
async def test_cancelled_stop_keeps_ownership_and_shutdown_waits_for_worker(setup_manager):
    manager, _, database, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    manager._runtime_registry = RuntimeRegistry(
        {
            "ort": lambda profile: BlockingStopRuntime(
                events,
                profile_model_name(profile),
                entered,
                release,
            )
        }
    )
    instance = await manager.start("local:embedding")
    stop_task = asyncio.create_task(manager.stop(instance.id))
    await asyncio.to_thread(entered.wait)
    stop_task.cancel()
    shutdown_task = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    instance_during_stop = manager.get(instance.id)
    database_closed_during_stop = database.closed
    lifecycle_count_during_stop = len(manager._lifecycle_tasks)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stop_task
    await shutdown_task
    assert instance_during_stop is not None
    assert database_closed_during_stop is False
    assert lifecycle_count_during_stop == 1
    assert manager.active_count == 0
    assert manager._lifecycle_tasks == set()
    assert events[-2:] == ["stop:local:embedding", "database:close"]


@pytest.mark.asyncio
async def test_shutdown_stops_runtime_under_model_lock(setup_manager):
    manager, _, _, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    manager._runtime_registry = RuntimeRegistry(
        {
            "ort": lambda profile: BlockingStopRuntime(
                events,
                profile_model_name(profile),
                entered,
                release,
            )
        }
    )
    instance = await manager.start("local:embedding")
    shutdown_task = asyncio.create_task(manager.shutdown())
    await asyncio.to_thread(entered.wait)
    assert manager._model_locks[instance.model_id].locked()
    stop_task = asyncio.create_task(manager.stop(instance.id))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(shutdown_task, stop_task)
    assert events.count("stop:local:embedding") == 1


@pytest.mark.asyncio
async def test_cancelled_shutdown_finishes_in_order_and_retry_awaits_same_cleanup(setup_manager):
    manager, _, database, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    manager._runtime_registry = RuntimeRegistry(
        {
            "ort": lambda profile: BlockingStopRuntime(
                events,
                profile_model_name(profile),
                entered,
                release,
            )
        }
    )
    await manager.start("local:embedding")
    shutdown_task = asyncio.create_task(manager.shutdown())
    await asyncio.to_thread(entered.wait)
    shutdown_task.cancel()
    retry_task = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    shutdown_task.cancel()
    assert database.closed is False
    assert len(manager._lifecycle_tasks) == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await shutdown_task
    await retry_task
    assert manager.active_count == 0
    assert database.closed is True
    assert manager._lifecycle_tasks == set()
    assert events[-2:] == ["stop:local:embedding", "database:close"]


@pytest.mark.asyncio
async def test_catalog_profile_data_can_be_supplied_explicitly(setup_manager):
    manager, devices, _, _, _ = setup_manager
    manager.catalog_resolver = lambda _: catalog_model(ModelPurpose.EMBEDDING)
    await manager.start("local:embedding")
    assert devices.overrides == [None]


@pytest.mark.asyncio
async def test_health_writeback_preserves_route_bound_during_health_check(setup_manager):
    manager, _, _, _, events = setup_manager
    entered = threading.Event()
    release = threading.Event()
    manager._runtime_registry = RuntimeRegistry(
        {
            "ort_genai": lambda profile: BlockingHealthRuntime(
                events,
                profile_model_name(profile),
                entered,
                release,
            )
        }
    )
    instance = await manager.start("local:chat")
    health_task = asyncio.create_task(manager.refresh_health())
    await asyncio.to_thread(entered.wait)
    bound = await asyncio.to_thread(manager.bind_route, instance.id, "chat")
    release.set()
    await health_task
    assert bound.active_routes == ("chat",)
    assert manager.get(instance.id).active_routes == ("chat",)


@pytest.mark.asyncio
async def test_stop_failure_keeps_instance_and_runtime_for_retry(setup_manager):
    manager, _, _, _, events = setup_manager
    runtime = RetryableStopRuntime(events, "local:embedding")
    manager._runtime_registry = RuntimeRegistry({"ort": lambda profile: runtime})
    instance = await manager.start("local:embedding")
    with pytest.raises(RuntimeError, match="cannot stop"):
        await manager.stop(instance.id)
    retained = manager.get(instance.id)
    assert retained is not None
    assert retained.state == "degraded"
    assert retained.health == "stop_failed"
    assert manager._runtimes[instance.id] is runtime
    await manager.stop(instance.id)
    assert manager.get(instance.id) is None
    assert runtime.stop_attempts == 2


@pytest.mark.asyncio
async def test_shutdown_retries_failed_stop_and_closes_owned_database(setup_manager):
    manager, _, database, _, events = setup_manager
    runtime = RetryableStopRuntime(events, "local:embedding")
    manager._runtime_registry = RuntimeRegistry({"ort": lambda profile: runtime})
    await manager.start("local:embedding")
    with pytest.raises(ExceptionGroup, match="shutdown failed"):
        await manager.shutdown()
    assert manager.active_count == 1
    assert database.closed is True
    await manager.shutdown()
    assert manager.active_count == 0
    assert runtime.stop_attempts == 2


@pytest.mark.asyncio
async def test_shutdown_does_not_close_borrowed_database(setup_manager):
    manager, devices, database, _, events = setup_manager
    borrowed = InstanceManager(
        manager._model_registry,
        devices,
        RuntimeRegistry(
            {"ort": lambda profile: FakeRuntime(events, profile_model_name(profile))}
        ),
        database=database,
    )
    await borrowed.start("local:embedding")
    await borrowed.shutdown()
    assert database.closed is False
