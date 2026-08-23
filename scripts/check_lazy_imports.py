#!/usr/bin/env python3
"""函数内延迟 import 棘轮检查（2026-08-23 技术债 P1-3 专项）。

背景：全仓（非测试源码）函数内延迟 import 存量约 1300 处（基线文件为准），
多为历史循环依赖的 workaround。逐点上提是多会话工程；本脚本把存量冻结为
基线，只拦增量——债不再增长，与 check_broad_except.sh 同款棘轮策略。

规则：当前计数 > 基线 → 失败。新代码请把 import 放到模块顶部；
确属必要延迟（重依赖/可选依赖/打破真实循环），随提交同步上调
scripts/lazy_import_baseline.txt 并在提交说明中给出理由。
基线只许下调或伴随净修复上调。

判定：import 节点的祖先链上出现 FunctionDef/AsyncFunctionDef/Lambda
即计为函数内延迟；类体直属 import（方法外）不算。
排除目录与 check_broad_except.sh 一致（tests/ 一并排除）。

用法：scripts/check_lazy_imports.py [--update-baseline]
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = PROJECT_ROOT / "scripts" / "lazy_import_baseline.txt"

EXCLUDE_TOP_DIRS = {".venv", "build", "dist", "tests", "node_modules"}
FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _in_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, FUNC_NODES):
            return True
        cur = parents.get(cur)
    return False


def count_function_level_imports(path: Path) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return 0
    parents = _build_parent_map(tree)
    return sum(
        1 for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom)) and _in_function(node, parents)
    )


def iter_source_files():
    for path in sorted(PROJECT_ROOT.rglob("*.py")):
        rel_parts = path.relative_to(PROJECT_ROOT).parts
        if rel_parts[0] in EXCLUDE_TOP_DIRS or ".git" in rel_parts:
            continue
        yield path


def main() -> int:
    total = sum(count_function_level_imports(p) for p in iter_source_files())
    baseline = int(BASELINE_FILE.read_text().strip()) if BASELINE_FILE.exists() else 0

    if "--update-baseline" in sys.argv:
        BASELINE_FILE.write_text(f"{total}\n")
        print(f"[lazy-import] 基线已更新为 {total}")
        return 0

    print(f"[lazy-import] 当前 {total} / 基线 {baseline}")
    if total > baseline:
        print(f"✗ 检测到新增函数内延迟 import（+{total - baseline}）。", file=sys.stderr)
        print("  请把 import 上提到模块顶部；确属必要延迟时，同步上调", file=sys.stderr)
        print("  scripts/lazy_import_baseline.txt 并在提交说明中给出理由。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
