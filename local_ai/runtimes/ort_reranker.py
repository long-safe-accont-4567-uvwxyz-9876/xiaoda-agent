"""标准 ORT Reranker 运行时（cross-encoder 打分）。

复用 memory/local_embed.py 的分词与会话降级思路：query 与每个 document 成对
编码（cross-encoder），会话输出 logits 作为相关性分数。分数顺序严格对应
文档输入顺序，绝不重排、绝不静默换模型。小批拆分避免大批次长时间占会话。
"""
from __future__ import annotations

from typing import Any

from local_ai.runtimes.base import RuntimeValidationError, _OrtRuntimeBase, np

# 单批最大条数：与 embedding 一致，onnxruntime 单会话串行，拆小批缩短排队窗口。
_MAX_RERANK_BATCH = 8


class RerankerRuntime(_OrtRuntimeBase):
    """按 RuntimeProfile 运行标准 ORT Reranker（cross-encoder）模型。"""

    def score(self, query: str, documents: list[str]) -> list[float]:
        """对 (query, document) 成对打分，返回与文档同序的分数列表。

        输出顺序严格对应 documents 输入顺序（无重排）；未 start 抛
        RuntimeValidationError。
        """
        if not documents:
            return []
        if not self._loaded:
            raise RuntimeValidationError("reranker runtime not started")

        scores: list[float] = []
        for start in range(0, len(documents), _MAX_RERANK_BATCH):
            chunk = documents[start : start + _MAX_RERANK_BATCH]
            encodings = self._tokenizer.encode_batch(
                [(query, doc) for doc in chunk],
                add_special_tokens=True,
            )
            feeds = self._encode(encodings)
            output = self._run_active(feeds)
            scores.extend(self._flatten_logits(output, len(chunk)))
        return scores

    @staticmethod
    def _flatten_logits(output: Any, expected: int) -> list[float]:
        """将会话 logits 展平为每文档一个分数（B,1) 或 (B,) 均支持）。"""
        array = np.asarray(output)
        if array.ndim == 1:
            pass
        elif array.ndim == 2 and array.shape[1] == 1:
            array = array[:, 0]
        else:
            raise RuntimeValidationError(
                f"reranker output shape must be (B,) or (B, 1), got {array.shape}"
            )
        if int(array.shape[0]) != expected:
            raise RuntimeValidationError(
                f"reranker produced {array.shape[0]} scores for {expected} documents"
            )
        # float32 logits 精度约 7 位有效数字，round 到 6 位得到干净可序列化的分数
        return [round(float(value), 6) for value in array.tolist()]
