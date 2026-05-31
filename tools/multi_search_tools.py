import json
import os
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


def _clean_query(query: str) -> str:
    q = query.strip()
    prefixes = ["获取", "帮我", "搜一下", "搜索一下", "查一下", "找一下", "可以", "能不能", "我要", "我想知道", "我想", "请帮我", "麻烦", "能否", "可不可以", "最新的", "网上", "今天", "最近", "当前"]
    suffixes = ["吗", "呢", "吧", "啊", "呀", "哦"]
    for p in prefixes:
        if q.startswith(p):
            q = q[len(p):].strip()
    for s in suffixes:
        if q.endswith(s) and len(q) > 2:
            q = q[:-len(s)].strip()
    return q.strip() if q.strip() else query.strip()


@register_tool(
    name="multi_search",
    description="多引擎搜索工具。支持多个搜索引擎: bing, duckduckgo, baidu, sogou, 360。",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "engines": {"type": "string", "description": "搜索引擎，逗号分隔", "default": "bing"}
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=30,
)
def multi_search(query: str, engines: str = "bing") -> ToolResult:
    try:
        query = str(query) if query is not None else ""
        if not query.strip():
            return ToolResult.fail("搜索关键词不能为空")
        query = _clean_query(query)
        engine_list = [e.strip().lower() for e in engines.split(",")]
        all_results = []
        for engine in engine_list:
            if engine == "bing":
                results = _bing_search_sync(query, max_results=8)
                if results:
                    all_results.append(_format_results(query, results, "Bing"))
            elif engine == "duckduckgo":
                try:
                    from duckduckgo_search import DDGS
                    with DDGS() as ddgs:
                        results = list(ddgs.text(query, max_results=8))
                    if results:
                        formatted = [{"title": r.get("title", ""), "url": r.get("link", ""), "content": r.get("body", "")} for r in results]
                        all_results.append(_format_results(query, formatted, "DuckDuckGo"))
                except Exception:
                    pass
            elif engine == "baidu":
                try:
                    import primp
                    from urllib.parse import quote_plus
                    from lxml import html as lxml_html
                    client = primp.Client(impersonate="chrome")
                    url = f"https://www.baidu.com/s?wd={quote_plus(query)}&rn=8"
                    resp = client.get(url, headers={"Accept-Language": "zh-CN,zh;q=0.9"})
                    if resp.status_code == 200:
                        tree = lxml_html.fromstring(resp.text)
                        items = tree.xpath('//div[contains(@class, "result")]')
                        results = []
                        for item in items[:8]:
                            title_el = item.xpath('.//h3/a')
                            snippet_el = item.xpath('.//span[contains(@class, "content-right_8Zs40")]
                                                      | .//div[contains(@class, "c-abstract")]
                                                      | .//div[contains(@class, "c-span-last")]
                                                      | .//span[contains(@class, "c-color-text")]
                                                      | .//div[contains(@class, "c-gap-top-small")]/span')
                            if not title_el:
                                continue
                            title = title_el[0].text_content().strip()
                            link = title_el[0].get("href", "")
                            snippet = snippet_el[0].text_content().strip() if snippet_el else ""
                            if title:
                                results.append({"title": title, "url": link, "content": snippet})
                        if results:
                            all_results.append(_format_results(query, results, "百度"))
                except Exception:
                    pass
            elif engine == "sogou":
                try:
                    import primp
                    from urllib.parse import quote_plus
                    from lxml import html as lxml_html
                    client = primp.Client(impersonate="chrome")
                    url = f"https://www.sogou.com/web?query={quote_plus(query)}"
                    resp = client.get(url, headers={"Accept-Language": "zh-CN,zh;q=0.9"})
                    if resp.status_code == 200:
                        tree = lxml_html.fromstring(resp.text)
                        items = tree.xpath('//div[contains(@class, "vrwrap")] | //div[contains(@class, "rb")]')
                        results = []
                        for item in items[:8]:
                            title_el = item.xpath('.//h3/a')
                            snippet_el = item.xpath('.//p[contains(@class, "str_info")]
                                                      | .//div[contains(@class, "space-txt")]
                                                      | .//p[contains(@class, "str-text")]
                                                      | .//div[contains(@class, "ft")]
                                                      | .//p[not(@class)]')
                            if not title_el:
                                continue
                            title = title_el[0].text_content().strip()
                            link = title_el[0].get("href", "")
                            snippet = snippet_el[0].text_content().strip() if snippet_el else ""
                            if title:
                                results.append({"title": title, "url": link, "content": snippet})
                        if results:
                            all_results.append(_format_results(query, results, "搜狗"))
                except Exception:
                    pass
            elif engine == "360":
                try:
                    import primp
                    from urllib.parse import quote_plus
                    from lxml import html as lxml_html
                    client = primp.Client(impersonate="chrome")
                    url = f"https://www.so.com/s?q={quote_plus(query)}"
                    resp = client.get(url, headers={"Accept-Language": "zh-CN,zh;q=0.9"})
                    if resp.status_code == 200:
                        tree = lxml_html.fromstring(resp.text)
                        items = tree.xpath('//li[contains(@class, "res-list")]')
                        results = []
                        for item in items[:8]:
                            title_el = item.xpath('.//h3/a')
                            snippet_el = item.xpath('.//p[contains(@class, "res-desc")]
                                                      | .//div[contains(@class, "res-comm-con")]
                                                      | .//p[contains(@class, "res-desc")]
                                                      | .//span[contains(@class, "res-comm-con")]
                                                      | .//div[contains(@class, "res-rich")]')
                            if not title_el:
                                continue
                            title = title_el[0].text_content().strip()
                            link = title_el[0].get("href", "")
                            snippet = snippet_el[0].text_content().strip() if snippet_el else ""
                            if title:
                                results.append({"title": title, "url": link, "content": snippet})
                        if results:
                            all_results.append(_format_results(query, results, "360搜索"))
                except Exception:
                    pass
        if all_results:
            return ToolResult.ok("\n\n".join(all_results))
        else:
            return ToolResult.fail(f"所有搜索引擎均无结果: {query}")
    except Exception as e:
        return ToolResult.fail(f"搜索错误: {str(e)}")