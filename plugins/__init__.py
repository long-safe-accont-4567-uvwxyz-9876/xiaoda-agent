"""插件系统"""
from plugins.manifest import PluginManifest, parse_manifest
from plugins.discovery import discover_plugins, DiscoveredPlugin
from plugins.manager import PluginManager, PluginState, PluginRecord

__all__ = [
    "DiscoveredPlugin",
    "PluginManager",
    "PluginManifest",
    "PluginRecord",
    "PluginState",
    "discover_plugins",
    "parse_manifest",
]
