from __future__ import annotations

import asyncio
import os
import re
import sys
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
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
from llm_gateway.transports.custom_mapping import validate_custom_headers
from security.ssrf_guard import build_secure_async_client, validate_url

# 本地 ONNX Runtime GenAI chat provider（与 model_router._LOCAL_ORT_PROVIDER 一致，
# 不经 provider catalog 注册，由 LocalChatService 提供推理）
_LOCAL_ORT_PROVIDER = "local-ort"


class ProviderConnectionError(RuntimeError):
    pass


class ProviderInUseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderSnapshot:
    provider_id: str
    definition: ProviderDefinition | None
    record: dict[str, Any] | None
    credential: str
    report_present: bool
    report: CapabilityReport | None
    runtime_kind: str
    client_present: bool
    client: Any


_OLLAMA_ALLOWED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
}


class ProviderCredentialStore:
    def read(self, provider_id: str) -> str:
        from core_runtime._provider_keys import load_provider_key

        return load_provider_key(provider_id)

    def write(self, provider_id: str, value: str) -> None:
        from core_runtime._provider_keys import _encode_key, _key_file
        from utils.atomic_write import _restrict_file_permissions_windows, atomic_write

        path = _key_file(provider_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, _encode_key(value) + "\n", encoding="utf-8", mode=0o600)
        try:
            os.chmod(path, 0o600)  # Unix: 限制为仅用户可读写
            _restrict_file_permissions_windows(path)  # Windows: 用 ACL 补偿
        except OSError:
            logger.debug("provider_service.key_chmod_failed: {}", path, exc_info=True)

    def delete(self, provider_id: str) -> None:
        from core_runtime._provider_keys import _key_file

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
                # 正常路径：provider 尚未注册，允许创建
                logger.debug("provider_service.create_dup_check_not_found provider_id={}", definition.id)
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

    async def bind_builtin(
        self,
        provider_id: str,
        draft: Mapping[str, Any] | None = None,
        credentials: Mapping[str, Any] | None = None,
    ) -> ProviderDefinition:
        async with self._lock(provider_id):
            definition = self.catalog.get(provider_id)
            if not definition.builtin:
                raise ValueError(f"provider is not builtin: {provider_id}")
            merged = self._record(definition)
            merged.update(draft or {})
            merged["id"] = definition.id
            candidate = self._definition(merged)
            candidate = ProviderDefinition(
                id=candidate.id,
                protocol=candidate.protocol,
                endpoint=candidate.endpoint,
                auth=candidate.auth,
                capabilities=candidate.capabilities,
                builtin=True,
                default_model=candidate.default_model,
                default_pro_model=definition.default_pro_model,
                max_tokens_cap=candidate.max_tokens_cap,
                metadata=candidate.metadata,
            )
            credential = self._credential(candidate, credentials)
            report, client = await self._stage(candidate, credential)
            old_credential = self.credentials.read(provider_id)
            old_report = self._reports.get(provider_id)
            old_client = None
            bound = False
            try:
                self.credentials.write(provider_id, credential)
                binder = getattr(self.runtime_router, "bind_builtin", None)
                if binder is None:
                    raise RuntimeError("runtime router does not support builtin binding")
                old_client = binder(provider_id, client)
                bound = True
                self._reports[provider_id] = report
            except (RuntimeError, OSError, ValueError):
                logger.exception("provider_service.bind_builtin_commit_failed provider_id={}", provider_id)
                if old_credential:
                    self.credentials.write(provider_id, old_credential)
                else:
                    self.credentials.delete(provider_id)
                if bound:
                    binder(provider_id, old_client)
                if old_report is None:
                    self._reports.pop(provider_id, None)
                else:
                    self._reports[provider_id] = old_report
                raise
            except Exception:
                logger.exception("provider_service.bind_builtin_commit_unexpected provider_id={}", provider_id)
                if old_credential:
                    self.credentials.write(provider_id, old_credential)
                else:
                    self.credentials.delete(provider_id)
                if bound:
                    binder(provider_id, old_client)
                if old_report is None:
                    self._reports.pop(provider_id, None)
                else:
                    self._reports[provider_id] = old_report
                raise
            return candidate

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
            old_client = self.runtime_router.get_custom_client(provider_id)
            try:
                self.config.delete(f"models.providers.{provider_id}")
                self.credentials.delete(provider_id)
                self.runtime_router.remove_custom_client(provider_id)
                self.catalog.unregister(provider_id)
                self._reports.pop(provider_id, None)
            except (RuntimeError, OSError, ValueError):
                logger.exception("provider_service.delete_commit_failed provider_id={}", provider_id)
                self._run_rollback(self._delete_rollback_actions(
                    provider_id, definition, old_record, old_credential, old_report, old_client))
                raise
            except Exception:
                logger.exception("provider_service.delete_commit_unexpected provider_id={}", provider_id)
                self._run_rollback(self._delete_rollback_actions(
                    provider_id, definition, old_record, old_credential, old_report, old_client))
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

    def snapshot(self, provider_id: str) -> ProviderSnapshot:
        try:
            definition = self.catalog.get(provider_id)
        except KeyError:
            definition = None
        record = self.config.get(f"models.providers.{provider_id}")
        credential = self.credentials.read(provider_id)
        report_present = provider_id in self._reports
        report = self._reports.get(provider_id)
        if definition is not None and definition.builtin:
            getter = getattr(self.runtime_router, "get_builtin_client", None)
            if getter is None:
                raise RuntimeError("runtime router does not support builtin snapshots")
            client = getter(provider_id)
            runtime_kind = "builtin"
            client_present = client is not None
        else:
            runtime_kind = "custom"
            client = self.runtime_router.get_custom_client(provider_id)
            client_present = self.runtime_router.has_custom_client(provider_id)
        return ProviderSnapshot(
            provider_id=provider_id,
            definition=definition,
            record=record,
            credential=credential,
            report_present=report_present,
            report=report,
            runtime_kind=runtime_kind,
            client_present=client_present,
            client=client,
        )

    async def restore_snapshot(self, snapshot: ProviderSnapshot) -> None:
        async with self._lock(snapshot.provider_id):
            provider_id = snapshot.provider_id
            path = f"models.providers.{provider_id}"

            def restore_config() -> None:
                if snapshot.record is None:
                    self.config.delete(path)
                else:
                    self.config.set(path, snapshot.record)

            def restore_credential() -> None:
                if snapshot.credential:
                    self.credentials.write(provider_id, snapshot.credential)
                else:
                    self.credentials.delete(provider_id)

            def restore_catalog() -> None:
                if snapshot.definition is None:
                    self._safe_unregister(provider_id)
                else:
                    self.catalog.register(snapshot.definition, replace_existing=True)

            def restore_runtime() -> None:
                if snapshot.runtime_kind == "builtin":
                    binder = getattr(self.runtime_router, "bind_builtin", None)
                    if binder is None:
                        raise RuntimeError("runtime router does not support builtin binding")
                    binder(provider_id, snapshot.client if snapshot.client_present else None)
                    return
                if snapshot.client_present:
                    self.runtime_router.set_custom_client(provider_id, snapshot.client)
                else:
                    self.runtime_router.remove_custom_client(provider_id)

            def restore_report() -> None:
                if snapshot.report_present:
                    self._reports[provider_id] = snapshot.report
                else:
                    self._reports.pop(provider_id, None)

            failures = self._collect_failures([
                restore_config,
                restore_credential,
                restore_catalog,
                restore_runtime,
                restore_report,
            ])
            if failures:
                raise ExceptionGroup(f"provider snapshot restore failed: {provider_id}", failures)

    def validate_route(self, provider_id: str, model_id: str) -> str | None:
        # 本地 ORT GenAI chat provider：不经 provider catalog 注册，
        # 只要本地 chat transport 已配置且模型名为 local:<id> 形式即可路由。
        # 实际推理由 LocalChatService 选取已启动的 chat 实例，未选中时
        # 会给出明确的 LocalModelUnavailableError，不做静默回退。
        if provider_id == _LOCAL_ORT_PROVIDER:
            get_transport = getattr(self.runtime_router, "get_transport", None)
            if get_transport is None or get_transport(_LOCAL_ORT_PROVIDER) is None:
                return "unavailable"
            if not model_id.startswith("local:"):
                return "model"
            return None
        try:
            definition = self.catalog.get(provider_id)
        except KeyError:
            return "missing"
        record = self.config.get(f"models.providers.{provider_id}") or {}
        if not definition.builtin and not record.get("enabled", True):
            return "disabled"
        if not self._provider_ready(definition):
            return "unavailable"
        report = self._reports.get(provider_id)
        if report and report.models and model_id not in report.models:
            return "model"
        return None

    def _provider_ready(self, definition: ProviderDefinition) -> bool:
        if definition.builtin:
            checker = getattr(self.runtime_router, "_is_client_configured", None)
            if checker is not None:
                return bool(checker(definition.id))
        return self.runtime_router.has_custom_client(definition.id)

    async def _stage(self, definition: ProviderDefinition, credential: str) -> tuple[CapabilityReport, Any]:
        report = await self.test(self._record(definition), {"api_key": credential})
        if not report.available:
            raise ProviderConnectionError(report.error or "provider unavailable")
        return report, self._runtime_client_factory(definition, credential)

    def _commit_create(self, definition: ProviderDefinition, credential: str, client: Any) -> None:
        path = f"models.providers.{definition.id}"
        old_record = self.config.get(path)
        old_credential = self.credentials.read(definition.id)
        had_client = self.runtime_router.has_custom_client(definition.id)
        old_client = self.runtime_router.get_custom_client(definition.id)
        had_report = definition.id in self._reports
        old_report = self._reports.get(definition.id)
        try:
            old_definition = self.catalog.get(definition.id)
        except KeyError:
            old_definition = None

        def restore_config() -> None:
            if old_record is None:
                self.config.delete(path)
            else:
                self.config.set(path, old_record)

        def restore_credential() -> None:
            if old_credential:
                self.credentials.write(definition.id, old_credential)
            else:
                self.credentials.delete(definition.id)

        def restore_catalog() -> None:
            if old_definition is None:
                self._safe_unregister(definition.id)
            else:
                self.catalog.register(old_definition, replace_existing=True)

        def restore_runtime() -> None:
            if had_client:
                self.runtime_router.set_custom_client(definition.id, old_client)
            else:
                self.runtime_router.remove_custom_client(definition.id)

        def restore_report() -> None:
            if had_report:
                self._reports[definition.id] = old_report
            else:
                self._reports.pop(definition.id, None)

        try:
            self.credentials.write(definition.id, credential)
            self.config.set(path, self._record(definition))
            self.catalog.register(definition)
            self.runtime_router.set_custom_client(definition.id, client)
        except (RuntimeError, OSError, ValueError):
            logger.exception("provider_service.create_commit_failed provider_id={}", definition.id)
            self._run_rollback([
                restore_credential,
                restore_config,
                restore_catalog,
                restore_runtime,
                restore_report,
            ])
            raise
        except Exception:
            logger.exception("provider_service.create_commit_unexpected provider_id={}", definition.id)
            self._run_rollback([
                restore_credential,
                restore_config,
                restore_catalog,
                restore_runtime,
                restore_report,
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
        old_client = self.runtime_router.get_custom_client(provider_id)

        def restore_runtime() -> None:
            if old_client is None:
                self.runtime_router.remove_custom_client(provider_id)
            else:
                self.runtime_router.set_custom_client(provider_id, old_client)

        try:
            self.credentials.write(provider_id, credential)
            self.config.set(path, self._record(definition))
            self.catalog.register(definition, replace_existing=True)
            self.runtime_router.set_custom_client(provider_id, client)
        except (RuntimeError, OSError, ValueError):
            logger.exception("provider_service.update_commit_failed provider_id={}", provider_id)
            self._run_rollback([
                lambda: (self.credentials.write(provider_id, old_credential)
                         if old_credential else self.credentials.delete(provider_id)),
                lambda: self.config.set(path, old_record),
                lambda: self.catalog.register(old_definition, replace_existing=True),
                restore_runtime,
            ])
            raise
        except Exception:
            logger.exception("provider_service.update_commit_unexpected provider_id={}", provider_id)
            self._run_rollback([
                lambda: (self.credentials.write(provider_id, old_credential)
                         if old_credential else self.credentials.delete(provider_id)),
                lambda: self.config.set(path, old_record),
                lambda: self.catalog.register(old_definition, replace_existing=True),
                restore_runtime,
            ])
            raise

    def _restore_custom_definitions(self) -> None:
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
            if definition.builtin or self.runtime_router.has_custom_client(provider_id):
                continue
            try:
                credential = self.credentials.read(provider_id)
                if definition.auth.required and not credential:
                    continue
                self.runtime_router.set_custom_client(
                    provider_id, self._runtime_client_factory(definition, credential)
                )
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
        if protocol is ProviderProtocol.OLLAMA:
            base_url = ProviderService._normalize_ollama_base_url(base_url)
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
        headers = validate_custom_headers(draft.get("headers") or {})
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

    def _lock(self, provider_id: str) -> asyncio.Lock:
        # 按规范化 provider ID 共享锁，避免 "Custom" 与 "custom" 各自持锁
        key = provider_id.strip().lower()
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _run_rollback(self, actions: list[Callable[[], None]]) -> None:
        failures = self._collect_failures(actions)
        if not failures:
            return
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

    @staticmethod
    def _collect_failures(actions: list[Callable[[], None]]) -> list[Exception]:
        failures: list[Exception] = []
        for action in actions:
            try:
                action()
            except Exception as error:
                failures.append(error)
                logger.warning("provider_service.rollback_action_failed", exc_info=True)
        return failures

    def _safe_unregister(self, provider_id: str) -> None:
        try:
            self.catalog.unregister(provider_id)
        except KeyError:
            logger.debug("provider_service.unregister_not_found provider_id={}", provider_id)

    def _delete_rollback_actions(self, provider_id, definition, old_record,
                                 old_credential, old_report, old_client) -> list[Callable[[], None]]:
        def restore_config() -> None:
            if old_record is not None:
                self.config.set(f"models.providers.{provider_id}", old_record)

        def restore_credential() -> None:
            if old_credential:
                self.credentials.write(provider_id, old_credential)

        def restore_runtime() -> None:
            if old_client is not None:
                self.runtime_router.set_custom_client(provider_id, old_client)

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
    def _normalize_ollama_base_url(base_url: str) -> str:
        value = base_url.rstrip("/")
        if value.endswith("/v1"):
            value = value[:-3].rstrip("/")
        return value

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
            from core_runtime.custom_providers import CustomMappingCompatClient

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
        from core_runtime.custom_providers import build_client

        format_name = "anthropic" if definition.protocol is ProviderProtocol.ANTHROPIC else "openai"
        return build_client(format_name, definition.endpoint.base_url, credential or "ollama")
