import asyncio
import subprocess
import sys
import io
import traceback
from tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger


@register_tool(
    name="python_executor",
    description="执行 Python 代码并返回结果",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的Python代码"},
        },
        "required": ["code"],
    },
    permission=ToolPermission.EXECUTE,
    category="code",
    max_frequency=10,
)
async def python_executor(code: str) -> ToolResult:
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    redirected_output = io.StringIO()
    redirected_error = io.StringIO()
    sys.stdout = redirected_output
    sys.stderr = redirected_error
    try:\        exec(code, {"__builtins__": __builtins__})
        output = redirected_output.getvalue()
        error = redirected_error.getvalue()
        if error:
            return ToolResult.fail(f"执行错误：{error}")
        return ToolResult.ok(output if output else "代码执行成功（无输出）")
    except Exception as e:
        return ToolResult.fail(f"执行异常：{traceback.format_exc()}")
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


@register_tool(
    name="shell_command",
    description="执行系统 Shell 命令",
    schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
        },
        "required": ["command"],
    },
    permission=ToolPermission.EXECUTE,
    category="system",
    max_frequency=5,
)
async def shell_command(command: str) -> ToolResult:
    BLOCKED = ["rm -rf /", "mkfs", "dd if=", ":(){ :|:& };:", "chmod -R 777 /"]
    for b in BLOCKED:
        if b in command:
            return ToolResult.fail("该命令已被安全策略拦截")
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            return ToolResult.fail(f"命令执行失败（返回码 {proc.returncode}）：{error}")
        return ToolResult.ok(output if output else "命令执行成功（无输出）")
    except asyncio.TimeoutError:
        return ToolResult.fail("命令执行超时（30秒）")
    except Exception as e:
        return ToolResult.fail(f"命令执行异常：{str(e)}")
