import re
import json
import httpx
from urllib.parse import quote_plus
from loguru import logger
from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult

_UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_UA_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_UA_MOBILE = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
_HEADERS = {
    "User-Agent": _UA,
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ─── 自动路由关键词映射 ──────────────────────────────────────────────────────

_NEWS_KEYWORDS = {"新闻", "时事", "最新", "今天", "发生", "动态", "报道", "消息", "突发", "热点", "资讯"}
_HOT_KEYWORDS = {"热搜", "热点", "热门", "大家都在搜", " trending", "火爆"}
_MOVIE_KEYWORDS = {"电影", "影片", "评分", "豆瓣", "电视剧", "综艺", "动漫", "番剧", "票房"}
_BOOK_KEYWORDS = {"书籍", "书评", "读书", "小说", "著作", "出版"}
_ZHIHU_KEYWORDS = {"知乎", "怎么看", "如何评价", "有什么建议", "经验", "讨论", "观点", "分析", "怎么学", "如何学", "为什么", "是什么", "有什么区别"}
_BILIBILI_KEYWORDS = {"视频", "教程", "B站", "bilibili", "UP主", "直播", "番剧"}


def _auto_detect_scope(query: str) -> str:
    """根据查询关键词自动判断最佳搜索范围。"""
    q = query.lower()
    for kw in _HOT_KEYWORDS:
        if kw in q:
            return "hot"
    for kw in _NEWS_KEYWORDS:
        if kw in q:
            return "news"
    for kw in _BILIBILI_KEYWORDS:
        if kw in q:
            return "bilibili"
    for kw in _MOVIE_KEYWORDS | _BOOK_KEYWORDS:
        if kw in q:
            return "movie"
    for kw in _ZHIHU_KEYWORDS:
        if kw in q:
            return "zhihu"
    return "web"


# ─── 统一中文搜索入口 ────────────────────────────────────────────────────────

@register_tool(
    name="search_cn",
    description=(
        "中文互联网搜索——统一搜索入口。根据搜索范围自动选择最佳搜索源："
        "通用搜索(B站+头条)、新闻(头条)、知乎(Bing site:zhihu)、"
        "豆瓣(电影/书籍评分)、B站视频、百度热搜。"
        "scope=auto时自动判断，大多数情况用auto即可。"
    ),
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "scope": {
                "type": "string",
                "description": "搜索范围: auto(自动判断)/web(通用)/news(新闻)/hot(热搜)/movie(电影书籍)/zhihu(知乎)/bilibili(B站视频)",
                "default": "auto",
            },
            "count": {"type": "integer", "description": "返回结果数，默认8", "default": 8},
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=30,
)
async def search_cn(query: str, scope: str = "auto", count: int = 8) -> ToolResult:
    """统一中文搜索入口：根据 scope 自动路由到最佳搜索源。"""
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

        scope = str(scope).lower() if scope else "auto"
        if scope == "auto":
            scope = _auto_detect_scope(query)

        logger.info("search_cn routing: query={} scope={}", query[:40], scope)

        if scope == "hot":
            return await _search_hot(count)
        elif scope == "news":
            return await _search_news(query, count)
        elif scope == "movie":
            return await _search_douban(query, "all", count)
        elif scope == "zhihu":
            return await _search_zhihu(query, count)
        elif scope == "bilibili":
            return await _search_bilibili(query, count)
        else:  # web
            return await _search_web(query, count)
    except Exception as e:
        logger.warning("search_cn.error query={} error={}", query[:40], repr(e)[:200])
        return ToolResult.fail(f"搜索失败: {str(e)[:150]}")


# ─── 通用搜索：B站 API + 头条 + Bing 降级 ────────────────────────────────────

async def _search_web(query: str, count: int) -> ToolResult:
    """通用中文搜索：B站 API → 头条 → Bing 降级。"""
    # B站搜索
    result = await _search_bilibili(query, count)
    if result and result.success:
        return result
    # 头条搜索
    result = await _search_news(query, count)
    if result and result.success:
        return result
    # Bing 降级
    try:
        from tools.web_tools_v2 import web_search
        return await web_search(query)
    except Exception as e:
        return ToolResult.fail(f"搜索失败: {str(e)[:150]}")


# ─── B站搜索 ─────────────────────────────────────────────────────────────────

async def _search_bilibili(query: str, count: int) -> ToolResult:
    """通过 B站搜索 API 搜索视频内容。"""
    try:
        url = (f"https://api.bilibili.com/x/web-interface/search/all/v2?"
               f"keyword={quote_plus(query)}&page=1")
        headers = {"User-Agent": _UA_WIN, "Referer": "https://www.bilibili.com/"}
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return ToolResult.fail(f"B站搜索失败: HTTP {resp.status_code}")
        data = resp.json()
        if data.get("code") != 0:
            return ToolResult.fail(f"B站搜索失败: code={data.get('code')}")

        items = []
        for group in data.get("data", {}).get("result", []):
            for entry in group.get("data", []):
                title = entry.get("title", "").replace('<em class="keyword">', "").replace("</em>", "")
                if not title:
                    continue
                author = entry.get("author", "")
                play = entry.get("play", 0)
                description = entry.get("description", "")
                link = f"https://www.bilibili.com/video/{entry.get('bvid', '')}" if entry.get("bvid") else ""
                tag = entry.get("tag", "") or entry.get("typename", "")
                items.append({
                    "title": title, "author": author, "play": play,
                    "description": description, "link": link, "tag": tag,
                })

        if not items:
            return ToolResult.fail(f"B站搜索 '{query}' 无结果")

        lines = [f"B站搜索: {query}", "=" * 40]
        for i, it in enumerate(items[:count]):
            meta_parts = []
            if it["author"]:
                meta_parts.append(f"UP: {it['author']}")
            if it["play"]:
                meta_parts.append(f"播放: {it['play']}")
            if it["tag"]:
                meta_parts.append(it["tag"])
            meta = " | ".join(meta_parts)
            lines.append(f"\n{i+1}. {it['title']}")
            if meta:
                lines.append(f"   {meta}")
            if it["description"]:
                lines.append(f"   {it['description'][:200]}")
            if it["link"]:
                lines.append(f"   链接: {it['link']}")

        return ToolResult.ok("\n".join(lines))
    except Exception as e:
        return ToolResult.fail(f"B站搜索失败: {str(e)[:150]}")


# ─── 头条新闻搜索 ────────────────────────────────────────────────────────────

async def _search_news(query: str, count: int) -> ToolResult:
    """通过头条搜索获取新闻资讯。"""
    try:
        url = (f"https://so.toutiao.com/search?dvpf=pc&source=input&keyword={quote_plus(query)}"
               f"&pd=information&action_type=search_subtab_switch&page_num=0&search_id=&backfill_id=")
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=_HEADERS) as client:
            resp = await client.get(url)

        if resp.status_code != 200:
            return ToolResult.fail(f"头条搜索失败: HTTP {resp.status_code}")

        items = []
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
            logger.warning("search_cn.news_parse_failed error={}", repr(e)[:150])

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

        lines = [f"新闻搜索: {query}", "=" * 40]
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


# ─── 知乎搜索 ────────────────────────────────────────────────────────────────

async def _search_zhihu(query: str, count: int) -> ToolResult:
    """通过 Bing 国内版 site:zhihu.com 搜索知乎内容。"""
    try:
        from lxml import html as lxml_html

        bing_url = f"https://cn.bing.com/search?q=site%3Azhihu.com+{quote_plus(query)}&count={count}"
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=_HEADERS) as client:
            resp = await client.get(bing_url)

        if resp.status_code != 200:
            return ToolResult.fail(f"知乎搜索 '{query}' 无结果")

        tree = lxml_html.fromstring(resp.text)
        results = tree.xpath('//li[@class="b_algo"]') or tree.xpath('//li[contains(@class,"b_algo")]')

        lines = [f"知乎搜索: {query}", "=" * 40]
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


# ─── 豆瓣搜索 ────────────────────────────────────────────────────────────────

async def _search_douban(query: str, cat: str, count: int) -> ToolResult:
    """通过豆瓣移动版搜索电影/书籍/音乐。"""
    try:
        cat_type_map = {"movie": "movie", "book": "book", "music": "music", "all": ""}
        type_param = f"&type={cat_type_map.get(cat, '')}" if cat_type_map.get(cat) else ""
        mobile_url = f"https://m.douban.com/search/?query={quote_plus(query)}{type_param}"
        headers = {"User-Agent": _UA_MOBILE, "Accept-Language": "zh-CN,zh;q=0.9"}
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers) as client:
            resp = await client.get(mobile_url)
        if resp.status_code != 200:
            return ToolResult.fail(f"豆瓣搜索失败: HTTP {resp.status_code}")

        from lxml import html as lxml_html
        tree = lxml_html.fromstring(resp.text)
        results = tree.xpath('//ul[@class="search-results"]//li') or tree.xpath('//div[contains(@class,"subject")]')
        if not results:
            return ToolResult.fail(f"豆瓣搜索 '{query}' 无结果")

        lines = [f"豆瓣搜索: {query}", "=" * 40]
        parsed = 0
        for li in results[:count * 2]:
            if parsed >= count:
                break
            title_el = li.xpath('.//a[contains(@href,"subject")]')
            if not title_el:
                continue
            title = title_el[0].text_content().strip()
            title = re.sub(r'\s+', ' ', title).strip()
            href = title_el[0].get("href", "")
            if not title or len(title) < 2:
                continue
            if href.startswith("/"):
                href = f"https://m.douban.com{href}"
            rating_el = li.xpath('.//span[contains(@class,"rating")]//text()')
            rating = rating_el[0].strip() if rating_el else ""
            meta_el = li.xpath('.//span[contains(@class,"subject-cast")]/text()') or li.xpath('.//p/text()')
            meta = meta_el[0].strip()[:200] if meta_el else ""
            pl_el = li.xpath('.//span[contains(@class,"pl")]/text()')
            pl = pl_el[0].strip() if pl_el else ""

            parsed += 1
            rating_str = f" ⭐{rating}" if rating else ""
            lines.append(f"\n{parsed}. {title}{rating_str}")
            if meta:
                lines.append(f"   {meta}")
            if pl:
                lines.append(f"   {pl}")
            if href:
                lines.append(f"   链接: {href}")

        if parsed == 0:
            return ToolResult.fail(f"豆瓣搜索 '{query}' 无结果")

        return ToolResult.ok("\n".join(lines))
    except Exception as e:
        return ToolResult.fail(f"豆瓣搜索失败: {str(e)[:150]}")


# ─── 百度热搜 ────────────────────────────────────────────────────────────────

async def _search_hot(count: int) -> ToolResult:
    """获取百度热搜榜。"""
    try:
        headers = {
            "User-Agent": _UA_WIN,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://top.baidu.com/",
            "Accept": "application/json, text/plain, */*",
        }
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers) as client:
            resp = await client.get(
                "https://top.baidu.com/api/board?platform=wise&tab=realtime")
        if resp.status_code != 200:
            return ToolResult.fail("获取百度热搜失败")

        data = resp.json()
        items = []
        cards = data.get("data", {}).get("cards", [])
        for card in cards:
            for entry in card.get("content", []):
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
