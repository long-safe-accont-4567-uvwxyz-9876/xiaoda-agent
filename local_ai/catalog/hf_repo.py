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

from loguru import logger

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
        except (OSError, RuntimeError, ValueError, ConnectionError) as error:
            logger.warning("hf_repo.inspect_failed repo={} error={}", repository, str(error)[:200])
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
            except (OSError, RuntimeError, ValueError) as e:
                logger.debug("hf_repo.file_inspect_failed path={} error={}", path, str(e))
                return path, None, None
            except Exception:
                logger.exception("hf_repo._inspect_file.work_unexpected path={}", path)
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
                    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
                        logger.debug("hf_repo.config_parse_failed error={}", str(e))
                        continue
                    except Exception:
                        logger.exception("hf_repo._resolve_small_files.config_unexpected")
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

    @staticmethod
    def _has_onnx_files(paths: set[str]) -> bool:
        return any(
            p.endswith(".onnx") or p.endswith(".onnx_data") or p.endswith(".onnx_weights")
            for p in paths
        )

    @staticmethod
    def _embedding_markers(paths: set[str]) -> list[str]:
        return [m for m in ("sentence_bert_config.json", "modules.json") if m in paths]

    @staticmethod
    def _classify_by_architectures(
        config: dict[str, Any],
    ) -> ModelPurpose | None:
        architectures = config.get("architectures", [])
        arch_list = list(architectures) if isinstance(architectures, list) else [str(architectures)]
        arch_str = " ".join(arch_list)
        if any(kw in arch_str for kw in _RERANKER_ARCH_KEYWORDS):
            return ModelPurpose.RERANKER
        if any(arch in _EMBEDDING_ARCHITECTURES for arch in arch_list) or "Embedding" in arch_str:
            return ModelPurpose.EMBEDDING
        return None

    @staticmethod
    def _unknown_layout_missing(paths: set[str], has_onnx: bool, embedding_markers: list[str]) -> tuple[str, ...]:
        missing: list[str] = []
        if not has_onnx:
            missing.append("onnx model file (.onnx / .onnx_data / .onnx_weights)")
        if "genai_config.json" not in paths and "config.json" not in paths and not embedding_markers:
            missing.append("recognized config file (genai_config.json / config.json / sentence_bert_config.json)")
        return tuple(missing)

    def _recognize_layout(
        self,
        repository: str,
        revision: str,
        files: list[RemoteFile],
        config: dict[str, Any] | None,
    ) -> CatalogInspection:
        paths = {item.path for item in files}
        has_onnx = self._has_onnx_files(paths)

        if "genai_config.json" in paths and has_onnx:
            return CatalogInspection(
                repository=repository, revision=revision, files=tuple(files),
                purpose=ModelPurpose.CHAT, runnable=True, state="ready",
                evidence={"layout": "ort_genai_chat", "configs": ["genai_config.json"]},
                missing=(),
            )

        markers = self._embedding_markers(paths)
        if markers and has_onnx:
            return CatalogInspection(
                repository=repository, revision=revision, files=tuple(files),
                purpose=ModelPurpose.EMBEDDING, runnable=True, state="ready",
                evidence={"layout": "embedding", "markers": markers},
                missing=(),
            )

        if config is not None:
            purpose = self._classify_by_architectures(config)
            if purpose is not None:
                arch_list = list(config.get("architectures", []))
                layout = "reranker" if purpose == ModelPurpose.RERANKER else "embedding"
                return CatalogInspection(
                    repository=repository, revision=revision, files=tuple(files),
                    purpose=purpose, runnable=True, state="ready",
                    evidence={"layout": layout, "configs": ["config.json"], "architectures": arch_list},
                    missing=(),
                )

        return CatalogInspection(
            repository=repository, revision=revision, files=tuple(files),
            purpose=None, runnable=False, state="requires_configuration",
            evidence={"layout": "unknown", "files": sorted(paths)},
            missing=self._unknown_layout_missing(paths, has_onnx, markers),
        )


__all__ = ["HuggingFaceRepository"]