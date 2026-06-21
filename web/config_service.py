"""ConfigService — Web UI 可改配置的统一存取层（R8/R22）。

所有 UI 修改的配置写入 config/webui_overrides.json（原子写盘），
并立即触发注册的回调使内存对象热生效。密钥永不进入此文件。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

from loguru import logger


def _get_overrides_path() -> Path:
    from config import get_config_dir
    return get_config_dir() / "webui_overrides.json"

_DEFAULTS: dict[str, Any] = {
    "schedule": {
        "enabled": True,
        "greeting_max_per_day": 3,
        "dnd_periods": [{"start": "23:00", "end": "08:00"}],
    },
    "tts": {"auto_speak": False, "default_voice": "nahida"},
    "ui": {"particles": "medium", "tilt3d": True},
    "tools": {},      # {tool_name: {"enabled": false, "max_frequency": 5}}
    "mcp": {},        # {server_name: {command, args, env, agents, enabled}} 用户新增的
    "models": {"providers": {}, "routes": {}},
    "dashboard": {"system_monitor_enabled": False},
}


class ConfigService:
    def __init__(self, path: Path | None = None):
        self._path = path or _get_overrides_path()
        self._lock = threading.Lock()
        self._data: dict[str, Any] = json.loads(json.dumps(_DEFAULTS))
        self._watchers: dict[str, list[Callable[[Any], None]]] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                saved = json.loads(self._path.read_text(encoding="utf-8"))
                self._deep_merge(self._data, saved)
            except Exception as e:
                logger.warning("config_service.load_failed error={}", str(e))

    @staticmethod
    def _deep_merge(base: dict, override: dict):
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                ConfigService._deep_merge(base[k], v)
            else:
                base[k] = v

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value: Any):
        with self._lock:
            parts = path.split(".")
            node = self._data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
            self._save()
        self._notify(path, value)

    def delete(self, path: str):
        with self._lock:
            parts = path.split(".")
            node = self._data
            for part in parts[:-1]:
                if part not in node:
                    return
                node = node[part]
            node.pop(parts[-1], None)
            self._save()
        self._notify(path, None)

    def _save(self):
        try:
            from utils.atomic_write import atomic_write
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self._path, json.dumps(self._data, ensure_ascii=False, indent=2))
        except Exception:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    def watch(self, prefix: str, callback: Callable[[Any], None]):
        self._watchers.setdefault(prefix, []).append(callback)

    def _notify(self, path: str, value: Any):
        for prefix, cbs in self._watchers.items():
            if path.startswith(prefix):
                for cb in cbs:
                    try:
                        cb(value)
                    except Exception as e:
                        logger.warning("config_service.watcher_error prefix={} error={}", prefix, str(e))


_instance: ConfigService | None = None


def get_config_service() -> ConfigService:
    global _instance
    if _instance is None:
        _instance = ConfigService()
    return _instance
