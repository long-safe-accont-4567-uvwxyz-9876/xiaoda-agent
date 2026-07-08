"""插件测试工具 — MockPluginContext"""
from __future__ import annotations

from typing import Any
from collections.abc import Callable

from plugins.manifest import MemoryPermission, PluginDataPermission, PluginManifest, PluginPermissions
from plugins.permissions import PermissionChecker
from plugins.context import PluginContext


class MockPluginContext(PluginContext):
    """用于测试的 Mock PluginContext"""

    def __init__(self, plugin_id: str = "test-plugin") -> None:
        manifest = PluginManifest(
            id=plugin_id,
            name="Test Plugin",
            version="0.0.1",
            entrypoint="test:TestPlugin",
            permissions=PluginPermissions(
                memory=MemoryPermission(read=True, write=True),
                plugin_data=PluginDataPermission(read=True, write=True),
                llm_access=True,
            ),
        )
        permissions = PermissionChecker(plugin_id, manifest.permissions)
        super().__init__(manifest, permissions)
        self.tool_calls: list[dict] = []
        self.event_subscriptions: list[str] = []

    def register_tool(self, name: str, handler: Callable, **kwargs: Any) -> None:
        self.tool_calls.append({"action": "register_tool", "name": name})

    def subscribe(self, event_type: str, handler: Callable) -> None:
        self.event_subscriptions.append(event_type)
