import os
import shutil
from pathlib import Path
from tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger


@register_tool(
    name="read_file",
    description="读取文件内容",
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件路径"},
            "max_lines": {"type": "integer", "description": "最大读取行数", "default": 100},
        },
        "required": ["file_path"],
    },
    permission=ToolPermission.READ_ONLY,
    category="file",
    max_frequency=10,
)
async def read_file(file_path: str, max_lines: int = 100) -> ToolResult:
    try:
        path = Path(file_path).resolve()
        if not path.exists():
            return ToolResult.fail(f"文件不存在：{file_path}")
        if path.stat().st_size > 10 * 1024 * 1024:
            return ToolResult.fail("文件太大（>10MB），请指定读取范围")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                lines.append(line)
        return ToolResult.ok("" .join(lines))
    except Exception as e:
        return ToolResult.fail(f"读取文件失败：{str(e)}")


@register_tool(
    name="write_file",
    description="写入文件内容",
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "要写入的内容"},
            "mode": {"type": "string", "description": "写入模式：write(覆盖) 或 append(追加)", "default": "write"},
        },
        "required": ["file_path", "content"],
    },
    permission=ToolPermission.READ_WRITE,
    category="file",
    max_frequency=5,
)
async def write_file(file_path: str, content: str, mode: str = "write") -> ToolResult:
    try:
        path = Path(file_path).resolve()
        os.makedirs(path.parent, exist_ok=True)
        with open(path, mode[0] if mode in ["w", "write"] else "a", encoding="utf-8") as f:
            f.write(content)
        return ToolResult.ok(f"文件已写入：{file_path}")
    except Exception as e:
        return ToolResult.fail(f"写入文件失败：{str(e)}")


@register_tool(
    name="list_files",
    description="列出目录中的文件",
    schema={
        "type": "object",
        "properties": {
            "dir_path": {"type": "string", "description": "目录路径", "default": "."},
            "max_items": {"type": "integer", "description": "最大列出数量", "default": 50},
        },
        "required": [],
    },
    permission=ToolPermission.READ_ONLY,
    category="file",
    max_frequency=10,
)
async def list_files(dir_path: str = ".", max_items: int = 50) -> ToolResult:
    try:
        path = Path(dir_path).resolve()
        if not path.exists():
            return ToolResult.fail(f"目录不存在：{dir_path}")
        items = []
        for i, item in enumerate(path.iterdir()):
            if i >= max_items:
                break
            items.append({
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else 0,
            })
        return ToolResult.ok(items)
    except Exception as e:
        return ToolResult.fail(f"列出目录失败：{str(e)}")


@register_tool(
    name="search_files",
    description="在文件中搜索文本",
    schema={
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "搜索关键词"},
            "dir_path": {"type": "string", "description": "搜索目录", "default": "."},
            "file_pattern": {"type": "string", "description": "文件名模式", "default": "*"},
        },
        "required": ["keyword"],
    },
    permission=ToolPermission.READ_ONLY,
    category="file",
    max_frequency=5,
)
async def search_files(keyword: str, dir_path: str = ".", file_pattern: str = "*") -> ToolResult:
    try:
        path = Path(dir_path).resolve()
        results = []
        for file_path in path.rglob(file_pattern):
            if not file_path.is_file():
                continue
            if file_path.stat().st_size > 5 * 1024 * 1024:
                continue
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if keyword.lower() in line.lower():
                            results.append({
                                "file": str(file_path),
                                "line": i,
                                "content": line.strip()[:200],
                            })
                            if len(results) >= 50:
                                return ToolResult.ok(results)
            except Exception:
                continue
        return ToolResult.ok(results)
    except Exception as e:
        return ToolResult.fail(f"搜索失败：{str(e)}")
