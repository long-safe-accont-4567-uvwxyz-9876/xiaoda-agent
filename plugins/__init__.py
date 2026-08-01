"""插件系统"""
from plugins.manifest import PluginManifest, parse_manifest
from plugins.manager import PluginManager, PluginState, PluginRecord

__all__ = [
    "PluginManager",
    "PluginManifest",
    "PluginRecord",
    "PluginState",
    "parse_manifest",
]
