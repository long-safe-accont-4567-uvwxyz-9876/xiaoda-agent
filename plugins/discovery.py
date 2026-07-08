"""插件发现机制"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from plugins.manifest import PluginManifest, parse_manifest


@dataclass
class DiscoveredPlugin:
    manifest: PluginManifest
    plugin_dir: Path
    yaml_path: Path


def discover_plugins(search_paths: list[str | Path] | None = None) -> list[DiscoveredPlugin]:
    """扫描目录发现插件"""
    if search_paths is None:
        search_paths = [Path(__file__).parent]

    results: list[DiscoveredPlugin] = []
    for search_path in search_paths:
        sp = Path(search_path)
        if not sp.is_dir():
            continue
        for child in sorted(sp.iterdir()):
            if not child.is_dir():
                continue
            yaml_path = child / "plugin.yaml"
            if not yaml_path.is_file():
                continue
            try:
                manifest = parse_manifest(yaml_path)
                results.append(DiscoveredPlugin(
                    manifest=manifest,
                    plugin_dir=child,
                    yaml_path=yaml_path,
                ))
                logger.info("plugin.discovered", id=manifest.id, path=str(child))
            except Exception as e:
                logger.warning("plugin.manifest_invalid", path=str(yaml_path), error=str(e))
    return results
