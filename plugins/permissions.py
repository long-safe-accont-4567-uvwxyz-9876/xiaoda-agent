"""插件权限系统 — 声明式白名单 + 运行时检查"""
from __future__ import annotations

import fnmatch

from plugins.manifest import PluginPermissions


class PermissionDenied(Exception):
    """权限被拒绝"""
    def __init__(self, plugin_id: str, permission: str, detail: str = "") -> None:
        self.plugin_id = plugin_id
        self.permission = permission
        self.detail = detail
        super().__init__(f"Plugin '{plugin_id}' denied {permission}: {detail}")


class PermissionChecker:
    """运行时权限检查器"""

    def __init__(self, plugin_id: str, permissions: PluginPermissions) -> None:
        self._plugin_id = plugin_id
        self._permissions = permissions

    # ── Network ──
    def check_network_outbound(self, url: str) -> None:
        allowed = self._permissions.network.outbound
        if not allowed:
            raise PermissionDenied(self._plugin_id, "network.outbound", url)
        if "*" not in allowed and not any(fnmatch.fnmatch(url, p) for p in allowed):
            raise PermissionDenied(self._plugin_id, "network.outbound", url)

    def check_network_inbound(self) -> None:
        if not self._permissions.network.inbound:
            raise PermissionDenied(self._plugin_id, "network.inbound")

    # ── Filesystem ──
    def check_filesystem_read(self, zone: str) -> None:
        allowed = self._permissions.filesystem.read
        if not allowed or zone not in allowed:
            raise PermissionDenied(self._plugin_id, "filesystem.read", zone)

    def check_filesystem_write(self, zone: str) -> None:
        allowed = self._permissions.filesystem.write
        if not allowed or zone not in allowed:
            raise PermissionDenied(self._plugin_id, "filesystem.write", zone)

    # ── Memory ──
    def check_memory_read(self) -> None:
        if not self._permissions.memory.read:
            raise PermissionDenied(self._plugin_id, "memory.read")

    def check_memory_write(self) -> None:
        if not self._permissions.memory.write:
            raise PermissionDenied(self._plugin_id, "memory.write")

    # ── Plugin Data ──
    def check_plugin_data_read(self) -> None:
        if not self._permissions.plugin_data.read:
            raise PermissionDenied(self._plugin_id, "plugin_data.read")

    def check_plugin_data_write(self) -> None:
        if not self._permissions.plugin_data.write:
            raise PermissionDenied(self._plugin_id, "plugin_data.write")

    # ── System ──
    def check_subprocess(self) -> None:
        if not self._permissions.system.subprocess:
            raise PermissionDenied(self._plugin_id, "system.subprocess")

    def check_env_var(self, key: str) -> None:
        patterns = self._permissions.system.env_vars
        if not patterns or not any(fnmatch.fnmatch(key, p) for p in patterns):
            raise PermissionDenied(self._plugin_id, "system.env_vars", key)

    # ── LLM ──
    def check_llm_access(self) -> None:
        if not self._permissions.llm_access:
            raise PermissionDenied(self._plugin_id, "llm_access")

    # ── Event ──
    def check_event_emit(self, event_name: str) -> None:
        _allowed = self._permissions.capabilities.emits if hasattr(self._permissions, 'capabilities') else []
