"""Python 版本兼容垫片 —— 必须在任何第三方库导入前生效。

## 为什么需要这个模块

Python 3.12 移除了 `pkgutil.ImpImporter`。而 `setuptools < 81` 提供的
`pkg_resources` 在模块级会执行：

    register_finder(pkgutil.ImpImporter, find_on_path)
    register_namespace_handler(pkgutil.ImpImporter, file_ns_handler)

属性缺失时抛 `AttributeError`。麻烦在于很多老库写的是：

    try:
        import pkg_resources
    except ImportError:
        ...降级...

`AttributeError` 穿透 `except ImportError` 保护，导致**整个库导入失败**。

典型受害者是 jieba（`jieba/_compat.py` 就这么写的），而 jieba 是
本项目分词 / 关键词 / 实体抽取的基础依赖，一旦崩掉会连带
`memory.key_extractor`、`memory.concept_graph` 等模块全部不可用。

## 什么环境会踩

- Python 3.12 / 3.13，且 `setuptools < 81`（旧版仍带 pkg_resources）
- Python 3.12+ 新建的 venv 默认**不预装** setuptools，但系统
  `dist-packages` 里可能残留一个旧版 pkg_resources 目录 —— 于是
  `import pkg_resources` 能命中这个坏掉的旧版，比完全没装更糟

## 怎么做

给 `pkgutil` 补一个占位 `ImpImporter`。`pkg_resources` 只把它当
字典键使用（`register_finder` / `register_namespace_handler`），不做
实例化、不做 isinstance 判断，所以占位类是安全的。现代 import 系统
走 PEP 451 meta path finders，不会遍历 `pkgutil` 的旧 finder 链，
这个占位不会被真正调用。

## 生效范围

在 `agent.py` / `web/server.py` / `cli.py` / `memory/key_extractor.py`
的导入区最前面执行。另外各 jieba 调用点的 `except ImportError` 已扩为
`except (ImportError, AttributeError)`，作为垫片失效时的降级兜底。
"""
from __future__ import annotations

import pkgutil


class _ImpImporterPlaceholder:
    """`pkgutil.ImpImporter` 在 Python 3.12+ 的占位实现。

    仅用于让旧版 `pkg_resources` 完成 finder 注册，不参与任何真实
    导入流程。
    """

    def __init__(self, *args, **kwargs) -> None:  # noqa: D107
        raise ImportError(
            "pkgutil.ImpImporter 在 Python 3.12+ 已移除；"
            "这是兼容占位类，不应被实例化"
        )


def ensure_pkgutil_impimporter() -> bool:
    """补齐 `pkgutil.ImpImporter`。

    Returns:
        True 表示本次补了占位（说明运行在 Python 3.12+ 且环境可能踩坑），
        False 表示已存在或无需处理。
    """
    if hasattr(pkgutil, "ImpImporter"):
        return False
    pkgutil.ImpImporter = _ImpImporterPlaceholder  # type: ignore[attr-defined]
    return True


# 导入即生效
ensure_pkgutil_impimporter()


__all__ = ["ensure_pkgutil_impimporter"]
