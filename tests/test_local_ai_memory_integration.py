from __future__ import annotations

import asyncio
import inspect
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import core.bootstrap as bootstrap
from local_ai.contracts import CatalogModel, ComputeDevice, InstalledModel, ModelPurpose, RuntimeProfile
from local_ai.instances.manager import InstanceInUseError, InstanceManager
from local_ai.integration.embedding import LocalEmbeddingService, LocalEmbeddingUnavailableError
from local_ai.integration.reranker import (
    LocalModelUnavailableError,
    LocalRerankerService,
    LocalRerankerUnavailableError,
)
from local_ai.runtimes.base import RuntimeValidationError
from local_ai.runtimes.registry import RuntimeRegistry
from memory.memory_manager import MemoryManager
from memory.query_cache import QueryCache
from memory.vector_store import VectorStore


class FakeEmbeddingRuntime:
    def __init__(self) -> None:
        self.running = True
        self.dimensions = 1
        self.calls: list[list[str]] = []

    def health(self) -> bool:
        return self.running

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(len(text))] for text in texts]

    def stop(self) -> None:
        self.running = False


class FakeRerankerRuntime:
    def __init__(self) -> None:
        self.running = True
        self.calls: list[tuple[str, list[str]]] = []

    def health(self) -> bool:
        return self.running

    def score(self, query: str, documents: list[str]) -> list[float]:
        self.calls.append((query, documents))
        return [float(index) for index, _ in enumerate(documents)]


class FakeInstanceManager:
    def __init__(self, runtimes=None, errors=None, selections=None) -> None:
        self.runtimes = runtimes or {}
        self.errors = errors or {}
        self.selections = selections or set()
        self.purposes = []

    async def resolve_runtime(self, purpose):
        self.purposes.append(purpose)
        error = self.errors.get(purpose.value)
        if error is not None:
            raise error
        return self.runtimes.get(purpose.value)

    def selection_identity(self, purpose):
        if purpose.value not in self.selections:
            return None
        return (f"selected:{purpose.value}", 1)

    def selection_available(self, purpose):
        return purpose.value in self.runtimes


class FailingBundledRuntime:
    ready = False

    def load(self) -> bool:
        return False


class FakeBundledEmbeddingRuntime:
    ready = True

    def embed(self, text: str) -> list[float]:
        return [float(len(text))]


class E2ERuntime:
    def __init__(self, value: float, purpose: ModelPurpose) -> None:
        self.value = value
        self.purpose = purpose
        self.running = False
        self.dimensions = 1
        self.embed_calls: list[list[str]] = []
        self.score_calls: list[tuple[str, list[str]]] = []
        self.embed_entered: threading.Event | None = None
        self.embed_release: threading.Event | None = None
        self.score_entered: threading.Event | None = None
        self.score_release: threading.Event | None = None

    def start(self, profile: RuntimeProfile) -> bool:
        self.running = True
        return True

    def stop(self) -> None:
        self.running = False

    def health(self) -> bool:
        return self.running

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(texts)
        if self.embed_entered is not None:
            self.embed_entered.set()
        if self.embed_release is not None:
            self.embed_release.wait()
        return [[self.value] for _ in texts]

    def score(self, query: str, documents: list[str]) -> list[float]:
        self.score_calls.append((query, documents))
        if self.score_entered is not None:
            self.score_entered.set()
        if self.score_release is not None:
            self.score_release.wait()
        return [self.value + index for index, _ in enumerate(documents)]


class E2EModelRegistry:
    def __init__(self, models: dict[str, InstalledModel]) -> None:
        self.models = models

    async def get(self, model_id: str) -> InstalledModel | None:
        return self.models.get(model_id)


class E2EDeviceRegistry:
    def recommend(self, model: CatalogModel, override: str | None = None) -> RuntimeProfile:
        return RuntimeProfile(
            runtime="ort",
            device_id=override or "cpu:0",
            provider="CPUExecutionProvider",
        )

    def scan(self, force: bool = False) -> list[ComputeDevice]:
        return [
            ComputeDevice(
                id="cpu:0",
                name="CPU",
                kind="cpu",
                architecture="x86_64",
                state="available",
                memory_total=16_000,
                memory_available=12_000,
            )
        ]


def e2e_installed_model(model_id: str, purpose: ModelPurpose) -> InstalledModel:
    return InstalledModel(
        id=model_id,
        catalog_id=f"catalog:{model_id}",
        revision="abcdef0",
        purpose=purpose,
        directory=f"/models/{model_id}",
        manifest_checksum="sha256:test",
        validation_state="validated",
        ownership="user",
        installed_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        metadata={
            "compatibility": {
                "runtimes": ["ort"],
                "providers": ["CPUExecutionProvider"],
            },
            "runtime_requirements": {"minimum_ram": 1},
        },
    )


def e2e_instance_manager() -> tuple[InstanceManager, dict[str, E2ERuntime]]:
    models = {
        "embedding-a": e2e_installed_model("embedding-a", ModelPurpose.EMBEDDING),
        "embedding-b": e2e_installed_model("embedding-b", ModelPurpose.EMBEDDING),
        "reranker-a": e2e_installed_model("reranker-a", ModelPurpose.RERANKER),
    }
    runtimes: dict[str, E2ERuntime] = {}
    values = iter((1.0, 2.0, 3.0))

    def factory(profile: RuntimeProfile) -> E2ERuntime:
        value = next(values)
        purpose = ModelPurpose.RERANKER if value == 2.0 else ModelPurpose.EMBEDDING
        runtime = E2ERuntime(value, purpose)
        runtimes[str(value)] = runtime
        return runtime

    manager = InstanceManager(
        E2EModelRegistry(models),
        E2EDeviceRegistry(),
        RuntimeRegistry({"ort": factory}),
    )
    return manager, runtimes


@pytest.mark.asyncio
async def test_production_services_follow_instances_started_after_bootstrap(tmp_path):
    instances, runtimes = e2e_instance_manager()
    services = await bootstrap._local_memory_services(
        instances,
        "local",
        "/models/bge",
        "",
    )
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=services.embedding,
    )
    memory = MemoryManager(None, None, reranker_service=services.reranker)

    embedding_instance = await instances.start("embedding-a")
    reranker_instance = await instances.start("reranker-a")

    assert await vector_store.embed(["same"]) == [[1.0]]
    assert await memory.rerank_with_selected_local_model("q", ["a", "b"]) == [
        {"index": 1, "relevance_score": 3.0},
        {"index": 0, "relevance_score": 2.0},
    ]
    assert runtimes["1.0"].embed_calls == [["same"]]
    assert runtimes["2.0"].score_calls == [("q", ["a", "b"])]

    await instances.stop(embedding_instance.id)
    await instances.stop(reranker_instance.id)

    with pytest.raises(LocalModelUnavailableError):
        await vector_store.embed(["new"])
    with pytest.raises(LocalModelUnavailableError):
        await memory.rerank_with_selected_local_model("q", ["a"])


@pytest.mark.asyncio
async def test_production_bootstrap_keeps_default_local_vector_store_enabled(tmp_path, monkeypatch):
    created = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            self._embed_mode = kwargs["embed_mode"]
            self.embedding_service = kwargs["embedding_service"]
            self.initialized = False
            created.append(self)

        async def init(self):
            self.initialized = True
            assert await self.embedding_service.resolve_dimensions() == 1

    bundled = LocalEmbeddingService(FakeEmbeddingRuntime(), source="bundled")
    monkeypatch.delenv("EMBED_MODE", raising=False)
    monkeypatch.delenv("EMBED_API_KEY", raising=False)
    monkeypatch.setattr("memory.vector_store.VectorStore", FakeVectorStore)
    monkeypatch.setattr(LocalEmbeddingService, "bundled", lambda *args, **kwargs: bundled)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    core = SimpleNamespace(
        db=SimpleNamespace(
            init=AsyncMock(),
            analytics=object(),
            db_path=tmp_path / "agent.db",
        ),
        router=SimpleNamespace(set_db=MagicMock(), set_local_transport=MagicMock()),
        local_ai_instances=FakeInstanceManager(),
    )

    await bootstrap.AgentCoreBootstrapper(core)._init_infrastructure()

    assert core._vec_store is created[0]
    assert created[0].initialized
    assert created[0]._embed_mode == "local"


@pytest.mark.asyncio
async def test_cached_embedding_checks_stopped_selected_instance(tmp_path):
    instances, _ = e2e_instance_manager()
    services = await bootstrap._local_memory_services(
        instances,
        "local",
        "/models/bge",
        "",
    )
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=services.embedding,
    )
    instance = await instances.start("embedding-a")

    assert await vector_store.embed(["cached"]) == [[1.0]]
    assert await vector_store.embed(["cached"]) == [[1.0]]
    await instances.stop(instance.id)

    with pytest.raises(LocalModelUnavailableError):
        await vector_store.embed(["cached"])


@pytest.mark.asyncio
async def test_embedding_cache_is_isolated_between_selected_instances(tmp_path):
    instances, runtimes = e2e_instance_manager()
    services = await bootstrap._local_memory_services(
        instances,
        "local",
        "/models/bge",
        "",
    )
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=services.embedding,
    )

    first = await instances.start("embedding-a")
    assert await vector_store.embed(["same"]) == [[1.0]]
    await instances.stop(first.id)
    await instances.start("embedding-b")

    assert await vector_store.embed(["same"]) == [[2.0]]
    assert runtimes["2.0"].embed_calls == [["same"]]


@pytest.mark.asyncio
async def test_embedding_singleflight_is_isolated_between_selected_instances(tmp_path):
    instances, runtimes = e2e_instance_manager()
    services = await bootstrap._local_memory_services(
        instances,
        "local",
        "/models/bge",
        "",
    )
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=services.embedding,
    )
    await instances.start("embedding-a")
    first_runtime = runtimes["1.0"]
    first_runtime.embed_entered = threading.Event()
    first_runtime.embed_release = threading.Event()

    first_embed = asyncio.create_task(vector_store.embed(["same"]))
    assert await asyncio.to_thread(first_runtime.embed_entered.wait, 1)
    await instances.start("embedding-b")

    try:
        second_result = await asyncio.wait_for(vector_store.embed(["same"]), 0.5)
    finally:
        first_runtime.embed_release.set()
        first_result = await first_embed

    assert first_result == [[1.0]]
    assert second_result == [[2.0]]
    assert runtimes["2.0"].embed_calls == [["same"]]


@pytest.mark.asyncio
async def test_embedding_singleflight_shares_same_instance_same_text(tmp_path):
    instances, runtimes = e2e_instance_manager()
    services = await bootstrap._local_memory_services(
        instances,
        "local",
        "/models/bge",
        "",
    )
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=services.embedding,
    )
    await instances.start("embedding-a")
    runtime = runtimes["1.0"]

    results = await asyncio.gather(
        vector_store.embed(["same"]),
        vector_store.embed(["same"]),
    )

    assert results == [[[1.0]], [[1.0]]]
    assert runtime.embed_calls == [["same"]]


@pytest.mark.asyncio
async def test_embedding_singleflight_leader_cancel_finishes_waiter_after_worker_cleanup(tmp_path):
    instances, runtimes = e2e_instance_manager()
    services = await bootstrap._local_memory_services(
        instances,
        "local",
        "/models/bge",
        "",
    )
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=services.embedding,
    )
    instance = await instances.start("embedding-a")
    runtime = runtimes["1.0"]
    runtime.embed_entered = threading.Event()
    runtime.embed_release = threading.Event()

    leader = asyncio.create_task(vector_store.embed(["cancelled"]))
    assert await asyncio.to_thread(runtime.embed_entered.wait, 1)
    waiter = asyncio.create_task(vector_store.embed(["cancelled"]))
    await asyncio.sleep(0)
    leader.cancel()
    await asyncio.sleep(0)
    assert not leader.done()
    runtime.embed_release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(leader, 1)
    done, pending = await asyncio.wait({waiter}, timeout=0.2)
    assert done == {waiter}
    assert pending == set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert instances.get(instance.id).active_routes == ()
    assert vector_store._inflight == {}


@pytest.mark.asyncio
async def test_embedding_singleflight_accepts_same_text_after_cancelled_generation(tmp_path):
    instances, runtimes = e2e_instance_manager()
    services = await bootstrap._local_memory_services(
        instances,
        "local",
        "/models/bge",
        "",
    )
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=services.embedding,
    )
    await instances.start("embedding-a")
    runtime = runtimes["1.0"]
    runtime.embed_entered = threading.Event()
    runtime.embed_release = threading.Event()

    leader = asyncio.create_task(vector_store.embed(["retry"]))
    assert await asyncio.to_thread(runtime.embed_entered.wait, 1)
    waiter = asyncio.create_task(vector_store.embed(["retry"]))
    await asyncio.sleep(0)
    leader.cancel()
    await asyncio.sleep(0)
    assert not leader.done()
    runtime.embed_release.set()
    results = await asyncio.gather(leader, waiter, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)

    runtime.embed_entered = None
    runtime.embed_release = None
    assert await asyncio.wait_for(vector_store.embed(["retry"]), 1) == [[1.0]]
    assert runtime.embed_calls == [["retry"], ["retry"]]
    assert vector_store._inflight == {}


@pytest.mark.asyncio
async def test_selected_local_embedding_instance_is_used(tmp_path):
    runtime = FakeEmbeddingRuntime()
    service = LocalEmbeddingService(runtime)
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=service,
    )

    assert await vector_store.embed(["hello"]) == [[5.0]]
    assert runtime.calls == [["hello"]]


@pytest.mark.asyncio
async def test_stopped_local_embedding_does_not_fallback_to_bundled_bge(tmp_path):
    runtime = FakeEmbeddingRuntime()
    service = LocalEmbeddingService(runtime)
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=service,
    )
    runtime.running = False

    with pytest.raises(LocalModelUnavailableError):
        await vector_store.embed(["hello"])


@pytest.mark.asyncio
async def test_stopping_selected_local_embedding_preserves_unavailable_selection(tmp_path):
    runtime = FakeEmbeddingRuntime()
    service = LocalEmbeddingService(runtime)
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=service,
    )

    vector_store.stop_local_engine()

    assert vector_store.embed_engine_status()["source"] == "instance"
    with pytest.raises(LocalModelUnavailableError):
        await vector_store.embed(["hello"])


@pytest.mark.asyncio
async def test_selected_local_reranker_instance_is_used():
    runtime = FakeRerankerRuntime()
    service = LocalRerankerService(runtime)
    manager = MemoryManager(None, None, reranker_service=service)

    results = await manager.rerank_with_selected_local_model("q", ["a", "b"])

    assert runtime.calls == [("q", ["a", "b"])]
    assert results == [
        {"index": 1, "relevance_score": 1.0},
        {"index": 0, "relevance_score": 0.0},
    ]


@pytest.mark.asyncio
async def test_stopped_local_reranker_reports_unavailable():
    runtime = FakeRerankerRuntime()
    service = LocalRerankerService(runtime)
    manager = MemoryManager(None, None, reranker_service=service)
    runtime.running = False

    with pytest.raises(LocalModelUnavailableError):
        await manager.rerank_with_selected_local_model("q", ["a"])


@pytest.mark.asyncio
async def test_remote_embedding_path_remains_compatible(tmp_path):
    vector_store = VectorStore(tmp_path / "vectors.db", embed_mode="remote")

    assert vector_store.embed_engine_status()["mode"] == "remote"


class ProbeVectorStore:
    def __init__(self, service) -> None:
        self._local_provider = service

    async def embed(self, text: str) -> list[float]:
        vectors = await self._local_provider.embed([text])
        return list(vectors[0])

    async def search(self, query, top_k, candidate_ids=None, deterministic=False, query_vec=None):
        await self._local_provider.embed([query])
        return []


@pytest.mark.asyncio
async def test_hybrid_vec_search_propagates_selected_embedding_unavailable():
    instances, _ = e2e_instance_manager()
    services = await bootstrap._local_memory_services(
        instances,
        "local",
        "/models/bge",
        "",
    )
    memory = MemoryManager(None, None, vector_store=ProbeVectorStore(services.embedding))

    instance = await instances.start("embedding-a")
    assert await memory._hybrid_vec_search("q", 5) == []
    await instances.stop(instance.id)

    with pytest.raises(LocalModelUnavailableError):
        await memory._hybrid_vec_search("q", 5)


@pytest.mark.asyncio
async def test_vector_store_rejects_runtime_dimension_mismatch_before_use(tmp_path):
    runtime = FakeEmbeddingRuntime()
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        dimensions=2,
        embed_mode="local",
        embedding_service=LocalEmbeddingService(runtime),
    )

    with pytest.raises(RuntimeValidationError, match="dimension 1.*expected 2"):
        await vector_store.embed(["hello"])


@pytest.mark.asyncio
async def test_selected_embedding_route_is_bound_only_during_inference():
    instances, runtimes = e2e_instance_manager()
    services = await bootstrap._local_memory_services(instances, "local", "/models/bge", "")
    instance = await instances.start("embedding-a")
    runtime = runtimes["1.0"]
    runtime.embed_entered = threading.Event()
    runtime.embed_release = threading.Event()

    task = asyncio.create_task(services.embedding.embed(["hello"]))
    assert await asyncio.to_thread(runtime.embed_entered.wait, 1)
    assert instances.get(instance.id).active_routes == ("memory:embedding",)
    with pytest.raises(InstanceInUseError):
        await instances.stop(instance.id)

    runtime.embed_release.set()
    assert await task == [[1.0]]
    assert instances.get(instance.id).active_routes == ()


@pytest.mark.asyncio
async def test_embedding_runtime_failure_is_observable_and_does_not_block_loop():
    runtime = FakeEmbeddingRuntime()

    def fail(_texts):
        raise RuntimeError("inference failed")

    runtime.embed = fail
    service = LocalEmbeddingService(runtime)
    loop_progressed = asyncio.Event()

    async def tick():
        await asyncio.sleep(0)
        loop_progressed.set()

    tick_task = asyncio.create_task(tick())
    with pytest.raises(RuntimeError, match="inference failed"):
        await service.embed(["hello"])
    await tick_task
    assert loop_progressed.is_set()


@pytest.mark.asyncio
async def test_bundled_bge_provider_remains_default_local_compatibility_path(tmp_path, monkeypatch):
    service = LocalEmbeddingService(FakeEmbeddingRuntime(), source="bundled")
    monkeypatch.setattr(VectorStore, "_build_local_provider", lambda self: service)

    vector_store = VectorStore(tmp_path / "vectors.db", embed_mode="local")

    assert vector_store.embed_engine_status()["source"] == "bundled"


def test_bootstrap_translates_local_mode_to_bundled_embedding_service(monkeypatch):
    service = object()
    calls = []

    monkeypatch.setattr(
        LocalEmbeddingService,
        "bundled",
        lambda model_dir, query_prefix="": calls.append((model_dir, query_prefix)) or service,
    )

    assert bootstrap._embedding_service_for_mode("local", "/models/bge", "query: ") is service
    assert calls == [("/models/bge", "query: ")]


def test_bootstrap_keeps_remote_embedding_without_local_service():
    assert bootstrap._embedding_service_for_mode("remote", "/models/bge", "query: ") is None


@pytest.mark.asyncio
async def test_bootstrap_injects_selected_embedding_and_reranker_runtimes():
    embedding = FakeEmbeddingRuntime()
    reranker = FakeRerankerRuntime()
    instances = FakeInstanceManager(
        {"embedding": embedding, "reranker": reranker},
        selections={"embedding", "reranker"},
    )

    services = await bootstrap._local_memory_services(
        instances,
        "local",
        "/models/bge",
        "query: ",
        remote_reranker=object(),
    )

    assert await services.embedding.embed(["hello"]) == [[5.0]]
    assert await services.reranker.score("q", ["a", "b"]) == [0.0, 1.0]
    assert [purpose.value for purpose in instances.purposes] == ["embedding", "reranker"]


@pytest.mark.asyncio
async def test_bootstrap_uses_bundled_embedding_without_managed_selection(monkeypatch):
    bundled = LocalEmbeddingService(FakeBundledEmbeddingRuntime(), source="bundled")
    remote = LocalRerankerService(FakeRerankerRuntime())
    instances = FakeInstanceManager()
    monkeypatch.setattr(
        LocalEmbeddingService,
        "bundled",
        lambda model_dir, query_prefix="": bundled,
    )

    services = await bootstrap._local_memory_services(
        instances,
        "local",
        "/models/bge",
        "query: ",
        remote_reranker=remote,
    )

    assert await services.embedding.embed(["hello"]) == [[5.0]]
    assert await services.reranker.score("q", ["a"]) == [0.0]


@pytest.mark.asyncio
async def test_bootstrap_propagates_selected_runtime_unavailable(monkeypatch):
    instances = FakeInstanceManager(
        errors={"embedding": RuntimeError("selected embedding unavailable")}
    )
    bundled_calls = []
    monkeypatch.setattr(
        LocalEmbeddingService,
        "bundled",
        lambda *args, **kwargs: bundled_calls.append(True),
    )

    services = await bootstrap._local_memory_services(
        instances,
        "local",
        "/models/bge",
        "",
        remote_reranker=object(),
    )
    with pytest.raises(LocalModelUnavailableError, match="selected embedding unavailable"):
        await services.embedding.embed(["hello"])
    assert bundled_calls == []


def test_bundled_embedding_load_failure_is_explicit():
    service = LocalEmbeddingService(FailingBundledRuntime(), source="bundled")

    with pytest.raises(LocalModelUnavailableError, match="bundled.*failed to load"):
        service.load()


def test_vector_store_propagates_bundled_embedding_load_failure(tmp_path):
    service = LocalEmbeddingService(FailingBundledRuntime(), source="bundled")
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=service,
    )

    with pytest.raises(LocalModelUnavailableError, match="bundled.*failed to load"):
        vector_store.start_local_engine()


@pytest.mark.asyncio
async def test_embedding_stale_completion_does_not_pollute_cache_after_switch(tmp_path):
    instances, runtimes = e2e_instance_manager()
    services = await bootstrap._local_memory_services(
        instances,
        "local",
        "/models/bge",
        "",
    )
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=services.embedding,
    )
    await instances.start("embedding-a")
    first_runtime = runtimes["1.0"]
    first_runtime.embed_entered = threading.Event()
    first_runtime.embed_release = threading.Event()

    first_embed = asyncio.create_task(vector_store.embed(["same"]))
    assert await asyncio.to_thread(first_runtime.embed_entered.wait, 1)
    await instances.start("embedding-b")

    try:
        second_result = await asyncio.wait_for(vector_store.embed(["same"]), 0.5)
    finally:
        first_runtime.embed_release.set()
        first_result = await first_embed

    assert second_result == [[2.0]]
    assert first_result == [[1.0]]

    assert await vector_store.embed(["same"]) == [[2.0]]
    assert runtimes["2.0"].embed_calls == [["same"]]


@pytest.mark.asyncio
async def test_vector_store_embed_is_the_batch_embedding_entrypoint(tmp_path):
    runtime = FakeEmbeddingRuntime()
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=LocalEmbeddingService(runtime),
    )

    assert await vector_store.embed(["hello", "world!"]) == [[5.0], [6.0]]
    assert runtime.calls == [["hello", "world!"]]


@pytest.mark.asyncio
async def test_managed_local_embedding_without_running_instance_is_structured_error(tmp_path):
    bundled_calls = []
    service = LocalEmbeddingService.managed(
        FakeInstanceManager(selections={"embedding"}),
        lambda: bundled_calls.append(True),
    )
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=service,
    )

    with pytest.raises(LocalEmbeddingUnavailableError) as exc_info:
        await vector_store.embed(["hello"])

    assert exc_info.value.code == "local_embedding_unavailable"
    assert exc_info.value.details == {"purpose": "embedding", "mode": "local"}
    assert bundled_calls == []


@pytest.mark.asyncio
async def test_vector_store_batch_validates_every_embedding_dimension(tmp_path):
    runtime = FakeEmbeddingRuntime()
    runtime.dimensions = 2
    runtime.embed = lambda texts: [[1.0, 2.0], [3.0]]
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        dimensions=2,
        embed_mode="local",
        embedding_service=LocalEmbeddingService(runtime),
    )

    with pytest.raises(RuntimeValidationError, match="dimension 1.*expected 2"):
        await vector_store.embed(["valid", "invalid"])


@pytest.mark.asyncio
async def test_batch_upsert_children_embeds_once_and_writes_all_rows():
    connection = MagicMock()
    vector_store = VectorStore.__new__(VectorStore)
    vector_store._initialized = True
    vector_store._closed = False
    vector_store._vec_conn = connection
    vector_store._lock = threading.Lock()
    vector_store._brute = None
    vector_store._dimensions = 2
    vector_store.embed = AsyncMock(return_value=[[1.0, 2.0], [3.0, 4.0]])

    assert await vector_store.batch_upsert_children([(11, "first"), (12, "second")])
    vector_store.embed.assert_awaited_once_with(["first", "second"])
    inserts = [
        call for call in connection.execute.call_args_list
        if "INSERT OR REPLACE" in call.args[0]
    ]
    assert [call.args[1][0] for call in inserts] == [11, 12]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vectors",
    [
        [[1.0, 2.0]],
        [[1.0, 2.0], [3.0]],
    ],
)
async def test_batch_upsert_children_rejects_incomplete_or_wrong_dimension_batch(vectors):
    connection = MagicMock()
    vector_store = VectorStore.__new__(VectorStore)
    vector_store._initialized = True
    vector_store._closed = False
    vector_store._vec_conn = connection
    vector_store._lock = threading.Lock()
    vector_store._brute = None
    vector_store._dimensions = 2
    vector_store.embed = AsyncMock(return_value=vectors)

    assert not await vector_store.batch_upsert_children([(11, "first"), (12, "second")])
    assert not any(
        "INSERT OR REPLACE" in call.args[0]
        for call in connection.execute.call_args_list
    )


@pytest.mark.asyncio
async def test_child_chunk_insert_is_compensated_when_vector_write_fails():
    class FakeMemory:
        def __init__(self) -> None:
            self.rows = {}
            self.next_id = 1

        async def insert_child_chunks(self, parent_id, children, auto_commit=True):
            child_ids = []
            for fields in children:
                child_id = self.next_id
                self.next_id += 1
                self.rows[child_id] = {"parent_id": parent_id, **fields}
                child_ids.append(child_id)
            return child_ids

        async def delete_child_chunks(self, child_ids):
            for child_id in child_ids:
                self.rows.pop(child_id, None)

    class FakeVectorStore:
        async def batch_upsert_children(self, items):
            return False

        async def delete_child(self, child_id):
            raise AssertionError("failed vector transaction must not leave vectors to delete")

    memory = FakeMemory()
    manager = MemoryManager.__new__(MemoryManager)
    manager.memory = memory
    manager.vec = FakeVectorStore()
    children = [
        {
            "content": "first",
            "embed_content": "first vector",
            "chunk_type": "segment",
            "weight": 1.0,
            "overlap_hash": "",
        },
        {
            "content": "second",
            "embed_content": "second vector",
            "chunk_type": "segment",
            "weight": 0.8,
            "overlap_hash": "",
        },
    ]

    assert not await manager._insert_indexed_children(7, children, 0.9)
    assert memory.rows == {}


@pytest.mark.asyncio
async def test_hybrid_reranker_uses_remote_when_no_local_instance_is_selected():
    remote = LocalRerankerService(FakeRerankerRuntime())
    service = LocalRerankerService.managed(FakeInstanceManager(), remote)

    assert await service.score("q", ["a", "b"]) == [0.0, 1.0]


@pytest.mark.asyncio
async def test_hybrid_reranker_does_not_fallback_when_selected_local_instance_stops():
    remote_runtime = FakeRerankerRuntime()
    remote = LocalRerankerService(remote_runtime)
    instances = FakeInstanceManager(selections={"reranker"})
    service = LocalRerankerService.managed(instances, remote)

    with pytest.raises(LocalRerankerUnavailableError) as exc_info:
        await service.score("q", ["a"])

    assert exc_info.value.code == "local_reranker_unavailable"
    assert exc_info.value.details == {"purpose": "reranker", "mode": "local"}
    assert remote_runtime.calls == []


@pytest.mark.asyncio
async def test_selected_reranker_route_blocks_stop_until_score_finishes():
    instances, runtimes = e2e_instance_manager()
    services = await bootstrap._local_memory_services(instances, "local", "/models/bge", "")
    instance = await instances.start("reranker-a")
    runtime = runtimes["1.0"]
    runtime.score_entered = threading.Event()
    runtime.score_release = threading.Event()

    task = asyncio.create_task(services.reranker.score("q", ["a"]))
    assert await asyncio.to_thread(runtime.score_entered.wait, 1)
    assert instances.get(instance.id).active_routes == ("memory:reranker",)
    with pytest.raises(InstanceInUseError):
        await instances.stop(instance.id)

    runtime.score_release.set()
    assert await task == [1.0]
    assert instances.get(instance.id).active_routes == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", [ModelPurpose.EMBEDDING, ModelPurpose.RERANKER])
async def test_cancelled_local_inference_keeps_route_until_worker_finishes(purpose):
    instances, runtimes = e2e_instance_manager()
    services = await bootstrap._local_memory_services(instances, "local", "/models/bge", "")
    model_id = "embedding-a" if purpose is ModelPurpose.EMBEDDING else "reranker-a"
    instance = await instances.start(model_id)
    runtime = runtimes["1.0"]
    entered = threading.Event()
    release = threading.Event()
    if purpose is ModelPurpose.EMBEDDING:
        runtime.embed_entered = entered
        runtime.embed_release = release
        inference = services.embedding.embed(["a"])
    else:
        runtime.score_entered = entered
        runtime.score_release = release
        inference = services.reranker.score("q", ["a"])

    task = asyncio.create_task(inference)
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)

    with pytest.raises(InstanceInUseError):
        await instances.stop(instance.id)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await instances.stop(instance.id)


@pytest.mark.asyncio
async def test_managed_embedding_rejects_awaitable_fallback_factory():
    async def fallback_factory():
        return LocalEmbeddingService(FakeEmbeddingRuntime())

    service = LocalEmbeddingService.managed(FakeInstanceManager(), fallback_factory)

    with pytest.raises(RuntimeValidationError, match="fallback_factory.*awaitable"):
        await service.embed(["hello"])
    assert inspect.iscoroutinefunction(fallback_factory)


def test_managed_reranker_without_selection_or_fallback_is_unavailable():
    service = LocalRerankerService.managed(FakeInstanceManager(), None)

    assert service.available is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        LocalEmbeddingUnavailableError("embedding stopped"),
        LocalRerankerUnavailableError("reranker stopped"),
    ],
)
async def test_query_cache_propagates_structured_local_unavailable(error):
    async def unavailable(_text):
        raise error

    cache = QueryCache(embed_func=unavailable)

    with pytest.raises(type(error)) as exc_info:
        await cache.get("query")
    assert exc_info.value is error


@pytest.mark.asyncio
async def test_message_context_propagates_structured_local_unavailable(monkeypatch):
    from agent_core.message_processor import MessageProcessorMixin

    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)
    processor.memory = MagicMock()
    processor.memory._suggest_k.return_value = 1
    processor.memory.retrieve_memories = AsyncMock(
        side_effect=LocalEmbeddingUnavailableError("embedding stopped")
    )
    processor._load_notebook_context = AsyncMock()
    monkeypatch.setattr(
        "agent_core.mixins.main_path.get_degradation_strategy",
        lambda: SimpleNamespace(is_feature_available=lambda _feature: True),
    )
    monkeypatch.setattr(
        "memory.scope.current_scope",
        lambda: SimpleNamespace(user_id="user"),
    )

    with pytest.raises(LocalEmbeddingUnavailableError):
        await processor._retrieve_main_memories("query", True, None)


@pytest.mark.asyncio
async def test_insight_rest_propagates_structured_local_unavailable():
    from web.routers.insight import list_memories

    core = SimpleNamespace(
        memory=SimpleNamespace(
            retrieve_memories=AsyncMock(
                side_effect=LocalRerankerUnavailableError("reranker stopped")
            )
        ),
        db=SimpleNamespace(fetch_all=AsyncMock(return_value=[])),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(core=core)))

    with pytest.raises(LocalRerankerUnavailableError):
        await list_memories(request, q="query", importance_min=0.0, page=0, limit=30)


@pytest.mark.parametrize(
    ("error", "code", "purpose"),
    [
        (LocalEmbeddingUnavailableError("embedding stopped"), "local_embedding_unavailable", "embedding"),
        (LocalRerankerUnavailableError("reranker stopped"), "local_reranker_unavailable", "reranker"),
    ],
)
def test_real_http_response_preserves_local_unavailable_error(error, code, purpose):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.error_handler import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)

    @app.get("/local-error")
    async def local_error():
        raise error

    response = TestClient(app, raise_server_exceptions=False).get("/local-error")

    assert response.status_code == 503
    assert response.json() == {
        "error_code": code,
        "message": error.message,
        "retryable": True,
        "details": {"purpose": purpose, "mode": "local"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [True, False])
async def test_multi_query_propagates_structured_local_unavailable(parallel):
    manager = MemoryManager.__new__(MemoryManager)
    unavailable = LocalEmbeddingUnavailableError("embedding stopped")
    manager.retrieve_memories_hybrid = AsyncMock(side_effect=[[], unavailable])
    manager._reranker = None

    with pytest.raises(LocalEmbeddingUnavailableError) as exc_info:
        if parallel:
            await manager._multi_query_parallel_search(["one", "two"], "original", 2)
        else:
            await manager._multi_query_serial_search(["one", "two"], 2)
    assert exc_info.value is unavailable


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [True, False])
async def test_multi_query_tolerates_ordinary_errors(parallel):
    manager = MemoryManager.__new__(MemoryManager)
    manager.retrieve_memories_hybrid = AsyncMock(
        side_effect=[[{"id": 1, "summary": "ok"}], RuntimeError("ordinary failure")]
    )
    manager._reranker = None

    if parallel:
        result = await manager._multi_query_parallel_search(["one", "two"], "original", 2)
    else:
        result = await manager._multi_query_serial_search(["one", "two"], 2)

    assert result == [{"id": 1, "summary": "ok"}]


@pytest.mark.asyncio
async def test_query_cache_adapter_uses_vector_store_batch_embed_entrypoint():
    class BatchOnlyVectorStore:
        def __init__(self) -> None:
            self.calls = []

        async def embed(self, texts):
            assert isinstance(texts, list)
            self.calls.append(texts)
            return [[3.0] for _ in texts]

    vector_store = BatchOnlyVectorStore()
    manager = MemoryManager.__new__(MemoryManager)
    manager.vec = vector_store

    assert await manager._get_query_embedding_func()("query") == [3.0]
    assert vector_store.calls == [["query"]]
