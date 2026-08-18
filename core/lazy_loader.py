"""懒加载包装器 — 首次访问时才真正 import 和初始化

冷启动优化: 启动时只初始化核心组件, 非核心组件按需加载。
"""
import importlib
from typing import Any

from loguru import logger


class LazyLoader:
    """懒加载包装器 — 首次访问时才真正 import 和初始化"""

    def __init__(self, import_path: str, init_args: dict | None = None) -> None:
        self._path = import_path
        self._args = init_args or {}
        self._instance = None
        self._loaded = False

    def __getattr__(self, name: Any) -> Any:
        if self._instance is None:
            self._load()
        return getattr(self._instance, name)

    def _load(self) -> None:
        """实际加载"""
        if self._loaded:
            return
        module_path, class_name = self._path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        self._instance = cls(**self._args)
        self._loaded = True
        logger.debug(f"LazyLoader: 已加载 {self._path}")

    @property
    def is_loaded(self) -> bool:
        """返回目标模块是否已完成首次加载."""
        return self._loaded

    def preload(self) -> None:
        """预加载"""
        if not self._loaded:
            self._load()


def lazy_import(import_path: str) -> Any:
    """惰性导入模块 — 返回一个代理,首次访问时才真正 import"""
    return LazyLoader(import_path)
