from __future__ import annotations

import asyncio
from typing import Any


class LocalModelUnavailableError(RuntimeError):
    pass


class LocalRerankerService:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    @property
    def available(self) -> bool:
        health = getattr(self._runtime, "health", None)
        return bool(health is not None and health())

    async def score(self, query: str, documents: list[str]) -> list[float]:
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
