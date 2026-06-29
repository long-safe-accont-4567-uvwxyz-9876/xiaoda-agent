"""列出缺失 docstring 的公开函数及其签名 (含精确行号)."""
import ast
import os
import sys


def collect(root_dir):
    skip = ['.git', '__pycache__', '.venv', 'dist', 'build', 'node_modules', 'tests']
    out = []
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
                        has_doc = (
                            node.body
                            and isinstance(node.body[0], ast.Expr)
                            and isinstance(node.body[0].value, ast.Constant)
                            and isinstance(node.body[0].value.value, str)
                        )
                        if not has_doc:
                            args = [a.arg for a in node.args.args]
                            out.append({
                                'path': path,
                                'line': node.lineno,
                                'name': node.name,
                                'args': args,
                                'is_async': isinstance(node, ast.AsyncFunctionDef),
                            })
    return out


if __name__ == '__main__':
    root = sys.argv[1] if len(sys.argv) > 1 else '.'
    out = collect(root)
    # group by file
    by_file = {}
    for x in out:
        by_file.setdefault(x['path'], []).append(x)
    # priority directories
    priority = ['core/', 'agent_core/', 'tool_engine/', 'web/routers', 'web/', 'memory/', 'db/', 'security/', 'utils/', 'emotion/', 'chaos/']
    def prio_key(path):
        for i, p in enumerate(priority):
            if p in path:
                return (i, path)
        return (len(priority), path)
    for path in sorted(by_file.keys(), key=prio_key):
        items = by_file[path]
        print(f"\n=== {path} ({len(items)}) ===")
        for x in items:
            sig = f"{'async ' if x['is_async'] else ''}{x['name']}({', '.join(x['args'])})"
            print(f"  L{x['line']:4d}  {sig}")
