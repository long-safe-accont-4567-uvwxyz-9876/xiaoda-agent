"""本地 Embedding Provider — onnxruntime CPU 推理 BGE-small-zh-v1.5。

香橙派本地向量化（先 CPU 后 NPU，2026-08-07 决策）：
- 模型：Xenova/bge-small-zh-v1.5 ONNX（fp32，512 维，CPU 阶段）
        onnx/model_int8.onnx（INT8，后续 NPU 阶段替换 provider 即可）
- 推理：onnxruntime CPUExecutionProvider；CPU 推理走 to_thread，不阻塞事件循环
- 分词：tokenizers.Tokenizer.from_file（加载 tokenizer.json，无需 transformers/torch）
- 池化：CLS 池化 + L2 归一化（BGE 官方要求，输出可直接做点积/余弦相似度）

与远程 API 对齐的约定：
- embed() 统一无指令前缀（与远程 bge-m3 行为一致，检索/写入共用同一入口）
- BGE 官方建议查询侧加"为这个句子生成表示以用于检索相关文章："，
  通过 LOCAL_EMBED_QUERY_PREFIX 配置项按需开启（默认关闭，先跑通全链路）。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from loguru import logger

# onnxruntime / tokenizers 为可选依赖：未安装时 Provider 降级不可用，
# vector_store 保持远程 API 路径，不强制本地推理。
try:
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    HAS_LOCAL_EMBED_DEPS = True
except ImportError:  # pragma: no cover
    np = None  # type: ignore
    ort = None  # type: ignore
    Tokenizer = None  # type: ignore
    HAS_LOCAL_EMBED_DEPS = False

# BGE 中文查询指令（官方推荐，Xenova 导出的 config.json 中同步标注）
BGE_ZH_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class LocalEmbeddingProvider:
    """基于 onnxruntime 的本地 BGE 小模型 Embedding。

    懒加载：首次 embed 时才加载模型（启动不拖慢）。
    线程安全：推理为 CPU 密集操作，调用方经 asyncio.to_thread 串行执行。
    """

    def __init__(self, model_dir: str | Path, *,
                 query_prefix: str = "", max_length: int = 512) -> None:
        self._model_dir = Path(model_dir)
        self._query_prefix = query_prefix
        self._max_length = max_length
        self._session: Any = None
        self._tokenizer: Any = None
        self._dimensions: int = 0
        self._load_lock = threading.Lock()
        self._loaded = False
        self._load_error: str = ""

    # ── 加载 ──────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """模型是否已加载可用。"""
        return self._loaded

    @property
    def dimensions(self) -> int:
        """输出向量维度（加载后为 512；未加载为 0）。"""
        return self._dimensions

    def load(self) -> bool:
        """加载 ONNX session 与 tokenizer（幂等，带锁）。

        模型目录约定（与下载脚本一致）：
        - model.onnx / onnx/model.onnx：优先
        - tokenizer.json：必需
        """
        if self._loaded:
            return True
        with self._load_lock:
            if self._loaded:
                return True
            try:
                if not HAS_LOCAL_EMBED_DEPS:
                    raise RuntimeError("onnxruntime/tokenizers not installed")
                onnx_path = self._model_dir / "model.onnx"
                if not onnx_path.exists():
                    onnx_path = self._model_dir / "onnx" / "model.onnx"
                if not onnx_path.exists():
                    raise FileNotFoundError(f"model.onnx not found in {self._model_dir}")
                tokenizer_path = self._model_dir / "tokenizer.json"
                if not tokenizer_path.exists():
                    raise FileNotFoundError(f"tokenizer.json not found in {self._model_dir}")

                # 单线程推理（板子 CPU 推理，多线程不加速反而抖动）
                self._session = ort.InferenceSession(
                    str(onnx_path),
                    providers=["CPUExecutionProvider"],
                    sess_options=ort.SessionOptions(),
                )
                self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
                # 维度：从模型输出形状检测（B, S, H）取 H
                out_shape = self._session.get_outputs()[0].shape
                hidden = getattr(out_shape[-1], "dim_value", None)
                self._dimensions = int(hidden) if hidden else 512
                if self._dimensions <= 0:
                    self._dimensions = 512
                self._loaded = True
                logger.info("local_embed.ready", model=str(onnx_path),
                            dims=self._dimensions,
                            providers=self._session.get_providers())
                return True
            except Exception as e:  # noqa: BLE001
                self._load_error = str(e)
                logger.warning("local_embed.load_failed error={}", str(e))
                return False

    # ── 推理 ──────────────────────────────────────────────

    def _apply_prefix(self, text: str) -> str:
        return f"{self._query_prefix}{text}" if self._query_prefix else text

    def _tokenize(self, texts: list[str]) -> tuple[Any, Any, Any]:
        """批量分词 + padding 到批内最大长度（截断 max_length）。"""
        encodings = self._tokenizer.encode_batch(
            [self._apply_prefix(t) for t in texts],
            add_special_tokens=True,
        )
        seq_lens = [len(e.ids) for e in encodings]
        max_len = min(max(seq_lens), self._max_length)
        input_ids, attention, types = [], [], []
        for enc in encodings:
            ids = enc.ids[:max_len]
            pad = max_len - len(ids)
            input_ids.append(ids + [0] * pad)
            attention.append([1] * len(ids) + [0] * pad)
            types.append(list(enc.type_ids[:max_len]) + [0] * pad)
        return (
            np.array(input_ids, dtype=np.int64),
            np.array(attention, dtype=np.int64),
            np.array(types, dtype=np.int64),
        )

    @staticmethod
    def _pool_cls(last_hidden: Any) -> Any:
        """CLS 池化（取 [CLS] 位置向量）并 L2 归一化。"""
        cls_vec = last_hidden[:, 0, :]
        norm = np.linalg.norm(cls_vec, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return cls_vec / norm

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（同步，CPU 密集，调用方应经 to_thread 执行）。"""
        if not texts:
            return []
        if not self._loaded and not self.load():
            return []
        try:
            input_ids, attention, types = self._tokenize(texts)
            outputs = self._session.run(
                None,
                {
                    "input_ids": input_ids,
                    "attention_mask": attention,
                    "token_type_ids": types,
                },
            )
            # 输出可能是 last_hidden_state（B,S,H）或已池化向量（B,H）
            out = outputs[0]
            if out.ndim == 3:
                vecs = self._pool_cls(out)
            else:
                norm = np.linalg.norm(out, axis=1, keepdims=True)
                norm[norm == 0] = 1.0
                vecs = out / norm
            return [v.tolist() for v in vecs]
        except Exception as e:  # noqa: BLE001
            logger.warning("local_embed.encode_failed error={}", str(e))
            return []

    def embed(self, text: str) -> list[float]:
        """单条文本向量化（返回空列表表示失败，调用方有兜底）。"""
        batch = self.encode_batch([text])
        return batch[0] if batch else []

    def close(self) -> None:
        """释放推理会话（进程退出时调用，非必须）。"""
        self._session = None
        self._tokenizer = None
        self._loaded = False
