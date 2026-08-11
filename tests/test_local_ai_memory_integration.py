from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

import pytest

import core.bootstrap as bootstrap
from local_ai.contracts import CatalogModel, ComputeDevice, InstalledModel, ModelPurpose, RuntimeProfile
from local_ai.instances.manager import InstanceInUseError, InstanceManager
from local_ai.integration.embedding import LocalEmbeddingService
from local_ai.integration.reranker import LocalModelUnavailableError, LocalRerankerService
from local_ai.runtimes.registry import RuntimeRegistry
from local_ai.runtimes.base import RuntimeValidationError
from memory.memory_manager import MemoryManager
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
    def __init__(self, runtimes=None, errors=None) -> None:
        self.runtimes = runtimes or {}
        self.errors = errors or {}
        self.purposes = []

    async def resolve_runtime(self, purpose):
        self.purposes.append(purpose)
        error = self.errors.get(purpose.value)
        if error is not None:
            raise error
        return self.runtimes.get(purpose.value)


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

    assert await vector_store.embed("same") == [1.0]
    assert await memory.rerank_with_selected_local_model("q", ["a", "b"]) == [
        {"index": 1, "relevance_score": 3.0},
        {"index": 0, "relevance_score": 2.0},
    ]
    assert runtimes["1.0"].embed_calls == [["same"]]
    assert runtimes["2.0"].score_calls == [("q", ["a", "b"])]

    await instances.stop(embedding_instance.id)
    await instances.stop(reranker_instance.id)

    with pytest.raises(LocalModelUnavailableError):
        await vector_store.embed("new")
    with pytest.raises(LocalModelUnavailableError):
        await memory.rerank_with_selected_local_model("q", ["a"])


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

    assert await vector_store.embed("cached") == [1.0]
    assert await vector_store.embed("cached") == [1.0]
    await instances.stop(instance.id)

    with pytest.raises(LocalModelUnavailableError):
        await vector_store.embed("cached")


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
    assert await vector_store.embed("same") == [1.0]
    await instances.stop(first.id)
    await instances.start("embedding-b")

    assert await vector_store.embed("same") == [2.0]
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

    first_embed = asyncio.create_task(vector_store.embed("same"))
    assert await asyncio.to_thread(first_runtime.embed_entered.wait, 1)
    await instances.start("embedding-b")

    try:
        second_result = await asyncio.wait_for(vector_store.embed("same"), 0.5)
    finally:
        first_runtime.embed_release.set()
        first_result = await first_embed

    assert first_result == [1.0]
    assert second_result == [2.0]
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
        vector_store.embed("same"),
        vector_store.embed("same"),
    )

    assert results == [[1.0], [1.0]]
    assert runtime.embed_calls == [["same"]]


@pytest.mark.asyncio
async def test_selected_local_embedding_instance_is_used(tmp_path):
    runtime = FakeEmbeddingRuntime()
    service = LocalEmbeddingService(runtime)
    vector_store = VectorStore(
        tmp_path / "vectors.db",
        embed_mode="local",
        embedding_service=service,
    )

    assert await vector_store.embed("hello") == [5.0]
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
        await vector_store.embed("hello")


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
        await vector_store.embed("hello")


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

    async def search(self, query, top_k, candidate_ids=None, deterministic=False):
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
        await vector_store.embed("hello")


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
    instances = FakeInstanceManager({"embedding": embedding, "reranker": reranker})

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
async def test_bootstrap_only_uses_compatibility_paths_without_selection(monkeypatch):
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

    first_embed = asyncio.create_task(vector_store.embed("same"))
    assert await asyncio.to_thread(first_runtime.embed_entered.wait, 1)
    await instances.start("embedding-b")

    try:
        second_result = await asyncio.wait_for(vector_store.embed("same"), 0.5)
    finally:
        first_runtime.embed_release.set()
        first_result = await first_embed

    assert second_result == [2.0]
    assert first_result == [1.0]

    assert await vector_store.embed("same") == [2.0]
    assert runtimes["2.0"].embed_calls == [["same"]]
