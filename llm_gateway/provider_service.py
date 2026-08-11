from __future__ import annotations

import asyncio
import os
import re
import sys
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any

from loguru import logger

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
from security.ssrf_guard import build_secure_async_client, validate_url


class ProviderConnectionError(RuntimeError):
    pass


class ProviderInUseError(RuntimeError):
    pass


_OLLAMA_ALLOWED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
}


class ProviderCredentialStore:
    def read(self, provider_id: str) -> str:
        from web._provider_keys import load_provider_key

        return load_provider_key(provider_id)

    def write(self, provider_id: str, value: str) -> None:
        from utils.atomic_write import atomic_write
        from web._provider_keys import _encode_key, _key_file

        path = _key_file(provider_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, _encode_key(value) + "\n", encoding="utf-8", mode=0o600)
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
        self._locks: dict[str, asyncio.Lock] = {}
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
        async with self._lock(definition.id):
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
        async with self._lock(provider_id):
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
        async with self._lock(provider_id):
            definition = self.catalog.get(provider_id)
            if definition.builtin:
                raise ValueError(f"builtin provider cannot be removed: {provider_id}")
            routes = self.config.get("models.routes", {}) or {}
            used_by = [task for task, entry in routes.items() if entry.get("client") == provider_id]
            if used_by:
                raise ProviderInUseError(", ".join(used_by))
            old_record = self.config.get(f"models.providers.{provider_id}")
            old_credential = self.credentials.read(provider_id)
            old_report = self._reports.get(provider_id)
            clients = self._runtime_clients()
            old_client = clients.get(provider_id)
            try:
                self.config.delete(f"models.providers.{provider_id}")
                self.credentials.delete(provider_id)
                clients.pop(provider_id, None)
                self.catalog.unregister(provider_id)
                self._reports.pop(provider_id, None)
            except Exception:
                self._run_rollback(self._delete_rollback_actions(
                    provider_id, definition, old_record, old_credential, old_report, clients, old_client))
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
        record = self.config.get(f"models.providers.{provider_id}") or {}
        if not definition.builtin and not record.get("enabled", True):
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
            self._run_rollback([
                lambda: self.credentials.delete(definition.id),
                lambda: self.config.delete(path) if self.config.get(path) is not None else None,
                lambda: self._safe_unregister(definition.id),
                lambda: self._runtime_clients().pop(definition.id, None),
            ])
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

        def restore_runtime() -> None:
            if old_client is None:
                self._runtime_clients().pop(provider_id, None)
            else:
                self._runtime_clients()[provider_id] = old_client

        try:
            self.credentials.write(provider_id, credential)
            self.config.set(path, self._record(definition))
            self.catalog.register(definition, replace_existing=True)
            self._runtime_clients()[provider_id] = client
        except Exception:
            self._run_rollback([
                lambda: (self.credentials.write(provider_id, old_credential)
                         if old_credential else self.credentials.delete(provider_id)),
                lambda: self.config.set(path, old_record),
                lambda: self.catalog.register(old_definition, replace_existing=True),
                restore_runtime,
            ])
            raise

    def _restore_custom_definitions(self) -> None:
        clients = self._runtime_clients()
        for provider_id, record in (self.config.get("models.providers", {}) or {}).items():
            try:
                definition = self._definition(dict(record, id=provider_id))
            except Exception as error:
                logger.warning("provider_service.restore_definition_failed id={} error={}", provider_id, error)
                continue
            try:
                self.catalog.get(provider_id)
            except KeyError:
                self.catalog.register(definition)
            # 内置 provider 与凭证池 provider 不在重建范围：
            # 内置由 ModelRouter 初始化，凭证池由 _register_credential_pool_providers 注册。
            if definition.builtin or provider_id in clients:
                continue
            try:
                credential = self.credentials.read(provider_id)
                if definition.auth.required and not credential:
                    continue
                clients[provider_id] = self._runtime_client_factory(definition, credential)
            except Exception as error:
                logger.warning(
                    "provider_service.restore_client_failed id={} error={}",
                    provider_id,
                    error,
                )
                continue

    def _credential(self, definition: ProviderDefinition, credentials: Mapping[str, Any] | None) -> str:
        supplied = str((credentials or {}).get("api_key", "") or "").strip()
        credential = supplied or self.credentials.read(definition.id)
        if definition.auth.required and not credential:
            raise ValueError("api_key is required")
        return credential

    @staticmethod
    def _definition(draft: Mapping[str, Any]) -> ProviderDefinition:
        provider_id = str(draft.get("id", "")).strip().lower()
        # 凭证文件按 provider_id 落盘（web/_provider_keys._key_file 仅保留 alnum/-/_），
        # 含点号等字符的 id（如 a.b）会与 ab 映射到同一凭证文件造成 ID 冲突，直接拒绝。
        if not re.fullmatch(r"[a-z0-9_-]+", provider_id):
            raise ValueError(f"provider id must match ^[a-z0-9_-]+$: {provider_id!r}")
        protocol_value = draft.get("protocol", draft.get("format", "openai_compatible"))
        protocol_aliases = {
            "openai": ProviderProtocol.OPENAI_COMPATIBLE.value,
            "custom-map": ProviderProtocol.CUSTOM_MAPPING.value,
        }
        protocol = ProviderProtocol(protocol_aliases.get(str(protocol_value), str(protocol_value)))
        base_url = str(draft.get("base_url", "")).strip()
        if protocol is not ProviderProtocol.LOCAL_ORT and not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        if protocol is not ProviderProtocol.LOCAL_ORT:
            if protocol is ProviderProtocol.OLLAMA:
                # 严格本地策略：仅 http://localhost:11434、http://127.0.0.1:11434、http://[::1]:11434，
                # 拒绝 HTTPS 本地目标、回环别名（localhost.localdomain/ip6-localhost 等）、
                # 0.0.0.0、容器宿主别名与其他端口。
                if not ProviderService._is_ollama_local(base_url):
                    raise ValueError(
                        "Ollama local provider must use http://localhost:11434, "
                        "http://127.0.0.1:11434 or http://[::1]:11434"
                    )
            else:
                allowed, reason = validate_url(base_url)
                if not allowed:
                    raise ValueError(f"base_url safety check failed: {reason}")
        headers = dict(draft.get("headers") or {})
        for header_name, header_value in headers.items():
            if "{api_key}" not in str(header_value) and "{base_url}" not in str(header_value):
                raise ValueError(
                    f"header {header_name} must use {{api_key}}/{{base_url}} placeholder, got literal value"
                )
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
                "headers": headers,
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

    def _lock(self, provider_id: str) -> asyncio.Lock:
        # 按规范化 provider ID 共享锁，避免 "Custom" 与 "custom" 各自持锁
        key = provider_id.strip().lower()
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _run_rollback(self, actions: list[Callable[[], None]]) -> None:
        failures: list[BaseException] = []
        for action in actions:
            try:
                action()
            except Exception as error:
                failures.append(error)
                logger.warning("provider_service.rollback_action_failed", exc_info=True)
        if not failures:
            return
        # 补偿步骤全部尝试后，将补偿失败聚合到当前主异常的 __context__ 链：
        # 原始提交异常仍保持为主异常，链遍历可看到全部补偿失败。
        primary = sys.exc_info()[1]
        if primary is None:
            return
        node: BaseException | None = primary
        seen = {id(node)}
        while node.__context__ is not None and id(node.__context__) not in seen:
            node = node.__context__
            seen.add(id(node))
        for error in failures:
            if error is primary or id(error) in seen:
                continue
            error.__context__ = None
            error.__cause__ = None
            node.__context__ = error
            node = error
            seen.add(id(error))

    def _safe_unregister(self, provider_id: str) -> None:
        try:
            self.catalog.unregister(provider_id)
        except KeyError:
            pass

    def _delete_rollback_actions(self, provider_id, definition, old_record,
                                 old_credential, old_report, clients, old_client) -> list[Callable[[], None]]:
        def restore_config() -> None:
            if old_record is not None:
                self.config.set(f"models.providers.{provider_id}", old_record)

        def restore_credential() -> None:
            if old_credential:
                self.credentials.write(provider_id, old_credential)

        def restore_runtime() -> None:
            if old_client is not None:
                clients[provider_id] = old_client

        def restore_catalog() -> None:
            try:
                self.catalog.get(provider_id)
            except KeyError:
                self.catalog.register(definition)

        def restore_report() -> None:
            if old_report is not None:
                self._reports[provider_id] = old_report
            else:
                self._reports.pop(provider_id, None)

        return [restore_config, restore_credential, restore_runtime, restore_catalog, restore_report]

    @staticmethod
    def _is_ollama_local(base_url: str) -> bool:
        """严格判断 Ollama 本地目标：http + 规范回环主机（localhost/127.0.0.1/[::1]）+ 端口 11434。"""
        try:
            parsed = urllib.parse.urlparse(base_url)
            if parsed.scheme != "http":
                return False
            host = (parsed.hostname or "").lower().rstrip(".")
            return host in _OLLAMA_ALLOWED_HOSTS and parsed.port == 11434
        except ValueError:
            return False

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

            client = AsyncOpenAI(
                api_key=credential or "not-required",
                base_url=definition.endpoint.base_url,
                http_client=build_secure_async_client(definition.endpoint.base_url),
            )
            return OpenAICompatibleTransport(
                client,
                capabilities=definition.capabilities,
                default_model=definition.default_model,
                base_url=definition.endpoint.base_url,
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
        if definition.protocol is ProviderProtocol.OLLAMA:
            from openai import AsyncOpenAI

            # Ollama 的 OpenAI 兼容端点是 base_url + /v1
            return AsyncOpenAI(
                api_key=credential or "ollama",
                base_url=f"{definition.endpoint.base_url.rstrip('/')}/v1",
            )
        if definition.protocol is ProviderProtocol.CUSTOM_MAPPING:
            from web.custom_providers import CustomMappingCompatClient

            headers = dict(definition.metadata.get("headers") or {})
            if definition.auth.header and definition.auth.header not in headers and credential:
                value = f"{definition.auth.scheme} {{api_key}}".strip()
                headers[definition.auth.header] = value
            return CustomMappingCompatClient(
                credential,
                definition.endpoint.base_url,
                mapping=dict(definition.metadata.get("mapping") or {}),
                headers=headers,
                capabilities=definition.capabilities,
                default_model=definition.default_model,
                chat_path=definition.endpoint.chat_path,
                models_path=definition.endpoint.models_path,
            )
        from web.custom_providers import build_client

        format_name = "anthropic" if definition.protocol is ProviderProtocol.ANTHROPIC else "openai"
        return build_client(format_name, definition.endpoint.base_url, credential or "ollama")
