import os
import httpx
import re
from tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger


@register_tool(
    name="web_browse",
    description="浏览网页并提取文本内容",
    schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "网页URL"},
            "max_length": {"type": "integer", "description": "最大返回长度", "default": 5000},
        },
        "required": ["url"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=5,
)
async def web_browse(url: str, max_length: int = 5000) -> ToolResult:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return ToolResult.fail(f"HTTP {resp.status_code}")

            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type:
                return ToolResult.ok(f"非HTML内容：{content_type}")

            html = resp.text

            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
                text = re.sub(r'\n{3,}', '\n\n', text)
                return ToolResult.ok(text[:max_length])
            except ImportError:
                text = re.sub(r'<[^>]+>', ' ', html)
                text = re.sub(r'\s+', ' ', text).strip()
                return ToolResult.ok(text[:max_length])
    except Exception as e:
        return ToolResult.fail(f"浏览网页失败：{str(e)}")
