"""VIP NPU Embedding 运行时（包装 memory.npu_embed.NpuEmbeddingProvider）。

NPU 推理是同步阻塞的流式协议（BGEVEC01，VIPLite 常驻子进程），调用方
（LocalEmbeddingService）会经 run_worker_to_completion 放入工作线程执行，
因此本运行时对外仍是同步接口。本类只做生命周期编排与
start/stop/health/embed/ready/dimensions 协议对齐（与 EmbeddingRuntime 一致）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from local_ai.contracts import RuntimeProfile
from local_ai.runtimes.base import Runtime, RuntimeValidationError


class VIPEmbeddingRuntime(Runtime):
    """按 RuntimeProfile 驱动 VIP9000 NPU 运行 BGE embedding。

    模型目录（model_dir）提供 tokenizer.json；NBG 固化文件与 runner
    可执行文件缺省取 memory/npu_embed.py 的默认路径，也支持经
    profile.options（runner_path / nbg_path / query_prefix / timeout_s）
    覆盖，便于部署时指向自定义固化包。
    """

    def __init__(self, model_dir: str | Path) -> None:
        self._model_dir = Path(model_dir)
        self._provider: Any = None

    # ── 生命周期 ──────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return bool(self._provider is not None and self._provider.ready)

    @property
    def dimensions(self) -> int:
        if self._provider is None:
            return 0
        return int(self._provider.dimensions or 0)

    def start(self, profile: RuntimeProfile, **kwargs: Any) -> bool:
        if self.ready:
            return True
        from memory.npu_embed import NpuEmbeddingProvider

        options = dict(profile.options or {})
        provider = NpuEmbeddingProvider(
            self._model_dir,
            query_prefix=str(options.get("query_prefix", "")),
            max_length=int(options.get("max_length", 512)),
            runner_path=str(options.get("runner_path", "")),
            nbg_path=str(options.get("nbg_path", "")),
            timeout_s=float(options.get("timeout_s", 15.0)),
        )
        if not provider.load():
            self._provider = None
            return False
        self._provider = provider
        return True

    def stop(self) -> None:
        provider = self._provider
        self._provider = None
        if provider is not None:
            try:
                provider.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响状态重置
                pass

    def health(self) -> bool:
        return self.ready

    # ── 推理 ──────────────────────────────────────────────

    def embed(
        self,
        texts: list[str],
        *,
        expected_dimensions: int | None = None,
    ) -> list[list[float]]:
        """批量向量化，返回 list[list[float]]（保持输入顺序）。

        expected_dimensions 给定时，与实际输出维度不匹配即抛
        RuntimeValidationError（清单契约防护，与 EmbeddingRuntime 一致）。
        """
        if not texts:
            return []
        if not self.ready:
            raise RuntimeValidationError("vip embedding runtime not started")
        vectors = self._provider.encode_batch(list(texts))
        if expected_dimensions is not None:
            for vector in vectors:
                if len(vector) != expected_dimensions:
                    raise RuntimeValidationError(
                        f"embedding dimension {len(vector)} does not match manifest "
                        f"expected {expected_dimensions}"
                    )
        return vectors


__all__ = ["VIPEmbeddingRuntime"]
