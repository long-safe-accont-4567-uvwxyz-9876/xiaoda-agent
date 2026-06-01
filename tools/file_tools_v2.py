import subprocess
import os
from pathlib import Path
from tool_registry import register_tool, ToolPermission, ToolResult


BLOCKED_COMMANDS = {'rm -rf /', 'mkfs', 'dd if='}


@register_tool(
    name="shell_command",
    description="执行 Shell 命令。输入要执行的命令字符串。",
    schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 Shell 命令"}
        },
        "required": ["command"],
    },
    permission=ToolPermission.EXECUTE,
    category="system",
    max_frequency=30,
)
def shell_command(command: str) -> ToolResult:
    for blocked in BLOCKED_COMMANDS:
        if blocked in command:
            return ToolResult.fail(f"命令包含不允许的操作: {blocked}")

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True, text=True,
            timeout=30, cwd=os.path.expanduser("~")
        )
        output = result.stdout if result.stdout else result.stderr
        data = output[:3000] if output else "命令执行成功（无输出）"
        return ToolResult.ok(data)
    except subprocess.TimeoutExpired:
        return ToolResult.fail("命令执行超时（30秒）")
    except Exception as e:
        return ToolResult.fail(f"执行错误: {str(e)}")


@register_tool(
    name="list_files",
    description="列出目录中的文件和文件夹。输入目录路径，默认为当前目录。",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径，默认 ~"}
        },
        "required": [],
    },
    permission=ToolPermission.READ_ONLY,
    category="file",
)
def list_files(path: str = "~") -> ToolResult:
    try:
        target_path = os.path.expanduser(path)
        if not os.path.exists(target_path):
            return ToolResult.fail(f"路径不存在: {path}")

        items = []
        for item in sorted(os.listdir(target_path)):
            full_path = os.path.join(target_path, item)
            is_dir = os.path.isdir(full_path)
            prefix = "📁" if is_dir else "📄"
            if is_dir:
                items.append(f"{prefix} {item}")
            else:
                try:
                    size = os.path.getsize(full_path)
                    for unit in ['B', 'KB', 'MB', 'GB']:
                        if size < 1024:
                            items.append(f"{prefix} {item} ({size:.1f}{unit})")
                            break
                        size /= 1024
                except OSError:
                    items.append(f"{prefix} {item}")

        return ToolResult.ok(f"目录: {target_path}\n" + "\n".join(items[:50]))
    except Exception as e:
        return ToolResult.fail(f"错误: {str(e)}")


@register_tool(
    name="read_file",
    description="读取文件内容。输入文件路径。",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "offset": {"type": "integer", "description": "起始行号，默认0", "default": 0},
            "limit": {"type": "integer", "description": "读取行数，默认200", "default": 200}
        },
        "required": ["path"],
    },
    permission=ToolPermission.READ_ONLY,
    category="file",
)
def read_file(path: str, offset: int = 0, limit: int = 200) -> ToolResult:
    try:
        target_path = os.path.expanduser(path)
        if not os.path.exists(target_path):
            return ToolResult.fail(f"文件不存在: {path}")
        if os.path.isdir(target_path):
            return ToolResult.fail(f"这是一个目录，不是文件: {path}")

        with open(target_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        selected = lines[offset:offset + limit]
        content = ''.join(selected)
        return ToolResult.ok(f"文件: {target_path}\n{'='*40}\n{content}")
    except Exception as e:
        return ToolResult.fail(f"读取错误: {str(e)}")


@register_tool(
    name="write_file",
    description="写入文件。输入格式: '文件路径|||内容'",
    schema={
        "type": "object",
        "properties": {
            "input_str": {"type": "string", "description": "格式: '文件路径|||内容'"}
        },
        "required": ["input_str"],
    },
    permission=ToolPermission.READ_WRITE,
    category="file",
    max_frequency=15,
)
def write_file(input_str: str) -> ToolResult:
    try:
        if '|||' not in input_str:
            return ToolResult.fail("格式错误。请使用: '文件路径|||内容'")

        path, content = input_str.split('|||', 1)
        target_path = os.path.expanduser(path)

        os.makedirs(os.path.dirname(target_path), exist_ok=True)

        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(content)

        return ToolResult.ok(f"文件已写入: {target_path}")
    except Exception as e:
        return ToolResult.fail(f"写入错误: {str(e)}")


@register_tool(
    name="search_files",
    description="搜索文件。输入搜索模式（支持通配符），如 '*.py' 或 '/home/**/*.txt'",
    schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "搜索模式，支持通配符"}
        },
        "required": ["pattern"],
    },
    permission=ToolPermission.READ_ONLY,
    category="file",
)
def search_files(pattern: str) -> ToolResult:
    import glob
    try:
        expanded = os.path.expanduser(pattern)
        matches = glob.glob(expanded, recursive=True)
        if not matches:
            return ToolResult.fail(f"未找到匹配文件: {pattern}")

        result = [f"📄 {m}" for m in matches[:30]]
        if len(matches) > 30:
            result.append(f"... 还有 {len(matches) - 30} 个文件")
        return ToolResult.ok("\n".join(result))
    except Exception as e:
        return ToolResult.fail(f"搜索错误: {str(e)}")
