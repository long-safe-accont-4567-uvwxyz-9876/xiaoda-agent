import os
import json
import asyncio
import time
from typing import Any
from loguru import logger
from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# SearXNG 本地搜索元引擎配置（聚合 70+ 引擎，无追踪，无需 API Key）
SEARXNG_URL = os.getenv("SEARXNG_URL", "")
SEARXNG_ENABLED = os.getenv("SEARXNG_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SEARXNG_TIMEOUT = float(os.getenv("SEARXNG_TIMEOUT", "8"))

# 搜索结果缓存：5分钟TTL
_search_cache: dict[str, tuple[float, Any]] = {}
_SEARCH_CACHE_TTL = 300.0  # 5分钟

# 模块级 primp.Client 单例
_primp_client = None

# 模块级 TavilyClient 单例（懒初始化）
_tavily_client = None

# SearXNG 可用性缓存：失败后冷却期内跳过，避免每次搜索都等超时
_searxng_status: dict[str, Any] = {"available": True, "last_check": 0.0, "last_error": ""}
_SEARXNG_FAIL_COOLDOWN = 30.0  # 失败冷却 30 秒
_SEARXNG_OK_TTL = 120.0  # 可用状态 120 秒内免探测


def _get_primp_client():
    global _primp_client
    if _primp_client is None:
        import primp
        _primp_client = primp.Client(impersonate="chrome")
    return _primp_client


def _get_tavily_client():
    global _tavily_client
    if _tavily_client is None and TAVILY_API_KEY:
        from tavily import TavilyClient
        _tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
    return _tavily_client


def _bing_search_sync(query: str, max_results: int = 8) -> list[dict]:
    from lxml import html as lxml_html
    from urllib.parse import quote_plus

    client = _get_primp_client()
    url = f"https://cn.bing.com/search?q={quote_plus(query)}&count={max_results}&setlang=zh-Hans"
    try:
        resp = client.get(url, headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    except Exception as e:
        logger.warning("bing.request_failed query={} error={}", query[:40], repr(e)[:200])
        return []

    if resp.status_code != 200:
        logger.warning("bing.bad_status status={} query={}", resp.status_code, query[:40])
        return []

    tree = lxml_html.fromstring(resp.text)
    items = tree.xpath('//li[@class="b_algo"]')
    if not items:
        title = (tree.xpath("//title/text()") or [""])[0]
        logger.warning("bing.no_items query={} page_title={} len={}",
                       query[:40], title[:60], len(resp.text))

    results = []
    for item in items[:max_results]:
        # Bing 有多个 A/B 版式：标准版标题在 h2/a；变体版没有 h2，
        # 标题藏在其他标签里——退化为找第一个有文本的 http 链接
        title_el = item.xpath('.//h2/a') or [
            a for a in item.xpath('.//a[@href]')
            if a.get("href", "").startswith("http") and a.text_content().strip()
        ][:1]
        if not title_el:
            continue
        title = title_el[0].text_content().strip()
        link = title_el[0].get("href", "")
        snippet_el = (item.xpath('.//div[@class="b_caption"]//p')
                      or item.xpath('.//p'))
        snippet = snippet_el[0].text_content().strip() if snippet_el else ""
        if title:
            results.append({"title": title, "url": link, "content": snippet})
    if items and not results:
        logger.warning("bing.items_unparsed query={} items={} sample={}",
                       query[:40], len(items),
                       lxml_html.tostring(items[0])[:300])
    return results


def _tavily_search_sync(query: str, max_results: int = 6, search_depth: str = "basic",
                        news: bool = False) -> tuple[list[dict], str]:
    """Tavily 搜索。返回 (results, answer)；news=True 走新闻通道（近30天）。"""
    if not TAVILY_API_KEY:
        return [], ""
    client = _get_tavily_client()
    if client is None:
        return [], ""
    kwargs: dict = {"max_results": max_results, "search_depth": search_depth,
                    "include_answer": True}
    if news:
        kwargs.update(topic="news", days=30)
    response = client.search(query, **kwargs)
    results = []
    for r in response.get("results", []):
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            "date": r.get("published_date", ""),
        })
    return results, (response.get("answer") or "")


def _searxng_should_try() -> bool:
    """是否应该尝试 SearXNG。

    - 未配置 URL 或禁用 → False
    - 标记可用且在 OK_TTL 内 → True
    - 标记不可用但在冷却期外 → True（重新尝试）
    - 标记不可用且在冷却期内 → False（跳过避免等超时）
    """
    if not SEARXNG_URL or not SEARXNG_ENABLED:
        return False
    now = time.monotonic()
    if _searxng_status["available"]:
        return True
    # 不可用：冷却期外才重试
    return (now - _searxng_status["last_check"]) > _SEARXNG_FAIL_COOLDOWN


def _searxng_search_sync(query: str, max_results: int = 8, news: bool = False) -> list[dict]:
    """SearXNG 本地元搜索。失败抛异常，由调用方降级。

    news=True 走 news 分类（聚合 Google News/Bing News 等），结果带 publishedDate。
    """
    import urllib.request, urllib.parse
    base = SEARXNG_URL.rstrip("/")
    params = {
        "q": query,
        "format": "json",
        "pageno": "1",
        "language": "auto",
        "categories": "news" if news else "general",
    }
    url = f"{base}/search?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "nahida-agent/1.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=SEARXNG_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = []
    for r in data.get("results", [])[:max_results]:
        published = r.get("publishedDate") or ""
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            "date": published[:10] if published else "",
        })
    return results


# 时效性关键词：命中则优先走新闻搜索
_FRESH_KEYWORDS = (
    "最新", "近期", "今天", "昨天", "本周", "这周", "本月", "今年", "现在",
    "新闻", "时事", "动态", "发布", "刚刚", "最近", "目前", "当前", "实时",
    "2025", "2026", "2027",
)


def _is_time_sensitive(query: str) -> bool:
    return any(kw in query for kw in _FRESH_KEYWORDS)


def _format_results(query: str, results: list[dict], engine_name: str = "",
                    answer: str = "") -> str:
    if not results and not answer:
        return ""
    parts = [f"搜索: {query}"]
    if engine_name:
        parts[0] += f" (via {engine_name})"
    parts.append("=" * 40)
    if answer:
        parts.append(f"\n【AI 综合摘要】{answer}")
    for i, r in enumerate(results):
        date = f" [{r['date'][:10]}]" if r.get("date") else ""
        parts.append(f"\n{i+1}. {r.get('title', '')}{date}")
        if r.get("content"):
            parts.append(f"   {r['content'][:250]}")
        if r.get("url"):
            parts.append(f"   链接: {r['url']}")
    return "\n".join(parts)


def _dedup_results(results: list[dict]) -> list[dict]:
    seen_urls = set()
    unique = []
    for r in results:
        url = r.get("url", "")
        if url and url in seen_urls:
            continue
        seen_urls.add(url)
        unique.append(r)
    return unique


async def _do_search(query: str, max_results: int = 8,
                     use_tavily: bool = True) -> tuple[list[dict], str, str]:
    """引擎降级策略，返回 (results, engine, ai_answer)。

    优先级：
    1. SearXNG（本地元引擎，聚合 Google/Bing/DDG 等 70+ 源，无追踪）— 不可用自动降级
    2. 时效性查询 → Tavily 新闻（带日期+AI摘要）
    3. Bing 抓取（免费，重试一次）
    4. Tavily basic（兜底）
    """
    time_sensitive = _is_time_sensitive(query)
    logger.info("web_search.do_search query={} fresh={}", query[:40], time_sensitive)

    # 1. 优先本地 SearXNG（普通+新闻都支持，聚合多源结果最全）
    if _searxng_should_try():
        try:
            results = await asyncio.to_thread(
                _searxng_search_sync, query, max_results, time_sensitive)
            _searxng_status.update(available=True, last_check=time.monotonic(),
                                   last_error="")
            if results:
                engine = "SearXNG新闻" if time_sensitive else "SearXNG"
                return _dedup_results(results), engine, ""
            # 有响应但无结果，继续降级到其它引擎
        except Exception as e:
            _searxng_status.update(available=False, last_check=time.monotonic(),
                                   last_error=repr(e)[:120])
            logger.info("searxng.failed query={} error={} → 降级到 Bing/Tavily",
                        query[:40], repr(e)[:120])

    # 2. 时效性查询 → Tavily 新闻优先（带日期+AI摘要）
    if time_sensitive and use_tavily and TAVILY_API_KEY:
        try:
            results, answer = await asyncio.to_thread(
                _tavily_search_sync, query, max_results, "basic", True)
            if results:
                return _dedup_results(results), "Tavily新闻", answer
        except Exception as e:
            logger.warning("tavily.news_failed error={}", repr(e)[:150])

    # 3. Bing 抓取（免费）
    results = await asyncio.to_thread(_bing_search_sync, query, max_results)
    if results:
        return _dedup_results(results), "Bing", ""

    await asyncio.sleep(1)
    results = await asyncio.to_thread(_bing_search_sync, query, max_results)
    if results:
        return _dedup_results(results), "Bing", ""

    # 4. Tavily basic 兜底
    if use_tavily and TAVILY_API_KEY:
        try:
            results, answer = await asyncio.to_thread(
                _tavily_search_sync, query, max_results, "basic", time_sensitive)
            if results:
                return _dedup_results(results), "Tavily", answer
        except Exception as e:
            logger.warning("tavily.fallback_failed error={}", repr(e)[:150])

    return [], "", ""


def _clean_query(query: str) -> str:
    q = query.strip()
    question_starters = ("如何", "为什么", "什么是", "怎么", "怎样", "哪儿", "哪里", "谁", "何时", "多少")
    if q.startswith(question_starters):
        for s in ["吗", "呢", "吧", "啊", "呀", "哦"]:
            if q.endswith(s) and len(q) > 2:
                q = q[:-len(s)].strip()
        return q if q.strip() else query.strip()
    prefixes = ["获取", "帮我", "搜一下", "搜索一下", "查一下", "找一下", "可以", "能不能",
                "我要", "我想知道", "我想", "请帮我", "麻烦", "能否", "可不可以"]
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
    description=(
        "搜索互联网获取信息。查新闻/时事/最新动态时，请在 query 里带上'最新'或年份等时效词，"
        "会自动切换到新闻引擎（带发布日期和AI综合摘要）。"
        "搜索结果只有标题和摘要——回答前若需要细节，请挑 1-2 条最相关的链接用 web_browse 打开读全文，"
        "不要只凭摘要编造内容。一次搜索没找到，可换不同关键词再搜（中文查不到试英文）。"
        "注意：天气查询用 get_weather，不要用搜索。"
    ),
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "搜索关键词。查时事请带时效词，如'2026世界杯 夺冠热门 最新'"}
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=30,
)
async def web_search(query: str) -> ToolResult:
    try:
        query = str(query) if query is not None else ""
        if not query.strip():
            return ToolResult.fail("搜索关键词不能为空")
        query = _clean_query(query)

        # 检查搜索缓存
        now = time.monotonic()
        cached = _search_cache.get(query)
        if cached is not None and (now - cached[0]) < _SEARCH_CACHE_TTL:
            return cached[1]

        results, engine, answer = await _do_search(query, max_results=8)
        if not results and not answer:
            return ToolResult.fail(
                f"搜索 '{query}' 无结果。建议：换一组更具体或更宽泛的关键词重试，"
                f"中文无果可尝试英文关键词")

        formatted = _format_results(query, results, engine, answer)
        result = ToolResult.ok(formatted)

        # 更新搜索缓存
        _search_cache[query] = (now, result)

        return result
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
async def get_weather(city: str) -> ToolResult:
    try:
        city = str(city) if city is not None else ""
        if not city.strip():
            return ToolResult.fail("城市名称不能为空")

        def _fetch_weather():
            import urllib.request, urllib.parse
            url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3&lang=zh"
            req = urllib.request.Request(url, headers={'User-Agent': 'curl'})
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.read().decode('utf-8').strip()

        result = await asyncio.to_thread(_fetch_weather)
        return ToolResult.ok(f"🌤️ {result}")
    except Exception as e:
        return ToolResult.fail(f"获取天气失败: {str(e)}")
