from __future__ import annotations

from typing import Any

from local_ai.integration.errors import (
    LocalModelUnavailableError,
    LocalRerankerUnavailableError,
    is_structured_local_unavailable,
    run_worker_to_completion,
)


class LocalRerankerService:
    def __init__(
        self,
        runtime: Any,
        *,
        unavailable_error: Exception | None = None,
        instance_manager: Any = None,
        fallback: Any = None,
    ) -> None:
        self._runtime = runtime
        self._unavailable_error = unavailable_error
        self._instance_manager = instance_manager
        self._fallback = fallback

    @classmethod
    def managed(cls, instance_manager: Any, fallback: Any) -> LocalRerankerService:
        return cls(None, instance_manager=instance_manager, fallback=fallback)

    @property
    def available(self) -> bool:
        if self._instance_manager is not None:
            from local_ai.contracts import ModelPurpose

            identity = self._instance_manager.selection_identity(ModelPurpose.RERANKER)
            if identity is not None:
                checker = getattr(self._instance_manager, "selection_available", None)
                return bool(checker is not None and checker(ModelPurpose.RERANKER))
            return bool(self._fallback and self._fallback.available)
        health = getattr(self._runtime, "health", None)
        return bool(health is not None and health())

    async def score(self, query: str, documents: list[str]) -> list[float]:
        if self._unavailable_error is not None:
            raise LocalRerankerUnavailableError(str(self._unavailable_error))
        if self._instance_manager is not None:
            from local_ai.contracts import ModelPurpose

            identity = self._instance_manager.selection_identity(ModelPurpose.RERANKER)
            if identity is None:
                if self._fallback is None or not self._fallback.available:
                    raise LocalRerankerUnavailableError("no local reranker model is selected")
                return await self._fallback.score(query, documents)
            route = "memory:reranker"
            binding = None
            try:
                acquire = getattr(self._instance_manager, "acquire_runtime", None)
                if acquire is None:
                    runtime = await self._instance_manager.resolve_runtime(ModelPurpose.RERANKER)
                    if runtime is None:
                        raise LocalRerankerUnavailableError(
                            "selected local reranker model is unavailable"
                        )
                else:
                    acquired = await acquire(ModelPurpose.RERANKER, route)
                    if acquired is None:
                        raise LocalRerankerUnavailableError(
                            "selected local reranker model is unavailable"
                        )
                    instance_id, runtime = acquired
                    binding = (instance_id, route)
            except Exception as error:
                if isinstance(error, LocalRerankerUnavailableError):
                    raise
                raise LocalRerankerUnavailableError(str(error)) from error
            try:
                scores = await run_worker_to_completion(runtime.score, query, documents)
                if len(scores) != len(documents):
                    raise RuntimeError(
                        f"local reranker returned {len(scores)} scores for {len(documents)} documents"
                    )
                return scores
            finally:
                if binding is not None:
                    self._instance_manager.release_runtime(*binding)
        if not self.available:
            raise LocalRerankerUnavailableError("selected local reranker model is unavailable")
        scores = await run_worker_to_completion(self._runtime.score, query, documents)
        if len(scores) != len(documents):
            raise RuntimeError(
                f"local reranker returned {len(scores)} scores for {len(documents)} documents"
            )
        return scores

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int = 5,
        return_documents: bool = True,
    ) -> list[dict[str, Any]]:
        scores = await self.score(query, documents)
        results = [
            {
                "index": index,
                "relevance_score": score,
                **({"document": {"text": documents[index]}} if return_documents else {}),
            }
            for index, score in enumerate(scores)
        ]
        results.sort(key=lambda item: item["relevance_score"], reverse=True)
        return results[:top_n]


__all__ = [
    "LocalModelUnavailableError",
    "LocalRerankerService",
    "LocalRerankerUnavailableError",
    "is_structured_local_unavailable",
]
