"""标准 ORT Embedding / Reranker 运行时单元测试（fake session 注入）。

覆盖：
1. 维度不匹配抛 RuntimeValidationError（无静默 fallback）
2. reranker 保持文档输入顺序
3. embedding 小批拆分（_MAX_EMBED_BATCH=8）
4. CLS 池化 + L2 归一化
5. start/stop 生命周期与 health 就绪查询
6. provider options 通过 RuntimeProfile 传递到 binding

测试通过注入 fake session（get_outputs/get_providers/run）与 fake tokenizer
构造 runtime，脱离真实 onnxruntime / tokenizers 依赖。
"""
from __future__ import annotations

import numpy as np
import pytest

from local_ai.contracts import RuntimeKind, RuntimeProfile
from local_ai.runtimes import EmbeddingRuntime, RerankerRuntime
from local_ai.runtimes.base import Runtime, RuntimeValidationError


class _FakeOutput:
    """模拟 onnxruntime NodeArg：仅提供 shape。"""

    def __init__(self, shape: tuple) -> None:
        self.shape = shape


class _FakeEncoding:
    def __init__(self, ids: list[int]) -> None:
        self.ids = ids
        self.type_ids = [0] * len(ids)


class _FakeTokenizer:
    """记录每次编码的批大小，模拟 tokenizers.Tokenizer.encode_batch。"""

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def encode_batch(self, texts, add_special_tokens=True):  # noqa: ARG002
        self.batch_sizes.append(len(texts))
        return [_FakeEncoding([101, 200 + i, 102]) for i, _ in enumerate(texts)]


class _FakeEmbeddingSession:
    """按输入批大小产出可配置的 last_hidden_state（B,S,H）或已池化向量（B,H）。"""

    def __init__(self, hidden: int = 384) -> None:
        self._hidden = hidden
        self.output = None  # 可覆盖：直接返回该数组（用于维度不匹配测试）
        self.run_calls: list[dict] = []
        self.providers = ["CPUExecutionProvider"]

    def get_outputs(self):
        return [_FakeOutput((None, None, self._hidden))]

    def get_providers(self):
        return list(self.providers)

    def run(self, output_names, feeds):  # noqa: ARG002
        self.run_calls.append(feeds)
        if self.output is not None:
            return [self.output]
        batch = feeds["input_ids"].shape[0]
        seq = feeds["input_ids"].shape[1]
        # 构造非归一化的 3D 输出，CLS 位置为固定非零向量
        arr = np.ones((batch, seq, self._hidden), dtype=np.float32) * 2.0
        return [arr]


class _FakeRerankerSession:
    """按预设分数序列返回 logits（B,1），顺序对应输入文档顺序。"""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores
        self.output = None
        self.run_calls: list[dict] = []
        self.providers = ["CPUExecutionProvider"]

    def get_outputs(self):
        return [_FakeOutput((None, 1))]

    def get_providers(self):
        return list(self.providers)

    def run(self, output_names, feeds):  # noqa: ARG002
        self.run_calls.append(feeds)
        if self.output is not None:
            return [self.output]
        batch = feeds["input_ids"].shape[0]
        chunk = self._scores[: batch]
        self._scores = self._scores[batch:]
        # float64 避免 fake 夹具引入 float32 精度误差，保持计划样例的精确相等断言
        return [np.array(chunk, dtype=np.float64).reshape(batch, 1)]


def _profile(**options) -> RuntimeProfile:
    return RuntimeProfile(
        runtime=RuntimeKind.ORT,
        device_id="cpu:0",
        provider="CPUExecutionProvider",
        options=options,
    )


# ── Embedding ─────────────────────────────────────────────


@pytest.fixture
def embedding_runtime():
    runtime = EmbeddingRuntime("/tmp/does-not-exist")
    session = _FakeEmbeddingSession(hidden=384)
    runtime.start(_profile(), session=session, tokenizer=_FakeTokenizer())
    runtime.session = session
    return runtime


def test_embedding_rejects_manifest_dimension_mismatch(embedding_runtime):
    embedding_runtime.session.output = np.zeros((1, 384), dtype=np.float32)
    with pytest.raises(RuntimeValidationError):
        embedding_runtime.embed(["test"], expected_dimensions=512)


def test_embedding_cls_pooling_l2_normalized(embedding_runtime):
    vectors = embedding_runtime.embed(["hello", "world"])
    assert len(vectors) == 2
    for vec in vectors:
        assert len(vec) == 384
        norm = float(np.linalg.norm(np.array(vec)))
        assert abs(norm - 1.0) < 1e-5


def test_embedding_accepts_matching_dimensions(embedding_runtime):
    vectors = embedding_runtime.embed(["a"], expected_dimensions=384)
    assert len(vectors) == 1 and len(vectors[0]) == 384


def test_embedding_batches_in_chunks_of_eight(embedding_runtime):
    texts = [f"t{i}" for i in range(20)]
    embedding_runtime.embed(texts)
    # 20 条应拆为 8 + 8 + 4 三批
    assert embedding_runtime._tokenizer.batch_sizes == [8, 8, 4]


def test_embedding_empty_input_returns_empty(embedding_runtime):
    assert embedding_runtime.embed([]) == []


def test_embedding_requires_start():
    runtime = EmbeddingRuntime("/tmp/x")
    with pytest.raises(RuntimeValidationError):
        runtime.embed(["x"])


# ── Reranker ──────────────────────────────────────────────


@pytest.fixture
def reranker_runtime():
    runtime = RerankerRuntime("/tmp/does-not-exist")
    session = _FakeRerankerSession([0.8, 0.2])
    runtime.start(_profile(), session=session, tokenizer=_FakeTokenizer())
    runtime.session = session
    return runtime


def test_reranker_preserves_document_order(reranker_runtime):
    assert reranker_runtime.score("q", ["a", "b"]) == [0.8, 0.2]


def test_reranker_accepts_rank_one_output(reranker_runtime):
    reranker_runtime.session.output = np.array([0.8, 0.2], dtype=np.float64)
    assert reranker_runtime.score("q", ["a", "b"]) == [0.8, 0.2]


@pytest.mark.parametrize(
    "output",
    [
        np.array(0.8, dtype=np.float64),
        np.array([[[0.8]], [[0.2]]], dtype=np.float64),
    ],
)
def test_reranker_rejects_invalid_output_rank(reranker_runtime, output):
    reranker_runtime.session.output = output
    with pytest.raises(RuntimeValidationError):
        reranker_runtime.score("q", ["a", "b"])


def test_reranker_rejects_invalid_output_trailing_dimension(reranker_runtime):
    reranker_runtime.session.output = np.array(
        [[0.8, 0.1], [0.2, 0.9]], dtype=np.float64
    )
    with pytest.raises(RuntimeValidationError):
        reranker_runtime.score("q", ["a", "b"])


def test_reranker_rejects_output_batch_mismatch(reranker_runtime):
    reranker_runtime.session.output = np.array([[0.8]], dtype=np.float64)
    with pytest.raises(RuntimeValidationError):
        reranker_runtime.score("q", ["a", "b"])


def test_reranker_empty_documents_returns_empty(reranker_runtime):
    assert reranker_runtime.score("q", []) == []


def test_reranker_scores_all_documents():
    runtime = RerankerRuntime("/tmp/x")
    session = _FakeRerankerSession([0.1, 0.9, 0.5, 0.3, 0.7])
    runtime.start(_profile(), session=session, tokenizer=_FakeTokenizer())
    scores = runtime.score("query", ["d0", "d1", "d2", "d3", "d4"])
    assert scores == [0.1, 0.9, 0.5, 0.3, 0.7]


# ── 生命周期 / 协议 ───────────────────────────────────────


def test_runtimes_are_runtime_subclasses():
    assert issubclass(EmbeddingRuntime, Runtime)
    assert issubclass(RerankerRuntime, Runtime)


def test_start_stop_health_lifecycle():
    runtime = EmbeddingRuntime("/tmp/x")
    assert runtime.health() is False
    started = runtime.start(_profile(), session=_FakeEmbeddingSession(), tokenizer=_FakeTokenizer())
    assert started is True
    assert runtime.health() is True
    runtime.stop()
    assert runtime.health() is False


def test_profile_provider_options_passed_to_binding():
    runtime = EmbeddingRuntime("/tmp/x")
    profile = _profile(arena_extend_strategy="kSameAsRequested")
    runtime.start(profile, session=_FakeEmbeddingSession(), tokenizer=_FakeTokenizer())
    # 注入路径下 binding 为 injected；此处验证真实路径的 binding 构造逻辑
    bindings = runtime._bindings_from_profile(profile)
    assert bindings[0]["provider"] == "CPUExecutionProvider"
    assert bindings[0]["provider_options"]["arena_extend_strategy"] == "kSameAsRequested"
