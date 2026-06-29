"""扫描项目代码中缺少类型注解的函数。"""
import ast
import os
import sys


def scan(root_dir: str) -> tuple[list[str], int, int]:
    missing: list[str] = []
    total_funcs = 0
    for root, dirs, files in os.walk(root_dir):
        if any(x in root for x in ['.git', '__pycache__', '.venv', 'dist', 'build', 'node_modules', 'tests']):
            continue
        for f in files:
            if not f.endswith('.py'):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, encoding='utf-8') as fp:
                    tree = ast.parse(fp.read())
            except Exception:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    total_funcs += 1
                    args = [a for a in node.args.args if a.arg not in ('self', 'cls')]
                    has_annotation = all(a.annotation for a in args)
                    has_return = node.returns is not None
                    if not has_annotation or not has_return:
                        missing.append(f"{path}:{node.lineno} {node.name} args_missing={[a.arg for a in args if not a.annotation]} ret_missing={not has_return}")
    return missing, total_funcs, len(missing)


if __name__ == '__main__':
    root = sys.argv[1] if len(sys.argv) > 1 else '.'
    missing, total, n_missing = scan(root)
    print(f"Total functions: {total}")
    print(f"Missing: {n_missing}")
    if total:
        print(f"Missing rate: {n_missing / total * 100:.1f}%")
    print('---')
    for m in missing:
        print(m)
