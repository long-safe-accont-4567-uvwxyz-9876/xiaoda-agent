from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class ProviderProtocol(str, Enum):
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    CUSTOM_MAPPING = "custom_mapping"
    LOCAL_ORT = "local_ort"


@dataclass(frozen=True)
class ProviderCapabilities:
    tools: bool = False
    vision: bool = False
    streaming: bool = True
    model_discovery: bool = False
    json_mode: bool = False


@dataclass(frozen=True)
class AuthDefinition:
    environment_aliases: tuple[str, ...] = ()
    header: str = "Authorization"
    scheme: str = "Bearer"
    required: bool = True


@dataclass(frozen=True)
class EndpointDefinition:
    base_url: str = ""
    chat_path: str = "/chat/completions"
    models_path: str = "/models"


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    protocol: ProviderProtocol
    endpoint: EndpointDefinition
    auth: AuthDefinition
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    builtin: bool = False
    default_model: str = ""
    default_pro_model: str = ""
    max_tokens_cap: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
