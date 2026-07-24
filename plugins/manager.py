"""插件管理器 — 生命周期管理 + 状态机 + 安全校验"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger

from plugins.context import PluginContext
from plugins.discovery import discover_plugins
from plugins.manifest import PluginManifest
from plugins.permissions import PermissionChecker
from plugins.sdk import Plugin


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
    _has_been_loaded: bool = False


class PluginManager:
    """插件生命周期管理器"""

    LIFECYCLE_TIMEOUT = 60  # seconds
    # 信任存储路径（存储已知插件的 SHA256 hash）
    _TRUST_STORE_FILE = Path("config/plugins/trust_store.json")

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

    @classmethod
    def _load_trust_store(cls) -> dict[str, str]:
        """加载信任存储 {plugin_id: expected_sha256}。"""
        try:
            if cls._TRUST_STORE_FILE.exists():
                return json.loads(cls._TRUST_STORE_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("plugin_manager.trust_store_load_failed", exc_info=True)
        return {}

    @classmethod
    def _save_trust_store(cls, store: dict[str, str]) -> None:
        """保存信任存储。"""
        try:
            cls._TRUST_STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
            cls._TRUST_STORE_FILE.write_text(
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
        if record.state not in (PluginState.FOUND, PluginState.UNLOADED):
            logger.warning("plugin.invalid_state_for_load", id=plugin_id, state=record.state)
            return False

        try:
            manifest = record.manifest
            plugin_dir = record.plugin_dir

            # 安全校验：验证插件文件完整性（SHA256 hash）
            integrity_err = self._verify_integrity(plugin_id, plugin_dir)
            if integrity_err:
                record.state = PluginState.ERROR
                record.error_message = integrity_err
                logger.error("plugin.integrity_check_failed", id=plugin_id, error=integrity_err)
                return False

            # Add plugin dir to sys.path for import
            if str(plugin_dir) not in sys.path:
                sys.path.insert(0, str(plugin_dir))

            # Parse entrypoint "module.path:ClassName"
            module_path, class_name = manifest.entrypoint.rsplit(":", 1)
            module = importlib.import_module(module_path)
            plugin_class = getattr(module, class_name)

            if not issubclass(plugin_class, Plugin):
                raise TypeError(f"{manifest.entrypoint} is not a Plugin subclass")

            # Create permissions checker
            permissions = PermissionChecker(plugin_id, manifest.permissions)

            # Create plugin context
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

            # Instantiate plugin
            instance = plugin_class()
            instance.bind(context)

            # Bind decorator registrations
            instance.activate_registrations()

            record.instance = instance
            record.context = context
            record.state = PluginState.LOADED
            logger.info("plugin.loaded", id=plugin_id)
            return True

        except Exception as e:
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
        if record.state not in (PluginState.LOADED, PluginState.DISABLED):
            return False

        try:
            # Call on_load only once
            if not record._has_been_loaded:
                await asyncio.wait_for(record.instance.on_load(), timeout=self.LIFECYCLE_TIMEOUT)
                record._has_been_loaded = True

            await asyncio.wait_for(record.instance.on_enable(), timeout=self.LIFECYCLE_TIMEOUT)
            record.state = PluginState.ENABLED
            logger.info("plugin.enabled", id=plugin_id)
            return True

        except TimeoutError:
            record.state = PluginState.ERROR
            record.error_message = "Lifecycle timeout"
            logger.error("plugin.enable_timeout", id=plugin_id)
            return False
        except Exception as e:
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

            # Remove from sys.modules (non-builtin only)
            module_path = record.manifest.entrypoint.rsplit(":", 1)[0]
            keys_to_remove = [k for k in sys.modules if k.startswith(module_path.split(".")[0])]
            for k in keys_to_remove:
                if not k.startswith("plugins."):  # Don't remove our own modules
                    del sys.modules[k]

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
        # Re-parse manifest
        yaml_path = record.plugin_dir / "plugin.yaml"
        if yaml_path.exists():
            from plugins.manifest import parse_manifest
            record.manifest = parse_manifest(yaml_path)
        if not await self.load(plugin_id):
            return False
        if was_enabled:
            return await self.enable(plugin_id)
        return True

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

    def get_plugin_config(self, plugin_id: str) -> dict:
        """获取插件配置"""
        record = self._plugins.get(plugin_id)
        if not record:
            return {}
        import json
        # 使用用户目录下的插件配置目录，避免相对路径依赖 CWD
        try:
            from config import PLUGINS_CONFIG_DIR
            config_path = PLUGINS_CONFIG_DIR / plugin_id / "config.json"
        except ImportError:
            config_path = Path(f"config/plugins/{plugin_id}/config.json")
        if config_path.exists():
            try:
                return json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                logger.debug("plugin_manager.config_load_failed", exc_info=True)
        return dict(record.manifest.config)

    def set_plugin_config(self, plugin_id: str, config: dict) -> None:
        """保存插件配置"""
        import json
        try:
            from config import PLUGINS_CONFIG_DIR
            config_path = PLUGINS_CONFIG_DIR / plugin_id / "config.json"
        except ImportError:
            config_path = Path(f"config/plugins/{plugin_id}/config.json")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
