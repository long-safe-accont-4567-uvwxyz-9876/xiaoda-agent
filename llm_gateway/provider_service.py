from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any

from llm_gateway.contracts import (
    AuthDefinition,
    EndpointDefinition,
    ProviderCapabilities,
    ProviderDefinition,
    ProviderProtocol,
)
from llm_gateway.provider_catalog import ProviderCatalog
from llm_gateway.transports import (
    AnthropicTransport,
    CapabilityReport,
    CustomMappingTransport,
    OllamaTransport,
    OpenAICompatibleTransport,
    ProviderTransport,
)
from security.ssrf_guard import is_local_host, validate_url


class ProviderConnectionError(RuntimeError):
    pass


class ProviderInUseError(RuntimeError):
    pass


class ProviderCredentialStore:
    def read(self, provider_id: str) -> str:
        from web._provider_keys import load_provider_key

        return load_provider_key(provider_id)

    def write(self, provider_id: str, value: str) -> None:
        from web._provider_keys import _encode_key, _key_file

        path = _key_file(provider_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_encode_key(value) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def delete(self, provider_id: str) -> None:
        from web._provider_keys import _key_file

        _key_file(provider_id).unlink(missing_ok=True)


class ProviderService:
    def __init__(
        self,
        config_service: Any,
        catalog: ProviderCatalog,
        runtime_router: Any,
        *,
        credential_store: Any | None = None,
        transport_factory: Callable[[ProviderDefinition, str], ProviderTransport] | None = None,
        runtime_client_factory: Callable[[ProviderDefinition, str], Any] | None = None,
    ) -> None:
        self.config = config_service
        self.catalog = ProviderCatalog(catalog.list())
        self.runtime_router = runtime_router
        self.credentials = credential_store or ProviderCredentialStore()
        self._transport_factory = transport_factory or self._build_transport
        self._runtime_client_factory = runtime_client_factory or self._build_runtime_client
        self._reports: dict[str, CapabilityReport] = {}
        self._restore_custom_definitions()

    async def test(self, draft: Mapping[str, Any], credentials: Mapping[str, Any] | None = None) -> CapabilityReport:
        definition = self._definition(draft)
        credential = self._credential(definition, credentials)
        transport = self._transport_factory(definition, credential)
        try:
            return await transport.health_check()
        finally:
            await transport.aclose()

    async def create(
        self,
        draft: Mapping[str, Any],
        credentials: Mapping[str, Any] | None = None,
    ) -> ProviderDefinition:
        definition = self._definition(draft)
        try:
            self.catalog.get(definition.id)
        except KeyError:
            pass
        else:
            raise ValueError(f"provider already exists: {definition.id}")
        credential = self._credential(definition, credentials)
        report, client = await self._stage(definition, credential)
        self._commit_create(definition, credential, client)
        self._reports[definition.id] = report
        return definition

    async def update(
        self,
        provider_id: str,
        draft: Mapping[str, Any],
        credentials: Mapping[str, Any] | None = None,
    ) -> ProviderDefinition:
        old_definition = self.catalog.get(provider_id)
        if old_definition.builtin:
            raise ValueError(f"builtin provider cannot be replaced: {provider_id}")
        merged = self._record(old_definition)
        merged.update(draft)
        merged["id"] = provider_id
        definition = self._definition(merged)
        credential = self._credential(definition, credentials)
        report, client = await self._stage(definition, credential)
        self._commit_update(old_definition, definition, credential, client)
        self._reports[provider_id] = report
        return definition

    async def delete(self, provider_id: str) -> None:
        definition = self.catalog.get(provider_id)
        if definition.builtin:
            raise ValueError(f"builtin provider cannot be removed: {provider_id}")
        routes = self.config.get("models.routes", {}) or {}
        used_by = [task for task, entry in routes.items() if entry.get("client") == provider_id]
        if used_by:
            raise ProviderInUseError(", ".join(used_by))
        old_record = self.config.get(f"models.providers.{provider_id}")
        old_credential = self.credentials.read(provider_id)
        clients = self._runtime_clients()
        old_client = clients.get(provider_id)
        try:
            self.config.delete(f"models.providers.{provider_id}")
            self.credentials.delete(provider_id)
            clients.pop(provider_id, None)
            self.catalog.unregister(provider_id)
            self._reports.pop(provider_id, None)
        except Exception:
            if old_record is not None:
                self.config.set(f"models.providers.{provider_id}", old_record)
            if old_credential:
                self.credentials.write(provider_id, old_credential)
            if old_client is not None:
                clients[provider_id] = old_client
            try:
                self.catalog.get(provider_id)
            except KeyError:
                self.catalog.register(definition)
            raise

    async def capabilities(self, provider_id: str) -> CapabilityReport:
        if provider_id in self._reports:
            return self._reports[provider_id]
        definition = self.catalog.get(provider_id)
        credential = self.credentials.read(provider_id)
        transport = self._transport_factory(definition, credential)
        try:
            report = await transport.health_check()
        finally:
            await transport.aclose()
        self._reports[provider_id] = report
        return report

    async def discover_models(self, provider_id: str) -> tuple[str, ...]:
        report = await self.capabilities(provider_id)
        if not report.available:
            raise ProviderConnectionError(report.error or "provider unavailable")
        return report.models

    def list(self) -> tuple[ProviderDefinition, ...]:
        return self.catalog.list()

    def validate_route(self, provider_id: str, model_id: str) -> str | None:
        try:
            definition = self.catalog.get(provider_id)
        except KeyError:
            return "missing"
        if not definition.builtin:
            record = self.config.get(f"models.providers.{provider_id}") or {}
            if not record.get("enabled", True):
                return "disabled"
            if provider_id not in self._runtime_clients():
                return "unavailable"
        report = self._reports.get(provider_id)
        if report and report.models and model_id not in report.models:
            return "model"
        return None

    async def _stage(self, definition: ProviderDefinition, credential: str) -> tuple[CapabilityReport, Any]:
        report = await self.test(self._record(definition), {"api_key": credential})
        if not report.available:
            raise ProviderConnectionError(report.error or "provider unavailable")
        return report, self._runtime_client_factory(definition, credential)

    def _commit_create(self, definition: ProviderDefinition, credential: str, client: Any) -> None:
        path = f"models.providers.{definition.id}"
        try:
            self.credentials.write(definition.id, credential)
            self.config.set(path, self._record(definition))
            self.catalog.register(definition)
            self._runtime_clients()[definition.id] = client
        except Exception:
            self.credentials.delete(definition.id)
            if self.config.get(path) is not None:
                self.config.delete(path)
            try:
                self.catalog.unregister(definition.id)
            except KeyError:
                pass
            self._runtime_clients().pop(definition.id, None)
            raise

    def _commit_update(
        self,
        old_definition: ProviderDefinition,
        definition: ProviderDefinition,
        credential: str,
        client: Any,
    ) -> None:
        provider_id = definition.id
        path = f"models.providers.{provider_id}"
        old_record = self.config.get(path)
        old_credential = self.credentials.read(provider_id)
        old_client = self._runtime_clients().get(provider_id)
        try:
            self.credentials.write(provider_id, credential)
            self.config.set(path, self._record(definition))
            self.catalog.register(definition, replace_existing=True)
            self._runtime_clients()[provider_id] = client
        except Exception:
            if old_credential:
                self.credentials.write(provider_id, old_credential)
            else:
                self.credentials.delete(provider_id)
            self.config.set(path, old_record)
            self.catalog.register(old_definition, replace_existing=True)
            if old_client is None:
                self._runtime_clients().pop(provider_id, None)
            else:
                self._runtime_clients()[provider_id] = old_client
            raise

    def _restore_custom_definitions(self) -> None:
        for provider_id, record in (self.config.get("models.providers", {}) or {}).items():
            try:
                self.catalog.get(provider_id)
            except KeyError:
                self.catalog.register(self._definition(dict(record, id=provider_id)))

    def _credential(self, definition: ProviderDefinition, credentials: Mapping[str, Any] | None) -> str:
        supplied = str((credentials or {}).get("api_key", "") or "").strip()
        credential = supplied or self.credentials.read(definition.id)
        if definition.auth.required and not credential:
            raise ValueError("api_key is required")
        return credential

    @staticmethod
    def _definition(draft: Mapping[str, Any]) -> ProviderDefinition:
        provider_id = str(draft.get("id", "")).strip().lower()
        protocol_value = draft.get("protocol", draft.get("format", "openai_compatible"))
        protocol_aliases = {
            "openai": ProviderProtocol.OPENAI_COMPATIBLE.value,
            "custom-map": ProviderProtocol.CUSTOM_MAPPING.value,
        }
        protocol = ProviderProtocol(protocol_aliases.get(str(protocol_value), str(protocol_value)))
        base_url = str(draft.get("base_url", "")).strip()
        if protocol is not ProviderProtocol.LOCAL_ORT and not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        if protocol is not ProviderProtocol.LOCAL_ORT and not is_local_host(base_url):
            allowed, reason = validate_url(base_url)
            if not allowed:
                raise ValueError(f"base_url safety check failed: {reason}")
        capabilities_raw = draft.get("capabilities", {}) or {}
        capabilities = ProviderCapabilities(
            tools=bool(capabilities_raw.get("tools", draft.get("supports_tools", True))),
            vision=bool(capabilities_raw.get("vision", draft.get("supports_vision", False))),
            streaming=bool(capabilities_raw.get("streaming", draft.get("supports_streaming", True))),
            model_discovery=bool(capabilities_raw.get("model_discovery", draft.get("supports_model_discovery", True))),
            json_mode=bool(capabilities_raw.get("json_mode", draft.get("supports_json_mode", False))),
        )
        auth_raw = draft.get("auth", {}) or {}
        default_required = protocol not in {ProviderProtocol.OLLAMA, ProviderProtocol.CUSTOM_MAPPING}
        return ProviderDefinition(
            id=provider_id,
            protocol=protocol,
            endpoint=EndpointDefinition(
                base_url=base_url,
                chat_path=str(draft.get("chat_path", "/chat/completions") or "/chat/completions"),
                models_path=str(draft.get("models_path", "/models") or "/models"),
            ),
            auth=AuthDefinition(
                environment_aliases=tuple(auth_raw.get("environment_aliases", ())),
                header=str(auth_raw.get("header", "Authorization")),
                scheme=str(auth_raw.get("scheme", "Bearer")),
                required=bool(auth_raw.get("required", default_required)),
            ),
            capabilities=capabilities,
            default_model=str(draft.get("default_model", "") or ""),
            max_tokens_cap=draft.get("max_tokens_cap"),
            metadata={
                "label": str(draft.get("label", provider_id)),
                "enabled": bool(draft.get("enabled", True)),
                "order": int(draft.get("order", 9999)),
                "headers": dict(draft.get("headers") or {}),
                "mapping": dict(draft.get("mapping") or {}),
            },
        )

    @staticmethod
    def _record(definition: ProviderDefinition) -> dict[str, Any]:
        capabilities = asdict(definition.capabilities)
        return {
            "id": definition.id,
            "label": definition.metadata.get("label", definition.id),
            "protocol": definition.protocol.value,
            "format": "anthropic" if definition.protocol is ProviderProtocol.ANTHROPIC else "openai",
            "base_url": definition.endpoint.base_url,
            "chat_path": definition.endpoint.chat_path,
            "models_path": definition.endpoint.models_path,
            "default_model": definition.default_model,
            "enabled": definition.metadata.get("enabled", True),
            "order": definition.metadata.get("order", 9999),
            "capabilities": capabilities,
            "max_tokens_cap": definition.max_tokens_cap,
            "auth": {
                "environment_aliases": list(definition.auth.environment_aliases),
                "header": definition.auth.header,
                "scheme": definition.auth.scheme,
                "required": definition.auth.required,
            },
            "headers": dict(definition.metadata.get("headers") or {}),
            "mapping": dict(definition.metadata.get("mapping") or {}),
        }

    def _runtime_clients(self) -> dict[str, Any]:
        if not hasattr(self.runtime_router, "_custom_clients"):
            self.runtime_router._custom_clients = {}
        return self.runtime_router._custom_clients

    @staticmethod
    def _build_transport(definition: ProviderDefinition, credential: str) -> ProviderTransport:
        if definition.protocol is ProviderProtocol.ANTHROPIC:
            return AnthropicTransport(
                credential,
                definition.endpoint.base_url,
                capabilities=definition.capabilities,
                default_model=definition.default_model,
            )
        if definition.protocol is ProviderProtocol.OLLAMA:
            return OllamaTransport(
                definition.endpoint.base_url,
                api_key=credential,
                capabilities=definition.capabilities,
                default_model=definition.default_model,
            )
        if definition.protocol is ProviderProtocol.OPENAI_COMPATIBLE:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=credential or "not-required", base_url=definition.endpoint.base_url)
            return OpenAICompatibleTransport(
                client,
                capabilities=definition.capabilities,
                default_model=definition.default_model,
            )
        if definition.protocol is ProviderProtocol.CUSTOM_MAPPING:
            headers = dict(definition.metadata.get("headers") or {})
            if definition.auth.header and definition.auth.header not in headers and credential:
                value = f"{definition.auth.scheme} {{api_key}}".strip()
                headers[definition.auth.header] = value
            return CustomMappingTransport(
                definition.endpoint.base_url,
                mapping=dict(definition.metadata.get("mapping") or {}),
                headers=headers,
                api_key=credential,
                capabilities=definition.capabilities,
                default_model=definition.default_model,
                chat_path=definition.endpoint.chat_path,
                models_path=definition.endpoint.models_path,
            )
        raise ValueError(f"unsupported provider protocol: {definition.protocol.value}")

    @staticmethod
    def _build_runtime_client(definition: ProviderDefinition, credential: str) -> Any:
        from web.custom_providers import build_client

        format_name = "anthropic" if definition.protocol is ProviderProtocol.ANTHROPIC else "openai"
        return build_client(format_name, definition.endpoint.base_url, credential or "ollama")
