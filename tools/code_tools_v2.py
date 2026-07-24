import ast
import os
import signal
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger

from config import get_agent_display_name
from tool_engine.tool_registry import ToolPermission, ToolResult, register_tool

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
    'format': format,
    'slice': slice,
    'ValueError': ValueError, 'TypeError': TypeError,
    'KeyError': KeyError, 'IndexError': IndexError,
    'AttributeError': AttributeError, 'RuntimeError': RuntimeError,
    'StopIteration': StopIteration, 'ZeroDivisionError': ZeroDivisionError,
    'NotImplementedError': NotImplementedError,
    'Exception': Exception,
    'True': True, 'False': False, 'None': None,
}
# 注：已移除 id()/repr()/property — id() 泄漏内存地址，repr() 可触发 __repr__
# 副作用，property() 可用于描述符劫持逃逸

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
    except (ValueError, TypeError) as e:
        return f"代码解析失败: {e}"

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
        elif isinstance(node, ast.Attribute) and node.attr in _BANNED_DUNDER:
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


# 子进程执行用户代码的 wrapper 脚本：
# - 在子进程中重建 _SAFE_BUILTINS 沙箱（与父进程保持同步）
# - 从 stdin 读取用户代码（避免命令行长度限制和 shell 转义问题）
# - 通过单独 fd（_PYEXEC_RESULT_FD 环境变量传入）回传 _result，避免与 stdout/stderr 混淆
# 注意：safe_builtins 字典需与 _SAFE_BUILTINS 保持同步
_PYEXEC_WRAPPER_SOURCE = '''
import sys, io, contextlib, math, os

# Safe builtins: 仅保留无害的内建函数（与 _SAFE_BUILTINS 保持同步）
safe_builtins = {
    'print': print, 'len': len, 'range': range,
    'int': int, 'float': float, 'str': str, 'bool': bool,
    'list': list, 'dict': dict, 'tuple': tuple, 'set': set, 'frozenset': frozenset,
    'abs': abs, 'max': max, 'min': min, 'sum': sum,
    'sorted': sorted, 'reversed': reversed, 'enumerate': enumerate,
    'zip': zip, 'map': map, 'filter': filter, 'round': round,
    'isinstance': isinstance, 'issubclass': issubclass,
    'any': any, 'all': all, 'chr': chr, 'ord': ord,
    'hex': hex, 'oct': oct, 'bin': bin,
    'format': format,
    'slice': slice,
    'ValueError': ValueError, 'TypeError': TypeError,
    'KeyError': KeyError, 'IndexError': IndexError,
    'AttributeError': AttributeError, 'RuntimeError': RuntimeError,
    'StopIteration': StopIteration, 'ZeroDivisionError': ZeroDivisionError,
    'NotImplementedError': NotImplementedError,
    'Exception': Exception,
    'True': True, 'False': False, 'None': None,
}

exec_globals = {'__builtins__': safe_builtins, 'math': math}
local_vars = {}
_user_code = sys.stdin.read()
_stdout = io.StringIO()
_stderr = io.StringIO()
try:
    with contextlib.redirect_stdout(_stdout), contextlib.redirect_stderr(_stderr):
        exec(_user_code, exec_globals, local_vars)
except BaseException as e:
    _stderr.write("{}: {}".format(type(e).__name__, e))
sys.__stdout__.write(_stdout.getvalue())
sys.__stderr__.write(_stderr.getvalue())
# _result 通过单独 fd 回传（fd 号由环境变量 _PYEXEC_RESULT_FD 指定），
# 避免与用户 stdout 混淆；fd 号不固定，由父进程 os.pipe() 实际分配后传入
if "_result" in local_vars:
    try:
        _fd = int(os.environ.get("_PYEXEC_RESULT_FD", "0"))
        if _fd > 0:
            os.write(_fd, repr(local_vars["_result"]).encode("utf-8"))
    except (OSError, ValueError):
        pass
'''


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
    """获取当前时间（含星期），默认 Asia/Shanghai，支持 NUDGE_TIMEZONE 覆盖。"""
    tz_name = os.getenv("NUDGE_TIMEZONE", "Asia/Shanghai")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return ToolResult.ok(f"当前时间: {now.strftime('%Y年%m月%d日 %H:%M:%S')} {weekdays[now.weekday()]}")


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
    """执行 Python 代码并返回标准输出/错误（含安全审查和资源限制）。

    架构：用户代码在独立子进程中执行，父进程通过 stdin 传入代码，
    通过 stdout/stderr 收集输出，通过单独的 pipe 收集 _result。
    超时后通过 os.killpg(SIGKILL) 杀掉整个进程组（包括用户代码 spawn 的子进程）。

    安全：
    - 代码先经 _audit_code AST/正则 审查；
    - 子进程的 __builtins__ 被替换为 _SAFE_BUILTINS（无 __import__/open/eval 等）；
    - 子进程在独立进程组中运行，超时可强制 kill。
    """
    # 代码内容审查
    audit_result = _audit_code(code)
    if audit_result:
        logger.warning("pyexec.audit_blocked", reason=audit_result, code_preview=code[:200])
        return ToolResult.fail(f"代码安全审查未通过: {audit_result}")

    # 创建管道用于单独回传 _result（fd 号运行时由 os.pipe() 分配）
    result_r, result_w = os.pipe()

    try:
        # Unix: os.setsid 创建新进程组，便于 killpg 杀掉整个进程树
        # Windows: CREATE_NEW_PROCESS_GROUP（无 killpg，但 proc.kill() 可杀主进程）
        if os.name == "posix":
            preexec_fn = os.setsid
            creationflags = 0
        else:
            preexec_fn = None
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        # 通过环境变量把 result_w 的 fd 号传给子进程（pass_fds 保持原 fd 号不变）
        child_env = os.environ.copy()
        child_env["_PYEXEC_RESULT_FD"] = str(result_w)
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", _PYEXEC_WRAPPER_SOURCE],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(result_w,),
                env=child_env,
                cwd=os.path.expanduser("~"),
                preexec_fn=preexec_fn,
                creationflags=creationflags,
            )
        except FileNotFoundError:
            return ToolResult.fail(f"Python 解释器未找到: {sys.executable}")
        except OSError as e:
            return ToolResult.fail(f"启动子进程失败: {e!s}")
    finally:
        # 父进程关闭写端，子进程退出后读端可读到 EOF
        os.close(result_w)

    try:
        try:
            stdout_bytes, stderr_bytes = proc.communicate(
                input=code.encode("utf-8"), timeout=_EXEC_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            # 超时后强制 kill 整个进程组（包括用户代码 spawn 的子进程）
            _kill_process_group(proc)
            return ToolResult.fail(f"代码执行超时（{_EXEC_TIMEOUT}秒）")

        # 读取 _result（子进程退出后 pipe 写端已关闭，读到 EOF）
        try:
            result_data = os.read(result_r, 1024 * 1024).decode("utf-8", errors="replace")
        except OSError:
            result_data = ""
        finally:
            try:
                os.close(result_r)
            except OSError:
                pass

        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

        result = []
        if stdout:
            result.append(f"输出:\n{stdout}")
        if stderr:
            result.append(f"错误:\n{stderr}")
        if result_data:
            result.append(f"结果: {result_data}")

        return ToolResult.ok("\n".join(result) if result else "代码执行成功（无输出）")
    except Exception as e:
        return ToolResult.fail(f"执行错误: {e!s}")


def _kill_process_group(proc: subprocess.Popen) -> None:
    """杀掉子进程及其整个进程组（包括用户代码 spawn 的子进程）。

    Unix: 通过 os.killpg(SIGKILL) 杀掉进程组；
    Windows/兜底: proc.kill() 仅杀主进程，子进程可能泄漏（Windows 限制）。
    """
    if os.name == "posix" and proc.pid:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=5)
            return
        except ProcessLookupError:
            # 子进程已退出，无需 kill
            return
        except Exception:
            logger.debug("pyexec.killpg_error", exc_info=True)

    # Windows 或 killpg 失败的兜底
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except Exception:
        logger.debug("pyexec.kill_error", exc_info=True)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _safe_eval(expr_tree, allowed_names):
    """安全 AST 求值器：递归评估白名单节点，拒绝所有属性访问和导入。"""
    _SAFE_BINOPS = {
        ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b,
        ast.Mod: lambda a, b: a % b,
        ast.Pow: lambda a, b: a ** b,
        ast.FloorDiv: lambda a, b: a // b,
        ast.LShift: lambda a, b: a << b,
        ast.RShift: lambda a, b: a >> b,
        ast.BitOr: lambda a, b: a | b,
        ast.BitAnd: lambda a, b: a & b,
        ast.BitXor: lambda a, b: a ^ b,
    }
    _SAFE_UNARYOPS = {
        ast.USub: lambda a: -a,
        ast.UAdd: lambda a: +a,
        ast.Not: lambda a: not a,
        ast.Invert: lambda a: ~a,
    }
    _SAFE_CMPOPS = {
        ast.Eq: lambda a, b: a == b,
        ast.NotEq: lambda a, b: a != b,
        ast.Lt: lambda a, b: a < b,
        ast.LtE: lambda a, b: a <= b,
        ast.Gt: lambda a, b: a > b,
        ast.GtE: lambda a, b: a >= b,
        ast.Is: lambda a, b: a is b,
        ast.IsNot: lambda a, b: a is not b,
        ast.In: lambda a, b: a in b,
        ast.NotIn: lambda a, b: a not in b,
    }

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Num):  # pragma: no cover  兼容旧版
            return node.n
        if isinstance(node, ast.Str):  # pragma: no cover  兼容旧版
            return node.s
        if isinstance(node, ast.NameConstant):  # pragma: no cover  兼容旧版
            return node.value
        if isinstance(node, ast.Name):
            if node.id in allowed_names:
                return allowed_names[node.id]
            raise ValueError(f"不允许的名称: {node.id}")
        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in _SAFE_BINOPS:
                raise ValueError(f"不允许的运算符: {op_type.__name__}")
            return _SAFE_BINOPS[op_type](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            op_type = type(node.op)
            if op_type not in _SAFE_UNARYOPS:
                raise ValueError(f"不允许的一元运算符: {op_type.__name__}")
            return _SAFE_UNARYOPS[op_type](_eval(node.operand))
        if isinstance(node, ast.Compare):
            left = _eval(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                op_type = type(op)
                if op_type not in _SAFE_CMPOPS:
                    raise ValueError(f"不允许的比较运算符: {op_type.__name__}")
                right = _eval(comparator)
                if not _SAFE_CMPOPS[op_type](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result = True
                for value in node.values:
                    result = _eval(value)
                    if not result:
                        return result
                return result
            if isinstance(node.op, ast.Or):
                result = False
                for value in node.values:
                    result = _eval(value)
                    if result:
                        return result
                return result
            raise ValueError("不允许的布尔运算")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("不允许的函数调用方式")
            func_name = node.func.id
            if func_name not in allowed_names:
                raise ValueError(f"不允许的函数: {func_name}")
            func = allowed_names[func_name]
            args = [_eval(arg) for arg in node.args]
            kwargs = {kw.arg: _eval(kw.value) for kw in node.keywords if kw.arg is not None}
            return func(*args, **kwargs)
        if isinstance(node, ast.List):
            return [_eval(elt) for elt in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(_eval(elt) for elt in node.elts)
        if isinstance(node, ast.Dict):
            return {_eval(k): _eval(v) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.Set):
            return {_eval(elt) for elt in node.elts}
        if isinstance(node, ast.Subscript):
            value = _eval(node.value)
            if isinstance(node.slice, ast.Slice):
                lower = _eval(node.slice.lower) if node.slice.lower is not None else None
                upper = _eval(node.slice.upper) if node.slice.upper is not None else None
                step = _eval(node.slice.step) if node.slice.step is not None else None
                return value[lower:upper:step]
            return value[_eval(node.slice)]
        if isinstance(node, ast.IfExp):
            return _eval(node.body) if _eval(node.test) else _eval(node.orelse)
        if isinstance(node, ast.Attribute):
            raise ValueError("不允许属性访问")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("不允许导入")
        raise ValueError(f"不允许的表达式类型: {type(node).__name__}")

    return _eval(expr_tree)


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
        # 使用正则 \b 单词边界匹配，避免子串误报（如 'type' 误匹配 'prototype'）
        import re as _re
        dangerous_patterns = [
            (r'__', '双下划线属性'),           # 禁止所有双下划线属性
            (r'\bimport\b', 'import'),         # 禁止 import
            (r'\bexec\b', 'exec'),             # 禁止 exec
            (r'\beval\b', 'eval'),             # 禁止嵌套 eval
            (r'\bcompile\b', 'compile'),       # 禁止 compile
            (r'\bopen\b', 'open'),             # 禁止 open
            (r'\bgetattr\b', 'getattr'),       # 禁止 getattr 绕过
            (r'\bsetattr\b', 'setattr'),       # 禁止 setattr
            (r'\bdelattr\b', 'delattr'),       # 禁止 delattr
            (r'\btype\b', 'type'),             # 禁止 type 元编程
            (r'\bvars\b', 'vars'),             # 禁止 vars
            (r'\bdir\b', 'dir'),               # 禁止 dir
            (r'\bglobals\b', 'globals'),       # 禁止 globals
            (r'\blocals\b', 'locals'),         # 禁止 locals
            (r'\binput\b', 'input'),           # 禁止 input
        ]
        for pattern, name in dangerous_patterns:
            if _re.search(pattern, expression):
                return ToolResult.fail(f"表达式包含不允许的内容: {name}")

        _BLOCKED_CALLS = frozenset({
            '__import__', 'exec', 'eval', 'compile', 'open',
            'getattr', 'setattr', 'delattr', 'type',
            'vars', 'dir', 'globals', 'locals', 'input',
        })
        try:
            tree = ast.parse(expression, mode='eval')
        except SyntaxError:
            return ToolResult.fail("表达式语法错误")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.startswith('_'):
                return ToolResult.fail("表达式包含不允许的属性访问")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in _BLOCKED_CALLS:
                    return ToolResult.fail(f"表达式包含不允许的函数: {node.func.id}")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return ToolResult.fail("表达式包含不允许的 import")

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
        result = _safe_eval(tree, allowed_names)
        return ToolResult.ok(f"计算结果: {expression} = {result}")
    except Exception as e:
        return ToolResult.fail(f"计算错误: {e!s}")


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
    """委托问题给主体小妲处理（返回 DelegationRequest 占位）。"""
    try:
        from core.delegation import DelegationRequest
    except ImportError as e:
        logger.error("call_xiaoda.delegation_unavailable", exc_info=True)
        return ToolResult.fail(f"委托模块不可用: {e}")
    return ToolResult.ok(DelegationRequest(type="xiaoda", question=question, delegator="xiaoli"))
