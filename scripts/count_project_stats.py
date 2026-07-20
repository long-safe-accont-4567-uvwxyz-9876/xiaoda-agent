"""scripts/count_project_stats.py

自动统计 xiaoda-agent 项目的规模指标，用于 README.md 数值校验与 CI 集成。

覆盖 Task 3（项目统计脚本）+ Task 12（路由端点统计合并）。

统计指标：
  - Python 模块数（排除 .venv / node_modules / __pycache__ / web/frontend）
  - 代码行数（total / code / comments / blank）
  - 路由模块数（web/routers/*.py 排除 __init__.py）
  - API 端点数（@router.get/post/put/delete/patch 装饰器，按方法分类 + total）
  - DB 表数 / FTS5 虚拟表数 / 索引数（从 db/schema.sql 解析）
  - 情绪枚举数（emotion/emotion_enum.py 的 Emotion 成员数）
  - 内置工具数（tools/_builtin_manifest.py 的 BUILTIN_TOOLS 长度）
  - 测试文件数（tests/ 下 .py 文件，排除 __init__.py / conftest.py）
  - 核心子系统模块数（core/memory/tool_engine/emotion/security/plugins）

CLI:
  python scripts/count_project_stats.py [--format json|markdown] [--check-readme]

退出码：
  - 默认模式：0
  - --check-readme：发现过时数值时非 0

依赖：仅 Python 标准库（pathlib / ast / re / json / argparse）。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

# ============================================================
# 常量
# ============================================================

# count_python_modules / count_loc 共用的目录排除集合
EXCLUDED_DIRS: frozenset[str] = frozenset({
    ".venv",
    "node_modules",
    "__pycache__",
    "web/frontend",  # 前端 Vue 工程，非 Python
})

# 核心子系统目录名（默认）
DEFAULT_SUBSYSTEMS: list[str] = [
    "core", "memory", "tool_engine", "emotion", "security", "plugins",
]

# 路由装饰器正则：匹配 @router.get( / @router.post( 等，支持多行（\s 包含 \n）
# 用 \b 边界防止 @router.getting 误匹配；要求 `(` 才算装饰器调用。
ROUTER_DECORATOR_RE = re.compile(
    r"@router\.(get|post|put|delete|patch)\s*\(",
    re.IGNORECASE,
)

# SQL 语句正则（行首匹配，大小写不敏感）
CREATE_TABLE_RE = re.compile(r"^\s*CREATE\s+TABLE\b", re.IGNORECASE)
CREATE_VIRTUAL_TABLE_RE = re.compile(r"^\s*CREATE\s+VIRTUAL\s+TABLE\b", re.IGNORECASE)
CREATE_INDEX_RE = re.compile(r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\b", re.IGNORECASE)

# HTTP 方法列表（与 ROUTER_DECORATOR_RE 中的分组保持顺序一致）
HTTP_METHODS: list[str] = ["get", "post", "put", "delete", "patch"]


# ============================================================
# 统计函数
# ============================================================

def _is_excluded(path: Path, root: Path) -> bool:
    """判断路径是否落在任一排除目录下。

    EXCLUDED_DIRS 中既有简单目录名（.venv / __pycache__），也有相对路径
    （web/frontend），所以用 parts 匹配两种情况。
    """
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return False
    rel_path_str = "/".join(rel_parts)
    # 任一 part 命中简单名排除（.venv / node_modules / __pycache__）
    for part in rel_parts:
        if part in (".venv", "node_modules", "__pycache__"):
            return True
    # 命中相对路径排除（web/frontend）
    for excluded in EXCLUDED_DIRS:
        if "/" in excluded:
            if rel_path_str == excluded or rel_path_str.startswith(excluded + "/"):
                return True
    return False


def count_python_modules(root: Path) -> int:
    """统计 Python 模块数（.py 文件），排除 .venv / node_modules / __pycache__ / web/frontend。"""
    root = Path(root)
    if not root.exists():
        return 0
    count = 0
    for path in root.rglob("*.py"):
        if _is_excluded(path, root):
            continue
        count += 1
    return count


def count_loc(root: Path) -> dict[str, int]:
    """统计代码行数。

    Returns:
        {"total": int, "code": int, "comments": int, "blank": int}
        - blank: 空行或仅空白字符
        - comments: 行首（去空白后）以 # 开头
        - code: 其它（含行内注释的代码行）
    """
    root = Path(root)
    result = {"total": 0, "code": 0, "comments": 0, "blank": 0}
    if not root.exists():
        return result
    for path in root.rglob("*.py"):
        if _is_excluded(path, root):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            result["total"] += 1
            stripped = line.strip()
            if not stripped:
                result["blank"] += 1
            elif stripped.startswith("#"):
                result["comments"] += 1
            else:
                result["code"] += 1
    return result


def count_api_endpoints(routers_dir: Path) -> dict[str, int]:
    """统计路由模块数与 API 端点数（按 HTTP 方法分类）。

    模块数：routers_dir 下 .py 文件数，排除 __init__.py。
    端点数：扫描 @router.get/post/put/delete/patch( 装饰器，正确处理多行装饰器。

    Returns:
        {"modules": int, "get": int, "post": int, "put": int,
         "delete": int, "patch": int, "total": int}
    """
    routers_dir = Path(routers_dir)
    result: dict[str, int] = {
        "modules": 0, "get": 0, "post": 0, "put": 0,
        "delete": 0, "patch": 0, "total": 0,
    }
    if not routers_dir.exists() or not routers_dir.is_dir():
        return result

    py_files = sorted(p for p in routers_dir.glob("*.py") if p.name != "__init__.py")
    result["modules"] = len(py_files)

    for path in py_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in ROUTER_DECORATOR_RE.finditer(text):
            method = match.group(1).lower()
            result[method] = result.get(method, 0) + 1
            result["total"] += 1
    return result


def count_db_tables(schema_path: Path) -> dict[str, int]:
    """从 SQL schema 文件解析表 / 虚拟表 / 索引数。

    Returns:
        {"tables": int, "virtual_tables": int, "indexes": int}
        - tables: CREATE TABLE（不含 CREATE VIRTUAL TABLE）
        - virtual_tables: CREATE VIRTUAL TABLE
        - indexes: CREATE INDEX + CREATE UNIQUE INDEX
    """
    schema_path = Path(schema_path)
    result = {"tables": 0, "virtual_tables": 0, "indexes": 0}
    if not schema_path.exists():
        return result
    try:
        text = schema_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    for line in text.splitlines():
        # 先匹配 virtual_table（避免被 CREATE TABLE 误中）
        # 注意：CREATE VIRTUAL TABLE 不应被 CREATE_TABLE_RE 命中，
        # 因为 _TABLE_RE 要求 "TABLE" 紧跟 "CREATE\s+"，而 VIRTUAL 介于两者之间。
        if CREATE_VIRTUAL_TABLE_RE.match(line):
            result["virtual_tables"] += 1
        elif CREATE_TABLE_RE.match(line):
            result["tables"] += 1
        elif CREATE_INDEX_RE.match(line):
            result["indexes"] += 1
    return result


def count_emotion_enum(emotion_enum_path: Path) -> int:
    """统计 emotion_enum.py 中 Emotion 枚举的成员数。

    通过 AST 解析，找到 class Emotion 并统计其类体中的赋值节点
    （ast.Assign / ast.AnnAssign），不计方法（ast.FunctionDef）。
    使用 AST 而非 exec，避免触发模块级 import 的重依赖。
    """
    emotion_enum_path = Path(emotion_enum_path)
    if not emotion_enum_path.exists():
        return 0
    try:
        text = emotion_enum_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except (OSError, SyntaxError):
        return 0
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Emotion":
            count = 0
            for stmt in node.body:
                if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                    count += 1
            return count
    return 0


def count_builtin_tools(manifest_path: Path) -> int:
    """统计 _builtin_manifest.py 中 BUILTIN_TOOLS 列表长度。

    通过 AST 解析 BUILTIN_TOOLS = [...] 赋值，返回列表元素数。
    同时支持带类型注解的赋值：``BUILTIN_TOOLS: list[...] = [...]``。
    使用 AST 避免执行模块级 import（tool_engine / config）。
    """
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        return 0
    try:
        text = manifest_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except (OSError, SyntaxError):
        return 0
    for node in tree.body:
        # 普通赋值：BUILTIN_TOOLS = [...]
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "BUILTIN_TOOLS":
                    if isinstance(node.value, ast.List):
                        return len(node.value.elts)
                    return 0
        # 带类型注解的赋值：BUILTIN_TOOLS: list[...] = [...]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == "BUILTIN_TOOLS":
                if isinstance(node.value, ast.List):
                    return len(node.value.elts)
                return 0
    return 0


def count_test_files(tests_dir: Path) -> int:
    """统计 tests/ 下 .py 文件数，排除 __init__.py 与 conftest.py。"""
    tests_dir = Path(tests_dir)
    if not tests_dir.exists() or not tests_dir.is_dir():
        return 0
    excluded_names = {"__init__.py", "conftest.py"}
    count = 0
    for path in tests_dir.rglob("*.py"):
        # 排除 __pycache__
        if "__pycache__" in path.parts:
            continue
        if path.name in excluded_names:
            continue
        count += 1
    return count


def count_subsystem_modules(
    root: Path,
    subsystems: list[str] | None = None,
) -> dict[str, int]:
    """统计各核心子系统目录的 .py 文件数。

    Args:
        root: 项目根目录
        subsystems: 子系统目录名列表；None 时使用 DEFAULT_SUBSYSTEMS

    Returns:
        {subsystem_name: file_count}，目录不存在时计 0。
    """
    root = Path(root)
    if subsystems is None:
        subsystems = DEFAULT_SUBSYSTEMS
    result: dict[str, int] = {}
    for sub in subsystems:
        sub_dir = root / sub
        count = 0
        if sub_dir.exists() and sub_dir.is_dir():
            for path in sub_dir.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                count += 1
        result[sub] = count
    return result


# ============================================================
# README 校验
# ============================================================

def _parse_number(raw: str) -> int:
    """把正则捕获的数字字符串解析为 int。

    支持千位分隔符（"20,000" → 20000）。
    支持 k/K 后缀（"20k" → 20000）。
    """
    raw = raw.strip()
    has_k = raw.lower().endswith("k")
    if has_k:
        raw = raw[:-1]
    # 去掉千位分隔符
    cleaned = raw.replace(",", "")
    try:
        value = int(cleaned)
    except ValueError:
        return -1
    if has_k:
        value *= 1000
    return value


# README 中的数值模式定义：
#   (description, regex, actual_key)
# regex 必须有且仅有一个捕获组提取 README 中的数值。
# 大小写不敏感，逐行匹配。
README_PATTERNS: list[tuple[str, str, str]] = [
    # 数据库表数：匹配 "21 表" / "21 张表" / "21 张 + 22 索引"
    ("数据库表数", r"(\d+)\s*张\s*(?:\+|表|$)", "db_tables"),
    ("数据库表数", r"(\d+)\s*表", "db_tables"),
    # 索引数
    ("索引数", r"(\d+)\s*索引", "db_indexes"),
    # 路由模块数：匹配 "15 模块 + 139 端点" 或 "13 个 API 路由模块"
    ("路由模块数", r"(\d+)\s*个?\s*API\s*路由模块", "router_modules"),
    ("路由模块数", r"(\d+)\s*模块\s*\+\s*\d+\s*端点", "router_modules"),
    # API 端点数
    ("API 端点数", r"(\d+)\s*端点", "api_endpoints"),
    # 情绪枚举数：匹配 "9 类核心情绪" / "9 类情绪" / "9 种情绪"
    ("情绪枚举数", r"(\d+)\s*类(?:核心)?情绪", "emotion_enum"),
    ("情绪枚举数", r"(\d+)\s*种情绪", "emotion_enum"),
    # Python 模块数：匹配 "80+ 模块" / "Python 模块 | 80+"
    ("Python 模块数", r"(\d+)\+?\s*个?\s*Python\s*模块", "python_modules"),
    ("Python 模块数", r"Python\s*模块\s*\|\s*(\d+)\+?", "python_modules"),
    # 代码行数：匹配 "~20,000 行" / "~20k 行" / "131,984 行"
    ("代码行数", r"~\s*(\d[\d,]*[kK]?)\s*行", "loc_total"),
]


def check_readme(readme_path: Path, actual: dict) -> list[str]:
    """比对 README.md 中的数值与实际值，返回过时项描述列表。

    Args:
        readme_path: README.md 路径
        actual: 实际值 dict，可包含键：
            python_modules / loc_total / db_tables / db_indexes /
            router_modules / api_endpoints / emotion_enum

    Returns:
        过时项描述列表。空列表表示 README 与实际一致。
        README 不存在时返回空列表（不报错）。

    备注：
        - LOC 模式要求 tilde 前缀（~），避免误匹配 "v1 行为" / "1431 行 God Class"
          等模块级或非 LOC 上下文。
        - LOC 匹配逐行进行，跳过含 "测试" 的行（测试代码行数与总行数是不同指标）。
    """
    readme_path = Path(readme_path)
    if not readme_path.exists():
        return []
    try:
        text = readme_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    mismatches: list[str] = []
    seen: set[tuple[str, int]] = set()  # 去重：(description, readme_value)

    lines = text.splitlines()
    for description, pattern, actual_key in README_PATTERNS:
        if actual_key not in actual:
            continue
        actual_value = actual[actual_key]
        regex = re.compile(pattern, re.IGNORECASE)
        # LOC 模式需跳过 "测试" 行（测试代码行数与总行数是不同指标）
        skip_keywords = {"测试"} if actual_key == "loc_total" else set()
        for line in lines:
            if any(kw in line for kw in skip_keywords):
                continue
            for match in regex.finditer(line):
                raw = match.group(1)
                readme_value = _parse_number(raw)
                if readme_value < 0:
                    continue
                key = (description, readme_value)
                if key in seen:
                    continue
                seen.add(key)
                if readme_value != actual_value:
                    mismatches.append(
                        f"README 中 {description}={readme_value} 已过时，"
                        f"实际为 {actual_value}"
                    )
    return mismatches


# ============================================================
# 汇总 / 格式化
# ============================================================

def collect_stats(root: Path) -> dict:
    """汇总项目所有统计指标。

    Args:
        root: 项目根目录（默认 Path.cwd()）

    Returns:
        包含所有指标的 dict。
    """
    root = Path(root)
    routers_dir = root / "web" / "routers"
    schema_path = root / "db" / "schema.sql"
    emotion_enum_path = root / "emotion" / "emotion_enum.py"
    manifest_path = root / "tools" / "_builtin_manifest.py"
    tests_dir = root / "tests"

    endpoints = count_api_endpoints(routers_dir)
    db_stats = count_db_tables(schema_path)
    loc = count_loc(root)
    subsystems = count_subsystem_modules(root)

    return {
        "python_modules": count_python_modules(root),
        "loc": loc,
        "router_modules": endpoints["modules"],
        "api_endpoints": {
            "get": endpoints["get"],
            "post": endpoints["post"],
            "put": endpoints["put"],
            "delete": endpoints["delete"],
            "patch": endpoints["patch"],
            "total": endpoints["total"],
        },
        "db_tables": db_stats["tables"],
        "db_virtual_tables": db_stats["virtual_tables"],
        "db_indexes": db_stats["indexes"],
        "emotion_enum": count_emotion_enum(emotion_enum_path),
        "builtin_tools": count_builtin_tools(manifest_path),
        "test_files": count_test_files(tests_dir),
        "subsystems": subsystems,
    }


def format_json(stats: dict) -> str:
    """输出 JSON 格式统计结果。"""
    return json.dumps(stats, indent=2, ensure_ascii=False)


def format_markdown(stats: dict) -> str:
    """输出 Markdown 表格格式的统计结果。"""
    rows: list[tuple[str, str]] = [
        ("Python 模块数", str(stats["python_modules"])),
        ("代码行数（总）", f"{stats['loc']['total']:,}"),
        ("代码行数（代码）", f"{stats['loc']['code']:,}"),
        ("代码行数（注释）", f"{stats['loc']['comments']:,}"),
        ("代码行数（空行）", f"{stats['loc']['blank']:,}"),
        ("路由模块数", str(stats["router_modules"])),
        ("API 端点数（GET）", str(stats["api_endpoints"]["get"])),
        ("API 端点数（POST）", str(stats["api_endpoints"]["post"])),
        ("API 端点数（PUT）", str(stats["api_endpoints"]["put"])),
        ("API 端点数（DELETE）", str(stats["api_endpoints"]["delete"])),
        ("API 端点数（PATCH）", str(stats["api_endpoints"]["patch"])),
        ("API 端点数（总）", str(stats["api_endpoints"]["total"])),
        ("DB 表数", str(stats["db_tables"])),
        ("FTS5 虚拟表数", str(stats["db_virtual_tables"])),
        ("索引数", str(stats["db_indexes"])),
        ("情绪枚举数", str(stats["emotion_enum"])),
        ("内置工具数", str(stats["builtin_tools"])),
        ("测试文件数", str(stats["test_files"])),
    ]
    for name, count in stats["subsystems"].items():
        rows.append((f"{name} 模块数", str(count)))

    lines = ["| 指标 | 数值 |", "| --- | --- |"]
    for name, value in rows:
        lines.append(f"| {name} | {value} |")
    return "\n".join(lines)


# ============================================================
# CLI
# ============================================================

def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 命令行参数（None 时使用 sys.argv[1:]）

    Returns:
        退出码：默认模式 0；--check-readme 发现过时数值时非 0。
    """
    parser = argparse.ArgumentParser(
        description="统计 xiaoda-agent 项目规模指标",
    )
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="markdown",
        help="输出格式（默认 markdown）",
    )
    parser.add_argument(
        "--check-readme",
        action="store_true",
        help="与 README.md 中数值对比，报告过时项（不一致时退出码非 0）",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="项目根目录（默认当前工作目录）",
    )
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else Path.cwd()
    stats = collect_stats(root)

    if args.check_readme:
        readme_path = root / "README.md"
        actual = {
            "python_modules": stats["python_modules"],
            "loc_total": stats["loc"]["total"],
            "db_tables": stats["db_tables"],
            "db_indexes": stats["db_indexes"],
            "router_modules": stats["router_modules"],
            "api_endpoints": stats["api_endpoints"]["total"],
            "emotion_enum": stats["emotion_enum"],
        }
        mismatches = check_readme(readme_path, actual)
        if not mismatches:
            print("README.md 中数值与实际一致，无过时项。")
            return 0
        print(f"发现 {len(mismatches)} 处过时数值：")
        for m in mismatches:
            print(f"  - {m}")
        return 1

    if args.format == "json":
        print(format_json(stats))
    else:
        print(format_markdown(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
