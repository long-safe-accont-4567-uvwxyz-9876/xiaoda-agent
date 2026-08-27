"""桥接 shim — 实现已下沉 core_runtime/prompt_ab.py（H1 分层下沉 2026-08-27）。

本模块所有属性读写（含测试 monkeypatch 的符号如 resolve_and_pin/_cache）
实时转发 core_runtime 真身：普通 from-import 快照语义会让 web 层与
core_runtime 名字分叉，patch 失效。新代码请直接 import core_runtime.prompt_ab。
"""
from types import ModuleType

from core_runtime import prompt_ab as _impl


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
