from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Mapping, Self


class ModelPurpose(str, Enum):
    CHAT = "chat"
    EMBEDDING = "embedding"
    RERANKER = "reranker"


class RuntimeKind(str, Enum):
    ORT = "ort"
    ORT_GENAI = "ort_genai"
    VIP = "vip"


class TaskState(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    QUARANTINED = "quarantined"


class DeviceState(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_non_negative(name: str, value: int | float | None) -> None:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} must be a finite number")
    if value is not None and value < 0:
        raise ValueError(f"{name} must not be negative")


def _require_non_negative_int(name: str, value: int | None) -> None:
    if value is not None and type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    if value is not None and value < 0:
        raise ValueError(f"{name} must not be negative")


def _require_bool(name: str, value: bool) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")


def _require_optional_string(name: str, value: str | None) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string or None")


def _require_string_sequence(name: str, value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence of non-empty strings")
    normalized = tuple(value)
    for item in normalized:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} must contain non-empty strings")
    return normalized


def _require_utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} must be timezone-aware UTC")


def _require_path_string(name: str, value: str, *, absolute: bool = False) -> None:
    _require_text(name, value)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a path string")
    if absolute and not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        raise ValueError(f"{name} must be an absolute path string")


def _require_revision(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{7,64}", value) is None:
        raise ValueError("revision must be a 7-64 character hexadecimal commit hash")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"value is not strict JSON data: {type(value).__name__}")


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if hasattr(value, "to_dict"):
        return _json_value(value.to_dict())
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"value is not strict JSON data: {type(value).__name__}")


def _datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _mapping(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return data


def _freeze_mapping(name: str, value: Mapping[str, Any]) -> Mapping[str, Any]:
    return _freeze(_mapping(value, name))


class _Record:
    def to_dict(self) -> dict[str, Any]:
        return {field.name: _json_value(getattr(self, field.name)) for field in fields(self)}


@dataclass(frozen=True)
class ExecutionBackend(_Record):
    runtime: RuntimeKind
    provider: str
    healthy: bool
    options: Mapping[str, Any] = field(default_factory=dict)
    purposes: tuple[ModelPurpose, ...] = ()
    precisions: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text("provider", self.provider)
        _require_bool("healthy", self.healthy)
        object.__setattr__(self, "runtime", RuntimeKind(self.runtime))
        object.__setattr__(self, "options", _freeze_mapping("options", self.options))
        object.__setattr__(self, "purposes", tuple(ModelPurpose(value) for value in self.purposes))
        object.__setattr__(self, "precisions", _require_string_sequence("precisions", self.precisions))
        object.__setattr__(self, "evidence", _freeze_mapping("evidence", self.evidence))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        data = _mapping(data, "execution backend")
        return cls(
            runtime=RuntimeKind(data["runtime"]),
            provider=data["provider"],
            healthy=data["healthy"],
            options=data.get("options", {}),
            purposes=tuple(ModelPurpose(value) for value in data.get("purposes", ())),
            precisions=data.get("precisions", ()),
            evidence=data.get("evidence", {}),
        )


@dataclass(frozen=True)
class ComputeDevice(_Record):
    id: str
    name: str
    kind: str
    architecture: str
    state: DeviceState
    memory_total: int
    memory_available: int
    backends: tuple[ExecutionBackend, ...] = ()
    system: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("id", "name", "kind", "architecture"):
            _require_text(name, getattr(self, name))
        _require_non_negative_int("memory_total", self.memory_total)
        _require_non_negative_int("memory_available", self.memory_available)
        if self.memory_available > self.memory_total:
            raise ValueError("memory_available must not exceed memory_total")
        object.__setattr__(self, "state", DeviceState(self.state))
        normalized_backends = []
        for backend in self.backends:
            if isinstance(backend, ExecutionBackend):
                normalized_backends.append(backend)
            elif isinstance(backend, Mapping):
                normalized_backends.append(ExecutionBackend.from_dict(backend))
            else:
                raise ValueError("backends must contain ExecutionBackend records or mappings")
        object.__setattr__(self, "backends", tuple(normalized_backends))
        object.__setattr__(self, "system", _freeze_mapping("system", self.system))
        object.__setattr__(self, "evidence", _freeze_mapping("evidence", self.evidence))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        data = _mapping(data, "compute device")
        return cls(
            id=data["id"],
            name=data["name"],
            kind=data["kind"],
            architecture=data["architecture"],
            state=DeviceState(data["state"]),
            memory_total=data["memory_total"],
            memory_available=data["memory_available"],
            backends=tuple(ExecutionBackend.from_dict(item) for item in data.get("backends", ())),
            system=data.get("system", {}),
            evidence=data.get("evidence", {}),
        )


@dataclass(frozen=True)
class CatalogFile(_Record):
    path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        _require_path_string("path", self.path)
        if type(self.size) is not int or self.size <= 0:
            raise ValueError("size must be positive")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", self.sha256) is None:
            raise ValueError("sha256 must be a 64 character hexadecimal digest")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        data = _mapping(data, "catalog file")
        return cls(path=data["path"], size=data["size"], sha256=data["sha256"])


@dataclass(frozen=True)
class CatalogModel(_Record):
    id: str
    source: str
    repository: str
    revision: str
    purpose: ModelPurpose
    files: tuple[CatalogFile, ...]
    parameter_count: int | None = None
    quantization: str | None = None
    download_size: int = 0
    license: str | None = None
    compatibility: Mapping[str, Any] = field(default_factory=dict)
    runtime_requirements: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("id", "source", "repository", "revision"):
            _require_text(name, getattr(self, name))
        _require_revision(self.revision)
        if not self.files:
            raise ValueError("files must not be empty")
        normalized_files = []
        for manifest in self.files:
            if isinstance(manifest, CatalogFile):
                normalized_files.append(manifest)
            elif isinstance(manifest, Mapping):
                normalized_files.append(CatalogFile.from_dict(manifest))
            else:
                raise ValueError("files must contain CatalogFile records or mappings")
        _require_non_negative_int("parameter_count", self.parameter_count)
        _require_non_negative_int("download_size", self.download_size)
        _require_optional_string("quantization", self.quantization)
        _require_optional_string("license", self.license)
        object.__setattr__(self, "purpose", ModelPurpose(self.purpose))
        object.__setattr__(self, "files", tuple(normalized_files))
        object.__setattr__(self, "compatibility", _freeze_mapping("compatibility", self.compatibility))
        object.__setattr__(self, "runtime_requirements", _freeze_mapping("runtime_requirements", self.runtime_requirements))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        data = _mapping(data, "catalog model")
        return cls(
            **{
                **data,
                "purpose": ModelPurpose(data["purpose"]),
                "files": tuple(CatalogFile.from_dict(item) for item in data["files"]),
            }
        )


@dataclass(frozen=True)
class InstalledModel(_Record):
    id: str
    catalog_id: str
    revision: str
    purpose: ModelPurpose
    directory: str
    manifest_checksum: str
    validation_state: str
    ownership: str
    installed_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("id", "catalog_id", "revision", "manifest_checksum", "validation_state", "ownership"):
            _require_text(name, getattr(self, name))
        _require_revision(self.revision)
        _require_path_string("directory", self.directory, absolute=True)
        _require_utc("installed_at", self.installed_at)
        object.__setattr__(self, "purpose", ModelPurpose(self.purpose))
        object.__setattr__(self, "metadata", _freeze_mapping("metadata", self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        data = _mapping(data, "installed model")
        return cls(**{**data, "purpose": ModelPurpose(data["purpose"]), "installed_at": _datetime(data["installed_at"])})


@dataclass(frozen=True)
class DownloadTask(_Record):
    id: str
    model_id: str
    state: TaskState
    bytes_downloaded: int
    total_bytes: int
    destination: str
    created_at: datetime
    updated_at: datetime
    speed_bps: float | None = None
    eta_seconds: float | None = None
    resumable: bool = True
    error: str | None = None

    def __post_init__(self) -> None:
        _require_text("id", self.id)
        _require_text("model_id", self.model_id)
        _require_path_string("destination", self.destination)
        for name in ("bytes_downloaded", "total_bytes"):
            _require_non_negative_int(name, getattr(self, name))
        for name in ("speed_bps", "eta_seconds"):
            _require_non_negative(name, getattr(self, name))
        if self.bytes_downloaded > self.total_bytes:
            raise ValueError("bytes_downloaded must not exceed total_bytes")
        _require_utc("created_at", self.created_at)
        _require_utc("updated_at", self.updated_at)
        _require_bool("resumable", self.resumable)
        _require_optional_string("error", self.error)
        object.__setattr__(self, "state", TaskState(self.state))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        data = _mapping(data, "download task")
        return cls(**{**data, "state": TaskState(data["state"]), "created_at": _datetime(data["created_at"]), "updated_at": _datetime(data["updated_at"])})


@dataclass(frozen=True)
class RuntimeProfile(_Record):
    runtime: RuntimeKind
    device_id: str
    provider: str
    options: Mapping[str, Any] = field(default_factory=dict)
    estimated_ram: int = 0
    estimated_vram: int = 0
    allow_fallback: bool = True

    def __post_init__(self) -> None:
        _require_text("device_id", self.device_id)
        _require_text("provider", self.provider)
        _require_non_negative_int("estimated_ram", self.estimated_ram)
        _require_non_negative_int("estimated_vram", self.estimated_vram)
        _require_bool("allow_fallback", self.allow_fallback)
        object.__setattr__(self, "runtime", RuntimeKind(self.runtime))
        object.__setattr__(self, "options", _freeze_mapping("options", self.options))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        data = _mapping(data, "runtime profile")
        return cls(**{**data, "runtime": RuntimeKind(data["runtime"])})


@dataclass(frozen=True)
class ModelInstance(_Record):
    id: str
    model_id: str
    runtime: RuntimeKind
    device_id: str
    state: str
    health: str
    started_at: datetime
    updated_at: datetime
    active_routes: tuple[str, ...] = ()
    resource_usage: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("id", "model_id", "device_id", "state", "health"):
            _require_text(name, getattr(self, name))
        _require_utc("started_at", self.started_at)
        _require_utc("updated_at", self.updated_at)
        object.__setattr__(self, "runtime", RuntimeKind(self.runtime))
        object.__setattr__(self, "active_routes", _require_string_sequence("active_routes", self.active_routes))
        object.__setattr__(self, "resource_usage", _freeze_mapping("resource_usage", self.resource_usage))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        data = _mapping(data, "model instance")
        return cls(
            **{
                **data,
                "runtime": RuntimeKind(data["runtime"]),
                "started_at": _datetime(data["started_at"]),
                "updated_at": _datetime(data["updated_at"]),
                "active_routes": data.get("active_routes", ()),
            }
        )
