"""ModelScope repository adapter — SSRF-safe file listing and layout inspection.

Lists files in a remote ModelScope repository via the public HTTP API and
recognizes well-known ONNX model layouts (ORT GenAI chat, embedding, reranker)
from the actual config files found in the repository.  Purpose is never guessed
from the repository name.

Key constraints:
- Immutable revision only: "main"/"master"/"latest" and non-hex strings are
  rejected with ``InvalidRevisionError``.
- SSRF-safe: requests whose host resolves to a private, loopback, or link-local
  IP are blocked before any network call.
- Mockable transport: ``httpx.AsyncClient(transport=...)`` is used so tests can
  inject a ``httpx.MockTransport`` without real network calls.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

import httpx

from loguru import logger

from local_ai.contracts import ModelPurpose


class InvalidRevisionError(ValueError):
    """Raised when a revision is mutable (e.g. "main") or not a hex commit hash."""


# ── Constants ──

_MUTABLE_REVISIONS = frozenset({"main", "master", "latest"})
_HEX_REVISION_RE = re.compile(r"[0-9a-fA-F]{7,64}")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_PAGE_SIZE = 200
_MAX_PAGES = 1000
# 仓库目录树递归深度上限（防止恶意/异常仓库制造无限深目录拖垮请求）。
_MAX_DEPTH = 6

# Private / loopback / link-local / reserved IP ranges (IPv4 + IPv6).
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),        # current network
    ipaddress.ip_network("10.0.0.0/8"),       # private A
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("169.254.0.0/16"),   # link-local (incl. cloud metadata)
    ipaddress.ip_network("172.16.0.0/12"),    # private B
    ipaddress.ip_network("192.0.0.0/24"),     # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),     # TEST-NET-1
    ipaddress.ip_network("192.88.99.0/24"),   # 6to4 relay anycast
    ipaddress.ip_network("192.168.0.0/16"),   # private C
    ipaddress.ip_network("198.18.0.0/15"),    # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),   # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),      # multicast
    ipaddress.ip_network("240.0.0.0/4"),      # reserved
    # IPv6
    ipaddress.ip_network("::1/128"),          # loopback
    ipaddress.ip_network("::/128"),           # unspecified
    ipaddress.ip_network("::ffff:0:0/96"),    # IPv4-mapped
    ipaddress.ip_network("64:ff9b::/96"),     # NAT64
    ipaddress.ip_network("fc00::/7"),         # unique-local
    ipaddress.ip_network("fe80::/10"),        # link-local
    ipaddress.ip_network("ff00::/8"),         # multicast
)

# Architecture class names that indicate an embedding model (bare encoder
# without a task head).
_EMBEDDING_ARCHITECTURES = frozenset(
    {"BertModel", "XLMRobertaModel", "RobertaModel", "NomicBertModel"}
)

# Substrings that indicate a reranker (cross-encoder) architecture.
_RERANKER_ARCH_KEYWORDS = ("ForSequenceClassification", "Ranker", "ReRanker")

_VALID_STATES = frozenset({"ready", "requires_configuration", "error"})


# ── Validation helpers ──

def _validate_revision(revision: str) -> None:
    if not isinstance(revision, str) or not revision:
        raise InvalidRevisionError("revision must be a non-empty string")
    if revision.lower() in _MUTABLE_REVISIONS:
        raise InvalidRevisionError(
            f"revision {revision!r} is mutable; use an immutable commit hash"
        )
    if _HEX_REVISION_RE.fullmatch(revision) is None:
        raise InvalidRevisionError(
            f"revision {revision!r} must be a hexadecimal commit hash"
        )


def _is_ip_blocked(addr: ipaddress._BaseAddress) -> bool:
    for network in _BLOCKED_NETWORKS:
        if addr in network:
            return True
    return False


def _check_ssrf(url: str, *, skip_dns: bool = False) -> None:
    """Block requests whose host is (or resolves to) a private/loopback/link-local IP.

    IP literals are checked directly.  Hostnames are resolved via
    ``socket.getaddrinfo`` when ``skip_dns`` is False (production mode); if
    DNS resolution fails the request is rejected (fail-closed).  When
    ``skip_dns`` is True (mock-transport test mode), DNS resolution is skipped
    so offline tests are not broken.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("SSRF check failed: URL has no hostname")

    # IP literal check
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        addr = None
    if addr is not None:
        if _is_ip_blocked(addr):
            raise ValueError(
                f"SSRF blocked: {hostname!r} is a private/loopback/link-local IP"
            )
        return

    if skip_dns:
        return

    # DNS resolution (fail-closed in production: unresolved host = blocked)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as error:
        raise ValueError(
            f"SSRF blocked: cannot resolve {hostname!r}: {error}"
        ) from error
    for info in infos:
        ip = info[4][0].split("%", 1)[0]
        try:
            resolved = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if _is_ip_blocked(resolved):
            raise ValueError(
                f"SSRF blocked: {hostname!r} resolves to "
                f"private/loopback/link-local IP {ip}"
            )


def _freeze_value(value: Any) -> Any:
    """Recursively freeze a JSON-safe value into immutable containers."""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("evidence keys must be strings")
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value
    raise ValueError(f"evidence value is not JSON-safe: {type(value).__name__}")


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("evidence must be a mapping")
    return _freeze_value(value)


# ── Dataclasses ──

@dataclass(frozen=True)
class RemoteFile:
    """A single file in a remote ModelScope repository."""

    path: str
    size: int
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("path must be a non-empty string")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("size must be a non-negative integer")
        if self.sha256 is not None:
            if not isinstance(self.sha256, str) or _SHA256_RE.fullmatch(self.sha256) is None:
                raise ValueError(
                    "sha256 must be a 64-character hexadecimal digest or None"
                )


@dataclass(frozen=True)
class CatalogInspection:
    """Result of inspecting a remote repository for a recognized model layout."""

    repository: str
    revision: str
    files: tuple[RemoteFile, ...]
    purpose: ModelPurpose | None
    runnable: bool
    state: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    missing: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise ValueError("repository must be a non-empty string")
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise ValueError("revision must be a non-empty string")
        if self.state not in _VALID_STATES:
            raise ValueError(
                f"state must be one of {sorted(_VALID_STATES)}, got {self.state!r}"
            )
        if type(self.runnable) is not bool:
            raise ValueError("runnable must be a boolean")
        normalized_purpose = (
            ModelPurpose(self.purpose) if self.purpose is not None else None
        )
        normalized_files = tuple(self.files)
        for item in normalized_files:
            if not isinstance(item, RemoteFile):
                raise ValueError("files must contain RemoteFile records")
        object.__setattr__(self, "purpose", normalized_purpose)
        object.__setattr__(self, "files", normalized_files)
        object.__setattr__(self, "evidence", _freeze_mapping(self.evidence))
        object.__setattr__(self, "missing", tuple(self.missing))


# ── Adapter ──

class ModelScopeRepository:
    """Inspect remote ModelScope repositories via the public HTTP API."""

    def __init__(
        self,
        base_url: str = "https://www.modelscope.cn/api/v1/",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url
        self._transport = transport
        self._timeout = timeout
        self._skip_dns_ssrf = transport is not None

    # ── Public API ──

    async def list_files(
        self,
        repository: str,
        revision: str,
        token: str | None,
    ) -> list[RemoteFile]:
        _validate_revision(revision)
        # 顶层与子目录（Type == "tree"）全部递归列出：常见 ONNX 仓库把模型文件
        # 放在 onnx/ 子目录，只列顶层会漏掉 model.onnx，导致布局识别误判。
        return await self._walk_repository(
            repository, revision, token, root="", depth=0, visited=set()
        )

    async def _walk_repository(
        self,
        repository: str,
        revision: str,
        token: str | None,
        *,
        root: str,
        depth: int,
        visited: set[str],
    ) -> list[RemoteFile]:
        headers = self._auth_headers(token)
        files: list[RemoteFile] = []
        subdirs: list[str] = []
        page_number = 1
        while page_number <= _MAX_PAGES:
            url = f"{self._base_url}models/{repository}/repo/files"
            params = {
                "Revision": revision,
                "Root": root,
                "PageNumber": str(page_number),
                "PageSize": str(_PAGE_SIZE),
            }
            payload = await self._get_json(url, params=params, headers=headers)
            data = payload.get("Data") or {}
            page_entries = data.get("Files") or []
            for entry in page_entries:
                if entry.get("Type") == "tree" and depth < _MAX_DEPTH:
                    path = entry.get("Path") or entry.get("Name") or ""
                    if isinstance(path, str) and path and path not in visited:
                        subdirs.append(path)
                else:
                    files.append(self._parse_file_entry(entry))
            total_pages = data.get("TotalPages")
            if total_pages is not None:
                if page_number >= int(total_pages):
                    break
            elif len(page_entries) < _PAGE_SIZE:
                break
            page_number += 1
        for subdir in subdirs:
            visited.add(subdir)
            files.extend(
                await self._walk_repository(
                    repository,
                    revision,
                    token,
                    root=subdir,
                    depth=depth + 1,
                    visited=visited,
                )
            )
        return files

    async def inspect(
        self,
        repository: str,
        revision: str,
        token: str | None,
    ) -> CatalogInspection:
        _validate_revision(revision)
        try:
            files = await self.list_files(repository, revision, token)
        except (InvalidRevisionError, PermissionError):
            raise
        except (OSError, RuntimeError, ValueError, ConnectionError) as error:
            logger.warning("modelscope.inspect_failed repo={} error={}", repository, str(error)[:200])
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
        return await self._recognize_layout(repository, revision, files, token)

    # ── Internal helpers ──

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
    def _classify_by_architectures(config: dict[str, Any]) -> ModelPurpose | None:
        architectures = config.get("architectures", [])
        arch_list = list(architectures) if isinstance(architectures, list) else [str(architectures)]
        arch_str = " ".join(arch_list)
        if any(kw in arch_str for kw in _RERANKER_ARCH_KEYWORDS):
            return ModelPurpose.RERANKER
        if any(arch in _EMBEDDING_ARCHITECTURES for arch in arch_list) or "Embedding" in arch_str:
            return ModelPurpose.EMBEDDING
        return None

    @staticmethod
    def _unknown_layout_missing(paths: set[str], has_onnx: bool, markers: list[str]) -> tuple[str, ...]:
        missing: list[str] = []
        if not has_onnx:
            missing.append("onnx model file (.onnx / .onnx_data / .onnx_weights)")
        if "genai_config.json" not in paths and "config.json" not in paths and not markers:
            missing.append("recognized config file (genai_config.json / config.json / sentence_bert_config.json)")
        return tuple(missing)

    async def _recognize_layout(
        self,
        repository: str,
        revision: str,
        files: list[RemoteFile],
        token: str | None,
    ) -> CatalogInspection:
        paths = {f.path for f in files}
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

        if "config.json" in paths:
            config = await self._fetch_json_config(repository, revision, "config.json", token)
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

    async def _fetch_json_config(
        self,
        repository: str,
        revision: str,
        file_path: str,
        token: str | None,
    ) -> dict[str, Any] | None:
        headers = self._auth_headers(token)
        url = f"{self._base_url}models/{repository}/repo"
        params = {"Revision": revision, "FilePath": file_path}
        try:
            content = await self._get_raw(url, params=params, headers=headers)
        except httpx.HTTPError:
            return None
        try:
            parsed = json.loads(content.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> dict[str, Any]:
        response = await self._send("GET", url, params=params, headers=headers)
        return response.json()

    async def _get_raw(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> bytes:
        response = await self._send("GET", url, params=params, headers=headers)
        return response.content

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> httpx.Response:
        _check_ssrf(url, skip_dns=self._skip_dns_ssrf)
        async with httpx.AsyncClient(
            transport=self._transport, timeout=self._timeout
        ) as client:
            response = await client.request(
                method, url, params=dict(params), headers=dict(headers)
            )
            if response.status_code == 401:
                raise PermissionError(
                    f"authentication required for {url} (HTTP 401)"
                )
            response.raise_for_status()
            return response

    @staticmethod
    def _auth_headers(token: str | None) -> dict[str, str]:
        if token is None:
            return {}
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _parse_file_entry(entry: Mapping[str, Any]) -> RemoteFile:
        path = entry.get("Path") or entry.get("path") or ""
        size = entry.get("Size") or entry.get("size") or 0
        sha256 = entry.get("Sha256") or entry.get("sha256")
        if sha256 == "":
            sha256 = None
        return RemoteFile(path=path, size=size, sha256=sha256)