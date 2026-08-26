from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Callable

from core.app_exception import AppException
from local_ai.runtimes.base import RuntimeValidationError


@dataclass(frozen=True)
class LocalUnavailableCode:
    code: str
    http_status: int = 503
    message: str = "Local model is unavailable"
    retryable: bool = True


class LocalModelUnavailableError(AppException, RuntimeError):
    code = "local_model_unavailable"
    purpose = "local"

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            error_code=LocalUnavailableCode(self.code),
            details={"purpose": self.purpose, "mode": "local"},
        )


class LocalEmbeddingUnavailableError(LocalModelUnavailableError):
    code = "local_embedding_unavailable"
    purpose = "embedding"


class LocalRerankerUnavailableError(LocalModelUnavailableError):
    code = "local_reranker_unavailable"
    purpose = "reranker"


def is_structured_local_unavailable(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, LocalModelUnavailableError):
            return True
        code = getattr(current, "code", "")
        error_code = getattr(current, "error_code", None)
        stable_code = getattr(error_code, "code", error_code)
        if (
            isinstance(code, str) and code.startswith("local_") and code.endswith("_unavailable")
        ) or (
            isinstance(stable_code, str)
            and stable_code.startswith("local_")
            and stable_code.endswith("_unavailable")
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


async def run_worker_to_completion(function: Callable[..., Any], *args: Any) -> Any:
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    current_task = asyncio.current_task()
    cancellation: asyncio.CancelledError | None = None
    uncancelled = 0
    while True:
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
            if current_task is not None and current_task.cancelling():
                current_task.uncancel()
                uncancelled += 1
            continue
        except BaseException:
            if cancellation is None:
                raise
            break
        if cancellation is None:
            return result
        break
    if current_task is not None:
        for _ in range(uncancelled):
            current_task.cancel()
    raise cancellation


def reject_awaitable_factory_result(result: Any, factory_name: str) -> Any:
    if inspect.isawaitable(result):
        closer = getattr(result, "close", None)
        if closer is not None:
            closer()
        raise RuntimeValidationError(f"{factory_name} returned an awaitable")
    return result


__all__ = [
    "LocalEmbeddingUnavailableError",
    "LocalModelUnavailableError",
    "LocalRerankerUnavailableError",
    "is_structured_local_unavailable",
    "reject_awaitable_factory_result",
    "run_worker_to_completion",
]
