"""扫描 docstring 覆盖率.

统计公开函数 (不以 _ 开头, 或以 __init__ 开头) 的 docstring 覆盖情况.
"""
import ast
import os


def scan(root_dir: str = '.') -> tuple[int, list[str]]:
    missing = []
    total = 0
    skip = ['.git', '__pycache__', '.venv', 'dist', 'build', 'node_modules', 'tests']
    for root, dirs, files in os.walk(root_dir):
        if any(x in root for x in skip):
            continue
        for f in files:
            if not f.endswith('.py'):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, encoding='utf-8') as fh:
                    src = fh.read()
                tree = ast.parse(src)
            except Exception:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith('_') or node.name.startswith('__init__'):
                        total += 1
                        has_doc = (
                            node.body
                            and isinstance(node.body[0], ast.Expr)
                            and isinstance(node.body[0].value, ast.Constant)
                            and isinstance(node.body[0].value.value, str)
                        )
                        if not has_doc:
                            missing.append(f"{path}:{node.lineno} {node.name}")
    if total == 0:
        print("No public functions found.")
        return total, missing
    cov = (total - len(missing)) / total * 100
    print(f"Total public: {total}, Missing: {len(missing)}, Coverage: {cov:.1f}%")
    return total, missing


if __name__ == '__main__':
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else '.'
    total, missing = scan(root)
    # 按文件分组统计缺失
    by_file = {}
    for m in missing:
        fname = m.rsplit(':', 1)[0]
        by_file.setdefault(fname, 0)
        by_file[fname] += 1
    print("\nTop files with missing docstrings:")
    for fname, n in sorted(by_file.items(), key=lambda x: -x[1])[:30]:
        print(f"  {n:4d}  {fname}")
