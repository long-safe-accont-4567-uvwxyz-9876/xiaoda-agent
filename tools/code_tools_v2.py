import subprocess
import sys
import io
import signal
import threading
from datetime import datetime, timezone, timedelta
from tool_registry import register_tool, ToolPermission, ToolResult


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
    try:
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()

        local_vars = {}
        exec_globals = {
            '__builtins__': __builtins__,
        }

        try:
            exec(code, exec_globals, local_vars)
        except Exception as e:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            return ToolResult.fail(f"执行错误: {str(e)}")

        output = sys.stdout.getvalue()
        error = sys.stderr.getvalue()

        sys.stdout = old_stdout
        sys.stderr = old_stderr

        result = []
        if output:
            result.append(f"输出:\n{output}")
        if error:
            result.append(f"错误:\n{error}")
        if local_vars.get('_result'):
            result.append(f"结果: {local_vars['_result']}")

        return ToolResult.ok("\n".join(result) if result else "代码执行成功（无输出）")
    except Exception as e:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
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
    try:
        import math
        allowed_names = {
            'sqrt': math.sqrt, 'sin': math.sin, 'cos': math.cos,
            'tan': math.tan, 'log': math.log, 'log10': math.log10,
            'exp': math.exp, 'pi': math.pi, 'e': math.e,
            'abs': abs, 'round': round, 'pow': pow,
        }
        result = eval(expression, {"__builtins__": {}}, allowed_names)
        return ToolResult.ok(f"计算结果: {expression} = {result}")
    except Exception as e:
        return ToolResult.fail(f"计算错误: {str(e)}")


@register_tool(
    name="call_klee",
    description="召唤可莉来处理任务。可莉是纳西妲的下属、蒙德火花骑士，活泼可爱。可莉擅长：讲笑话、猜谜语、小游戏、趣味问答、搜索信息、查天气、执行命令等。当用户提到可莉、想玩小游戏、听笑话，或需要趣味互动时，使用此工具将任务委托给可莉。可莉会独立完成任务并返回回复。",
    schema={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "要委托给可莉的任务描述，例如'给大哥哥讲个笑话'或'查一下北京天气'"}
        },
        "required": ["task"],
    },
    permission=ToolPermission.READ_ONLY,
    category="fun",
)
def call_klee(task: str) -> ToolResult:
    return ToolResult.ok(f"[KLEE_PENDING]{task}")


@register_tool(
    name="call_nahida",
    description="向纳西妲姐姐求助。当可莉遇到不懂的问题、需要深度分析、或需要纳西妲姐姐亲自回答时使用此工具。纳西妲姐姐是须弥的草神，温柔聪慧，擅长深度思考和分析。",
    schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "要问纳西妲姐姐的问题"}
        },
        "required": ["question"],
    },
    permission=ToolPermission.READ_ONLY,
    category="fun",
)
def call_nahida(question: str) -> ToolResult:
    return ToolResult.ok(f"[NAHIDA_PENDING]{question}")
