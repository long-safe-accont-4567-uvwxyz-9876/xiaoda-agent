"""HuggingFace 镜像（hf-mirror.com）仓库检视 — 官方 huggingface_hub CLI 库实现。

与 ModelScope 检视共用同一套布局识别规则（genai_config.json /
sentence-transformers 标记 / config.json 架构），保证两个获取源在前端
展示、检视解析与下载契约上完全统一：

- 只接受不可变 commit hash（与 ModelScope 同约束）；
- 文件清单带 sha256（LFS 文件直接用官方元数据，非 LFS 小文件下载内容计算），
  满足下载契约「每个文件必须有 SHA256」；
- 检视失败返回 error 状态（不向调用方泄漏原始异常）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from local_ai.catalog.modelscope import (
    CatalogInspection,
    InvalidRevisionError,
    RemoteFile,
    _validate_revision,
)
from local_ai.contracts import ModelPurpose

_EMBEDDING_ARCHITECTURES = frozenset(
    {"BertModel", "XLMRobertaModel", "RobertaModel", "NomicBertModel"}
)
_RERANKER_ARCH_KEYWORDS = ("ForSequenceClassification", "Ranker", "ReRanker")

# 单仓库检视总超时：hf-mirror 网络不稳定时避免请求无限挂起，
# 超时返回 error 状态（前端展示"检视失败"而非一直转圈）。
_INSPECT_TIMEOUT_SECONDS = 150.0


def _env() -> None:
    """镜像与禁用 Xet 通过环境变量指定（Xet 存储国内不可达）。"""
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")


class HuggingFaceRepository:
    """通过 hf-mirror.com 检视 HuggingFace 仓库（官方 huggingface_hub）。"""

    def __init__(self, endpoint: str = "https://hf-mirror.com") -> None:
        self._endpoint = endpoint

    async def inspect(
        self,
        repository: str,
        revision: str,
        token: str | None,
    ) -> CatalogInspection:
        _validate_revision(revision)
        loop = asyncio.get_running_loop()
        try:
            files, config = await asyncio.wait_for(
                loop.run_in_executor(None, self._collect, repository, revision, token),
                timeout=_INSPECT_TIMEOUT_SECONDS,
            )
        except (InvalidRevisionError, PermissionError):
            raise
        except asyncio.TimeoutError:
            return CatalogInspection(
                repository=repository,
                revision=revision,
                files=(),
                purpose=None,
                runnable=False,
                state="error",
                evidence={"error": "inspect timed out (hf-mirror 响应超时)"},
                missing=(),
            )
        except Exception as error:  # noqa: BLE001 — surface as error-state inspection
            return CatalogInspection(
                repository=repository,
                revision=revision,
                files=(),
                purpose=None,
                runnable=False,
                state="error",
                evidence={"error": str(error)},
                missing=(),
            )
        return self._recognize_layout(repository, revision, files, config)

    # ── 文件清单收集（同步，在线程池中执行） ──

    def _collect(
        self,
        repository: str,
        revision: str,
        token: str | None,
    ) -> tuple[list[RemoteFile], dict[str, Any] | None]:
        _env()
        from huggingface_hub import HfApi

        api = HfApi(endpoint=self._endpoint)
        info = api.model_info(
            repo_id=repository, revision=revision, files_metadata=True, token=token
        )
        files: list[RemoteFile] = []
        pending: list[str] = []
        for sibling in info.siblings:
            path = sibling.rfilename
            size = int(
                sibling.size
                or (sibling.lfs.size if sibling.lfs else None)
                or 0
            )
            if size <= 0:
                continue
            sha256 = sibling.lfs.sha256 if sibling.lfs else None
            if sha256 is None:
                pending.append(path)
            files.append(RemoteFile(path=path, size=size, sha256=sha256))
        # 非 LFS 文件（config.json / tokenizer 等）官方元数据没有内容 sha256：
        # 并行下载内容计算，与下载契约「每个文件必须有 SHA256」保持一致；
        # config.json 的内容在计算时顺带读取（避免重复下载）。
        resolved, config = self._resolve_small_files(
            repository, revision, files, pending, token
        )
        return resolved, config

    def _resolve_small_files(
        self,
        repository: str,
        revision: str,
        files: list[RemoteFile],
        pending: list[str],
        token: str | None,
    ) -> tuple[list[RemoteFile], dict[str, Any] | None]:
        # 非 LFS 文件用直连下载（带超时）计算内容 sha256：
        # 绕开 huggingface_hub 的本地缓存与文件锁——hf-mirror 一旦变慢，
        # 缓存锁会让 hf_hub_download 无限等待，检视请求永不返回。
        import requests

        def work(path: str) -> tuple[str, str | None, bytes | None]:
            try:
                url = f"{self._endpoint}/{repository}/resolve/{revision}/{path}"
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                digest = hashlib.sha256()
                chunks: list[bytes] = []
                with requests.get(
                    url, timeout=30, headers=headers, stream=True
                ) as resp:
                    resp.raise_for_status()
                    for chunk in resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        digest.update(chunk)
                        if path == "config.json":
                            chunks.append(chunk)
                content = b"".join(chunks) if path == "config.json" else None
                return path, digest.hexdigest(), content
            except Exception:  # noqa: BLE001 — 单个文件失败不阻断整体检视
                return path, None, None

        sha_map: dict[str, str] = {}
        config: dict[str, Any] | None = None
        with ThreadPoolExecutor(max_workers=4) as pool:
            for path, digest, content in pool.map(work, pending):
                if digest:
                    sha_map[path] = digest
                if path == "config.json" and content:
                    try:
                        parsed = json.loads(content.decode("utf-8"))
                        if isinstance(parsed, dict):
                            config = parsed
                    except Exception:  # noqa: BLE001
                        continue
        resolved: list[RemoteFile] = []
        for item in files:
            if item.sha256 is None:
                digest = sha_map.get(item.path)
                if digest is None:
                    continue  # 无法取得 sha256 的文件从清单剔除
                resolved.append(RemoteFile(path=item.path, size=item.size, sha256=digest))
            else:
                resolved.append(item)
        return resolved, config

    # ── 布局识别（与 ModelScopeRepository 同一套规则） ──

    def _recognize_layout(
        self,
        repository: str,
        revision: str,
        files: list[RemoteFile],
        config: dict[str, Any] | None,
    ) -> CatalogInspection:
        paths = {item.path for item in files}
        has_onnx = any(
            path.endswith(".onnx")
            or path.endswith(".onnx_data")
            or path.endswith(".onnx_weights")
            for path in paths
        )

        # ORT GenAI chat 布局
        if "genai_config.json" in paths and has_onnx:
            return CatalogInspection(
                repository=repository,
                revision=revision,
                files=tuple(files),
                purpose=ModelPurpose.CHAT,
                runnable=True,
                state="ready",
                evidence={"layout": "ort_genai_chat", "configs": ["genai_config.json"]},
                missing=(),
            )

        # Sentence-transformers embedding 标记
        embedding_markers = [
            marker for marker in ("sentence_bert_config.json", "modules.json")
            if marker in paths
        ]
        if embedding_markers and has_onnx:
            return CatalogInspection(
                repository=repository,
                revision=revision,
                files=tuple(files),
                purpose=ModelPurpose.EMBEDDING,
                runnable=True,
                state="ready",
                evidence={"layout": "embedding", "markers": embedding_markers},
                missing=(),
            )

        # config.json 架构区分 reranker / embedding
        if config is not None:
            architectures = config.get("architectures", [])
            arch_list = (
                list(architectures)
                if isinstance(architectures, list)
                else [str(architectures)]
            )
            arch_str = " ".join(arch_list)
            if any(keyword in arch_str for keyword in _RERANKER_ARCH_KEYWORDS):
                return CatalogInspection(
                    repository=repository,
                    revision=revision,
                    files=tuple(files),
                    purpose=ModelPurpose.RERANKER,
                    runnable=True,
                    state="ready",
                    evidence={
                        "layout": "reranker",
                        "configs": ["config.json"],
                        "architectures": arch_list,
                    },
                    missing=(),
                )
            if any(
                arch in _EMBEDDING_ARCHITECTURES for arch in arch_list
            ) or "Embedding" in arch_str:
                return CatalogInspection(
                    repository=repository,
                    revision=revision,
                    files=tuple(files),
                    purpose=ModelPurpose.EMBEDDING,
                    runnable=True,
                    state="ready",
                    evidence={
                        "layout": "embedding",
                        "configs": ["config.json"],
                        "architectures": arch_list,
                    },
                    missing=(),
                )

        # 无法识别的布局
        missing: list[str] = []
        if not has_onnx:
            missing.append("onnx model file (.onnx / .onnx_data / .onnx_weights)")
        if (
            "genai_config.json" not in paths
            and "config.json" not in paths
            and not embedding_markers
        ):
            missing.append(
                "recognized config file "
                "(genai_config.json / config.json / sentence_bert_config.json)"
            )
        return CatalogInspection(
            repository=repository,
            revision=revision,
            files=tuple(files),
            purpose=None,
            runnable=False,
            state="requires_configuration",
            evidence={"layout": "unknown", "files": sorted(paths)},
            missing=tuple(missing),
        )


__all__ = ["HuggingFaceRepository"]
