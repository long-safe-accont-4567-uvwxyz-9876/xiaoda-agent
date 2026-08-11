from __future__ import annotations

import pytest

import core.bootstrap as bootstrap
from local_ai.integration.embedding import LocalEmbeddingService
from local_ai.integration.reranker import LocalModelUnavailableError, LocalRerankerService
from memory.memory_manager import MemoryManager
from memory.vector_store import VectorStore


class FakeEmbeddingRuntime:
    def __init__(self) -> None:
        self.running = True
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
