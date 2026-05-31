import os
import json
import httpx
from tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger


@register_tool(
    name="web_search",
    description="搜索互联网获取信息",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "max_results": {"type": "integer", "description": "最大结果数", "default": 5},
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=5,
)
async def web_search(query: str, max_results: int = 5) -> ToolResult:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            )
            if resp.status_code == 200:
                data = resp.json()
                results = []
                if data.get("AbstractText"):
                    results.append({"title": "摘要", "snippet": data["AbstractText"]})
                for topic in data.get("RelatedTopics", [])[:max_results]:
                    if isinstance(topic, dict) and topic.get("Text"):
                        results.append({"title": topic.get("Text", "")[:50], "snippet": topic.get("Text", "")})
                if results:
                    return ToolResult.ok(results)

            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "html.parser")
                results = []
                for result in soup.select(".result")[:max_results]:
                    title = result.select_one(".result__title")
                    snippet = result.select_one(".result__snippet")
                    if title and snippet:
                        results.append({"title": title.get_text(strip=True), "snippet": snippet.get_text(strip=True)})
                if results:
                    return ToolResult.ok(results)

            return ToolResult.ok([{"title": "搜索结果", "snippet": f"未能获取到关于「{query}」的搜索结果，请尝试其他关键词。"}])
    except Exception as e:
        return ToolResult.fail(f"搜索失败：{str(e)}")
