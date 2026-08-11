from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from llm_gateway.contracts import (
    AuthDefinition,
    EndpointDefinition,
    ProviderCapabilities,
    ProviderDefinition,
    ProviderProtocol,
)

_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_SCHEMA_VERSION = 1
_KNOWN_FIELDS = {
    "api_key_env",
    "auth",
    "base_url_default",
    "builtin",
    "capabilities",
    "default_model",
    "default_pro_model",
    "endpoint",
    "environment_aliases",
    "max_tokens_cap",
    "protocol",
    "supports_json_mode",
    "supports_model_discovery",
    "supports_streaming",
    "supports_tools",
    "supports_vision",
}


class ProviderCatalog:
    def __init__(self, definitions: tuple[ProviderDefinition, ...] = ()) -> None:
        self._definitions: dict[str, ProviderDefinition] = {}
        self.source_path: Path | None = None
        self.load_errors: tuple[tuple[Path, Exception], ...] = ()
        for definition in definitions:
            self.register(definition)

    @classmethod
    def from_path(cls, path: str | Path) -> ProviderCatalog:
        source_path = Path(path)
        data = json.loads(
            source_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        if not isinstance(data, Mapping):
            raise ValueError("provider metadata must be an object")
        if "schema_version" not in data:
            raise ValueError("schema_version is required")
        if data["schema_version"] != _SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {data['schema_version']}")
        providers = data.get("providers", {})
        if not isinstance(providers, Mapping):
            raise ValueError("providers metadata must be an object")
        catalog = cls()
        for provider_id, raw_definition in providers.items():
            if not isinstance(raw_definition, Mapping):
                raise ValueError(f"provider definition must be an object: {provider_id}")
            catalog.register(_definition_from_metadata(provider_id, raw_definition))
        catalog.source_path = source_path
        return catalog

    @classmethod
    def from_paths(cls, *paths: str | Path) -> ProviderCatalog:
        errors: list[tuple[Path, Exception]] = []
        for path in paths:
            source_path = Path(path)
            try:
                catalog = cls.from_path(source_path)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
                errors.append((source_path, error))
                continue
            catalog.load_errors = tuple(errors)
            return catalog
        if not errors:
            raise ValueError("at least one provider metadata path is required")
        raise ValueError("all provider metadata sources failed") from errors[-1][1]

    def get(self, provider_id: str) -> ProviderDefinition:
        normalized = _normalize_id(provider_id)
        try:
            return self._definitions[normalized]
        except KeyError as error:
            raise KeyError(f"unknown provider: {normalized}") from error

    def list(self) -> tuple[ProviderDefinition, ...]:
        return tuple(self._definitions.values())

    def resolve_environment_alias(
        self,
        provider_id: str,
        environment: Mapping[str, str],
    ) -> tuple[str, str] | None:
        for alias in self.get(provider_id).auth.environment_aliases:
            value = environment.get(alias, "").strip()
            if value:
                return alias, value
        return None

    def register(
        self,
        definition: ProviderDefinition,
        *,
        replace_existing: bool = False,
    ) -> None:
        self.validate(definition)
        existing = self._definitions.get(definition.id)
        if existing is not None:
            if not replace_existing:
                raise ValueError(f"provider already registered: {definition.id}")
            if existing.builtin:
                raise ValueError(f"builtin provider cannot be replaced: {definition.id}")
        self._definitions[definition.id] = definition

    def unregister(self, provider_id: str) -> ProviderDefinition:
        normalized = _normalize_id(provider_id)
        definition = self.get(normalized)
        if definition.builtin:
            raise ValueError(f"builtin provider cannot be removed: {normalized}")
        del self._definitions[normalized]
        return definition

    def validate(self, definition: ProviderDefinition) -> None:
        if not isinstance(definition, ProviderDefinition):
            raise TypeError("definition must be a ProviderDefinition")
        if not _PROVIDER_ID_PATTERN.fullmatch(definition.id):
            raise ValueError(f"invalid provider id: {definition.id}")
        if not isinstance(definition.protocol, ProviderProtocol):
            raise ValueError("invalid provider protocol")
        aliases = definition.auth.environment_aliases
        if len(aliases) != len(set(aliases)):
            raise ValueError(f"duplicate environment alias: {definition.id}")
        if any(not alias or alias != alias.upper() for alias in aliases):
            raise ValueError(f"invalid environment alias: {definition.id}")
        if definition.max_tokens_cap is not None and definition.max_tokens_cap <= 0:
            raise ValueError(f"invalid max_tokens_cap: {definition.id}")


def _normalize_id(provider_id: str) -> str:
    if not isinstance(provider_id, str):
        raise TypeError("provider id must be a string")
    return provider_id.strip().lower()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate metadata key: {key}")
        result[key] = value
    return result


def _definition_from_metadata(
    provider_id: str,
    raw: Mapping[str, Any],
) -> ProviderDefinition:
    auth_raw = raw.get("auth", {})
    endpoint_raw = raw.get("endpoint", {})
    capabilities_raw = raw.get("capabilities", {})
    if not isinstance(auth_raw, Mapping):
        raise ValueError(f"provider auth must be an object: {provider_id}")
    if not isinstance(endpoint_raw, Mapping):
        raise ValueError(f"provider endpoint must be an object: {provider_id}")
    if not isinstance(capabilities_raw, Mapping):
        raise ValueError(f"provider capabilities must be an object: {provider_id}")
    aliases = auth_raw.get("environment_aliases", raw.get("environment_aliases", ()))
    if not isinstance(aliases, list):
        raise ValueError(f"environment_aliases must be a list: {provider_id}")
    protocol = ProviderProtocol(raw.get("protocol", ProviderProtocol.OPENAI_COMPATIBLE.value))
    return ProviderDefinition(
        id=_normalize_id(provider_id),
        protocol=protocol,
        endpoint=EndpointDefinition(
            base_url=str(endpoint_raw.get("base_url", raw.get("base_url_default", "")) or ""),
            chat_path=str(endpoint_raw.get("chat_path", "/chat/completions") or ""),
            models_path=str(endpoint_raw.get("models_path", "/models") or ""),
        ),
        auth=AuthDefinition(
            environment_aliases=tuple(str(alias) for alias in aliases),
            header=str(auth_raw.get("header", "Authorization") or ""),
            scheme=str(auth_raw.get("scheme", "Bearer") or ""),
            required=bool(auth_raw.get("required", True)),
        ),
        capabilities=ProviderCapabilities(
            tools=bool(capabilities_raw.get("tools", raw.get("supports_tools", False))),
            vision=bool(capabilities_raw.get("vision", raw.get("supports_vision", False))),
            streaming=bool(capabilities_raw.get("streaming", raw.get("supports_streaming", True))),
            model_discovery=bool(
                capabilities_raw.get("model_discovery", raw.get("supports_model_discovery", False))
            ),
            json_mode=bool(capabilities_raw.get("json_mode", raw.get("supports_json_mode", False))),
        ),
        builtin=bool(raw.get("builtin", False)),
        default_model=str(raw.get("default_model", "") or ""),
        default_pro_model=str(raw.get("default_pro_model", "") or ""),
        max_tokens_cap=raw.get("max_tokens_cap"),
        metadata={key: value for key, value in raw.items() if key not in _KNOWN_FIELDS},
    )
