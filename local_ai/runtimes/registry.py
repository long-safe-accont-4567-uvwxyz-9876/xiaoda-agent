from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from local_ai.contracts import InstalledModel, ModelPurpose, RuntimeKind, RuntimeProfile
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

    def create(
        self,
        profile: RuntimeProfile,
        *,
        installed_model: InstalledModel | None = None,
    ) -> RuntimeAdapter:
        if installed_model is None:
            raise RuntimeValidationError("installed_model is required")
        purpose = installed_model.purpose
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
            return OrtGenAiChatRuntime(Path(installed_model.directory))
        model_dir = Path(installed_model.directory)
        if combination == (RuntimeKind.ORT, ModelPurpose.EMBEDDING):
            return EmbeddingRuntime(model_dir)
        if combination == (RuntimeKind.ORT, ModelPurpose.RERANKER):
            return RerankerRuntime(model_dir)
        raise RuntimeValidationError(
            "unsupported runtime and purpose combination: "
            f"{profile.runtime.value}/{purpose.value}"
        )

__all__ = [
    "RuntimeAdapter",
    "RuntimeRegistry",
]
