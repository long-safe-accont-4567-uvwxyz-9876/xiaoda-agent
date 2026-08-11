from __future__ import annotations

from typing import Any, AsyncIterator, Mapping, Sequence

from local_ai.contracts import ModelPurpose
from local_ai.integration.reranker import LocalModelUnavailableError


class _CancelToken:
    """ORT GenAI stream 的取消令牌：满足 is_cancelled / check 契约。"""

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def check(self) -> None:
        return None


class LocalChatService:
    """本地 ONNX Runtime GenAI chat 服务。

    显式选中的本地实例停止 / 未选中时抛 LocalModelUnavailableError，绝不静默
    回退到云端或 bundled 服务（用户硬约束：无静默 fallback）。
    """

    def __init__(
        self,
        instance_manager: Any,
        *,
        source: str = "instance",
        unavailable_error: Exception | None = None,
    ) -> None:
        self._instance_manager = instance_manager
        self.source = source
        self._unavailable_error = unavailable_error

    @classmethod
    def managed(cls, instance_manager: Any) -> LocalChatService:
        return cls(instance_manager, source="instance")

    @property
    def available(self) -> bool:
        if self._unavailable_error is not None:
            return False
        if self._instance_manager is None:
            return False
        available = getattr(self._instance_manager, "selection_available", None)
        if available is None:
            return False
        return bool(available(ModelPurpose.CHAT))

    async def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        options: Mapping[str, Any],
        route: str,
    ) -> AsyncIterator[str]:
        if self._unavailable_error is not None:
            raise LocalModelUnavailableError(str(self._unavailable_error))
        acquire = getattr(self._instance_manager, "acquire_runtime", None)
        if acquire is None:
            raise LocalModelUnavailableError("local chat instance manager is unavailable")
        try:
            acquired = await acquire(ModelPurpose.CHAT, route)
        except Exception as error:
            raise LocalModelUnavailableError(str(error)) from error
        if acquired is None:
            raise LocalModelUnavailableError("no local chat model is selected")
        instance_id, runtime = acquired
        try:
            cancel_token = _CancelToken()
            async for chunk in runtime.stream(messages, options, cancel_token):
                yield chunk
        finally:
            self._instance_manager.release_runtime(instance_id, route)


__all__ = ["LocalChatService"]