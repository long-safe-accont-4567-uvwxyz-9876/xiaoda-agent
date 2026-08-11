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
        model_dir: str | Path | None = None,
        purpose: ModelPurpose | str | None = None,
    ) -> RuntimeAdapter:
        if installed_model is not None:
            model_dir = installed_model.directory
            purpose = installed_model.purpose
        if not isinstance(model_dir, (str, Path)) or not str(model_dir):
            raise RuntimeValidationError("model_dir is required")
        if purpose is None:
            raise RuntimeValidationError("purpose is required")
        try:
            resolved_purpose = ModelPurpose(purpose)
        except ValueError as error:
            raise RuntimeValidationError(f"unsupported model purpose: {purpose}") from error
        combination = (profile.runtime, resolved_purpose)
        supported = {
            (RuntimeKind.ORT, ModelPurpose.EMBEDDING),
            (RuntimeKind.ORT, ModelPurpose.RERANKER),
            (RuntimeKind.ORT_GENAI, ModelPurpose.CHAT),
        }
        if combination not in supported:
            raise RuntimeValidationError(
                "unsupported runtime and purpose combination: "
                f"{profile.runtime.value}/{resolved_purpose.value}"
            )
        factory = self._factories.get(profile.runtime)
        if factory is not None:
            return factory(profile)
        if combination == (RuntimeKind.ORT_GENAI, ModelPurpose.CHAT):
            return OrtGenAiChatRuntime(Path(model_dir))
        resolved_model_dir = Path(model_dir)
        if combination == (RuntimeKind.ORT, ModelPurpose.EMBEDDING):
            return EmbeddingRuntime(resolved_model_dir)
        if combination == (RuntimeKind.ORT, ModelPurpose.RERANKER):
            return RerankerRuntime(resolved_model_dir)
        raise RuntimeValidationError(
            "unsupported runtime and purpose combination: "
            f"{profile.runtime.value}/{resolved_purpose.value}"
        )

__all__ = [
    "RuntimeAdapter",
    "RuntimeRegistry",
]
