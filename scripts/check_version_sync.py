"""版本号四源一致性校验脚本.

读取 4 个版本号来源并比对:
  - VERSION 文件 (plain text)
  - pyproject.toml  (`version = "..."`)
  - .version 文件 (plain text)
  - web/frontend/package.json (`"version": "..."`)

CLI:
  --ci     仅检查 (默认模式)；一致退出 0，不一致退出 1 并打印差异
  --fix    以 VERSION 为准，自动同步其他 3 个文件
  --root   项目根目录 (默认: 脚本所在父目录)

使用 pathlib.Path 处理路径 (Windows 兼容)。
Python >= 3.11。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 4 个来源的相对路径
_SOURCES: dict[str, str] = {
    "VERSION": "VERSION",
    "pyproject.toml": "pyproject.toml",
    ".version": ".version",
    "package.json": "web/frontend/package.json",
}

# pyproject.toml 的 version 行正则: version = "x.y.z"
_PYPROJECT_VERSION_RE = re.compile(
    r'^(\s*version\s*=\s*")([^"]*)("\s*)$', re.MULTILINE
)

# package.json 的 version 行正则: "version": "x.y.z"  (允许任意空格/缩进)
_PKGJSON_VERSION_RE = re.compile(
    r'^(\s*"version"\s*:\s*")([^"]*)("\s*,?\s*)$', re.MULTILINE
)


# ---------- 读取函数 ----------

def read_plain_version(path: Path) -> str | None:
    """读取 plain text 版本文件 (VERSION / .version)，去除首尾空白."""
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip()


def read_pyproject_version(path: Path) -> str | None:
    """从 pyproject.toml 提取 version = "..." 的值."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    m = _PYPROJECT_VERSION_RE.search(text)
    return m.group(2) if m else None


def read_package_json_version(path: Path) -> str | None:
    """从 package.json 提取 "version": "..." 的值.

    使用正则而非 json 解析，以便兼容注释/尾随逗号等容忍场景，
    且在 --fix 时能保留原始格式。
    """
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    m = _PKGJSON_VERSION_RE.search(text)
    return m.group(2) if m else None


_READERS = {
    "VERSION": read_plain_version,
    "pyproject.toml": read_pyproject_version,
    ".version": read_plain_version,
    "package.json": read_package_json_version,
}


def read_all_versions(project_root: Path) -> dict[str, str | None]:
    """读取全部 4 源版本号，缺失的文件返回 None."""
    return {
        name: reader(project_root / rel)
        for name, rel in _SOURCES.items()
        for reader in (_READERS[name],)
    }


# ---------- 检查 ----------

def check_sync(project_root: Path) -> tuple[bool, list[str]]:
    """检查 4 源一致性.

    Returns:
        (is_in_sync, messages)
        is_in_sync: True 表示完全一致
        messages: 失败时的人类可读差异信息列表
    """
    versions = read_all_versions(project_root)
    messages: list[str] = []

    # VERSION 是 source of truth
    truth = versions["VERSION"]
    if truth is None:
        messages.append(
            f"VERSION file missing: {project_root / 'VERSION'}"
        )
        # 无法判定期望值，直接报错
        for name, rel in _SOURCES.items():
            if name == "VERSION":
                continue
            if versions[name] is None:
                messages.append(f"{name}: missing ({rel})")
        return False, messages

    for name, rel in _SOURCES.items():
        if name == "VERSION":
            continue
        current = versions[name]
        if current is None:
            messages.append(
                f"{name}: missing file (expected {truth}) at {project_root / rel}"
            )
        elif current != truth:
            messages.append(
                f"{name}: current='{current}' expected='{truth}'"
            )

    return len(messages) == 0, messages


# ---------- 修复 ----------

def _replace_in_file(path: Path, pattern: re.Pattern[str], new_value: str) -> bool:
    """用正则替换文件中的版本号，保留其他格式.

    Returns: True 表示发生了替换；False 表示未匹配到。
    """
    text = path.read_text(encoding="utf-8")
    new_text, n = pattern.subn(
        lambda m: m.group(1) + new_value + m.group(3), text
    )
    if n == 0:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def fix_sync(project_root: Path) -> list[str]:
    """以 VERSION 为准同步其他 3 个文件.

    Returns: 已修复的文件名列表 (空列表 = 无需修复或 VERSION 缺失)。
    """
    truth = read_plain_version(project_root / "VERSION")
    if truth is None:
        # VERSION 缺失时无法 fix
        return []

    fixed: list[str] = []
    # pyproject.toml
    pyproject_path = project_root / "pyproject.toml"
    if pyproject_path.exists():
        if _replace_in_file(pyproject_path, _PYPROJECT_VERSION_RE, truth):
            fixed.append("pyproject.toml")
    # .version
    dot_version_path = project_root / ".version"
    if dot_version_path.exists():
        current = read_plain_version(dot_version_path)
        if current != truth:
            dot_version_path.write_text(truth + "\n", encoding="utf-8")
            fixed.append(".version")
    # package.json
    pkg_path = project_root / "web" / "frontend" / "package.json"
    if pkg_path.exists():
        if _replace_in_file(pkg_path, _PKGJSON_VERSION_RE, truth):
            fixed.append("package.json")

    return fixed


# ---------- CLI ----------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="版本号四源一致性校验 (VERSION / pyproject.toml / .version / package.json)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ci", action="store_true",
                     help="仅检查模式 (默认): 一致退出 0，不一致退出 1")
    mode.add_argument("--fix", action="store_true",
                     help="以 VERSION 为准自动同步其他 3 个文件")
    parser.add_argument("--root", default=None,
                       help="项目根目录 (默认: 脚本所在父目录)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.root is not None:
        project_root = Path(args.root).resolve()
    else:
        # 默认: 脚本文件的父目录的父目录 (scripts/check_version_sync.py -> project root)
        project_root = Path(__file__).resolve().parent.parent

    if args.fix:
        # --fix 模式
        truth = read_plain_version(project_root / "VERSION")
        if truth is None:
            print(f"ERROR: VERSION file missing at {project_root / 'VERSION'}",
                  file=sys.stderr)
            return 1
        fixed = fix_sync(project_root)
        if not fixed:
            # 检查是否已经一致或全部文件缺失
            is_sync, _ = check_sync(project_root)
            if is_sync:
                print(f"all versions in sync ({truth}) - no changes needed")
                return 0
            print("ERROR: --fix could not sync (some files missing?)",
                  file=sys.stderr)
            return 1
        print(f"fixed {len(fixed)} file(s) to version {truth}:")
        for name in fixed:
            print(f"  - {name}")
        # 修复后再校验一次
        is_sync, _ = check_sync(project_root)
        if is_sync:
            print("all versions in sync after fix")
            return 0
        print("WARNING: still out of sync after fix", file=sys.stderr)
        return 1

    # --ci 模式 (默认)
    is_sync, messages = check_sync(project_root)
    if is_sync:
        truth = read_plain_version(project_root / "VERSION")
        print(f"all versions in sync ({truth})")
        return 0
    print("version mismatch detected:")
    for msg in messages:
        print(f"  - {msg}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
