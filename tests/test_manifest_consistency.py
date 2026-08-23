"""manifest ↔ 装饰器元数据一致性守卫（技术债 P0-2）。

``tools/_builtin_manifest.py`` 手工复制各工具模块 ``@register_tool`` 的元数据
供冷启动懒加载。两份事实源一旦漂移：懒加载路径对外暴露旧 description/schema，
甚至丢失 ``requires_confirmation`` 确认门禁（web/routers/tools.py 的 X-Confirm
检查读的就是注册表里的这个字段）。

本测试把 manifest 声明的全部模块真实 import（触发装饰器注册），逐条比对
"懒注册生效值"。比较基准与 ``register_builtin_tools_lazy()`` 完全一致：
缺失字段取 ``register_lazy_tool`` 的默认值；permission 按枚举值比较；
schema 中的路径字符串两侧先做 ``expanduser``（装饰器侧的默认值来自
运行时展开，如 system_tools._DEFAULT_PROJECT_DIR）。

import 顺序刻意用 manifest 声明序：web_browse 在 web_tools_v2 与
web_browse_enhanced 双重注册、后者覆写前者（见 manifest 文件头 docstring），
声明序保证最终注册表状态与 manifest 记载的生效版本一致。
"""
from __future__ import annotations

import importlib
import os

import pytest

import tool_engine  # noqa: F401 — 包导入即触发 register_builtin_tools_lazy()
from tool_engine.tool_registry import ToolPermission, _tools
from tools._builtin_manifest import BUILTIN_TOOLS

_LAZY_DEFAULTS = {"category": "general", "max_frequency": 10, "requires_confirmation": False}
_COMPARE_FIELDS = (
    "description",
    "schema",
    "permission",
    "category",
    "max_frequency",
    "requires_confirmation",
)


def _expanduser_deep(value):
    """递归展开字符串中的 ~，复现装饰器侧运行时路径求值。"""
    if isinstance(value, str):
        return os.path.expanduser(value)
    if isinstance(value, dict):
        return {k: _expanduser_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expanduser_deep(v) for v in value]
    return value


def _effective(entry: dict, field: str):
    """manifest 条目经 register_lazy_tool 默认值补全后的生效值。"""
    value = entry.get(field, _LAZY_DEFAULTS.get(field))
    if isinstance(value, ToolPermission):
        return value.value
    if field == "schema":
        return _expanduser_deep(value)
    return value


@pytest.fixture(scope="module", autouse=True)
def _import_all_declared_tool_modules():
    seen: list[str] = []
    for entry in BUILTIN_TOOLS:
        module_path = entry["module_path"]
        if module_path not in seen:
            seen.append(module_path)
            importlib.import_module(module_path)


def test_manifest_names_unique() -> None:
    names = [e["name"] for e in BUILTIN_TOOLS]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"manifest 存在重复工具名: {sorted(duplicates)}"


def test_every_manifest_tool_registered_by_decorator() -> None:
    missing = [e["name"] for e in BUILTIN_TOOLS if e["name"] not in _tools]
    assert missing == [], f"manifest 声明了但没有任何装饰器注册的工具: {missing}"


def test_manifest_metadata_matches_decorators() -> None:
    drifts: list[str] = []
    for entry in BUILTIN_TOOLS:
        name = entry["name"]
        real = _tools[name]
        for field in _COMPARE_FIELDS:
            expected = real.get(field)
            if isinstance(expected, ToolPermission):
                expected = expected.value
            actual = _effective(entry, field)
            if field == "schema":
                expected = _expanduser_deep(expected)
            if actual != expected:
                drifts.append(
                    f"{name}.{field}\n  manifest : {actual!r}\n  decorator: {expected!r}"
                )
    assert not drifts, (
        "manifest 与 @register_tool 元数据漂移 "
        f"({len(drifts)} 处)。请以装饰器为准同步 _builtin_manifest.py:\n"
        + "\n".join(drifts)
    )


def test_no_decorator_builtin_missing_from_manifest() -> None:
    """装饰器注册的生产 builtin 工具必须进 manifest，否则懒启动阶段不可见。

    只检查 func 定义在生产模块（tools.*/memory.*）的工具：测试进程里其他
    用例可能向全局注册表塞入 builtin 来源的假工具，按定义模块过滤隔离。
    """
    from tool_engine.tool_registry import register_builtin_tools_lazy

    register_builtin_tools_lazy()
    manifest_names = {e["name"] for e in BUILTIN_TOOLS}
    undecorated = [
        name
        for name, tool in _tools.items()
        if tool.get("source") == "builtin"
        and not tool.get("_lazy")
        and name not in manifest_names
        and getattr(tool.get("func"), "__module__", "").startswith(("tools.", "memory."))
    ]
    assert undecorated == [], f"装饰器注册了但 manifest 缺失的工具: {undecorated}"
