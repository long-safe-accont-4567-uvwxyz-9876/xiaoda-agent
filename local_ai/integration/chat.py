from __future__ import annotations

import asyncio
import os
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
        max_concurrent: int | None = None,
    ) -> None:
        self._instance_manager = instance_manager
        self.source = source
        self._unavailable_error = unavailable_error
        # 权重仅加载一份，通过信号量限制并发推理路数：同一时刻最多 max_concurrent
        # 个请求排队执行，其余等待。避免多 session 内存翻倍 + 单 session 线程争抢。
        if max_concurrent is None:
            try:
                max_concurrent = int(os.getenv("LOCAL_CHAT_MAX_CONCURRENT", "3"))
            except ValueError:
                max_concurrent = 3
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))

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
        acquire_model = getattr(self._instance_manager, "acquire_runtime_for_model", None)
        if acquire is None:
            raise LocalModelUnavailableError("local chat instance manager is unavailable")
        # 功能节点可独立指定本地模型：options.model_id 非空且非占位时按模型定位实例，
        # 否则回退到全局按 purpose 选中的实例（向后兼容「全局共享」模式）。
        model_id = None
        if isinstance(options, Mapping):
            raw = options.get("model_id")
            if raw and str(raw) != "local-chat":
                model_id = str(raw)
        runtime_options = dict(options) if options else {}
        runtime_options.pop("model_id", None)
        # 并发信号量覆盖整个流式生命周期（含 yield），保证同一时刻最多 max_concurrent
        # 个推理共享唯一 Model，其余排队；单 session 权重不重复加载。
        async with self._semaphore:
            try:
                if model_id and acquire_model is not None:
                    acquired = await acquire_model(model_id, route)
                else:
                    acquired = await acquire(ModelPurpose.CHAT, route)
            except Exception as error:
                raise LocalModelUnavailableError(str(error)) from error
            if acquired is None:
                raise LocalModelUnavailableError("no local chat model is selected")
            instance_id, runtime = acquired
            try:
                cancel_token = _CancelToken()
                async for chunk in runtime.stream(messages, runtime_options, cancel_token):
                    yield chunk
            finally:
                self._instance_manager.release_runtime(instance_id, route)


__all__ = ["LocalChatService"]