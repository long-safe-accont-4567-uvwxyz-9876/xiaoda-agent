"""桥接 shim — 实现已下沉 core_runtime/config_service.py（H1 分层下沉 2026-08-27）。

本模块所有属性读写（含测试 monkeypatch 目标 `_instance`）实时转发
core_runtime 真身：普通 from-import 的快照语义会让 web 层与 core_runtime
的名字分叉，曾致 prompt_profile_api 等 4 个测试 patch 失效。
新代码请直接 from core_runtime.config_service import ...。
"""
from types import ModuleType

from core_runtime import config_service as _impl


class _ForwardModule(ModuleType):
    """属性读写双向转发到真身模块的 shim。"""

    def __getattr__(self, name: str):
        return getattr(_impl, name)

    def __setattr__(self, name: str, value) -> None:
        setattr(_impl, name, value)

    def __delattr__(self, name: str) -> None:
        delattr(_impl, name)


import sys as _sys  # noqa: E402

_sys.modules[__name__].__class__ = _ForwardModule
