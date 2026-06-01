import os
import json
from tool_registry import register_tool, ToolResult, ToolPermission
from tools.web_tools_v2 import _bing_search_sync, _tavily_search_sync, _format_results, _clean_query

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")


def _deep_search(query: str, max_results: int = 10) -> tuple[list[dict], str]:
    bing = _bing_search_sync(query, max_results=max_results)

    if bing:
        return bing, "Bing"

    if TAVILY_API_KEY:
        try:
            results = _tavily_search_sync(query, max_results, search_depth="advanced")
            if results:
                return results, "Tavily"
        except Exception:
            pass

    return [], ""


@register_tool(
    name="multi_search",
    description="[已禁用] 请使用 web_search 代替。",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="search",
    max_frequency=0,
)
def multi_search(query: str) -> ToolResult:
    return ToolResult.fail("multi_search 已禁用，请使用 web_search")


@register_tool(
    name="wolfram_query",
    description="WolframAlpha知识计算。用于数学计算、单位转换、科学查询等。",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "计算表达式"},
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="search",
    max_frequency=5,
)
def wolfram_query(query: str) -> ToolResult:
    try:
        import urllib.request, urllib.parse, ssl, re
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        url = f"https://www.wolframalpha.com/input?i={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
            values = re.findall(r'"plaintext":"([^"]+)"', html)
            if values:
                return ToolResult.ok(f"WolframAlpha: {query}\n结果: {values[0][:200]}")
            return ToolResult.ok(f"WolframAlpha: {query}\n请查看: {url}")
    except Exception as e:
        return ToolResult.fail(f"WolframAlpha查询失败: {str(e)[:100]}")
