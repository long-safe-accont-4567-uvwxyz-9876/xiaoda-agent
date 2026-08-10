from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from local_ai.contracts import CatalogModel


class CatalogSchemaError(ValueError):
    pass


_ROOT_FIELDS = frozenset({"schema_version", "remote_catalog_url", "models"})
_MODEL_FIELDS = frozenset(
    {
        "id",
        "source",
        "repository",
        "revision",
        "purpose",
        "files",
        "parameter_count",
        "quantization",
        "download_size",
        "license",
        "compatibility",
        "runtime_requirements",
    }
)
_REQUIRED_MODEL_FIELDS = frozenset(
    {
        "id",
        "source",
        "repository",
        "revision",
        "purpose",
        "files",
        "download_size",
        "license",
        "compatibility",
        "runtime_requirements",
    }
)
_FILE_FIELDS = frozenset({"path", "size", "sha256"})
_COMPATIBILITY_FIELDS = frozenset(
    {
        "architectures",
        "platforms",
        "providers",
        "runtimes",
        "purposes",
        "precisions",
    }
)
_RUNTIME_REQUIREMENT_FIELDS = frozenset(
    {"minimum_ram", "recommended_ram", "minimum_vram", "recommended_vram"}
)


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogSchemaError(f"{path} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise CatalogSchemaError(f"{path} keys must be strings")
    return value


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CatalogSchemaError(f"{path} must be an array")
    return value


def _fields(value: Mapping[str, Any], allowed: frozenset[str], required: frozenset[str], path: str) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise CatalogSchemaError(f"{path} has unexpected field: {unexpected[0]}")
    missing = sorted(required - set(value))
    if missing:
        raise CatalogSchemaError(f"{path} is missing field: {missing[0]}")


def _string_array(value: Any, path: str) -> None:
    items = _sequence(value, path)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise CatalogSchemaError(f"{path} must contain non-empty strings")


def _parse_model(value: Any, index: int) -> CatalogModel:
    path = f"models[{index}]"
    model = _mapping(value, path)
    _fields(model, _MODEL_FIELDS, _REQUIRED_MODEL_FIELDS, path)
    files = _sequence(model["files"], f"{path}.files")
    for file_index, file_value in enumerate(files):
        file_path = f"{path}.files[{file_index}]"
        file_data = _mapping(file_value, file_path)
        _fields(file_data, _FILE_FIELDS, _FILE_FIELDS, file_path)
    compatibility = _mapping(model["compatibility"], f"{path}.compatibility")
    _fields(compatibility, _COMPATIBILITY_FIELDS, frozenset(), f"{path}.compatibility")
    for name, item in compatibility.items():
        _string_array(item, f"{path}.compatibility.{name}")
    requirements = _mapping(model["runtime_requirements"], f"{path}.runtime_requirements")
    _fields(requirements, _RUNTIME_REQUIREMENT_FIELDS, frozenset(), f"{path}.runtime_requirements")
    for name, item in requirements.items():
        if type(item) is not int or item < 0:
            raise CatalogSchemaError(f"{path}.runtime_requirements.{name} must be a non-negative integer")
    try:
        return CatalogModel.from_dict(model)
    except (KeyError, TypeError, ValueError) as error:
        raise CatalogSchemaError(f"{path}: {error}") from error


def parse_catalog(value: Any) -> tuple[CatalogModel, ...]:
    root = _mapping(value, "catalog")
    _fields(root, _ROOT_FIELDS, _ROOT_FIELDS, "catalog")
    if root["schema_version"] != 1 or type(root["schema_version"]) is not int:
        raise CatalogSchemaError("schema_version must be 1")
    remote_url = root["remote_catalog_url"]
    if remote_url is not None:
        if not isinstance(remote_url, str):
            raise CatalogSchemaError("remote_catalog_url must be an HTTPS URL or null")
        parsed = urlparse(remote_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise CatalogSchemaError("remote_catalog_url must be an HTTPS URL or null")
    models = _sequence(root["models"], "models")
    return tuple(_parse_model(model, index) for index, model in enumerate(models))
