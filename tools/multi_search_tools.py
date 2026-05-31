import os
import asyncio
import httpx
from tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger


@register_tool(
    name="multi_search",
    description="多引擎搜索，同时搜索多个搜索引擎获取更全面的结果",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "engines": {"type": "string", "description": "搜索引擎列表，逗号分隔", "default": "duckduckgo,searx"},
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=3,
)
async def multi_search(query: str, engines: str = "duckduckgo,searx") -> ToolResult:
    engine_list = [e.strip() for e in engines.split(",")]
    results = []

    async def search_ddg():
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": 1},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    items = []
                    if data.get("AbstractText"):
                        items.append({"source": "DuckDuckGo", "title": "摘要", "snippet": data["AbstractText"]})
                    for topic in data.get("RelatedTopics", [])[:3]:
                        if isinstance(topic, dict) and topic.get("Text"):
                            items.append({"source": "DuckDuckGo", "title": topic.get("Text", "")[:50], "snippet": topic.get("Text", "")})
                    return items
        except Exception:
            return []
        return []

    async def search_searx():
        searx_instances = [
            "https://search.projectsegfau.lt/search",
            "https://searx.be/search",
        ]
        for instance in searx_instances:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        instance,
                        params={"q": query, "format": "json"},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        items = []
                        for r in data.get("results", [])[:3]:
                            items.append({"source": "SearX", "title": r.get("title", ""), "snippet": r.get("content", "")})
                        return items
            except Exception:
                continue
        return []

    tasks = []
    if "duckduckgo" in engine_list:
        tasks.append(search_ddg())
    if "searx" in engine_list:
        tasks.append(search_searx())

    if not tasks:
        return ToolResult.fail("没有可用的搜索引擎")

    engine_results = await asyncio.gather(*tasks, return_exceptions=True)
    for er in engine_results:
        if isinstance(er, list):
            results.extend(er)

    if not results:
        return ToolResult.ok([{"title": "搜索结果", "snippet": f"未能获取到关于「{query}}」的搜索结果"}])

    return ToolResult.ok(results)
