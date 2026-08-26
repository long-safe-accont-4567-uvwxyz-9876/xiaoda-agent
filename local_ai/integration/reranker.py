from __future__ import annotations

from typing import Any

from loguru import logger

from local_ai.integration.errors import (
    LocalModelUnavailableError,
    LocalRerankerUnavailableError,
    is_structured_local_unavailable,
    run_worker_to_completion,
)


class LocalRerankerService:
    """本地重排服务：在本地模型（local_ai 实例）与远程 API 之间按 backend 选择。

    backend ∈ local / api / off（与前端按钮对齐，无 auto；历史值 auto 按 api）：
    - local: 强制本地（无本地实例时抛 LocalRerankerUnavailableError）
    - api:   强制远程 fallback（无 fallback 或不可用时抛错）
    - off:   禁用（任何调用抛 LocalRerankerUnavailableError）
    """

    def __init__(
        self,
        runtime: Any,
        *,
        unavailable_error: Exception | None = None,
        instance_manager: Any = None,
        fallback: Any = None,
        backend: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._unavailable_error = unavailable_error
        self._instance_manager = instance_manager
        self._fallback = fallback
        # 默认后端按构造形态推断：managed（有 instance_manager）默认 api；
        # 直连（仅传 runtime，无管理器/fallback）默认 local——直接用该 runtime。
        if backend is None:
            backend = "api" if instance_manager is not None else "local"
        backend = "api" if backend == "auto" else backend
        self._backend = backend if backend in ("local", "api", "off") else "api"

    def set_backend(self, backend: str, local_model: str | None = None) -> None:
        """热更新后端选择；local_model 由实例管理器选择契约消费。"""
        del local_model
        backend = "api" if backend == "auto" else backend
        if backend in ("local", "api", "off"):
            self._backend = backend
            logger.info("reranker.service_backend_set backend={}", backend)

    @classmethod
    def managed(cls, instance_manager: Any, fallback: Any) -> LocalRerankerService:
        # managed 形态默认 local：有实例管理器即优先用选中的本地实例；
        # bootstrap.py 会紧接 set_backend(get_backend(...)) 用配置值覆盖。
        return cls(None, instance_manager=instance_manager, fallback=fallback, backend="local")

    @property
    def available(self) -> bool:
        if self._backend == "off":
            return False
        if self._backend == "api":
            return bool(self._fallback and self._fallback.available)
        # local：有管理器看选中实例；直连形态看 runtime 健康
        if self._instance_manager is not None:
            return self._local_selection_available()
        health = getattr(self._runtime, "health", None)
        return bool(health is not None and health())

    def _local_selection_available(self) -> bool:
        if self._instance_manager is None:
            return False
        from local_ai.contracts import ModelPurpose

        identity = self._instance_manager.selection_identity(ModelPurpose.RERANKER)
        if identity is None:
            return False
        checker = getattr(self._instance_manager, "selection_available", None)
        return bool(checker is not None and checker(ModelPurpose.RERANKER))

    async def score(self, query: str, documents: list[str]) -> list[float]:
        if self._unavailable_error is not None:
            raise LocalRerankerUnavailableError(str(self._unavailable_error))
        if self._backend == "off":
            raise LocalRerankerUnavailableError("reranker disabled")
        if self._backend == "api":
            if self._fallback is None or not self._fallback.available:
                raise LocalRerankerUnavailableError("reranker api backend unavailable")
            return await self._score_via_fallback(query, documents)
        # local：强制本地实例，不可用即抛错（不静默回退云端）
        return await self._score_local(query, documents)

    async def _score_local(self, query: str, documents: list[str]) -> list[float]:
        """通过 local_ai 实例管理器获取本地 reranker 运行时打分。

        直连形态（构造时直接传 runtime、无实例管理器）直接用该 runtime。
        """
        if self._instance_manager is None:
            runtime = self._runtime
            if runtime is None:
                raise LocalRerankerUnavailableError("no local reranker runtime")
            health = getattr(runtime, "health", None)
            if health is not None and not health():
                raise LocalRerankerUnavailableError(
                    "selected local reranker model is unavailable"
                )
            scores = await run_worker_to_completion(runtime.score, query, documents)
            if len(scores) != len(documents):
                raise RuntimeError(
                    f"local reranker returned {len(scores)} scores for {len(documents)} documents"
                )
            return scores
        from local_ai.contracts import ModelPurpose

        identity = self._instance_manager.selection_identity(ModelPurpose.RERANKER)
        if identity is None:
            raise LocalRerankerUnavailableError("no local reranker model is selected")
        route = "memory:reranker"
        binding = None
        try:
            acquire = getattr(self._instance_manager, "acquire_runtime", None)
            if acquire is None:
                runtime = await self._instance_manager.resolve_runtime(ModelPurpose.RERANKER)
                if runtime is None:
                    raise LocalRerankerUnavailableError(
                        "selected local reranker model is unavailable"
                    )
            else:
                acquired = await acquire(ModelPurpose.RERANKER, route)
                if acquired is None:
                    raise LocalRerankerUnavailableError(
                        "selected local reranker model is unavailable"
                    )
                instance_id, runtime = acquired
                binding = (instance_id, route)
        except (OSError, RuntimeError, ConnectionError, ValueError) as error:
            logger.warning("local_reranker.acquire_failed error={}", str(error)[:200])
            if isinstance(error, LocalRerankerUnavailableError):
                raise
            raise LocalRerankerUnavailableError(str(error)) from error
        except Exception as error:
            logger.exception("local_reranker.acquire.unexpected_error")
            if isinstance(error, LocalRerankerUnavailableError):
                raise
            raise LocalRerankerUnavailableError(str(error)) from error
        try:
            scores = await run_worker_to_completion(runtime.score, query, documents)
            if len(scores) != len(documents):
                raise RuntimeError(
                    f"local reranker returned {len(scores)} scores for {len(documents)} documents"
                )
            return scores
        finally:
            if binding is not None:
                self._instance_manager.release_runtime(*binding)

    async def _score_via_fallback(self, query: str, documents: list[str]) -> list[float]:
        """通过 fallback 打分，兼容 score() 与 rerank() 两种接口。

        fallback 可能是本地 runtime 适配器（有同步 score(query, docs) ->
        list[float]，走 worker 线程执行），也可能是远程 API 客户端（如
        memory.reranker.Reranker，只有 async rerank(query, docs, top_n) ->
        按分数排序的 [{index, relevance_score}]）。对后者取回全量分数后按
        index 映射回输入顺序，保证返回值与 documents 同序。
        """
        import inspect

        score_method = getattr(self._fallback, "score", None)
        if score_method is not None:
            if inspect.iscoroutinefunction(score_method):
                scores = await score_method(query, documents)
            else:
                scores = await run_worker_to_completion(score_method, query, documents)
        else:
            rerank_method = getattr(self._fallback, "rerank", None)
            if rerank_method is None:
                raise LocalRerankerUnavailableError(
                    "fallback reranker exposes neither score() nor rerank()"
                )
            ranked = await rerank_method(query, documents, top_n=len(documents))
            scores = [0.0] * len(documents)
            for item in ranked:
                index = item.get("index")
                if index is None or not 0 <= index < len(documents):
                    raise LocalRerankerUnavailableError(
                        f"fallback reranker returned invalid index {index!r}"
                    )
                scores[index] = float(item.get("relevance_score", 0.0))
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


__all__ = [
    "LocalModelUnavailableError",
    "LocalRerankerService",
    "LocalRerankerUnavailableError",
    "is_structured_local_unavailable",
]
