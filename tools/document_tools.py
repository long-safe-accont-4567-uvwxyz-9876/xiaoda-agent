import os
import json
from pathlib import Path
from tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger


@register_tool(
    name="document_reader",
    description="读取文档文件内容（支持 PDF、Word、Excel、CSV、TXT）",
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文档文件路径"},
            "max_pages": {"type": "integer", "description": "最大读取页数", "default": 10},
        },
        "required": ["file_path"],
    },
    permission=ToolPermission.READ_ONLY,
    category="document",
    max_frequency=5,
)
async def document_reader(file_path: str, max_pages: int = 10) -> ToolResult:
    try:
        path = Path(file_path).resolve()
        if not path.exists():
            return ToolResult.fail(f"文件不存在：{file_path}")

        suffix = path.suffix.lower()

        if suffix == ".txt":
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(50000)
            return ToolResult.ok(content)

        if suffix == ".csv":
            import csv
            rows = []
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f)
                for i, row in enumerate(reader):
                    if i > max_pages * 50:
                        break
                    rows.append(row)
            return ToolResult.ok(rows)

        if suffix in [".xlsx", ".xls"]:
            try:
                import openpyxl
                wb = openpyxl.load_workbook(path, read_only=True)
                result = {}
                for sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]
                    rows = []
                    for i, row in enumerate(ws.iter_rows(values_only=True)):
                        if i > 100:
                            break
                        rows.append(list(row))
                    result[sheet_name] = rows
                wb.close()
                return ToolResult.ok(result)
            except ImportError:
                return ToolResult.fail("需要安装 openpyxl：pip install openpyxl")

        if suffix == ".pdf":
            try:
                import PyPDF2
                with open(path, "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    text = ""
                    for i, page in enumerate(reader.pages):
                        if i >= max_pages:
                            break
                        text += page.extract_text() or ""
                return ToolResult.ok(text[:50000])
            except ImportError:
                return ToolResult.fail("需要安装 PyPDF2：pip install PyPDF2")

        if suffix in [".docx", ".doc"]:
            try:
                import docx
                doc = docx.Document(path)
                text = "\n".join([p.text for p in doc.paragraphs])
                return ToolResult.ok(text[:50000])
            except ImportError:
                return ToolResult.fail("需要安装 python-docx：pip install python-docx")

        return ToolResult.fail(f"不支持的文件格式：{suffix}")
    except Exception as e:
        return ToolResult.fail(f"读取文档失败：{str(e)}")
