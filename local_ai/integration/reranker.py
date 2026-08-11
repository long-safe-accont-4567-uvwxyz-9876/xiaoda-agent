from __future__ import annotations

import asyncio
from typing import Any


class LocalModelUnavailableError(RuntimeError):
    pass


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
            return True
        health = getattr(self._runtime, "health", None)
        return bool(health is not None and health())

    async def score(self, query: str, documents: list[str]) -> list[float]:
        if self._unavailable_error is not None:
            raise LocalModelUnavailableError(str(self._unavailable_error))
        if self._instance_manager is not None:
            from local_ai.contracts import ModelPurpose

            try:
                runtime = await self._instance_manager.resolve_runtime(ModelPurpose.RERANKER)
            except Exception as error:
                raise LocalModelUnavailableError(str(error)) from error
            if runtime is None:
                if self._fallback is None:
                    raise LocalModelUnavailableError("no local reranker model is selected")
                return await self._fallback.score(query, documents)
            scores = await asyncio.to_thread(runtime.score, query, documents)
            if len(scores) != len(documents):
                raise RuntimeError(
                    f"local reranker returned {len(scores)} scores for {len(documents)} documents"
                )
            return scores
        if not self.available:
            raise LocalModelUnavailableError("selected local reranker model is unavailable")
        scores = await asyncio.to_thread(self._runtime.score, query, documents)
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


__all__ = ["LocalModelUnavailableError", "LocalRerankerService"]
