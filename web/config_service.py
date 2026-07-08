"""ConfigService — Web UI 可改配置的统一存取层（R8/R22）。

所有 UI 修改的配置写入 config/webui_overrides.json（原子写盘），
并立即触发注册的回调使内存对象热生效。密钥永不进入此文件。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from collections.abc import Callable

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
    "tts": {"auto_speak": False, "default_voice": "xiaoda"},
    "ui": {"particles": "medium", "tilt3d": True},
    "tools": {},      # {tool_name: {"enabled": false, "max_frequency": 5}}
    "mcp": {},        # {server_name: {command, args, env, agents, enabled}} 用户新增的
    "models": {"providers": {}, "routes": {}},
    "dashboard": {"system_monitor_enabled": False},
    "mail": {
        "enabled": False,
        "mode": "off",  # off / allowlist / all
        "allowed_senders": [],
        "reply_channel": "mail",  # mail / mail_and_qq
        "max_per_day": 50,
        "dnd_start": 0,  # 免打扰开始小时（0-23），0=不启用 DND
        "dnd_end": 0,    # 免打扰结束小时（0-23），与 dnd_start 相同=不启用
    },
}


class ConfigService:
    def __init__(self, path: Path | None = None) -> None:
        """初始化配置服务, 加载已存在的覆盖文件.

        Args:
            path: 覆盖配置文件路径, None 表示使用默认路径
        """
        self._path = path or _get_overrides_path()
        self._lock = threading.Lock()
        self._data: dict[str, Any] = json.loads(json.dumps(_DEFAULTS))
        self._watchers: dict[str, list[Callable[[Any], None]]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                saved = json.loads(self._path.read_text(encoding="utf-8"))
                self._deep_merge(self._data, saved)
            except Exception as e:
                logger.warning("config_service.load_failed error={}", str(e))

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> None:
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                ConfigService._deep_merge(base[k], v)
            else:
                base[k] = v

    def get(self, path: str, default: Any = None) -> Any:
        """按点分路径读取配置值.

        Args:
            path: 点分路径 (如 "schedule.enabled")
            default: 路径不存在时的默认返回值

        Returns:
            配置值或 default
        """
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value: Any) -> None:
        """按点分路径写入配置值, 落盘并通知 watcher.

        Args:
            path: 点分路径
            value: 新值
        """
        with self._lock:
            parts = path.split(".")
            node = self._data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
            self._save()
        self._notify(path, value)

    def delete(self, path: str) -> None:
        """按点分路径删除配置项, 落盘并通知 watcher."""
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

    def _save(self) -> None:
        try:
            from utils.atomic_write import atomic_write
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self._path, json.dumps(self._data, ensure_ascii=False, indent=2))
        except Exception:
            logger.debug("config_service.atomic_write_fallback", exc_info=True)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    def watch(self, prefix: str, callback: Callable[[Any], None]) -> None:
        """注册监听器, 路径前缀匹配时回调.

        Args:
            prefix: 点分路径前缀
            callback: 值变更回调函数
        """
        self._watchers.setdefault(prefix, []).append(callback)

    def _notify(self, path: str, value: Any) -> None:
        for prefix, cbs in self._watchers.items():
            if path.startswith(prefix):
                for cb in cbs:
                    try:
                        cb(value)
                    except Exception as e:
                        logger.warning("config_service.watcher_error prefix={} error={}", prefix, str(e))


_instance: ConfigService | None = None
_instance_lock = threading.Lock()


def get_config_service() -> ConfigService:
    """获取全局 ConfigService 单例."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = ConfigService()
    return _instance
