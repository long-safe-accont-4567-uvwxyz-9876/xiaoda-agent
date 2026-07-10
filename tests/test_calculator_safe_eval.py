"""TDD 测试: calculator 工具的安全 AST 求值器（H4 - eval() 注入修复）。

验证 calculator 不再使用 eval()，而是用安全 AST 求值器替代。
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from tools.code_tools_v2 import calculator


# ── 源码文件路径，用于静态检查 eval( 是否已被移除 ──
_SOURCE_FILE = Path(__file__).parent.parent / "tools" / "code_tools_v2.py"


# ━━━━━━━━ 正常数学表达式应正常工作 ━━━━━━━━

@pytest.mark.parametrize("expr,expected", [
    ("2+2", 4),
    ("sqrt(16)", 4.0),
    ("sin(pi/2)", 1.0),
    ("2**10", 1024),
])
def test_normal_math_expressions_work(expr, expected):
    """正常数学表达式应返回正确结果。"""
    result = calculator(expr)
    assert result.success, f"表达式 {expr} 应成功，但失败: {result.error}"
    assert str(expected) in result.data, f"结果数据 {result.data!r} 应包含 {expected}"


# ━━━━━━━━ eval() 不再被使用 ━━━━━━━━

def test_eval_not_used_in_source():
    """calculator 函数中不应调用 eval() 内建函数。

    使用 AST 精确检查 calculator 函数体内不存在对 eval 的 Call 节点，
    避免 _safe_eval 等含 'eval' 子串的标识符造成误报。
    """
    source = _SOURCE_FILE.read_text(encoding="utf-8")
    module_tree = ast.parse(source)
    for node in ast.walk(module_tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'calculator':
            for child in ast.walk(node):
                if (isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and child.func.id == 'eval'):
                    pytest.fail("calculator 函数中仍存在 eval() 调用")
            return
    pytest.fail("未找到 calculator 函数")


# ━━━━━━━━ dunder 访问应被阻止 ━━━━━━━━

@pytest.mark.parametrize("expr", [
    '().__class__',
    '"".__class__',
])
def test_dunder_access_blocked(expr, ):
    """双下划线属性访问应被阻止。"""
    result = calculator(expr)
    assert not result.success, f"表达式 {expr!r} 应被阻止，但成功了: {result.data}"


# ━━━━━━━━ 格式化字符串漏洞应被阻止 ━━━━━━━━

def test_format_string_exploit_blocked():
    """format 字符串属性泄露应被阻止。"""
    result = calculator('"{0.__class__}".format(42)')
    assert not result.success, "格式化字符串漏洞应被阻止"


# ━━━━━━━━ import 应被阻止 ━━━━━━━━

def test_import_blocked():
    """__import__ 应被阻止。"""
    result = calculator('__import__("os")')
    assert not result.success, "__import__ 应被阻止"


# ━━━━━━━━ 下划线开头的属性访问应被阻止 ━━━━━━━━

def test_underscore_attribute_blocked():
    """下划线开头的属性访问应被阻止。"""
    result = calculator('()._private')
    assert not result.success, "下划线开头属性访问应被阻止"


# ━━━━━━━━ 所有属性访问都应被阻止（安全加固） ━━━━━━━━

@pytest.mark.parametrize("expr", [
    '"abc".upper()',
    '(1).real',
    '[1,2,3].append(4)',
])
def test_all_attribute_access_blocked(expr):
    """安全 AST 求值器应阻止所有属性访问，而非仅下划线开头。"""
    result = calculator(expr)
    assert not result.success, f"表达式 {expr!r} 包含属性访问，应被阻止"
