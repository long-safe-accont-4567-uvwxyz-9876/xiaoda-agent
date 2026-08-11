from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from local_ai.integration.reranker import LocalModelUnavailableError


class LocalEmbeddingService:
    def __init__(self, runtime: Any, *, source: str = "instance") -> None:
        self._runtime = runtime
        self.source = source

    @classmethod
    def bundled(
        cls,
        model_dir: str | Path,
        *,
        query_prefix: str = "",
    ) -> LocalEmbeddingService:
        backend = os.getenv("LOCAL_EMBED_BACKEND", "auto")
        if backend in ("npu", "auto"):
            from memory.npu_embed import AdaptiveEmbeddingProvider

            runtime = AdaptiveEmbeddingProvider(str(model_dir), query_prefix=query_prefix)
        else:
            from memory.local_embed import LocalEmbeddingProvider

            runtime = LocalEmbeddingProvider(str(model_dir), query_prefix=query_prefix)
        return cls(runtime, source="bundled")

    @property
    def ready(self) -> bool:
        return bool(getattr(self._runtime, "ready", False))

    @property
    def dimensions(self) -> int:
        return int(getattr(self._runtime, "dimensions", 0) or 0)

    def load(self) -> bool:
        loader = getattr(self._runtime, "load", None)
        if loader is None:
            return self.available
        return bool(loader())

    @property
    def available(self) -> bool:
        if self.source == "bundled":
            return True
        health = getattr(self._runtime, "health", None)
        if health is not None:
            return bool(health())
        return self.ready

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.available:
            raise LocalModelUnavailableError("selected local embedding model is unavailable")
        if self.source == "bundled":
            result = [await asyncio.to_thread(self._runtime.embed, text) for text in texts]
        else:
            result = await asyncio.to_thread(self._runtime.embed, texts)
        if len(result) != len(texts):
            raise RuntimeError(
                f"local embedding returned {len(result)} vectors for {len(texts)} texts"
            )
        return result

    def close(self) -> None:
        closer = getattr(self._runtime, "close", None) or getattr(self._runtime, "stop", None)
        if closer is not None:
            closer()


__all__ = ["LocalEmbeddingService"]
