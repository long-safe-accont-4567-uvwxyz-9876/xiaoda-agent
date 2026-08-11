"""标准 ORT Embedding 运行时（CLS 池化 + L2 归一化）。

复用 memory/local_embed.py 的行为：tokenizers 分词、CLS 池化、L2 归一化、
小批拆分（单会话串行，避免大批次长时间占用会话）。维度与清单契约不符时
抛 RuntimeValidationError，绝不静默降维或换模型。
"""
from __future__ import annotations

from typing import Any

from local_ai.runtimes.base import RuntimeValidationError, _OrtRuntimeBase, np

# 单批最大条数：onnxruntime 单会话串行推理，大批次会阻塞检索路径 embed。
_MAX_EMBED_BATCH = 8


class EmbeddingRuntime(_OrtRuntimeBase):
    """按 RuntimeProfile 运行标准 ORT Embedding 模型。"""

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
        if not self._loaded:
            raise RuntimeValidationError("embedding runtime not started")

        vectors: list[list[float]] = []
        for start in range(0, len(texts), _MAX_EMBED_BATCH):
            chunk = texts[start : start + _MAX_EMBED_BATCH]
            encodings = self._tokenizer.encode_batch(chunk, add_special_tokens=True)
            feeds = self._encode(encodings)
            output = self._run_active(feeds)
            pooled = self._pool(output)
            self._validate_dimensions(pooled, expected_dimensions)
            vectors.extend(vec.tolist() for vec in pooled)
        return vectors

    @staticmethod
    def _pool(output: Any) -> Any:
        """CLS 池化（3D 输出取 [CLS]）或直接归一化（2D 已池化）。"""
        if output.ndim == 3:
            vecs = output[:, 0, :]
        else:
            vecs = output
        norm = np.linalg.norm(vecs, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return vecs / norm

    @staticmethod
    def _validate_dimensions(pooled: Any, expected_dimensions: int | None) -> None:
        if expected_dimensions is None:
            return
        actual = int(pooled.shape[-1])
        if actual != expected_dimensions:
            raise RuntimeValidationError(
                f"embedding dimension {actual} does not match manifest "
                f"expected {expected_dimensions}"
            )
