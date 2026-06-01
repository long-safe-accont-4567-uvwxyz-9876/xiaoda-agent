import os
import json
from tool_registry import register_tool, ToolPermission, ToolResult

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")


def _bing_search_sync(query: str, max_results: int = 8) -> list[dict]:
    import primp
    from lxml import html as lxml_html
    from urllib.parse import quote_plus

    client = primp.Client(impersonate="chrome")
    url = f"https://cn.bing.com/search?q={quote_plus(query)}&count={max_results}&setlang=zh-Hans"
    resp = client.get(url, headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})

    if resp.status_code != 200:
        return []

    tree = lxml_html.fromstring(resp.text)
    items = tree.xpath('//li[@class="b_algo"]')

    results = []
    for item in items[:max_results]:
        title_el = item.xpath('.//h2/a')
        snippet_el = item.xpath('.//div[@class="b_caption"]//p')
        if not title_el:
            continue
        title = title_el[0].text_content().strip()
        link = title_el[0].get("href", "")
        snippet = snippet_el[0].text_content().strip() if snippet_el else ""
        if title:
            results.append({"title": title, "url": link, "content": snippet})
    return results


def _tavily_search_sync(query: str, max_results: int = 6, search_depth: str = "basic") -> list[dict]:
    if not TAVILY_API_KEY:
        return []
    from tavily import TavilyClient
    client = TavilyClient(api_key=TAVILY_API_KEY)
    response = client.search(query, max_results=max_results, search_depth=search_depth)
    results = []
    for r in response.get("results", []):
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
        })
    return results


def _format_results(query: str, results: list[dict], engine_name: str = "") -> str:
    if not results:
        return ""
    parts = [f"搜索: {query}"]
    if engine_name:
        parts[0] += f" (via {engine_name})"
    parts.append("=" * 40)
    for i, r in enumerate(results):
        parts.append(f"\n{i+1}. {r.get('title', '')}")
        if r.get("content"):
            parts.append(f"   {r['content'][:250]}")
        if r.get("url"):
            parts.append(f"   链接: {r['url']}")
    return "\n".join(parts)


def _do_search(query: str, max_results: int = 8, use_tavily: bool = True) -> tuple[list[dict], str]:
    results = _bing_search_sync(query, max_results)
    if results:
        return results, "Bing"

    if use_tavily and TAVILY_API_KEY:
        try:
            results = _tavily_search_sync(query, max_results)
            if results:
                return results, "Tavily"
        except Exception:
            pass

    return [], ""


def _clean_query(query: str) -> str:
    q = query.strip()
    prefixes = ["获取", "帮我", "搜一下", "搜索一下", "查一下", "找一下", "可以", "能不能",
                "我要", "我想知道", "我想", "请帮我", "麻烦", "能否", "可不可以",
                "最新的", "网上", "今天", "最近", "当前"]
    suffixes = ["吗", "呢", "吧", "啊", "呀", "哦"]
    for p in prefixes:
        if q.startswith(p):
            q = q[len(p):].strip()
    for s in suffixes:
        if q.endswith(s) and len(q) > 2:
            q = q[:-len(s)].strip()
    return q.strip() if q.strip() else query.strip()


@register_tool(
    name="web_search",
    description="搜索互联网获取新闻、资料、百科等通用信息。使用Bing搜索引擎，返回搜索结果（标题+摘要+URL）。注意：天气查询请使用 get_weather 工具，不要用搜索。",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"}
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=30,
)
def web_search(query: str) -> ToolResult:
    try:
        query = str(query) if query is not None else ""
        if not query.strip():
            return ToolResult.fail("搜索关键词不能为空")
        query = _clean_query(query)

        results, engine = _do_search(query, max_results=8)
        if not results:
            return ToolResult.fail(f"搜索 '{query}' 无结果")

        formatted = _format_results(query, results, engine)
        return ToolResult.ok(formatted)
    except Exception as e:
        return ToolResult.fail(f"搜索错误: {str(e)}")


@register_tool(
    name="get_weather",
    description="获取指定城市的实时天气信息，包括温度、天气状况、风力、湿度等。当用户询问天气、气温、温度、是否下雨/下雪/晴天时，必须调用此工具获取准确数据，不要凭记忆回答。",
    schema={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名称，如'北京'、'上海'、'武汉'"}
        },
        "required": ["city"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
)
def get_weather(city: str) -> ToolResult:
    try:
        city = str(city) if city is not None else ""
        if not city.strip():
            return ToolResult.fail("城市名称不能为空")
        import urllib.request, urllib.parse, ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3&lang=zh"
        req = urllib.request.Request(url, headers={'User-Agent': 'curl'})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
            return ToolResult.ok(f"🌤️ {response.read().decode('utf-8').strip()}")
    except Exception as e:
        return ToolResult.fail(f"获取天气失败: {str(e)}")
