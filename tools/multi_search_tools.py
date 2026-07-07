import os
import json
from loguru import logger
from tool_engine.tool_registry import register_tool, ToolResult, ToolPermission
from tools.web_tools_v2 import _bing_search_sync, _tavily_search_sync

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
WOLFRAMALPHA_API_KEY = os.getenv("WOLFRAMALPHA_API_KEY", "")


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


def _wolfram_api_query(query: str) -> ToolResult | None:
    """使用 WolframAlpha Full Results API v2 查询，失败返回 None 以便回退。"""
    try:
        import urllib.request, urllib.parse
        params = urllib.parse.urlencode({
            "appid": WOLFRAMALPHA_API_KEY,
            "input": query,
            "format": "plaintext",
            "output": "json",
        })
        url = f"https://api.wolframalpha.com/v2/query?{params}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        qr = data.get("queryresult", {})
        if not qr.get("success", False):
            return ToolResult.fail(f"WolframAlpha 无法理解查询: {query}")

        pods = qr.get("pods", [])
        if not pods:
            return ToolResult.fail(f"WolframAlpha 无结果: {query}")

        lines: list[str] = []
        extra_count = 0
        for pod in pods:
            pod_id = pod.get("id", "")
            title = pod.get("title", "")
            is_primary = pod.get("primary", False)
            subpods = pod.get("subpods", [])
            plaintexts = [sp.get("plaintext", "") for sp in subpods if sp.get("plaintext")]
            content = " | ".join(plaintexts)
            if not content:
                continue

            if pod_id == "Input":
                lines.insert(0, f"【{title}】{content}")
            elif is_primary:
                # 插入到 Input 之后（位置 1），确保主结果紧跟输入解释
                if lines and lines[0].startswith("【Input"):
                    lines.insert(1, f"【{title}】{content}")
                else:
                    lines.insert(0, f"【{title}】{content}")
            else:
                extra_count += 1
                if extra_count <= 3:
                    lines.append(f"【{title}】{content}")

        if not lines:
            return ToolResult.fail(f"WolframAlpha 无可用结果: {query}")

        return ToolResult.ok(f"WolframAlpha: {query}\n" + "\n".join(lines))
    except Exception:
        return None


@register_tool(
    name="wolfram_query",
    description=(
        "WolframAlpha 知识计算引擎。适用于：1)解方程/不等式（如'solve x^2+3x-4=0'）"
        "2)单位转换（如'100 km/h to mph'）3)科学数据查询（如'boiling point of ethanol'）"
        "4)化学方程式配平/分子量（如'molar mass of H2SO4'）5)物理常数（如'speed of light'）"
        "6)数学函数绘图/微积分（如'integrate sin(x) from 0 to pi'）"
        "注意：简单四则运算用 calculator，搜索新闻/资讯用 web_search，天气用 get_weather。"
        "query 建议用英文以获得最佳结果。"
    ),
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
    # 优先使用 API
    if WOLFRAMALPHA_API_KEY:
        result = _wolfram_api_query(query)
        if result is not None:
            return result
        # API 失败，回退到 web scraping

    if not WOLFRAMALPHA_API_KEY:
        logger.warning("WOLFRAMALPHA_API_KEY 未设置，使用不可靠的 web scraping 回退方案")

    # 回退：web scraping
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
