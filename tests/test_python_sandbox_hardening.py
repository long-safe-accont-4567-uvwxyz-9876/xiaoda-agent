"""TDD 测试: python_executor 沙箱 AST 审查加固。

验证 _audit_code_ast 封堵三类绕过路径：
1. dunder 属性访问（__base__ / __mro__ / __subclasses__ / __globals__ / __init__ 等）
2. format 迷你语言中的 dunder 探测（字符串字面量内隐藏属性访问）
3. __import__ / getattr 反射调用
同时验证安全代码（算术、简单函数、白名单模块）仍可通过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from tools.code_tools_v2 import _audit_code_ast


# ━━━━━━━━ 危险代码应被拦截 ━━━━━━━━

@pytest.mark.parametrize("code", [
    # dunder 属性访问
    "x.__base__",
    "str.__base__",
    "Exception.__base__",
    "x.__mro__",
    "x.__subclasses__",
    "x.__globals__",
    "x.__init__",
    "x.__class__",
    "().__class__.__base__.__subclasses__()",
    # format 迷你语言 dunder 探测（属性访问隐藏在字符串字面量内）
    '"{0.__class__.__mro__}".format(x)',
    '"{0.__class__.__mro__[1].__init__.__globals__}".format(x)',
    '"{0.__base__}".format(x)',
    # 反射调用
    "__import__('os')",
    "getattr(x, '__class__')",
    # 未知 / 危险模块导入
    "import requests",
    "from requests import get",
    "import os.path",
    "from . import helper",
    "from .json import dumps",
])
def test_dangerous_code_blocked(code):
    """危险代码应被 AST 审查拦截。"""
    assert _audit_code_ast(code) is not None, f"应拦截危险代码: {code!r}"


# ━━━━━━━━ 安全代码应仍可通过 ━━━━━━━━

@pytest.mark.parametrize("code", [
    "1 + 1",
    "def f(x):\n    return x * 2\nresult = f(21)",
    "print('hello')",
    "import math\nresult = math.sqrt(16)",
    "import json\nresult = json.dumps({'a': 1})",
    "result = [i * i for i in range(5)]",
    'result = "hello {}".format("world")',
])
def test_safe_code_allowed(code):
    """安全代码不应被拦截。"""
    assert _audit_code_ast(code) is None, f"不应拦截安全代码: {code!r}"


def test_python_executor_description_matches_import_allowlist():
    """工具描述不应再声称支持任意第三方库，需与 AST 白名单一致。"""
    from tool_engine.tool_registry import get_tool

    tool = get_tool("python_executor")
    assert tool is not None
    description = tool["description"]
    assert "第三方" not in description
    assert "标准库" in description
