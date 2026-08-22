#!/usr/bin/env python3
"""扫描项目源码中的动态导入目标，生成 PyInstaller 打包校验清单（每行一个模块）。

背景（rust_hybrid 契约漂移同族问题）：try/except 守卫导入与字符串拼接导入
对 PyInstaller 静态分析不可见，漏打包时运行期静默走降级分支（如 CLI 命令
面板/菜单失效、本地 embed 回退远程 API），构建期没有任何报错。本脚本把
"期望被打进包的模块"从代码自动提取，替代 build-release.sh 里手工维护的
硬编码白名单——今后新增守卫式导入会被自动纳入校验，无需改两处。

提取的字面量模式：
    importlib.import_module("X")   /   __import__("X")   /   LazyLoader("X")

f-string / 字符串拼接式动态导入无法静态提取（如 db.db_{attr}、
plugins.{plugin_id}.{module}），这些目标由对应包的常规静态导入兜底
（spec hiddenimports 已显式列出 db.* 与 plugins.* 全量子模块）；
真正需要基线兜底的条目维护在下方 MANUAL_BASELINE。

输出约定：stdout 仅输出模块名清单；诊断信息一律走 stderr。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 扫描范围排除：非运行时代码（测试/构建脚本/文档/产物）
EXCLUDE_DIRS = {
    ".venv", "venv", "__pycache__", ".git", "node_modules",
    "tests", "scripts", "docs", "dist", "build", "vendor",
    "web/frontend", "web/splash", "web/media", "web/dist",
}

# 无法静态提取、但确属"守卫导入且静态分析可能漏收"的基线条目
# （cli_palette/cli_menu：cli.py try/except 导入；prompt_toolkit：动态加载组件）
MANUAL_BASELINE = [
    "cli_palette",
    "cli_menu",
    "prompt_toolkit",
]

_PATTERNS = [
    re.compile(r'\bimport_module\(\s*["\']([A-Za-z_][\w.]*)["\']'),
    re.compile(r'\b__import__\(\s*["\']([A-Za-z_][\w.]*)["\']'),
    re.compile(r'\bLazyLoader\(\s*["\']([A-Za-z_][\w.]*)["\']'),
]

_STDLIB = set(getattr(sys, "stdlib_module_names", ()))


def _iter_project_files():
    for path in PROJECT_ROOT.rglob("*.py"):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if any(rel == d or rel.startswith(d + "/") for d in EXCLUDE_DIRS):
            continue
        yield path


def _longest_local_module_prefix(dotted: str) -> str | None:
    """把 'emotion.sticker_manager.StickerManager' 这类目标归约成仓库内
    真实存在的最长模块前缀（属性名不是模块）。非本项目模块返回 None。"""
    parts = dotted.split(".")
    best = None
    current = PROJECT_ROOT
    for i, part in enumerate(parts):
        nxt = current / part
        if (nxt / "__init__.py").is_file() or nxt.with_suffix(".py").is_file():
            best = ".".join(parts[: i + 1])
            if not nxt.is_dir():
                break  # 文件模块（xxx.py）已到叶子，后面是属性名
            current = nxt
        else:
            break
    return best


def collect() -> list[str]:
    found: set[str] = set()
    for path in _iter_project_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pat in _PATTERNS:
            for target in pat.findall(text):
                top = target.split(".")[0]
                if top in _STDLIB:
                    continue
                local = _longest_local_module_prefix(target)
                if local:
                    found.add(local)
                elif "." not in target:
                    # 第三方顶层模块（如 prompt_builder 类项目根单文件已覆盖；
                    # 纯第三方守卫依赖保留全名，由构建环境可用性决定是否校验）
                    found.add(target)
    return sorted(found | set(MANUAL_BASELINE))


def main() -> int:
    for mod in collect():
        print(mod)
    return 0


if __name__ == "__main__":
    sys.exit(main())
