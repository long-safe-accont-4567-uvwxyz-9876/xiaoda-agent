import os
import re
import json
import httpx
from urllib.parse import quote_plus
from loguru import logger
from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult

_UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_UA_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_HEADERS = {
    "User-Agent": _UA,
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _extract_balanced_json(text: str, start_pattern: str):
    """在 text 中查找 start_pattern 之后的平衡 JSON 对象并返回解析结果。"""
    m = re.search(start_pattern, text)
    if not m:
        return None
    start = m.end()
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


@register_tool(
    name="baidu_search",
    description="百度搜索——中文互联网搜索。当用户需要中文信息、国内资讯时优先使用。",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "count": {"type": "integer", "description": "返回结果数，默认8", "default": 8},
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=30,
)
async def baidu_search(query: str, count: int = 8) -> ToolResult:
    try:
        query = str(query) if query is not None else ""
        if not query.strip():
            return ToolResult.fail("搜索关键词不能为空")
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 8
        if count <= 0:
            count = 8

        from lxml import html as lxml_html

        baidu_headers = {**_HEADERS, "User-Agent": _UA_WIN}
        url = f"https://www.baidu.com/s?wd={quote_plus(query)}&rn={count}&ie=utf-8"
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=baidu_headers) as client:
            resp = await client.get(url)

        if resp.status_code != 200:
            logger.warning("baidu_search.bad_status status={} query={}", resp.status_code, query[:40])
            return await _fallback_web_search(query)

        tree = lxml_html.fromstring(resp.text)
        # 尝试多种 XPath 匹配百度结果容器
        items = (tree.xpath('//div[@tpl and contains(@class,"c-container")]')
                 or tree.xpath('//div[@tpl]')
                 or tree.xpath('//div[contains(@class,"c-container")]')
                 or tree.xpath('//div[contains(@class,"result")]'))

        lines = [f"百度搜索: {query}", "=" * 40]
        parsed = 0

        if items:
            for item in items[:count]:
                title_el = item.xpath('.//h3/a') or item.xpath('.//a[@href]')
                if not title_el:
                    continue
                title = title_el[0].text_content().strip()
                link = title_el[0].get("href", "")
                snippet_el = (item.xpath('.//span[contains(@class,"content-right")]')
                              or item.xpath('.//div[contains(@class,"c-abstract")]')
                              or item.xpath('.//span[contains(@class,"c-color-text")]')
                              or item.xpath('.//span[contains(@class,"c-font-normal")]')
                              or item.xpath('.//p'))
                snippet = snippet_el[0].text_content().strip() if snippet_el else ""
                if not title:
                    continue
                parsed += 1
                lines.append(f"\n{parsed}. {title}")
                if snippet:
                    lines.append(f"   {snippet[:250]}")
                if link:
                    lines.append(f"   链接: {link}")

        # 正则降级：从 HTML 中提取 <h3> 内的链接
        if parsed == 0:
            logger.info("baidu_search.regex_fallback query={}", query[:40])
            pattern = r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            for link, title_html in re.findall(pattern, resp.text, re.S)[:count]:
                title = re.sub(r'<[^>]+>', '', title_html).strip()
                if title and link:
                    parsed += 1
                    lines.append(f"\n{parsed}. {title}")
                    lines.append(f"   链接: {link}")

        if parsed == 0:
            logger.warning("baidu_search.no_items query={}", query[:40])
            return await _fallback_web_search(query)

        return ToolResult.ok("\n".join(lines))
    except Exception as e:
        logger.warning("baidu_search.error query={} error={}", query[:40], repr(e)[:200])
        return await _fallback_web_search(query)


async def _fallback_web_search(query: str) -> ToolResult:
    try:
        from tools.web_tools_v2 import web_search
        return await web_search(query)
    except Exception as e:
        return ToolResult.fail(f"百度搜索失败且降级 web_search 也失败: {str(e)[:150]}")


@register_tool(
    name="baidu_hot",
    description="获取百度热搜榜。当用户说'热搜''热点''大家都在搜什么'时使用。",
    schema={
        "type": "object",
        "properties": {
            "count": {"type": "integer", "description": "返回条数，默认15", "default": 15},
        },
        "required": [],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=10,
)
async def baidu_hot(count: int = 15) -> ToolResult:
    try:
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 15
        if count <= 0:
            count = 15

        hot_headers = {**_HEADERS, "Referer": "https://top.baidu.com/"}
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=hot_headers) as client:
            resp = await client.get("https://top.baidu.com/board?tab=realtime")

        items = []
        if resp.status_code == 200:
            state = _extract_balanced_json(resp.text, r'window\.__INITIAL_STATE__\s*=\s*')
            if state:
                try:
                    cards = (state.get("data", {}).get("cards", [])
                             or state.get("data", {}).get("content", []))
                    for card in cards:
                        for entry in card.get("content", []) or card.get("cards", []) or []:
                            # 热搜数据可能嵌套在 entry["content"] 中
                            if isinstance(entry, dict) and "content" in entry and isinstance(entry["content"], list):
                                for sub_entry in entry["content"]:
                                    title = sub_entry.get("word") or sub_entry.get("query") or sub_entry.get("title", "")
                                    desc = sub_entry.get("desc") or sub_entry.get("excerpt", "")
                                    hot = sub_entry.get("hotScore") or sub_entry.get("hot", "")
                                    if title:
                                        items.append({"title": title, "desc": desc, "hot": hot})
                            else:
                                title = entry.get("word") or entry.get("query") or entry.get("title", "")
                                desc = entry.get("desc") or entry.get("excerpt", "")
                                hot = entry.get("hotScore") or entry.get("hot", "")
                                if title:
                                    items.append({"title": title, "desc": desc, "hot": hot})
                except (AttributeError, TypeError) as e:
                    logger.warning("baidu_hot.parse_state_failed error={}", repr(e)[:150])

        if not items:
            items = await _baidu_hot_api_fallback()

        if not items:
            return ToolResult.fail("获取百度热搜失败：无数据返回")

        lines = ["百度热搜榜（实时）", "=" * 40]
        for i, it in enumerate(items[:count]):
            hot_str = f" 🔥{it['hot']}" if it.get("hot") else ""
            lines.append(f"\n{i+1}. {it['title']}{hot_str}")
            if it.get("desc"):
                lines.append(f"   {it['desc'][:200]}")

        return ToolResult.ok("\n".join(lines))
    except Exception as e:
        return ToolResult.fail(f"获取百度热搜失败: {str(e)[:150]}")


async def _baidu_hot_api_fallback() -> list[dict]:
    try:
        headers = {
            **_HEADERS,
            "Referer": "https://top.baidu.com/",
            "Accept": "application/json, text/plain, */*",
        }
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers) as client:
            resp = await client.get(
                "https://top.baidu.com/api/board?platform=wise&tab=realtime")
        if resp.status_code != 200:
            return []
        data = resp.json()
        items = []
        cards = data.get("data", {}).get("cards", [])
        for card in cards:
            for entry in card.get("content", []):
                # 热搜数据可能嵌套在 entry["content"] 中
                if isinstance(entry, dict) and "content" in entry and isinstance(entry["content"], list):
                    for sub_entry in entry["content"]:
                        title = sub_entry.get("word") or sub_entry.get("query", "")
                        desc = sub_entry.get("desc", "")
                        hot = sub_entry.get("hotScore", "")
                        if title:
                            items.append({"title": title, "desc": desc, "hot": hot})
                else:
                    title = entry.get("word") or entry.get("query", "")
                    desc = entry.get("desc", "")
                    hot = entry.get("hotScore", "")
                    if title:
                        items.append({"title": title, "desc": desc, "hot": hot})
        return items
    except Exception as e:
        logger.warning("baidu_hot.api_fallback_failed error={}", repr(e)[:150])
        return []


@register_tool(
    name="zhihu_search",
    description="搜索知乎问答。适合深度问题、技术讨论、经验分享、专业分析。当用户说'知乎上怎么说''搜一下知乎'时使用。",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "count": {"type": "integer", "description": "返回结果数，默认5", "default": 5},
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=15,
)
async def zhihu_search(query: str, count: int = 5) -> ToolResult:
    try:
        query = str(query) if query is not None else ""
        if not query.strip():
            return ToolResult.fail("搜索关键词不能为空")
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 5
        if count <= 0:
            count = 5

        api_url = (f"https://www.zhihu.com/api/v4/search_v3?t=general&q={quote_plus(query)}"
                   f"&correction=1&offset=0&limit={count}")
        headers = {**_HEADERS, "Referer": "https://www.zhihu.com/", "Cookie": "_zap="}

        items: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers) as client:
                resp = await client.get(api_url)
            if resp.status_code == 200:
                data = resp.json()
                for entry in data.get("data", []):
                    obj = entry.get("object", {})
                    title = obj.get("title", "") or entry.get("highlight", {}).get("title", "")
                    content = obj.get("excerpt", "") or entry.get("highlight", {}).get("content", "")
                    author = obj.get("author", {}).get("name", "")
                    link = obj.get("url", "")
                    if title:
                        items.append({"title": title, "content": content,
                                      "author": author, "url": link})
        except Exception as e:
            logger.warning("zhihu_search.api_failed error={}", repr(e)[:150])

        if not items:
            return await _zhihu_bing_fallback(query, count)

        lines = [f"知乎搜索: {query}", "=" * 40]
        for i, it in enumerate(items[:count]):
            lines.append(f"\n{i+1}. {it['title']}")
            if it.get("author"):
                lines.append(f"   作者: {it['author']}")
            if it.get("content"):
                lines.append(f"   {it['content'][:250]}")
            if it.get("url"):
                lines.append(f"   链接: {it['url']}")

        return ToolResult.ok("\n".join(lines))
    except Exception as e:
        return ToolResult.fail(f"知乎搜索失败: {str(e)[:150]}")


async def _zhihu_bing_fallback(query: str, count: int) -> ToolResult:
    """通过 Bing 的 site:zhihu.com 搜索知乎内容。"""
    try:
        from lxml import html as lxml_html
        bing_url = f"https://cn.bing.com/search?q=site%3Azhihu.com+{quote_plus(query)}&count={count}"
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=_HEADERS) as client:
            resp = await client.get(bing_url)
        if resp.status_code != 200:
            return ToolResult.fail(f"知乎搜索 '{query}' 无结果")
        tree = lxml_html.fromstring(resp.text)
        results = tree.xpath('//li[@class="b_algo"]') or tree.xpath('//li[contains(@class,"b_algo")]')
        lines = [f"知乎搜索: {query} (via Bing)", "=" * 40]
        parsed = 0
        for r in results[:count]:
            title_el = r.xpath('.//h2/a')
            if not title_el:
                continue
            title = title_el[0].text_content().strip()
            link = title_el[0].get("href", "")
            snippet_el = r.xpath('.//p') or r.xpath('.//div[contains(@class,"b_caption")]//p')
            snippet = snippet_el[0].text_content().strip() if snippet_el else ""
            if not title:
                continue
            parsed += 1
            lines.append(f"\n{parsed}. {title}")
            if snippet:
                lines.append(f"   {snippet[:250]}")
            if link:
                lines.append(f"   链接: {link}")
        if parsed == 0:
            return ToolResult.fail(f"知乎搜索 '{query}' 无结果")
        return ToolResult.ok("\n".join(lines))
    except Exception as e:
        return ToolResult.fail(f"知乎搜索失败: {str(e)[:150]}")


@register_tool(
    name="toutiao_search",
    description="今日头条搜索——国内新闻时事搜索。当用户搜新闻、时事、热点、最新动态时优先使用，时效性最好。",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "count": {"type": "integer", "description": "返回结果数，默认8", "default": 8},
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=20,
)
async def toutiao_search(query: str, count: int = 8) -> ToolResult:
    try:
        query = str(query) if query is not None else ""
        if not query.strip():
            return ToolResult.fail("搜索关键词不能为空")
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 8
        if count <= 0:
            count = 8

        url = (f"https://so.toutiao.com/search?dvpf=pc&source=input&keyword={quote_plus(query)}"
               f"&pd=information&action_type=search_subtab_switch&page_num=0&search_id=&backfill_id=")
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=_HEADERS) as client:
            resp = await client.get(url)

        if resp.status_code != 200:
            return ToolResult.fail(f"头条搜索失败: HTTP {resp.status_code}")

        items = []
        # 优先尝试解析内嵌 JSON 数据
        try:
            m = re.search(r"window\._INITIAL_STATE_\s*=\s*(\{.*?\});?\s*</script>",
                          resp.text, re.S)
            if m:
                state = json.loads(m.group(1))
                raw = (state.get("search", {}).get("data", [])
                       or state.get("prop", {}).get("asyncData", {}).get("search", {}).get("data", []))
                for entry in raw:
                    title = entry.get("title", "") or entry.get("abstract", "")
                    if not title:
                        continue
                    abstract = entry.get("abstract", "") or entry.get("description", "")
                    link = entry.get("url", "") or entry.get("display_url", "")
                    source = entry.get("source", "") or entry.get("media_name", "")
                    items.append({"title": title, "content": abstract,
                                  "source": source, "url": link})
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning("toutiao_search.parse_state_failed error={}", repr(e)[:150])

        # 降级：正则提取
        if not items:
            from lxml import html as lxml_html
            tree = lxml_html.fromstring(resp.text)
            cards = tree.xpath('//div[contains(@class,"result-content")]')
            for card in cards[:count]:
                title_el = card.xpath('.//a[contains(@class,"title")]') or card.xpath('.//a')
                if not title_el:
                    continue
                title = title_el[0].text_content().strip()
                link = title_el[0].get("href", "")
                snippet_el = card.xpath('.//p') or card.xpath('.//div[contains(@class,"abstract")]')
                snippet = snippet_el[0].text_content().strip() if snippet_el else ""
                if title:
                    items.append({"title": title, "content": snippet,
                                  "source": "", "url": link})

        if not items:
            return ToolResult.fail(f"头条搜索 '{query}' 无结果")

        lines = [f"头条搜索: {query}", "=" * 40]
        for i, it in enumerate(items[:count]):
            src = f" [{it['source']}]" if it.get("source") else ""
            lines.append(f"\n{i+1}. {it['title']}{src}")
            if it.get("content"):
                lines.append(f"   {it['content'][:250]}")
            if it.get("url"):
                lines.append(f"   链接: {it['url']}")

        return ToolResult.ok("\n".join(lines))
    except Exception as e:
        return ToolResult.fail(f"头条搜索失败: {str(e)[:150]}")


_DOUBAN_CAT_MAP = {
    "movie": "1002",
    "book": "1001",
    "music": "1003",
    "all": "",
}


@register_tool(
    name="douban_search",
    description="搜索豆瓣——查电影/书籍/音乐的评价和评分。当用户说'这部电影怎么样''这本书评分多少'时使用。",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词（书名/电影名等）"},
            "category": {"type": "string", "description": "分类: movie/book/music/all，默认all", "default": "all"},
            "count": {"type": "integer", "description": "返回结果数，默认5", "default": 5},
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=15,
)
async def douban_search(query: str, category: str = "all", count: int = 5) -> ToolResult:
    try:
        query = str(query) if query is not None else ""
        if not query.strip():
            return ToolResult.fail("搜索关键词不能为空")
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 5
        if count <= 0:
            count = 5

        cat = str(category) if category else "all"
        cat_code = _DOUBAN_CAT_MAP.get(cat, "")
        cat_param = f"&cat={cat_code}" if cat_code else ""

        douban_headers = {
            **_HEADERS,
            "Referer": "https://www.douban.com/",
            "Accept": "application/json, text/plain, */*",
            "Cookie": "bid=",
        }
        url = (f"https://www.douban.com/j/search?q={quote_plus(query)}"
               f"&start=0&limit={count}{cat_param}")
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=douban_headers) as client:
            resp = await client.get(url)

        if resp.status_code != 200:
            logger.warning("douban_search.bad_status status={}", resp.status_code)
            return await _douban_suggest_fallback(query, count)

        try:
            data = resp.json()
        except json.JSONDecodeError:
            return await _douban_suggest_fallback(query, count)

        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            return await _douban_suggest_fallback(query, count)

        from lxml import html as lxml_html
        lines = [f"豆瓣搜索: {query}", "=" * 40]
        parsed = 0
        for raw in items[:count]:
            if isinstance(raw, str):
                try:
                    entry = lxml_html.fromstring(raw)
                except Exception:
                    continue
                title_el = entry.xpath('//a[@class="nbg"]') or entry.xpath('//a')
                title = title_el[0].text_content().strip() if title_el else ""
                link = title_el[0].get("href", "") if title_el else ""
                rating_el = entry.xpath('//span[@class="rating_nums"]/text()')
                rating = rating_el[0].strip() if rating_el else ""
                pl_el = entry.xpath('//span[@class="pl"]/text()')
                pl = pl_el[0].strip() if pl_el else ""
                abstract_el = entry.xpath('//span[@class="subject-cast"]/text()')
                abstract = abstract_el[0].strip() if abstract_el else ""
            elif isinstance(raw, dict):
                title = raw.get("title", "")
                link = raw.get("url", "")
                rating = str(raw.get("rating", "")) if raw.get("rating") else ""
                pl = ""
                abstract = raw.get("abstract", "") or raw.get("card_subtitle", "")
            else:
                continue
            if not title:
                continue
            parsed += 1
            rating_str = f" ⭐{rating}" if rating else ""
            lines.append(f"\n{parsed}. {title}{rating_str}")
            if abstract:
                lines.append(f"   {abstract[:200]}")
            if pl:
                lines.append(f"   {pl}")
            if link:
                lines.append(f"   链接: {link}")

        if parsed == 0:
            return await _douban_suggest_fallback(query, count)

        return ToolResult.ok("\n".join(lines))
    except Exception as e:
        return ToolResult.fail(f"豆瓣搜索失败: {str(e)[:150]}")


async def _douban_suggest_fallback(query: str, count: int) -> ToolResult:
    """豆瓣搜索降级方案：先试 suggest API，再降级到 Bing site:douban.com。"""
    # 方案1:豆瓣 suggest API
    try:
        suggest_url = f"https://movie.douban.com/j/subject_suggest?q={quote_plus(query)}"
        headers = {**_HEADERS, "Referer": "https://movie.douban.com/", "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers) as client:
            resp = await client.get(suggest_url)
        if resp.status_code == 200 and resp.text.strip():
            data = resp.json()
            if data:
                lines = [f"豆瓣搜索: {query}", "=" * 40]
                for i, entry in enumerate(data[:count]):
                    title = entry.get("title", "")
                    year = entry.get("year", "")
                    subtype = entry.get("type", "")
                    link = entry.get("url", "")
                    if not title:
                        continue
                    meta = f" ({year})" if year else ""
                    lines.append(f"\n{i+1}. {title}{meta}")
                    if subtype:
                        lines.append(f"   类型: {subtype}")
                    if link:
                        lines.append(f"   链接: {link}")
                return ToolResult.ok("\n".join(lines))
    except Exception:
        pass

    # 方案2: Bing site:douban.com 降级
    try:
        from lxml import html as lxml_html
        bing_url = f"https://cn.bing.com/search?q=site:douban.com+{quote_plus(query)}&count={count}"
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=_HEADERS) as client:
            resp = await client.get(bing_url)
        if resp.status_code == 200:
            tree = lxml_html.fromstring(resp.text)
            results = tree.xpath('//li[@class="b_algo"]') or tree.xpath('//li[contains(@class,"b_algo")]')
            if results:
                lines = [f"豆瓣搜索: {query} (via Bing)", "=" * 40]
                parsed = 0
                for r in results[:count]:
                    title_el = r.xpath('.//h2/a')
                    if not title_el:
                        continue
                    title = title_el[0].text_content().strip()
                    link = title_el[0].get("href", "")
                    snippet_el = r.xpath('.//p') or r.xpath('.//div[contains(@class,"b_caption")]//p')
                    snippet = snippet_el[0].text_content().strip() if snippet_el else ""
                    if title:
                        parsed += 1
                        lines.append(f"\n{parsed}. {title}")
                        if snippet:
                            lines.append(f"   {snippet[:250]}")
                        if link:
                            lines.append(f"   链接: {link}")
                if parsed > 0:
                    return ToolResult.ok("\n".join(lines))
    except Exception:
        pass

    return ToolResult.fail(f"豆瓣搜索 '{query}' 无结果")
