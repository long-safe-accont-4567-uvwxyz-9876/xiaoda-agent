"""标准 ORT 运行时公共基类与错误类型。

Embedding / Reranker 均使用标准 ONNX Runtime（非 GenAI）：按 RuntimeProfile
建独立 Session、失败按清单顺序降级、绝不静默跨 provider 回退或自动换模型。
本模块只承载共享的 session 构建 / 分词 / 加载骨架，池化与打分由子类实现。
"""
from __future__ import annotations

import platform as platform_module
import threading
from pathlib import Path
from typing import Any

from loguru import logger

from local_ai.contracts import RuntimeProfile

# onnxruntime / tokenizers 为可选依赖：未安装时运行时降级不可用，
# 调用方保持既有远程路径（参照 memory/local_embed.py 的降级约定）。
try:
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    HAS_ORT_RUNTIME_DEPS = True
except ImportError:  # pragma: no cover - 依赖缺失路径
    np = None  # type: ignore
    ort = None  # type: ignore
    Tokenizer = None  # type: ignore
    HAS_ORT_RUNTIME_DEPS = False


class RuntimeValidationError(RuntimeError):
    """运行时输出与清单契约不符（如维度不匹配）时抛出。"""


class RuntimeDependencyError(RuntimeError):
    def __init__(
        self,
        dependency: str,
        runtime: str,
        detail: str,
        *,
        platform: str | None = None,
    ) -> None:
        self.code = "runtime_dependency_missing"
        self.dependency = dependency
        self.runtime = runtime
        self.detail = detail
        self.platform = platform or platform_module.platform()
        super().__init__(f"{dependency} is required for {runtime}: {detail}")


class Runtime:
    """本地模型运行时统一协议：start(profile) / stop() / health()。

    Embedding、Reranker（及后续 Chat）运行时均遵循同一生命周期契约，
    便于 InstanceManager 统一编排。
    """

    def start(self, profile: RuntimeProfile, **kwargs: Any) -> bool:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def health(self) -> bool:
        raise NotImplementedError


class _OrtRuntimeBase(Runtime):
    """标准 ORT 运行时骨架：负责 session 构建、清单降级、分词。

    子类实现对应的公开推理方法（embed / score）。
    ``start`` 允许注入假 session / tokenizer 以便无需真实依赖即可测试。
    """

    def __init__(self, model_dir: str | Path, *, max_length: int = 512) -> None:
        self._model_dir = Path(model_dir)
        self._max_length = max_length
        self._sessions: list[tuple[dict[str, Any], Any]] = []
        self._active_session_index = 0
        self._session: Any = None
        self._tokenizer: Any = None
        self._dimensions: int = 0
        self._loaded = False
        self._load_lock = threading.Lock()

    # ── 生命周期 ──────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._loaded

    def health(self) -> bool:
        """就绪查询：session 已加载即视为健康。"""
        return self._loaded

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def active_binding(self) -> dict[str, Any] | None:
        if not self._loaded or not self._sessions:
            return None
        return dict(self._sessions[self._active_session_index][0])

    def start(
        self,
        profile: RuntimeProfile,
        *,
        session: Any = None,
        tokenizer: Any = None,
    ) -> bool:
        """按 RuntimeProfile 建 session 与 tokenizer（幂等，带锁）。

        测试可直接注入 session / tokenizer，跳过真实 onnxruntime 依赖。
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
        self._session = None
        self._sessions = []
        self._active_session_index = 0
        self._tokenizer = None
        self._dimensions = 0
        self._loaded = False

    # ── 注入路径（测试）────────────────────────────────────

    def _install_injected(self, session: Any, tokenizer: Any) -> None:
        self._sessions = [({"device_id": "injected", "provider": "injected"}, session)]
        self._active_session_index = 0
        self._session = session
        self._tokenizer = tokenizer
        self._dimensions = self._infer_dimensions(session)
        self._loaded = True

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

    # ── 真实加载路径 ──────────────────────────────────────

    def _bindings_from_profile(self, profile: RuntimeProfile) -> list[dict[str, Any]]:
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

    def _session_options(self, providers: list[str]) -> Any:
        session_options = ort.SessionOptions()
        if "DmlExecutionProvider" in providers:
            session_options.enable_mem_pattern = False
            session_options.execution_mode = ort.ORT_SEQUENTIAL
        if providers != ["CPUExecutionProvider"]:
            session_options.add_session_config_entry(
                "session.disable_cpu_ep_fallback", "1"
            )
        return session_options

    def _create_session(
        self,
        onnx_path: Path,
        provider: str,
        provider_options: dict[str, Any],
    ) -> Any:
        return ort.InferenceSession(
            str(onnx_path),
            sess_options=self._session_options([provider]),
            providers=[provider],
            provider_options=[dict(provider_options)],
        )

    def _resolve_onnx_path(self) -> Path:
        onnx_path = self._model_dir / "model.onnx"
        if not onnx_path.exists():
            onnx_path = self._model_dir / "onnx" / "model.onnx"
        if not onnx_path.exists():
            raise FileNotFoundError(f"model.onnx not found in {self._model_dir}")
        return onnx_path

    def _load_from_profile(self, profile: RuntimeProfile) -> bool:
        try:
            if not HAS_ORT_RUNTIME_DEPS:
                raise RuntimeError("onnxruntime/tokenizers not installed")
            onnx_path = self._resolve_onnx_path()
            tokenizer_path = self._model_dir / "tokenizer.json"
            if not tokenizer_path.exists():
                raise FileNotFoundError(f"tokenizer.json not found in {self._model_dir}")

            sessions: list[tuple[dict[str, Any], Any]] = []
            for binding in self._bindings_from_profile(profile):
                provider = binding["provider"]
                try:
                    session = self._create_session(
                        onnx_path, provider, binding.get("provider_options", {})
                    )
                    active = list(session.get_providers())
                    if active != [provider]:
                        raise RuntimeError(
                            f"active providers {active} do not equal {[provider]}"
                        )
                    sessions.append((binding, session))
                except Exception as error:  # noqa: BLE001
                    logger.warning(
                        "ort_runtime.binding_load_failed device_id={} provider={} error={}",
                        binding.get("device_id"),
                        provider,
                        str(error),
                    )
            if not sessions:
                raise RuntimeError("no manifest binding could create a session")

            self._sessions = sessions
            self._active_session_index = 0
            self._session = sessions[0][1]
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
            self._dimensions = self._infer_dimensions(self._session)
            self._loaded = True
            logger.info(
                "ort_runtime.ready model={} dims={} providers={}",
                str(onnx_path),
                self._dimensions,
                self._session.get_providers(),
            )
            return True
        except Exception as error:  # noqa: BLE001
            self.stop()
            logger.warning("ort_runtime.load_failed error={}", str(error))
            return False

    # ── 分词 ──────────────────────────────────────────────

    def _encode(self, encodings: Any) -> dict[str, Any]:
        """将 tokenizer 编码批 padding 到批内最大长度，产出 ORT feeds。"""
        seq_lens = [len(enc.ids) for enc in encodings]
        max_len = min(max(seq_lens), self._max_length) if seq_lens else 0
        input_ids, attention, types = [], [], []
        for enc in encodings:
            ids = list(enc.ids[:max_len])
            pad = max_len - len(ids)
            input_ids.append(ids + [0] * pad)
            attention.append([1] * len(ids) + [0] * pad)
            types.append(list(enc.type_ids[:max_len]) + [0] * pad)
        return {
            "input_ids": np.array(input_ids, dtype=np.int64),
            "attention_mask": np.array(attention, dtype=np.int64),
            "token_type_ids": np.array(types, dtype=np.int64),
        }

    def _run_active(self, feeds: dict[str, Any]) -> Any:
        """按清单顺序运行 session，失败降级到下一个 binding。"""
        last_error: Exception | None = None
        for index in range(self._active_session_index, len(self._sessions)):
            binding, session = self._sessions[index]
            try:
                outputs = session.run(None, feeds)
                self._active_session_index = index
                self._session = session
                return outputs[0]
            except Exception as error:  # noqa: BLE001
                last_error = error
                logger.warning(
                    "ort_runtime.binding_run_failed device_id={} provider={} error={}",
                    binding.get("device_id"),
                    binding.get("provider"),
                    str(error),
                )
        raise RuntimeError("all manifest binding sessions failed") from last_error
