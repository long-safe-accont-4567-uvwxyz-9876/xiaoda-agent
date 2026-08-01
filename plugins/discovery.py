"""插件发现兼容层 — 精简版"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plugins.manifest import PluginManifest


@dataclass
class DiscoveredPlugin:
    """已发现插件 (兼容层)"""
    manifest: PluginManifest
    plugin_dir: Path


def discover_plugins(search_paths: list[str | Path] | None = None) -> list[DiscoveredPlugin]:
    """扫描并返回已发现的插件列表 (兼容层)"""
    return []
