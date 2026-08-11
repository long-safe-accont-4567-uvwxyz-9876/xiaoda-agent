from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable

from local_ai.integration.reranker import LocalModelUnavailableError


class LocalEmbeddingUnavailableError(LocalModelUnavailableError):
    code = "local_embedding_unavailable"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.details = {"purpose": "embedding", "mode": "local"}


class LocalEmbeddingService:
    def __init__(
        self,
        runtime: Any,
        *,
        source: str = "instance",
        unavailable_error: Exception | None = None,
        instance_manager: Any = None,
        fallback: LocalEmbeddingService | None = None,
        fallback_factory: Callable[[], LocalEmbeddingService | None] | None = None,
    ) -> None:
        self._runtime = runtime
        self.source = source
        self._unavailable_error = unavailable_error
        self._instance_manager = instance_manager
        self._fallback = fallback
        self._fallback_factory = fallback_factory

    @classmethod
    def managed(
        cls,
        instance_manager: Any,
        fallback_factory: Callable[[], LocalEmbeddingService | None],
    ) -> LocalEmbeddingService:
        return cls(
            None,
            source="instance",
            instance_manager=instance_manager,
            fallback_factory=fallback_factory,
        )

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
        if self._unavailable_error is not None:
            raise LocalModelUnavailableError(str(self._unavailable_error))
        loader = getattr(self._runtime, "load", None)
        if loader is None:
            return self.available
        if not loader():
            raise LocalModelUnavailableError(
                f"{self.source} embedding model failed to load"
            )
        return True

    @property
    def available(self) -> bool:
        if self._instance_manager is not None:
            return True
        if self.source == "bundled":
            return True
        health = getattr(self._runtime, "health", None)
        if health is not None:
            return bool(health())
        return self.ready

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._unavailable_error is not None:
            raise LocalEmbeddingUnavailableError(str(self._unavailable_error))
        runtime, binding = await self._resolve_runtime_for_inference()
        source = "instance" if runtime is not self._fallback else "bundled"
        try:
            if source == "bundled":
                return await runtime.embed(texts)
            if self._instance_manager is not None:
                result = await asyncio.to_thread(runtime.embed, texts)
                if len(result) != len(texts):
                    raise RuntimeError(
                        f"local embedding returned {len(result)} vectors for {len(texts)} texts"
                    )
                return result
            if not self.available:
                raise LocalEmbeddingUnavailableError("selected local embedding model is unavailable")
            if self.source == "bundled":
                result = [await asyncio.to_thread(self._runtime.embed, text) for text in texts]
            else:
                result = await asyncio.to_thread(self._runtime.embed, texts)
            if len(result) != len(texts):
                raise RuntimeError(
                    f"local embedding returned {len(result)} vectors for {len(texts)} texts"
                )
            return result
        finally:
            if binding is not None:
                instance_id, route = binding
                self._instance_manager.release_runtime(instance_id, route)

    async def resolve_dimensions(self) -> int:
        runtime = await self._resolve_runtime()
        return int(getattr(runtime, "dimensions", 0) or 0)

    async def selection_key(self) -> Any:
        runtime = await self._resolve_runtime()
        if self._instance_manager is not None:
            from local_ai.contracts import ModelPurpose

            identity = self._instance_manager.selection_identity(ModelPurpose.EMBEDDING)
            if identity is not None:
                return identity
        return id(runtime)

    async def _resolve_runtime(self) -> Any:
        if self._instance_manager is None:
            return self
        from local_ai.contracts import ModelPurpose

        try:
            runtime = await self._instance_manager.resolve_runtime(ModelPurpose.EMBEDDING)
        except Exception as error:
            raise LocalEmbeddingUnavailableError(str(error)) from error
        if runtime is not None:
            return runtime
        if self._fallback is None and self._fallback_factory is not None:
            self._fallback = self._fallback_factory()
        if self._fallback is not None:
            return self._fallback
        raise LocalEmbeddingUnavailableError("no running local embedding instance")

    async def _resolve_runtime_for_inference(self) -> tuple[Any, tuple[str, str] | None]:
        if self._instance_manager is None:
            return await self._resolve_runtime(), None
        from local_ai.contracts import ModelPurpose

        acquire = getattr(self._instance_manager, "acquire_runtime", None)
        if acquire is None:
            return await self._resolve_runtime(), None
        route = "memory:embedding"
        try:
            acquired = await acquire(ModelPurpose.EMBEDDING, route)
        except Exception as error:
            raise LocalEmbeddingUnavailableError(str(error)) from error
        if acquired is None:
            return await self._resolve_runtime(), None
        instance_id, runtime = acquired
        return runtime, (instance_id, route)

    def close(self) -> None:
        closer = getattr(self._runtime, "close", None) or getattr(self._runtime, "stop", None)
        if closer is not None:
            closer()


__all__ = ["LocalEmbeddingService", "LocalEmbeddingUnavailableError"]
