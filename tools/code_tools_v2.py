from typing import Any
import subprocess
import sys
import io
import signal
import contextlib
import threading
import ast
import os
import math
from datetime import datetime, timezone, timedelta
from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger
from config import get_agent_display_name

_NAHIDA_DN = get_agent_display_name('xiaoda')
_KELI_DN = get_agent_display_name('xiaoli')

# python_executor 执行超时（秒）
_EXEC_TIMEOUT = 30

# 审计模式：ast（默认）或 regex（回退）
_AUDIT_MODE = os.getenv("PYEXEC_AUDIT_MODE", "ast").strip().lower()

# ── 安全 builtins：仅保留无害的内建函数 ──
_SAFE_BUILTINS = {
    'print': print, 'len': len, 'range': range,
    'int': int, 'float': float, 'str': str, 'bool': bool,
    'list': list, 'dict': dict, 'tuple': tuple, 'set': set, 'frozenset': frozenset,
    'abs': abs, 'max': max, 'min': min, 'sum': sum,
    'sorted': sorted, 'reversed': reversed, 'enumerate': enumerate,
    'zip': zip, 'map': map, 'filter': filter, 'round': round,
    'isinstance': isinstance, 'issubclass': issubclass,
    'any': any, 'all': all, 'chr': chr, 'ord': ord,
    'hex': hex, 'oct': oct, 'bin': bin,
    'repr': repr, 'format': format, 'id': id,
    'slice': slice, 'property': property,
    'ValueError': ValueError, 'TypeError': TypeError,
    'KeyError': KeyError, 'IndexError': IndexError,
    'AttributeError': AttributeError, 'RuntimeError': RuntimeError,
    'StopIteration': StopIteration, 'ZeroDivisionError': ZeroDivisionError,
    'NotImplementedError': NotImplementedError,
    'Exception': Exception,
    'True': True, 'False': False, 'None': None,
}

# ── AST 审查：禁止导入的模块 ──
_BANNED_MODULE_NAMES = frozenset({
    'os', 'subprocess', 'socket', 'sys', 'shutil', 'pathlib', 'ctypes',
    'pickle', 'shelve', 'marshal', 'codecs', 'io', 'signal', 'threading',
    'multiprocessing', 'logging', 'tempfile', 'glob', 'fnmatch', 'linecache',
    'tokenize', 'dis', 'inspect', 'pkgutil', 'importlib', 'platform',
    'errno', 'fcntl', 'grp', 'pwd', 'resource', 'syslog', 'mmap',
    'nis', 'spwd', 'crypt', 'pty', 'pipes', 'commands', 'asyncio',
    'http', 'urllib', 'xml', 'email', 'ftplib', 'smtplib', 'telnetlib',
    'xmlrpc', 'webbrowser', 'cgi', 'cgitb', 'wsgiref',
})

# 安全开放的标准库（子进程隔离落地后可用）
_ALLOWED_MODULES = frozenset({'json', 're', 'datetime', 'collections', 'itertools', 'math'})

# 禁止调用的内建函数
_BANNED_BUILTINS = frozenset({
    '__import__', 'eval', 'exec', 'compile', 'open',
    'getattr', 'setattr', 'delattr', 'hasattr',
    'type', 'vars', 'globals', 'locals', 'dir', 'input',
    'breakpoint', 'memoryview',
})

# 禁止访问的 dunder 属性
_BANNED_DUNDER = frozenset({
    '__class__', '__mro__', '__subclasses__', '__bases__', '__globals__',
    '__builtins__', '__code__', '__func__', '__dict__', '__init__',
    '__new__', '__reduce__', '__getattribute__', '__setattr__',
    '__delattr__', '__import__', '__slots__',
})


def _audit_code_ast(code: str) -> str | None:
    """AST 解析审查：遍历语法树拦截危险操作，无法被字符串拼接/字节转义绕过"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"代码语法错误: {e}"

    for node in ast.walk(tree):
        # 检查 import 语句
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod_name = alias.name.split('.')[0]
                if mod_name in _BANNED_MODULE_NAMES:
                    return f"禁止导入模块: {alias.name}"
                if mod_name not in _ALLOWED_MODULES and mod_name not in {'math'}:
                    # 允许 math（已在 exec_globals 中提供）和 _ALLOWED_MODULES
                    pass  # 不拦截未知模块，只拦截已知危险模块

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod_name = node.module.split('.')[0]
                if mod_name in _BANNED_MODULE_NAMES:
                    return f"禁止从模块导入: {node.module}"

        # 检查函数调用
        elif isinstance(node, ast.Call):
            func_name = None
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                # 检查 dunder 属性访问
                attr = node.func.attr
                if attr in _BANNED_DUNDER:
                    return f"禁止访问危险属性: {attr}"

            if func_name in _BANNED_BUILTINS:
                return f"禁止调用: {func_name}()"

        # 检查属性访问（dunder）
        elif isinstance(node, ast.Attribute):
            if node.attr in _BANNED_DUNDER:
                return f"禁止访问危险属性: {node.attr}"

    return None


def _audit_code_regex(code: str) -> str | None:
    """正则审查（回退模式，可被绕过）"""
    import re
    _BANNED_MODULES = re.compile(
        r'\b(import\s+(os|subprocess|socket|sys|shutil|pathlib|ctypes|pickle|shelve|marshal|codecs|io|signal|threading|multiprocessing|logging|tempfile|glob|fnmatch|linecache|tokenize|ast|dis|inspect|pkgutil|importlib|platform|errno|fcntl|grp|pwd|resource|syslog|mmap|nis|spwd|crypt|pty|pipes|commands|asyncio|http|urllib|xml|email|ftplib|smtplib|telnetlib|xmlrpc|webbrowser|cgi|cgitb|wsgiref)|'
        r'from\s+(os|subprocess|socket|sys|shutil|pathlib|ctypes|pickle|shelve|marshal|codecs|io|signal|threading|multiprocessing|logging|tempfile|glob|fnmatch|linecache|tokenize|ast|dis|inspect|pkgutil|importlib|platform|errno|fcntl|grp|pwd|resource|syslog|mmap|nis|spwd|crypt|pty|pipes|commands|asyncio|http|urllib|xml|email|ftplib|smtplib|telnetlib|xmlrpc|webbrowser|cgi|cgitb|wsgiref)\s+import)\b'
    )
    _BANNED_PATTERNS = [
        (re.compile(r'__import__\s*\('), '禁止使用 __import__()'),
        (re.compile(r'\bopen\s*\('), '禁止使用 open() 文件操作'),
        (re.compile(r'__(class|mro|subclasses|bases|globals|builtins|code|func|dict|init|new|reduce|getattribute|setattr|delattr)__'), '禁止使用危险反射属性'),
        (re.compile(r'\beval\s*\('), '禁止使用 eval()'),
        (re.compile(r'\bexec\s*\('), '禁止使用 exec()'),
        (re.compile(r'\bcompile\s*\('), '禁止使用 compile()'),
        (re.compile(r'\bgetattr\s*\('), '禁止使用 getattr()'),
        (re.compile(r'\bsetattr\s*\('), '禁止使用 setattr()'),
        (re.compile(r'\bdelattr\s*\('), '禁止使用 delattr()'),
        (re.compile(r'\bhasattr\s*\('), '禁止使用 hasattr()'),
        (re.compile(r'\btype\s*\('), '禁止使用 type() 元编程'),
        (re.compile(r'\bvars\s*\('), '禁止使用 vars()'),
        (re.compile(r'\bglobals\s*\('), '禁止使用 globals()'),
        (re.compile(r'\blocals\s*\('), '禁止使用 locals()'),
        (re.compile(r'\bdir\s*\('), '禁止使用 dir()'),
    ]
    if _BANNED_MODULES.search(code):
        return "代码包含禁止导入的危险模块"
    for pattern, reason in _BANNED_PATTERNS:
        if pattern.search(code):
            return reason
    return None


def _audit_code(code: str) -> str | None:
    """审查代码内容，返回拒绝原因或 None（通过）"""
    if _AUDIT_MODE == "regex":
        return _audit_code_regex(code)
    return _audit_code_ast(code)


class _ExecutionTimeout(Exception):
    """代码执行超时异常。"""
    pass


def _timeout_handler(signum: Any, frame: Any) -> None:
    """SIGALRM 信号处理函数，抛出执行超时异常。"""
    raise _ExecutionTimeout("代码执行超时")


@register_tool(
    name="get_current_time",
    description="获取当前的日期和时间（北京时间 Asia/Shanghai）。无需输入参数。",
    schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    permission=ToolPermission.READ_ONLY,
    category="system",
)
def get_current_time() -> ToolResult:
    """获取当前北京时间（含星期）。"""
    cst = timezone(timedelta(hours=8))
    now = datetime.now(cst)
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return ToolResult.ok(f"当前北京时间: {now.strftime('%Y年%m月%d日 %H:%M:%S')} {weekdays[now.weekday()]}")


@register_tool(
    name="python_executor",
    description="执行 Python 代码并返回结果。输入要执行的 Python 代码字符串。可用于计算、数据处理、文件操作等。支持 import 标准库和已安装的第三方库。",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码"}
        },
        "required": ["code"],
    },
    permission=ToolPermission.EXECUTE,
    category="code",
    max_frequency=5,
)
def python_executor(code: str) -> ToolResult:
    """执行 Python 代码并返回标准输出/错误（含安全审查和资源限制）。"""
    # 代码内容审查
    audit_result = _audit_code(code)
    if audit_result:
        logger.warning("pyexec.audit_blocked", reason=audit_result, code_preview=code[:200])
        return ToolResult.fail(f"代码安全审查未通过: {audit_result}")

    # resource 限制（子进程内存 50MB / CPU 10s）
    _resource_limits_set = False
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (50 * 1024 * 1024, 50 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        _resource_limits_set = True
    except (ImportError, ValueError, OSError):
        pass  # 非Linux 或权限不足，跳过

    try:
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        local_vars = {}
        exec_globals = {
            '__builtins__': _SAFE_BUILTINS,
            'math': math,
        }

        # 使用 contextlib.redirect_stdout/stderr 替代全局 sys.stdout 重定向，确保线程安全
        # 跨平台执行超时：UNIX 用 signal.alarm，Windows（无 SIGALRM）用守护线程 + join 超时
        _exec_state = {}

        def _run_code() -> None:
            """在受限全局环境中执行用户代码，捕获异常到 _exec_state。"""
            try:
                with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                    exec(code, exec_globals, local_vars)
            except BaseException as e:  # 捕获后交由主线程重新抛出
                _exec_state['error'] = e

        if hasattr(signal, 'SIGALRM'):
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(_EXEC_TIMEOUT)
            try:
                _run_code()
            except _ExecutionTimeout:
                return ToolResult.fail(f"代码执行超时（{_EXEC_TIMEOUT}秒）")
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
        else:
            # Windows 不支持 SIGALRM，改用守护线程执行 + join 超时
            _exec_thread = threading.Thread(target=_run_code, daemon=True)
            _exec_thread.start()
            _exec_thread.join(_EXEC_TIMEOUT)
            if _exec_thread.is_alive():
                return ToolResult.fail(f"代码执行超时（{_EXEC_TIMEOUT}秒）")

        # 重新抛出 exec 内部异常（如 MemoryError），交由外层 except 统一处理
        if 'error' in _exec_state:
            raise _exec_state['error']

        output = stdout_buf.getvalue()
        error = stderr_buf.getvalue()

        result = []
        if output:
            result.append(f"输出:\n{output}")
        if error:
            result.append(f"错误:\n{error}")
        if local_vars.get('_result'):
            result.append(f"结果: {local_vars['_result']}")

        return ToolResult.ok("\n".join(result) if result else "代码执行成功（无输出）")
    except _ExecutionTimeout:
        return ToolResult.fail(f"代码执行超时（{_EXEC_TIMEOUT}秒）")
    except MemoryError:
        return ToolResult.fail("代码执行内存超限（50MB）")
    except Exception as e:
        return ToolResult.fail(f"执行错误: {str(e)}")


@register_tool(
    name="calculator",
    description="计算数学表达式。输入数学表达式，如 '2+2' 或 'sqrt(16)'",
    schema={
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "数学表达式"}
        },
        "required": ["expression"],
    },
    permission=ToolPermission.READ_ONLY,
    category="code",
)
def calculator(expression: str) -> ToolResult:
    """计算数学表达式，仅允许常用数学函数和常量。"""
    try:
        # 安全加固：禁止访问危险属性和绕过手法
        dangerous_patterns = [
            '__',           # 禁止所有双下划线属性（涵盖 __globals__, __builtins__, __class__ 等）
            'import',       # 禁止 import
            'exec',         # 禁止 exec
            'eval',         # 禁止嵌套 eval
            'compile',      # 禁止 compile
            'open',         # 禁止 open
            'getattr',      # 禁止 getattr 绕过
            'setattr',      # 禁止 setattr
            'delattr',      # 禁止 delattr
            'type',         # 禁止 type 元编程
            'vars',         # 禁止 vars
            'dir',          # 禁止 dir
            'globals',      # 禁止 globals
            'locals',       # 禁止 locals
            'input',        # 禁止 input
        ]
        for pattern in dangerous_patterns:
            if pattern in expression:
                return ToolResult.fail(f"表达式包含不允许的内容: {pattern}")

        import math as _math
        allowed_names = {
            'sqrt': _math.sqrt, 'sin': _math.sin, 'cos': _math.cos,
            'tan': _math.tan, 'log': _math.log, 'log10': _math.log10,
            'log2': _math.log2, 'exp': _math.exp, 'pow': pow,
            'pi': _math.pi, 'e': _math.e, 'tau': _math.tau,
            'abs': abs, 'round': round,
            'ceil': _math.ceil, 'floor': _math.floor,
            'factorial': _math.factorial, 'gcd': _math.gcd,
        }
        result = eval(expression, {"__builtins__": {}}, allowed_names)
        return ToolResult.ok(f"计算结果: {expression} = {result}")
    except Exception as e:
        return ToolResult.fail(f"计算错误: {str(e)}")


@register_tool(
    name="call_xiaoda",
    description=f"向{_NAHIDA_DN}姐姐求助。当{_KELI_DN}遇到不懂的问题、需要深度分析、或需要{_NAHIDA_DN}姐姐亲自回答时使用此工具。{_NAHIDA_DN}姐姐是须弥的草神，温柔聪慧，擅长深度思考和分析。",
    schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": f"要问{_NAHIDA_DN}姐姐的问题"}
        },
        "required": ["question"],
    },
    permission=ToolPermission.READ_ONLY,
    category="fun",
)
def call_xiaoda(question: str) -> ToolResult:
    """委托问题给主体纳西妲处理（返回 DelegationRequest 占位）。"""
    from core.delegation import DelegationRequest
    return ToolResult.ok(DelegationRequest(type="xiaoda", question=question, delegator="xiaoli"))
