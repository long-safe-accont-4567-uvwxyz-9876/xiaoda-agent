"""插件系统"""
from plugins.discovery import DiscoveredPlugin, discover_plugins
from plugins.manager import PluginManager, PluginRecord, PluginState
from plugins.manifest import PluginManifest, parse_manifest

__all__ = [
    "DiscoveredPlugin",
    "PluginManager",
    "PluginManifest",
    "PluginRecord",
    "PluginState",
    "discover_plugins",
    "parse_manifest",
]
