"""基于 libcst 的类型注解自动补全脚本。

策略:
- 遍历项目所有 .py 文件 (排除 tests/, venv 等)
- 对每个 FunctionDef / AsyncFunctionDef:
  - 缺少注解的参数 (除 self/cls) -> Any
  - 返回类型:
    - 无 return / return None / return -> None
    - 全为 bool 字面量 -> bool
    - 全为 str 字面量 -> str
    - 全为 int 字面量 -> int
    - 全为 bytes 字面量 -> bytes
    - 包含 yield -> Iterator[Any] / AsyncIterator[Any]
    - 否则 -> Any
- 文件含 `from __future__ import annotations` 时, 用 X | None 语法
- 自动添加 `from typing import Any` (若需要且未导入)
"""
from __future__ import annotations

import os
import sys

import libcst as cst


SKIP_DIRS = {'.git', '__pycache__', '.venv', 'dist', 'build', 'node_modules', 'tests', '.pytest_cache', 'frontend'}

# 需要处理的目录与文件 (优先级目录)
TARGET_DIRS = [
    'agent_core', 'core', 'tool_engine', 'web', 'memory', 'db',
    'security', 'utils', 'emotion', 'tools', 'plugins', 'transports',
    'doctor', 'quality', 'chaos', 'scripts',
]
# 根目录 .py 文件也需处理
TARGET_ROOT_FILES = [
    'agent.py', 'agent_context.py', 'agent_dispatcher.py', 'belief_router.py',
    'cli.py', 'cli_client.py', 'config.py', 'hooks.py', 'instinct_manager.py',
    'xiaoli_agent.py', 'model_router.py', 'qq_bot_adapter.py', 'setup_wizard.py',
    'slash_commands.py', 'task_orchestrator.py', 'prompt_builder.py',
    'backfill_kg_kioxia.py',
]
# scripts/ 目录下需要处理的文件 (排除已加入 TARGET_DIRS 后重复)
SCRIPTS_FILES = {'generate_requirements_lock.py'}


def is_target(path: str) -> bool:
    rel = os.path.relpath(path, '.')
    if rel.startswith('tests' + os.sep):
        return False
    parts = rel.split(os.sep)
    for p in parts:
        if p in SKIP_DIRS:
            return False
    # 顶层 .py 文件
    if os.sep not in rel and rel.endswith('.py'):
        return rel in TARGET_ROOT_FILES
    # 顶层目录文件
    return any(rel.startswith(td + os.sep) for td in TARGET_DIRS)


def has_future_annotations(module: cst.Module) -> bool:
    for stmt in module.body:
        imp = stmt
        if isinstance(imp, cst.SimpleStatementLine):
            for small in imp.body:
                if isinstance(small, cst.ImportFrom):
                    if (small.module and isinstance(small.module, cst.Name)
                            and small.module.value == '__future__'):
                        for n in small.names:
                            if isinstance(n, (cst.ImportAlias,)) and isinstance(n.name, cst.Name) and n.name.value == 'annotations':
                                return True
    return False


class ReturnAnalyzer(cst.CSTVisitor):
    """收集函数体内 return / yield 语句的类型线索。"""

    def __init__(self) -> None:
        self.returns: list[cst.BaseExpression | None] = []
        self.has_yield = False
        self.has_yield_from = False
        self.has_return_value = False
        # 跟踪嵌套函数, 不进入
        self._depth = 0

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._depth += 1

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._depth -= 1

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._depth += 1

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        self._depth -= 1

    def visit_Return(self, node: cst.Return) -> None:
        if self._depth > 0:
            return  # 嵌套函数内的 return 跳过
        if node.value is None:
            self.returns.append(None)
        else:
            self.returns.append(node.value)
            self.has_return_value = True

    def visit_Yield(self, node: cst.Yield) -> None:
        if self._depth > 0:
            return
        self.has_yield = True

    def visit_YieldFrom(self, node: cst.YieldFrom) -> None:
        if self._depth > 0:
            return
        self.has_yield_from = True
        self.has_yield = True


def literal_type(expr: cst.BaseExpression) -> str | None:
    """识别简单字面量类型。"""
    if isinstance(expr, cst.Name):
        if expr.value in ('True', 'False'):
            return 'bool'
        if expr.value == 'None':
            return 'None'
    if isinstance(expr, (cst.SimpleString, cst.ConcatenatedString, cst.FormattedString)):
        return 'str'
    if isinstance(expr, cst.Integer):
        return 'int'
    if isinstance(expr, cst.Float):
        return 'float'
    if isinstance(expr, cst.Imaginary):
        return 'complex'
    if isinstance(expr, cst.List):
        return 'list'
    if isinstance(expr, cst.Tuple):
        return 'tuple'
    if isinstance(expr, cst.Dict):
        return 'dict'
    if isinstance(expr, cst.Set):
        return 'set'
    return None


def infer_return_type(func: cst.FunctionDef, is_async: bool) -> str:
    analyzer = ReturnAnalyzer()
    func.body.visit(analyzer)
    if analyzer.has_yield:
        if is_async:
            return 'AsyncIterator[Any]'
        return 'Iterator[Any]'
    if not analyzer.returns:
        return 'None'
    types = set()
    for r in analyzer.returns:
        if r is None:
            types.add('None')
        else:
            t = literal_type(r)
            if t:
                types.add(t)
            else:
                return 'Any'
    # 全部为 None
    if types == {'None'}:
        return 'None'
    # 全为同一字面量类型
    if len(types) == 1:
        return next(iter(types))
    return 'Any'


def needs_any_param(func: cst.FunctionDef) -> bool:
    """检查是否有未注解的普通参数 (除 self/cls)。"""
    skip = {'self', 'cls'}
    for p in func.params.params:
        if p.name.value in skip:
            continue
        if p.annotation is None:
            return True
    for p in func.params.kwonly_params:
        if p.annotation is None:
            return True
    for p in func.params.posonly_params:
        if p.name.value in skip:
            continue
        if p.annotation is None:
            return True
    if func.params.star_arg and isinstance(func.params.star_arg, cst.Param):
        if func.params.star_arg.annotation is None:
            return True
    return bool(func.params.star_kwarg and func.params.star_kwarg.annotation is None)


def needs_return(func: cst.FunctionDef) -> bool:
    return func.returns is None


class AnnotationTransformer(cst.CSTTransformer):
    """为函数补全类型注解。"""

    def __init__(self, future_annotations: bool) -> None:
        self.future_annotations = future_annotations
        self.added_any_import = False
        self.needs_any = False
        self.needs_iterator = False
        self.needs_async_iterator = False
        # 记录已修改标志
        self.modified = False

    def _type_any(self) -> str:
        return 'Any'

    def _optional(self, inner: str) -> str:
        if self.future_annotations:
            return f'{inner} | None'
        return f'Optional[{inner}]'

    def leave_ImportFrom(self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom) -> cst.BaseStatement:
        return super().leave_ImportFrom(original_node, updated_node)

    def _annotate_param(self, p: cst.Param, skip_names: set[str]) -> cst.Param:
        if p.name.value in skip_names:
            return p
        if p.annotation is not None:
            return p
        # 默认值为 None -> Optional[Any]
        ann_str: str
        # 参数统一使用 Any (Optional[Any] == Any, 更简洁)
        ann_str = 'Any'
        self.needs_any = True
        annotation = cst.Annotation(annotation=cst.parse_expression(ann_str))
        self.modified = True
        return p.with_changes(annotation=annotation)

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.BaseStatement:
        # 仅处理直接包含函数体的函数; 递归处理嵌套由 cst 自动完成
        skip = {'self', 'cls'}
        params = updated_node.params
        new_params = []
        changed = False
        for p in params.params:
            np = self._annotate_param(p, skip)
            if np is not p:
                changed = True
            new_params.append(np)
        new_kwonly = []
        for p in params.kwonly_params:
            np = self._annotate_param(p, skip)
            if np is not p:
                changed = True
            new_kwonly.append(np)
        new_posonly = []
        for p in params.posonly_params:
            np = self._annotate_param(p, skip)
            if np is not p:
                changed = True
            new_posonly.append(np)
        # *args
        star_arg = params.star_arg
        if isinstance(star_arg, cst.Param) and star_arg.annotation is None:
            ann = cst.Annotation(annotation=cst.parse_expression('Any'))
            star_arg = star_arg.with_changes(annotation=ann)
            changed = True
            self.needs_any = True
        # **kwargs
        star_kwarg = params.star_kwarg
        if star_kwarg is not None and star_kwarg.annotation is None:
            ann = cst.Annotation(annotation=cst.parse_expression('Any'))
            star_kwarg = star_kwarg.with_changes(annotation=ann)
            changed = True
            self.needs_any = True
        # 返回类型
        returns = updated_node.returns
        if returns is None:
            is_async = isinstance(updated_node, cst.FunctionDef) and original_node.asynchronous is not None
            ret_str = infer_return_type(original_node, is_async)
            if 'Any' in ret_str:
                self.needs_any = True
            if 'Iterator' in ret_str:
                self.needs_iterator = True
            if 'AsyncIterator' in ret_str:
                self.needs_async_iterator = True
            returns = cst.Annotation(annotation=cst.parse_expression(ret_str))
            changed = True
        if not changed:
            return updated_node
        self.modified = True
        new_params_obj = params.with_changes(
            params=new_params,
            kwonly_params=new_kwonly,
            posonly_params=new_posonly,
            star_arg=star_arg,
            star_kwarg=star_kwarg,
        )
        return updated_node.with_changes(params=new_params_obj, returns=returns)

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        if not self.modified:
            return updated_node
        # 决定需要添加的 typing 名字
        needed: list[str] = []
        if self.needs_any:
            needed.append('Any')
        if self.needs_iterator:
            needed.append('Iterator')
        if self.needs_async_iterator:
            needed.append('AsyncIterator')
        if not self.future_annotations:
            # 检查是否需要 Optional
            text = cst.Module(body=updated_node.body).code
            if 'Optional[' in text:
                needed.append('Optional')
        if not needed:
            return updated_node
        # 查找现有的 typing 导入
        existing_typing_imports: set[str] = set()
        typing_import_idx: int | None = None
        for i, stmt in enumerate(updated_node.body):
            if not isinstance(stmt, cst.SimpleStatementLine):
                continue
            for small in stmt.body:
                if isinstance(small, cst.ImportFrom) and small.module is not None:
                    mod = small.module
                    mod_name = ''
                    if isinstance(mod, cst.Name):
                        mod_name = mod.value
                    elif isinstance(mod, cst.Attribute):
                        # typing.Optional 之类
                        cur = mod
                        while isinstance(cur, cst.Attribute):
                            cur = cur.value
                        if isinstance(cur, cst.Name):
                            mod_name = cur.value
                    if mod_name == 'typing':
                        typing_import_idx = i
                        for n in small.names:
                            if isinstance(n, cst.ImportAlias) and isinstance(n.name, cst.Name):
                                existing_typing_imports.add(n.name.value)
        to_add = [n for n in needed if n not in existing_typing_imports]
        if not to_add:
            return updated_node
        if typing_import_idx is not None:
            # 扩展现有 import
            stmt = updated_node.body[typing_import_idx]
            assert isinstance(stmt, cst.SimpleStatementLine)
            new_body = []
            for small in stmt.body:
                if isinstance(small, cst.ImportFrom) and small.module is not None and isinstance(small.module, cst.Name) and small.module.value == 'typing':
                    existing_names = list(small.names)
                    new_aliases = []
                    for n in to_add:
                        new_aliases.append(cst.ImportAlias(name=cst.Name(n)))
                    # 排序保持有序 (按字母)
                    all_aliases = existing_names + new_aliases
                    # 过滤 ImportStar 情况
                    if any(isinstance(a, cst.ImportStar) for a in all_aliases):
                        # 不动
                        new_body.append(small)
                        continue
                    # 去重
                    seen = set()
                    unique = []
                    for a in all_aliases:
                        if isinstance(a, cst.ImportAlias) and isinstance(a.name, cst.Name):
                            if a.name.value in seen:
                                continue
                            seen.add(a.name.value)
                            unique.append(a)
                        else:
                            unique.append(a)
                    unique.sort(key=lambda a: a.name.value if isinstance(a, cst.ImportAlias) and isinstance(a.name, cst.Name) else 'ZZZ')
                    new_small = small.with_changes(names=unique)
                    new_body.append(new_small)
                else:
                    new_body.append(small)
            new_stmt = stmt.with_changes(body=new_body)
            body_list = list(updated_node.body)
            body_list[typing_import_idx] = new_stmt
            return updated_node.with_changes(body=body_list)
        # 新增 import 行
        names = ', '.join(sorted(to_add))
        import_code = f'from typing import {names}\n'
        # 解析为语句
        new_module = cst.parse_module(import_code)
        new_stmt = new_module.body[0]
        # 放置位置: 在文件开头 (任何 docstring 之后)
        body_list = list(updated_node.body)
        insert_idx = 0
        # 跳过开头的 docstring / __future__ 导入
        for i, stmt in enumerate(body_list):
            if isinstance(stmt, cst.SimpleStatementLine):
                first = stmt.body[0] if stmt.body else None
                if isinstance(first, cst.ImportFrom) and isinstance(first.module, cst.Name) and first.module.value == '__future__':
                    insert_idx = i + 1
                    continue
                if isinstance(first, cst.Expr) and isinstance(first.value, (cst.SimpleString, cst.ConcatenatedString)):
                    # 顶层 docstring
                    insert_idx = i + 1
                    continue
                if isinstance(first, cst.EmptyLine):
                    continue
            break
        body_list.insert(insert_idx, new_stmt)
        return updated_node.with_changes(body=body_list)


def process_file(path: str) -> bool:
    try:
        with open(path, encoding='utf-8') as f:
            source = f.read()
    except Exception as e:
        print(f'读取失败 {path}: {e}')
        return False
    try:
        module = cst.parse_module(source)
    except Exception as e:
        print(f'解析失败 {path}: {e}')
        return False
    future_ann = has_future_annotations(module)
    transformer = AnnotationTransformer(future_annotations=future_ann)
    new_module = module.visit(transformer)
    if not transformer.modified:
        return False
    new_code = new_module.code
    if new_code == source:
        return False
    # 写入前验证语法
    try:
        compile(new_code, path, 'exec')
    except SyntaxError as e:
        print(f'语法错误 {path}: {e}')
        return False
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_code)
    return True


def main() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else '.'
    changed: list[str] = []
    errors: list[str] = []
    for r, dirs, files in os.walk(root):
        if any(x in r for x in ['.git', '__pycache__', '.venv', 'dist', 'build', 'node_modules', '.pytest_cache']):
            continue
        # 修剪目录
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if not f.endswith('.py'):
                continue
            full = os.path.join(r, f)
            # 转为相对路径
            rel = os.path.relpath(full, root)
            if not is_target(rel):
                continue
            try:
                if process_file(full):
                    changed.append(rel)
            except Exception as e:
                errors.append(f'{rel}: {e}')
    print(f'Modified: {len(changed)} files')
    for c in changed:
        print(f'  {c}')
    if errors:
        print('Errors:')
        for e in errors:
            print(f'  {e}')


if __name__ == '__main__':
    main()
