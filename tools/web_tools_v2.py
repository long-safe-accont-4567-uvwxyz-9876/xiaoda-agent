import asyncio
import os
import time
from collections import OrderedDict
from typing import Any

import httpx
from loguru import logger

from security.ssrf_guard import validate_url as _ssrf_validate_url
from tool_engine.tool_registry import ToolPermission, ToolResult, register_tool
from tools.anysearch_client import AnySearchAuthError, anysearch_available, anysearch_search_sync

# 搜索结果缓存：5分钟TTL + LRU 上限
_search_cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
_SEARCH_CACHE_TTL = 300.0  # 5分钟
_SEARCH_CACHE_MAX_SIZE = 256

# 模块级 primp.Client 单例
_primp_client = None

# 模块级 TavilyClient 单例（懒初始化）
_tavily_client = None


def _get_primp_client() -> Any:
    """懒初始化并返回模块级 primp.Client 单例。"""
    global _primp_client
    if _primp_client is None:
        import primp
        _primp_client = primp.Client(impersonate="chrome")
    return _primp_client


def _get_tavily_client() -> Any:
    """懒初始化并返回 TavilyClient 单例（API Key 存在时）。

    动态读取 env（而非模块级常量），避免因 import 顺序导致 .env 尚未加载
    而读不到 key（config.load_dotenv 在模块导入期执行）。
    """
    global _tavily_client
    key = os.getenv("TAVILY_API_KEY", "")
    if _tavily_client is None and key:
        from tavily import TavilyClient
        _tavily_client = TavilyClient(api_key=key)
    return _tavily_client


def _tavily_available() -> bool:
    """动态检测 Tavily 是否可用（API Key 存在）。"""
    return bool(os.getenv("TAVILY_API_KEY", ""))


def _bing_search_sync(query: str, max_results: int = 8) -> list[dict]:
    """同步抓取 Bing 搜索结果，解析标题、链接和摘要。"""
    from urllib.parse import quote_plus

    from lxml import html as lxml_html

    client = _get_primp_client()
    url = f"https://cn.bing.com/search?q={quote_plus(query)}&count={max_results}&setlang=zh-Hans"
    # SSRF 防护：5步法校验搜索 URL (防御性, host 为固定公网)
    ok, reason = _ssrf_validate_url(url)
    if not ok:
        logger.warning("bing.ssrf_blocked reason={}", reason)
        return []
    try:
        resp = client.get(url, headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    except (RuntimeError, OSError, ConnectionError, ValueError) as e:
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
    if not _tavily_available():
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


# 时效性关键词：命中则优先走新闻搜索
_FRESH_KEYWORDS = (
    "最新", "近期", "今天", "昨天", "本周", "这周", "本月", "今年", "现在",
    "新闻", "时事", "动态", "发布", "刚刚", "最近", "目前", "当前", "实时",
    "2025", "2026", "2027",
)


def _is_time_sensitive(query: str) -> bool:
    """判断查询是否包含时效性关键词，决定是否走新闻搜索。"""
    return any(kw in query for kw in _FRESH_KEYWORDS)


def _format_results(query: str, results: list[dict], engine_name: str = "",
                    answer: str = "") -> str:
    """将搜索结果格式化为可读的字符串。"""
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
    """根据 URL 对搜索结果去重。"""
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

    优先级（依据 AnySearch 手册的分层路由思想：意图识别→路由→融合）：
    0. AnySearch 统一搜索（默认关；ANYSEARCH_API_KEY/ANYSEARCH_ENABLED 开启，
       服务端做意图识别与实时路由，含新鲜度意图）——熔断器防不可达拖慢
    1. 时效性查询 → Tavily 新闻（带日期+AI摘要）
    2. Tavily basic（质量高、带AI摘要，对中文专有名词/游戏术语匹配准确）
    3. Bing 抓取（免费兜底）

    修复说明：原实现 Bing 优先，但 Bing 中文搜索会把"纳西妲"（原神角色）
    匹配成"纳西族"返回无关结果，且 Bing 返回非空结果后不再触发 Tavily 兜底，
    导致 LLM 拿到无关内容反复搜索直至超时降级。改为 Tavily 优先。
    """
    time_sensitive = _is_time_sensitive(query)
    logger.info("web_search.do_search query={} fresh={}", query[:40], time_sensitive)

    # 0. AnySearch（开启时）：401/403 按手册不降级匿名，直接换下一引擎
    if anysearch_available():
        try:
            results, answer = await asyncio.to_thread(
                anysearch_search_sync, query, max_results)
            if results:
                return _dedup_results(results), "AnySearch", answer
        except AnySearchAuthError as e:
            logger.warning("anysearch.auth_failed error={}", str(e)[:150])
        except (RuntimeError, OSError, ValueError, ConnectionError) as e:
            logger.warning("anysearch.failed error={}", str(e)[:150])

    # 1. 时效性查询 → Tavily 新闻优先（带日期+AI摘要）
    if time_sensitive and use_tavily and _tavily_available():
        try:
            results, answer = await asyncio.to_thread(
                _tavily_search_sync, query, max_results, "basic", True)
            if results:
                return _dedup_results(results), "Tavily新闻", answer
        except (RuntimeError, OSError, ConnectionError, ValueError) as e:
            logger.warning("tavily.news_failed error={}", repr(e)[:150])

    # 2. Tavily basic 优先（质量高、带AI摘要，中文专有名词比 Bing 准）
    if use_tavily and _tavily_available():
        try:
            results, answer = await asyncio.to_thread(
                _tavily_search_sync, query, max_results, "basic", False)
            if results:
                return _dedup_results(results), "Tavily", answer
        except (RuntimeError, OSError, ConnectionError, ValueError) as e:
            logger.warning("tavily.primary_failed error={}", repr(e)[:150])

    # 3. Bing 抓取（免费兜底）
    results = await asyncio.to_thread(_bing_search_sync, query, max_results)
    if results:
        return _dedup_results(results), "Bing", ""

    await asyncio.sleep(1)
    results = await asyncio.to_thread(_bing_search_sync, query, max_results)
    if results:
        return _dedup_results(results), "Bing", ""

    return [], "", ""


def _clean_query(query: str) -> str:
    """清理搜索关键词：去除前缀/语气助词等冗余文本。"""
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


async def _search_core(query: str) -> ToolResult:
    """单查询搜索核心（含 5 分钟 LRU 缓存），web_search 与 web_search_batch 共用。"""
    try:
        query = str(query) if query is not None else ""
        if not query.strip():
            return ToolResult.fail("搜索关键词不能为空")
        query = _clean_query(query)

        # 检查搜索缓存
        now = time.monotonic()
        cached = _search_cache.get(query)
        if cached is not None:
            if (now - cached[0]) < _SEARCH_CACHE_TTL:
                _search_cache.move_to_end(query)
                return cached[1]
            # 已过期，移除
            _search_cache.pop(query, None)

        results, engine, answer = await _do_search(query, max_results=8)
        if not results and not answer:
            return ToolResult.fail(
                f"搜索 '{query}' 无结果。建议：换一组更具体或更宽泛的关键词重试，"
                f"中文无果可尝试英文关键词")

        formatted = _format_results(query, results, engine, answer)
        result = ToolResult.ok(formatted)

        # 更新搜索缓存
        _search_cache[query] = (now, result)
        _search_cache.move_to_end(query)
        while len(_search_cache) > _SEARCH_CACHE_MAX_SIZE:
            _search_cache.popitem(last=False)

        return result
    except (RuntimeError, OSError, ValueError, ConnectionError,
                httpx.TimeoutException, httpx.RequestError) as e:
        return ToolResult.fail(f"搜索错误: {e!s}")


@register_tool(
    name="web_search",
    description=(
        "搜索互联网获取信息。一次只搜一个意图——多个互相独立的问题请改用 web_search_batch 并行搜索。"
        "查新闻/时事/最新动态时，请在 query 里带上'最新'或年份等时效词，"
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
    """搜索互联网信息，自动选择新闻或常规引擎，结果带 5 分钟缓存。"""
    return await _search_core(query)


@register_tool(
    name="web_search_batch",
    description=(
        "并行搜索多个互相独立的查询意图（2-5 个）。适合用户一句话里含多个独立问题的场景，"
        "例如'对比 A 和 B 的价格，再查下 C 的最新动态'——拆成 2-3 条各含单一意图的 query 一次并行，"
        "比连续多次调用 web_search 更快。每条 query 遵循一次一个意图；结果按查询分组返回。"
        "单一问题不要用本工具，直接 web_search。"
    ),
    schema={
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 5,
                "description": "2-5 条互相独立的搜索关键词，每条只表达一个意图",
            }
        },
        "required": ["queries"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=10,
)
async def web_search_batch(queries) -> ToolResult:
    """并行执行 2-5 条独立查询（引擎链与缓存与 web_search 一致），分组返回。"""
    if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
        return ToolResult.fail("queries 必须是字符串数组")
    cleaned = [q.strip() for q in queries if q.strip()]
    if len(cleaned) < 2:
        return ToolResult.fail("至少需要 2 条独立查询；单一问题请直接用 web_search")
    if len(cleaned) > 5:
        return ToolResult.fail("一次最多并行 5 条查询，请拆分多次调用")

    results = await asyncio.gather(*(_search_core(q) for q in cleaned))
    sections = [f"并行搜索 {len(cleaned)} 个意图", "=" * 40]
    ok_count = 0
    for q, r in zip(cleaned, results):
        sections.append(f"\n{'─' * 40}\n【查询】{q}")
        if r.success:
            ok_count += 1
            sections.append(r.data if isinstance(r.data, str) else str(r.data))
        else:
            sections.append(f"（失败）{r.error}")
    if ok_count == 0:
        return ToolResult.fail("全部查询无结果。建议：换更具体或更宽泛的关键词重试，中文无果可尝试英文")
    return ToolResult.ok("\n".join(sections))


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
    """获取指定城市的实时天气信息（通过 wttr.in）。"""
    try:
        city = str(city) if city is not None else ""
        if not city.strip():
            return ToolResult.fail("城市名称不能为空")

        def _fetch_weather() -> Any:
            """同步请求 wttr.in 获取天气信息。"""
            import urllib.parse
            import urllib.request
            url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3&lang=zh"
            # SSRF 防护：5步法校验 (city 为用户输入, 防注入内网地址)
            ok, reason = _ssrf_validate_url(url)
            if not ok:
                raise ValueError(f"安全限制: {reason}")
            req = urllib.request.Request(url, headers={'User-Agent': 'curl'})
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.read().decode('utf-8').strip()

        result = await asyncio.to_thread(_fetch_weather)
        return ToolResult.ok(f"🌤️ {result}")
    except (RuntimeError, OSError, ValueError, ConnectionError,
                httpx.TimeoutException, httpx.RequestError) as e:
        return ToolResult.fail(f"获取天气失败: {e!s}")
