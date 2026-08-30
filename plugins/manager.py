"""插件管理器 — 生命周期管理 + 状态机 + 安全校验"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from enum import Enum
from importlib.machinery import ModuleSpec, PathFinder
from pathlib import Path
from types import ModuleType
from typing import Any

from loguru import logger
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

import config

try:
    from utils.atomic_write import atomic_write
except (ImportError, AttributeError):
    atomic_write = None  # type: ignore[assignment]

except Exception:
    logger.exception(".plugins.manager.unexpected")
    atomic_write = None  # type: ignore[assignment]

from plugins.context import PluginContext
from plugins.discovery import discover_plugins
from plugins.manifest import PluginManifest
from plugins.permissions import PermissionChecker
from plugins.sdk import Plugin


def _read_host_version() -> str:
    """读取应用版本；优先复用 WebUI 的运行时版本解析规则。"""
    try:
        from web.routers.system import _read_version

        version = _read_version()
        if version != "dev":
            return version
    except (ImportError, OSError, ValueError):
        logger.debug("plugin_manager.host_version_helper_failed", exc_info=True)
    try:
        return (Path(__file__).resolve().parents[1] / "VERSION").read_text(
            encoding="utf-8",
        ).strip()
    except (OSError, ValueError):
        return "0.0.0"


HOST_VERSION = _read_host_version()
PLUGIN_SDK_VERSION = HOST_VERSION
_LOAD_PHASE_ORDER = {"pre-agent": 0, "post-agent": 1}


class PluginState(str, Enum):
    """插件生命周期状态枚举。"""
    FOUND = "found"
    LOADED = "loaded"
    ENABLED = "enabled"
    DISABLED = "disabled"
    UNLOADED = "unloaded"
    ERROR = "error"


@dataclass
class PluginRecord:
    manifest: PluginManifest
    plugin_dir: Path
    state: PluginState = PluginState.FOUND
    instance: Plugin | None = None
    context: PluginContext | None = None
    error_message: str = ""
    module_namespace: str = ""
    loaded_module_names: set[str] = field(default_factory=set)
    loaded_modules: dict[str, ModuleType] = field(default_factory=dict)
    _has_been_loaded: bool = False


class PluginManager:
    """插件生命周期管理器"""

    LIFECYCLE_TIMEOUT = 60  # 秒

    def __init__(self, tool_registry: Any | None=None, hook_engine: Any | None=None, memory_manager: Any | None=None,
                 knowledge_graph: Any | None=None, mcp_manager: Any | None=None, agent_core: Any | None=None) -> None:
        self._plugins: dict[str, PluginRecord] = {}
        self._tool_registry = tool_registry
        self._hook_engine = hook_engine
        self._memory = memory_manager
        self._kg = knowledge_graph
        self._mcp = mcp_manager
        self._agent_core = agent_core



    @property
    def plugins(self) -> dict[str, PluginRecord]:
        return self._plugins

    # ── Integrity Check ──
    @staticmethod
    def _hash_plugin_dir(plugin_dir: Path) -> str:
        """计算插件目录下所有 .py 文件的 SHA256 hash（排序确保确定性）。"""
        h = hashlib.sha256()
        py_files = sorted(plugin_dir.rglob("*.py"))
        for f in py_files:
            h.update(f.relative_to(plugin_dir).as_posix().encode())
            h.update(f.read_bytes())
        return h.hexdigest()

    # 类方法（原 @staticmethod）：信任存储路径成为可替换点——测试侧
    # monkeypatch 到临时目录即可隔离落盘（详见 tests/conftest.py 的
    # _isolate_plugin_trust_store），默认路径语义不变。
    @classmethod
    def _trust_store_file(cls) -> Path:
        return config.PLUGINS_CONFIG_DIR / "trust_store.json"

    @classmethod
    def _load_trust_store(cls) -> dict[str, str]:
        """加载信任存储 {plugin_id: expected_sha256}。"""
        trust_store_file = cls._trust_store_file()
        try:
            if trust_store_file.exists():
                return json.loads(trust_store_file.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("plugin_manager.trust_store_load_failed", exc_info=True)
        return {}

    @classmethod
    def _save_trust_store(cls, store: dict[str, str]) -> None:
        """保存信任存储。"""
        trust_store_file = cls._trust_store_file()
        try:
            trust_store_file.parent.mkdir(parents=True, exist_ok=True)
            trust_store_file.write_text(
                json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.debug("plugin_manager.trust_store_save_failed", exc_info=True)

    def _verify_integrity(self, plugin_id: str, plugin_dir: Path) -> str | None:
        """验证插件文件完整性。

        逻辑：
        1. 如果信任存储中有该插件的 hash → 必须匹配（防篡改）
        2. 如果信任存储中没有 → 首次加载，自动记录 hash（信任首次）
        3. PLUGINS_TRUST_MODE=off 时跳过校验（调试用）

        Returns:
            None = 校验通过，str = 拒绝原因
        """
        import os
        if os.getenv("PLUGINS_TRUST_MODE", "on").strip().lower() == "off":
            return None  # 调试模式跳过

        if not plugin_dir.exists():
            return None  # 内置插件无需校验

        current_hash = self._hash_plugin_dir(plugin_dir)
        store = self._load_trust_store()

        if plugin_id in store:
            expected = store[plugin_id]
            if current_hash != expected:
                return (f"插件文件已被篡改！期望 hash={expected[:16]}…，"
                        f"实际 hash={current_hash[:16]}…。"
                        f"如确认安全，删除 trust_store.json 中 {plugin_id} 条目后重试")
        else:
            # 首次加载：信任并记录
            store[plugin_id] = current_hash
            self._save_trust_store(store)
            logger.info("plugin.trust_registered", id=plugin_id, hash=current_hash[:16])

        return None

    @staticmethod
    def _contained_path(path: Path, plugin_dir: Path, module_path: str) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(plugin_dir)
        except ValueError as exc:
            raise ValueError(
                f"Entrypoint module must be inside the plugin directory: {module_path!r}",
            ) from exc
        return resolved

    @staticmethod
    def _resolve_entrypoint_file(module_path: str, plugin_dir: Path) -> tuple[Path, bool]:
        """解析入口文件并确认真实路径仍位于插件目录内。"""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", module_path):
            raise ValueError(f"Invalid entrypoint module name: {module_path!r}")
        top = module_path.split(".", 1)[0]
        if top in sys.stdlib_module_names:
            raise ValueError(f"Entrypoint module must not be a stdlib module: {module_path!r}")

        plugin_dir = Path(plugin_dir).resolve()
        parts = module_path.split(".")
        candidates = (
            (plugin_dir.joinpath(*parts, "__init__.py"), True),
            (plugin_dir.joinpath(*parts).with_suffix(".py"), False),
        )
        for candidate, is_package in candidates:
            if not candidate.is_file():
                continue
            resolved = PluginManager._contained_path(candidate, plugin_dir, module_path)
            return resolved, is_package
        raise ValueError(f"Entrypoint module must be inside the plugin directory: {module_path!r}")

    @classmethod
    def _resolve_entrypoint_module(cls, module_path: str, plugin_dir: Path) -> str:
        """校验 entrypoint 模块只能来自插件目录，阻止任意模块导入。"""
        cls._resolve_entrypoint_file(module_path, plugin_dir)
        return module_path

    @staticmethod
    def _new_namespace(plugin_id: str) -> str:
        safe_id = re.sub(r"[^A-Za-z0-9_]", "_", plugin_id)
        return f"_xiaoda_plugin_{safe_id}_{uuid.uuid4().hex}"

    @staticmethod
    def _install_namespace_package(name: str, directory: Path) -> ModuleType:
        """在唯一名称下安装轻量 namespace package，供相对导入解析。"""
        spec = ModuleSpec(name, loader=None, is_package=True)
        spec.submodule_search_locations = [str(directory)]
        module = ModuleType(name)
        module.__package__ = name
        module.__path__ = [str(directory)]
        module.__spec__ = spec
        sys.modules[name] = module
        return module

    @staticmethod
    def _execute_module(name: str, path: Path, *, is_package: bool) -> ModuleType:
        locations = [str(path.parent)] if is_package else None
        spec = importlib.util.spec_from_file_location(
            name,
            path,
            submodule_search_locations=locations,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to create module spec for {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
        return module

    @staticmethod
    def _module_is_under(module: ModuleType, plugin_dir: Path) -> bool:
        module_file = getattr(module, "__file__", None)
        if not module_file:
            return False
        try:
            Path(module_file).resolve().relative_to(plugin_dir)
        except (OSError, ValueError):
            return False
        return True

    @classmethod
    def _local_module_roots(cls, plugin_dir: Path) -> set[str]:
        """返回当前插件目录可解析的顶层模块名。"""
        roots: set[str] = set()
        for child in plugin_dir.iterdir():
            if child.is_file() and child.suffix == ".py":
                candidate = child.stem
            elif child.is_dir():
                candidate = child.name
            else:
                continue
            if not candidate.isidentifier():
                continue
            spec = PathFinder.find_spec(candidate, [str(plugin_dir)])
            if spec is None:
                continue
            origin = spec.origin
            if origin is None:
                locations = spec.submodule_search_locations or ()
                try:
                    if any(
                        Path(location).resolve().is_relative_to(plugin_dir)
                        for location in locations
                    ):
                        roots.add(candidate)
                except OSError:
                    continue
                continue
            try:
                Path(origin).resolve().relative_to(plugin_dir)
            except (OSError, ValueError):
                continue
            roots.add(candidate)
        return roots

    @staticmethod
    def _record_module(record: PluginRecord, name: str, module: ModuleType) -> None:
        record.loaded_module_names.add(name)
        record.loaded_modules[name] = module

    def _capture_loaded_modules(
        self,
        record: PluginRecord,
        modules_before: dict[str, ModuleType],
    ) -> None:
        """记录 namespace 模块，并将本次 bare 本地导入迁入唯一命名空间。"""
        namespace = record.module_namespace
        prefix = f"{namespace}."
        plugin_dir = record.plugin_dir.resolve()
        for name, module in list(sys.modules.items()):
            if not isinstance(module, ModuleType):
                continue
            if name == namespace or name.startswith(prefix):
                self._record_module(record, name, module)
                continue
            if modules_before.get(name) is module:
                continue
            if not self._module_is_under(module, plugin_dir):
                continue

            alias = f"{namespace}.{name}"
            existing = sys.modules.get(alias)
            if existing is not None and existing is not module:
                alias = f"{namespace}.__legacy__.{name}"
            sys.modules[alias] = module
            self._record_module(record, alias, module)
            if sys.modules.get(name) is module:
                sys.modules.pop(name, None)

    def _load_entrypoint_module(
        self,
        record: PluginRecord,
        module_path: str,
    ) -> ModuleType:
        """在 UUID namespace 中加载入口，并隔离旧式 bare sibling import。"""
        plugin_dir = record.plugin_dir.resolve()
        entry_file, is_package = self._resolve_entrypoint_file(module_path, plugin_dir)
        namespace = self._new_namespace(record.manifest.id)
        record.module_namespace = namespace
        record.loaded_module_names.clear()
        record.loaded_modules.clear()

        modules_before = dict(sys.modules)
        path_before = list(sys.path)
        local_roots = self._local_module_roots(plugin_dir)
        evicted = {
            name: module
            for name, module in modules_before.items()
            if name.partition(".")[0] in local_roots
        }
        for name, module in evicted.items():
            if sys.modules.get(name) is module:
                sys.modules.pop(name, None)

        sys.path.insert(0, str(plugin_dir))
        try:
            self._install_namespace_package(namespace, plugin_dir)
            parts = module_path.split(".")
            for depth in range(1, len(parts)):
                package_name = ".".join((namespace, *parts[:depth]))
                package_dir = plugin_dir.joinpath(*parts[:depth])
                package_init = package_dir / "__init__.py"
                if package_init.is_file():
                    contained_init = self._contained_path(
                        package_init,
                        plugin_dir,
                        ".".join(parts[:depth]),
                    )
                    self._execute_module(package_name, contained_init, is_package=True)
                else:
                    self._install_namespace_package(package_name, package_dir)

            qualified_name = f"{namespace}.{module_path}"
            module = sys.modules.get(qualified_name)
            if module is None:
                module = self._execute_module(
                    qualified_name,
                    entry_file,
                    is_package=is_package,
                )
            return module
        finally:
            try:
                self._capture_loaded_modules(record, modules_before)
            finally:
                for name, module in evicted.items():
                    sys.modules[name] = module
                sys.path[:] = path_before

    def _refresh_record_module_names(self, record: PluginRecord) -> None:
        namespace = record.module_namespace
        if not namespace:
            return
        prefix = f"{namespace}."
        for name, module in list(sys.modules.items()):
            if isinstance(module, ModuleType) and (
                name == namespace or name.startswith(prefix)
            ):
                self._record_module(record, name, module)

    def _unload_record_modules(self, record: PluginRecord) -> None:
        """只移除当前记录拥有且对象身份仍匹配的模块名。"""
        self._refresh_record_module_names(record)
        for name in sorted(record.loaded_module_names, reverse=True):
            module = record.loaded_modules.get(name)
            if module is not None and sys.modules.get(name) is module:
                sys.modules.pop(name, None)
        record.loaded_module_names.clear()
        record.loaded_modules.clear()
        record.module_namespace = ""

    @staticmethod
    def _matches_version(version: str, constraint: str, field_name: str) -> None:
        """验证 PEP 440 版本约束；空串与依赖默认值 ``*`` 表示不限制。"""
        constraint = constraint.strip()
        if not constraint or constraint == "*":
            return
        try:
            matches = Version(version) in SpecifierSet(constraint)
        except (InvalidSpecifier, InvalidVersion) as exc:
            raise ValueError(f"Invalid {field_name} constraint {constraint!r}: {exc}") from exc
        if not matches:
            raise ValueError(
                f"Incompatible {field_name}: requires {constraint}, current version is {version}",
            )

    @classmethod
    def _validate_compatibility(cls, manifest: PluginManifest) -> None:
        cls._matches_version(
            HOST_VERSION,
            manifest.xiaoda_bot_version,
            "xiaoda_bot_version",
        )
        cls._matches_version(
            PLUGIN_SDK_VERSION,
            manifest.sdk_version,
            "sdk_version",
        )

    # ── Discovery ──
    def discover(self, search_paths: list[str | Path] | None = None) -> list[str]:
        """扫描并注册发现的插件"""
        discovered = discover_plugins(search_paths)
        new_ids = []
        for dp in discovered:
            pid = dp.manifest.id
            if pid not in self._plugins:
                self._plugins[pid] = PluginRecord(
                    manifest=dp.manifest,
                    plugin_dir=dp.plugin_dir,
                )
                new_ids.append(pid)
                logger.info("plugin.found", id=pid, path=str(dp.plugin_dir))
        return new_ids

    # ── Load ──
    async def load(self, plugin_id: str) -> bool:
        """加载插件：安全校验 → 动态导入 → 创建上下文 → 实例化"""
        record = self._plugins.get(plugin_id)
        if not record:
            logger.warning("plugin.not_found", id=plugin_id)
            return False
        if record.state in (PluginState.LOADED, PluginState.ENABLED):
            return True
        if record.state not in (PluginState.FOUND, PluginState.UNLOADED):
            logger.warning("plugin.invalid_state_for_load", id=plugin_id, state=record.state)
            return False

        try:
            manifest = record.manifest
            plugin_dir = record.plugin_dir
            self._validate_compatibility(manifest)

            # 安全校验：验证插件文件完整性（SHA256 hash）
            integrity_err = self._verify_integrity(plugin_id, plugin_dir)
            if integrity_err:
                record.state = PluginState.ERROR
                record.error_message = integrity_err
                logger.error("plugin.integrity_check_failed", id=plugin_id, error=integrity_err)
                return False

            # 解析入口格式 "module.path:ClassName"
            module_path, class_name = manifest.entrypoint.rsplit(":", 1)
            module = self._load_entrypoint_module(record, module_path)
            plugin_class = getattr(module, class_name)

            if not issubclass(plugin_class, Plugin):
                raise TypeError(f"{manifest.entrypoint} is not a Plugin subclass")

            # 创建权限检查器
            permissions = PermissionChecker(plugin_id, manifest.permissions)

            # 创建插件上下文
            context = PluginContext(
                manifest=manifest,
                permissions=permissions,
                tool_registry=self._tool_registry,
                hook_engine=self._hook_engine,
                memory_manager=self._memory,
                knowledge_graph=self._kg,
                mcp_manager=self._mcp,
                agent_core=self._agent_core,
            )

            # 实例化插件；先挂到记录上，确保 on_load 失败时能完整回收上下文。
            instance = plugin_class()
            instance.bind(context)
            record.instance = instance
            record.context = context
            await asyncio.wait_for(instance.on_load(), timeout=self.LIFECYCLE_TIMEOUT)

            record.state = PluginState.LOADED
            record.error_message = ""
            record._has_been_loaded = True
            logger.info("plugin.loaded", id=plugin_id)
            return True

        except TimeoutError:
            if record.context:
                record.context.clear_registrations()
            self._unload_record_modules(record)
            record.instance = None
            record.context = None
            record.state = PluginState.ERROR
            record.error_message = "Lifecycle timeout during on_load"
            logger.error("plugin.load_timeout", id=plugin_id)
            return False
        except Exception as e:
            if record.context:
                record.context.clear_registrations()
            self._unload_record_modules(record)
            record.instance = None
            record.context = None
            record.state = PluginState.ERROR
            record.error_message = str(e)
            logger.error("plugin.load_failed", id=plugin_id, error=str(e))
            return False

    # ── Enable ──
    async def enable(self, plugin_id: str) -> bool:
        """启用插件"""
        record = self._plugins.get(plugin_id)
        if not record or not record.instance:
            return False
        if record.state == PluginState.ENABLED:
            return True
        if record.state not in (PluginState.LOADED, PluginState.DISABLED):
            return False

        try:
            await asyncio.wait_for(record.instance.on_enable(), timeout=self.LIFECYCLE_TIMEOUT)
            try:
                record.instance.activate_registrations()
                if record.context:
                    record.context.activate_registrations()
                self._refresh_record_module_names(record)
            except Exception:
                if record.context:
                    record.context.clear_registrations()
                raise
            record.state = PluginState.ENABLED
            record.error_message = ""
            logger.info("plugin.enabled", id=plugin_id)
            return True

        except TimeoutError:
            if record.context:
                record.context.clear_registrations()
            record.state = PluginState.ERROR
            record.error_message = "Lifecycle timeout"
            logger.error("plugin.enable_timeout", id=plugin_id)
            return False
        except Exception as e:
            if record.context:
                record.context.clear_registrations()
            record.state = PluginState.ERROR
            record.error_message = str(e)
            logger.error("plugin.enable_failed", id=plugin_id, error=str(e))
            return False

    # ── Disable ──
    async def disable(self, plugin_id: str) -> bool:
        """禁用插件"""
        record = self._plugins.get(plugin_id)
        if not record or not record.instance:
            return False
        if record.state != PluginState.ENABLED:
            return False

        try:
            await asyncio.wait_for(record.instance.on_disable(), timeout=self.LIFECYCLE_TIMEOUT)
            if record.context:
                record.context.clear_registrations()
            record.state = PluginState.DISABLED
            logger.info("plugin.disabled", id=plugin_id)
            return True
        except Exception as e:
            if record.context:
                record.context.clear_registrations()
            record.state = PluginState.ERROR
            record.error_message = str(e)
            logger.error("plugin.disable_failed", id=plugin_id, error=str(e))
            return False

    # ── Unload ──
    async def unload(self, plugin_id: str) -> bool:
        """卸载插件"""
        record = self._plugins.get(plugin_id)
        if not record:
            return False

        try:
            if record.instance:
                if record.state == PluginState.ENABLED:
                    await self.disable(plugin_id)
                await asyncio.wait_for(record.instance.on_unload(), timeout=self.LIFECYCLE_TIMEOUT)

            if record.context:
                record.context.clear_registrations()

            self._unload_record_modules(record)

            record.instance = None
            record.context = None
            record.state = PluginState.UNLOADED
            record._has_been_loaded = False
            logger.info("plugin.unloaded", id=plugin_id)
            return True
        except Exception as e:
            record.state = PluginState.ERROR
            record.error_message = str(e)
            logger.error("plugin.unload_failed", id=plugin_id, error=str(e))
            return False

    # ── Reload ──
    async def reload(self, plugin_id: str) -> bool:
        """热重载插件"""
        record = self._plugins.get(plugin_id)
        if not record:
            return False
        was_enabled = record.state == PluginState.ENABLED
        await self.unload(plugin_id)
        # 重新解析 manifest
        yaml_path = record.plugin_dir / "plugin.yaml"
        if yaml_path.exists():
            from plugins.manifest import parse_manifest
            record.manifest = parse_manifest(yaml_path)
        if not await self.load(plugin_id):
            return False
        if was_enabled:
            return await self.enable(plugin_id)
        return True

    # ── Orchestration ──
    def _mark_error(self, plugin_id: str, message: str) -> None:
        record = self._plugins[plugin_id]
        record.state = PluginState.ERROR
        record.error_message = message
        logger.error("plugin.orchestration_failed", id=plugin_id, error=message)

    def _cycle_members(
        self,
        pending: set[str],
        dependencies: dict[str, set[str]],
    ) -> set[str]:
        """用强连通分量找出真实环成员，不包含仅依赖环的后继。"""
        index = 0
        indexes: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        cycle_members: set[str] = set()

        def visit(plugin_id: str) -> None:
            nonlocal index
            indexes[plugin_id] = index
            lowlinks[plugin_id] = index
            index += 1
            stack.append(plugin_id)
            on_stack.add(plugin_id)

            for dependency_id in dependencies[plugin_id] & pending:
                if dependency_id not in indexes:
                    visit(dependency_id)
                    lowlinks[plugin_id] = min(
                        lowlinks[plugin_id],
                        lowlinks[dependency_id],
                    )
                elif dependency_id in on_stack:
                    lowlinks[plugin_id] = min(
                        lowlinks[plugin_id],
                        indexes[dependency_id],
                    )

            if lowlinks[plugin_id] != indexes[plugin_id]:
                return
            component: set[str] = set()
            while stack:
                member = stack.pop()
                on_stack.remove(member)
                component.add(member)
                if member == plugin_id:
                    break
            if len(component) > 1 or plugin_id in dependencies[plugin_id]:
                cycle_members.update(component)

        for plugin_id in pending:
            if plugin_id not in indexes:
                visit(plugin_id)
        return cycle_members

    def _dependency_order(self) -> list[str]:
        """按阶段优先、同阶段稳定发现顺序生成依赖拓扑序。"""
        discovery_index = {plugin_id: index for index, plugin_id in enumerate(self._plugins)}
        invalid: set[str] = set()
        dependencies: dict[str, set[str]] = {}
        dependents: dict[str, set[str]] = {plugin_id: set() for plugin_id in self._plugins}

        for plugin_id, record in self._plugins.items():
            declared = {dependency.id for dependency in record.manifest.depends_on}
            missing = sorted(declared - self._plugins.keys())
            if missing:
                self._mark_error(
                    plugin_id,
                    f"Missing plugin dependencies: {', '.join(missing)}",
                )
                invalid.add(plugin_id)
            dependencies[plugin_id] = declared & self._plugins.keys()
            for dependency_id in dependencies[plugin_id]:
                dependents[dependency_id].add(plugin_id)

        pending = set(self._plugins)
        ready = [plugin_id for plugin_id in pending if not dependencies[plugin_id]]
        ordered: list[str] = []
        while ready:
            ready.sort(
                key=lambda plugin_id: (
                    _LOAD_PHASE_ORDER[self._plugins[plugin_id].manifest.load_phase],
                    discovery_index[plugin_id],
                ),
            )
            plugin_id = ready.pop(0)
            if plugin_id not in pending:
                continue
            pending.remove(plugin_id)
            if plugin_id not in invalid:
                ordered.append(plugin_id)
            for dependent_id in dependents[plugin_id]:
                dependencies[dependent_id].discard(plugin_id)
                if dependent_id in pending and not dependencies[dependent_id]:
                    ready.append(dependent_id)

        if pending:
            cycle_members = self._cycle_members(pending, dependencies)
            cycle_ids = sorted(cycle_members, key=discovery_index.get)
            if cycle_ids:
                message = f"Plugin dependency cycle detected: {', '.join(cycle_ids)}"
                for plugin_id in cycle_ids:
                    self._mark_error(plugin_id, message)
            for plugin_id in sorted(pending - cycle_members, key=discovery_index.get):
                unavailable = sorted(
                    dependencies[plugin_id] & pending,
                    key=discovery_index.get,
                )
                self._mark_error(
                    plugin_id,
                    f"Plugin dependency unavailable: {', '.join(unavailable)}",
                )
        return ordered

    def _validate_dependencies(self, plugin_id: str) -> str | None:
        record = self._plugins[plugin_id]
        for dependency in record.manifest.depends_on:
            dependency_record = self._plugins.get(dependency.id)
            if dependency_record is None:
                return f"Missing plugin dependency: {dependency.id}"
            if dependency_record.state != PluginState.ENABLED:
                return (
                    f"Plugin dependency {dependency.id} is not enabled "
                    f"(state={dependency_record.state.value})"
                )
            try:
                self._matches_version(
                    dependency_record.manifest.version,
                    dependency.version,
                    f"dependency {dependency.id}",
                )
            except ValueError as exc:
                return str(exc)
        return None

    async def load_and_enable_all(self) -> dict[str, bool]:
        """验证依赖并按阶段化拓扑序加载、启用全部已发现插件。"""
        statuses = {plugin_id: False for plugin_id in self._plugins}
        for plugin_id in self._dependency_order():
            record = self._plugins[plugin_id]
            dependency_error = self._validate_dependencies(plugin_id)
            if dependency_error:
                self._mark_error(plugin_id, dependency_error)
                continue

            if record.state in (PluginState.FOUND, PluginState.UNLOADED):
                if not await self.load(plugin_id):
                    continue
            if record.state in (PluginState.LOADED, PluginState.DISABLED):
                if not await self.enable(plugin_id):
                    continue
            statuses[plugin_id] = record.state == PluginState.ENABLED
        return statuses

    # ── Shutdown ──
    async def shutdown_all(self) -> None:
        """逆序关闭所有插件"""
        for plugin_id in reversed(list(self._plugins.keys())):
            record = self._plugins[plugin_id]
            if record.state in (PluginState.ENABLED, PluginState.LOADED):
                await self.unload(plugin_id)

    # ── Query ──
    def get_plugin(self, plugin_id: str) -> PluginRecord | None:
        return self._plugins.get(plugin_id)

    def list_plugins(self) -> list[PluginRecord]:
        return list(self._plugins.values())

    def _config_path(self, plugin_id: str) -> Path:
        return config.PLUGINS_CONFIG_DIR / plugin_id / "config.json"

    def get_plugin_config(self, plugin_id: str) -> dict:
        """获取插件配置"""
        record = self._plugins.get(plugin_id)
        if not record:
            return {}
        config_path = self._config_path(plugin_id)
        if config_path.exists():
            try:
                return json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                logger.debug("plugin_manager.config_load_failed", exc_info=True)
        return dict(record.manifest.config)

    def set_plugin_config(self, plugin_id: str, config: dict) -> None:
        """保存插件配置"""
        config_path = self._config_path(plugin_id)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(config, ensure_ascii=False, indent=2)
        if atomic_write is not None:
            atomic_write(config_path, payload, encoding="utf-8")
        else:
            config_path.write_text(payload, encoding="utf-8")
# ── 活动实例注册点（依赖倒置）────────────────────────────────
# PluginManager 由 web 层 lifespan 创建；core/bootstrap 需要启用插件但不得
# 反向 import web.server（跨层倒挂，技术债 P1-3）。双方都只依赖本模块的
# set/get：web 创建后 set，bootstrap 经 get 获取。未注册（如纯 CLI、测试）
# 返回 None，调用方自行降级。
_active_plugin_manager: "PluginManager | None" = None


def set_active_plugin_manager(pm: "PluginManager | None") -> None:
    global _active_plugin_manager
    _active_plugin_manager = pm


def get_active_plugin_manager() -> "PluginManager | None":
    return _active_plugin_manager
