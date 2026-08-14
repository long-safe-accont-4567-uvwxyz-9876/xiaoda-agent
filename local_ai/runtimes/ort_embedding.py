"""标准 ORT Embedding 运行时（复用 memory.local_embed.LocalEmbeddingProvider）。

EmbeddingRuntime 是对 LocalEmbeddingProvider 的薄包装：onnxruntime session
加载、tokenizers 分词、CLS 池化、L2 归一化与小批拆分均由 provider 承担，
本类只做生命周期编排、start/stop/health/ready/dimensions/embed 协议对齐与
清单维度契约校验（与 VIPEmbeddingRuntime 复用 NpuEmbeddingProvider 同构）。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from local_ai.contracts import RuntimeProfile
from local_ai.runtimes.base import Runtime, RuntimeValidationError


class EmbeddingRuntime(Runtime):
    """按 RuntimeProfile 运行标准 ORT Embedding 模型。

    模型加载 / 分词 / 池化复用 memory.local_embed.LocalEmbeddingProvider，
    避免与 bundled 本地向量化路径各自维护一套 ONNX 推理逻辑。
    """

    def __init__(self, model_dir: str | Path, *, max_length: int = 512) -> None:
        self._model_dir = Path(model_dir)
        self._max_length = max_length
        self._provider: Any = None
        self._load_lock = threading.Lock()
        # 兼容测试注入路径与外部读取的推理状态镜像
        self._session: Any = None
        self._tokenizer: Any = None
        self._sessions: list[tuple[dict[str, Any], Any]] = []
        self._active_session_index = 0
        self._dimensions = 0
        self._loaded = False

    # ── 生命周期 ──────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._loaded

    def health(self) -> bool:
        return self._loaded

    @property
    def dimensions(self) -> int:
        if self._provider is not None:
            return int(self._provider.dimensions or 0)
        return self._dimensions

    @property
    def active_binding(self) -> dict[str, Any] | None:
        if self._provider is not None:
            return self._provider.active_binding
        return None

    def start(
        self,
        profile: RuntimeProfile,
        *,
        session: Any = None,
        tokenizer: Any = None,
    ) -> bool:
        """按 RuntimeProfile 加载模型（幂等，带锁）。

        测试可注入 session / tokenizer，跳过真实 onnxruntime 依赖。
        """
        if self._loaded:
            return True
        with self._load_lock:
            if self._loaded:
                return True
            if session is not None:
                self._install_injected(session, tokenizer)
                return True
            return self._load_from_profile(profile)

    def stop(self) -> None:
        provider = self._provider
        self._provider = None
        self._session = None
        self._sessions = []
        self._active_session_index = 0
        self._tokenizer = None
        self._dimensions = 0
        self._loaded = False
        if provider is not None:
            try:
                provider.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响状态重置
                pass

    # ── 加载 ──────────────────────────────────────────────

    def _install_injected(self, session: Any, tokenizer: Any) -> None:
        """测试注入路径：绕过真实依赖把 fake session/tokenizer 装入 provider。"""
        from memory.local_embed import LocalEmbeddingProvider

        provider = LocalEmbeddingProvider(self._model_dir, max_length=self._max_length)
        provider._sessions = [({"device_id": "injected", "provider": "injected"}, session)]
        provider._active_session_index = 0
        provider._session = session
        provider._tokenizer = tokenizer
        provider._input_names = []  # fake session 仅需 input_ids，让 encode_batch 默认单输入
        provider._input_dtypes = {}
        provider._fixed_seq = 0
        provider._dimensions = self._infer_dimensions(session)
        provider._loaded = True
        self._adopt_provider(provider)

    def _load_from_profile(self, profile: RuntimeProfile) -> bool:
        from memory.local_embed import LocalEmbeddingProvider

        provider = LocalEmbeddingProvider(
            self._model_dir,
            max_length=self._max_length,
            bindings=self._bindings_from_profile(profile),
            disable_fallback=True,
        )
        if not provider.load():
            self._provider = None
            return False
        self._adopt_provider(provider)
        return True

    def _adopt_provider(self, provider: Any) -> None:
        self._provider = provider
        self._session = provider._session
        self._tokenizer = provider._tokenizer
        self._sessions = provider._sessions
        self._active_session_index = provider._active_session_index
        self._dimensions = provider._dimensions
        self._loaded = True

    @staticmethod
    def _bindings_from_profile(profile: RuntimeProfile) -> list[dict[str, Any]]:
        fallback_bindings = profile.options.get("fallback_bindings", ())
        primary_options = {
            key: value
            for key, value in profile.options.items()
            if key
            not in {
                "fallback_bindings",
                "fallback_providers",
                "provider_options",
                "providers",
            }
        }
        return [
            {
                "device_id": profile.device_id,
                "provider": profile.provider,
                "provider_options": dict(primary_options),
            },
            *(dict(binding) for binding in fallback_bindings),
        ]

    @staticmethod
    def _infer_dimensions(session: Any) -> int:
        try:
            out_shape = session.get_outputs()[0].shape
        except Exception:  # noqa: BLE001 - 假 session 或形状缺失
            return 0
        hidden = out_shape[-1] if out_shape else None
        hidden = getattr(hidden, "dim_value", hidden)
        try:
            return int(hidden) if hidden else 0
        except (TypeError, ValueError):
            return 0

    # ── 推理 ──────────────────────────────────────────────

    def embed(
        self,
        texts: list[str],
        *,
        expected_dimensions: int | None = None,
    ) -> list[list[float]]:
        """批量向量化，返回 list[list[float]]（保持输入顺序）。

        expected_dimensions 给定时，与实际输出维度不匹配即抛
        RuntimeValidationError（清单契约防护）。
        """
        if not texts:
            return []
        if not self._loaded or self._provider is None:
            raise RuntimeValidationError("embedding runtime not started")

        vectors = self._provider.encode_batch(list(texts))
        if expected_dimensions is not None:
            for vector in vectors:
                if len(vector) != expected_dimensions:
                    raise RuntimeValidationError(
                        f"embedding dimension {len(vector)} does not match manifest "
                        f"expected {expected_dimensions}"
                    )
        return vectors


__all__ = ["EmbeddingRuntime"]
