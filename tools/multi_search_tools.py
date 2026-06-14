import os
import json
from tool_engine.tool_registry import register_tool, ToolResult, ToolPermission
from tools.web_tools_v2 import _bing_search_sync, _tavily_search_sync, _format_results, _clean_query

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")


def _deep_search(query: str, max_results: int = 10) -> tuple[list[dict], str]:
    bing = _bing_search_sync(query, max_results=max_results)

    if bing:
        return bing, "Bing"

    if TAVILY_API_KEY:
        try:
            results, _answer = _tavily_search_sync(query, max_results, search_depth="advanced")
            if results:
                return results, "Tavily"
        except Exception:
            pass

    return [], ""


# multi_search 已禁用，不再注册到工具列表。
# 如需重新启用，取消下方注释即可。

# @register_tool(
#     name="multi_search",
#     description="多源搜索：先尝试 Bing，无结果时回退到 Tavily。",
#     schema={
#         "type": "object",
#         "properties": {
#             "query": {"type": "string", "description": "搜索关键词"},
#         },
#         "required": ["query"],
#     },
#     permission=ToolPermission.READ_ONLY,
#     category="search",
#     max_frequency=5,
# )
# def multi_search(query: str) -> ToolResult:
#     results, source = _deep_search(query)
#     if not results:
#         return ToolResult.fail("搜索无结果，请尝试其他关键词")
#     formatted = _format_results(results, source)
#     return ToolResult.ok(formatted)


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
        import urllib.request, urllib.parse, re
        url = f"https://www.wolframalpha.com/input?i={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
            values = re.findall(r'"plaintext":"([^"]+)"', html)
            if values:
                return ToolResult.ok(f"WolframAlpha: {query}\n结果: {values[0][:200]}")
            return ToolResult.ok(f"WolframAlpha: {query}\n请查看: {url}")
    except Exception as e:
        return ToolResult.fail(f"WolframAlpha查询失败: {str(e)[:100]}")
