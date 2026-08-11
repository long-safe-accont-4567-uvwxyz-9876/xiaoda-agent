from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from local_ai.contracts import ModelPurpose, RuntimeKind, RuntimeProfile
from local_ai.runtimes.base import Runtime, RuntimeValidationError
from local_ai.runtimes.ort_embedding import EmbeddingRuntime
from local_ai.runtimes.ort_genai import (
    OrtGenAiChatRuntime,
)
from local_ai.runtimes.ort_reranker import RerankerRuntime

RuntimeAdapter = Runtime
RuntimeFactory = Callable[[RuntimeProfile], RuntimeAdapter]


class RuntimeRegistry:
    def __init__(
        self,
        factories: Mapping[RuntimeKind | str, RuntimeFactory] | None = None,
    ) -> None:
        self._factories = {
            RuntimeKind(kind): factory for kind, factory in (factories or {}).items()
        }

    def create(self, profile: RuntimeProfile) -> RuntimeAdapter:
        purpose = self._purpose(profile)
        combination = (profile.runtime, purpose)
        supported = {
            (RuntimeKind.ORT, ModelPurpose.EMBEDDING),
            (RuntimeKind.ORT, ModelPurpose.RERANKER),
            (RuntimeKind.ORT_GENAI, ModelPurpose.CHAT),
        }
        if combination not in supported:
            raise RuntimeValidationError(
                "unsupported runtime and purpose combination: "
                f"{profile.runtime.value}/{purpose.value}"
            )
        factory = self._factories.get(profile.runtime)
        if factory is not None:
            return factory(profile)
        if combination == (RuntimeKind.ORT_GENAI, ModelPurpose.CHAT):
            return OrtGenAiChatRuntime(self._model_dir(profile))
        model_dir = self._model_dir(profile)
        if combination == (RuntimeKind.ORT, ModelPurpose.EMBEDDING):
            return EmbeddingRuntime(model_dir)
        if combination == (RuntimeKind.ORT, ModelPurpose.RERANKER):
            return RerankerRuntime(model_dir)
        raise RuntimeValidationError(
            "unsupported runtime and purpose combination: "
            f"{profile.runtime.value}/{purpose.value}"
        )

    @staticmethod
    def _purpose(profile: RuntimeProfile) -> ModelPurpose:
        purpose = profile.options.get("purpose")
        if purpose is None:
            raise RuntimeValidationError("profile.options.purpose is required")
        try:
            return ModelPurpose(purpose)
        except ValueError as error:
            raise RuntimeValidationError(f"unsupported model purpose: {purpose}") from error

    @staticmethod
    def _model_dir(profile: RuntimeProfile) -> Path:
        model_dir = profile.options.get("model_dir")
        if not isinstance(model_dir, str) or not model_dir:
            raise RuntimeValidationError("profile.options.model_dir is required")
        return Path(model_dir)


__all__ = [
    "RuntimeAdapter",
    "RuntimeRegistry",
]
