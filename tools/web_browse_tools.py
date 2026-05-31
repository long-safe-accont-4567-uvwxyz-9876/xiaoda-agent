import os
import time
from tool_registry import register_tool, ToolPermission, ToolResult

def _browse_httpx(url: str, timeout_sec: int = 20) -> dict:
    import httpx
    client = httpx.Client(follow_redirects=True, verify=False, timeout=timeout_sec, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"})
    try:
        resp = client.get(url)
        return {"text": resp.text, "status_code": resp.status_code, "url": str(resp.url)}
    finally:
        client.close()

def _extract_text_from_html(html: str) -> str:
    from lxml import html as lxml_html
    tree = lxml_html.fromstring(html)
    for tag in tree.xpath('//script | //style | //noscript | //header | //footer | //nav | //iframe'):
        tag.getparent().remove(tag)
    text = tree.text_content()
    lines = [line.strip() for line in text.splitlines()]
    cleaned = [line for line in lines if line]
    return "\n".join(cleaned)

def _extract_title(html: str) -> str:
    from lxml import html as lxml_html
    tree = lxml_html.fromstring(html)
    title = tree.xpath('//title/text()')
    return title[0].strip() if title else ""

def _extract_meta(html: str) -> dict:
    from lxml import html as lxml_html
    tree = lxml_html.fromstring(html)
    meta = {}
    for tag in tree.xpath('//meta'):
        name = tag.get('name', '') or tag.get('property', '')
        content = tag.get('content', '')
        if name and content:
            meta[name] = content
    return meta

def _detect_page_type(html: str) -> str:
    from lxml import html as lxml_html
    tree = lxml_html.fromstring(html)
    if tree.xpath('//script[@type="application/ld+json"]'):
        return "structured"
    article = tree.xpath('//article | //div[contains(@class, "article")] | //div[contains(@class, "post")]')
    if article:
        return "article"
    return "generic"

def _extract_article(html: str) -> str:
    from lxml import html as lxml_html
    tree = lxml_html.fromstring(html)
    for tag in tree.xpath('//script | //style | //noscript | //header | //footer | //nav | //aside | //iframe | //form'):
        tag.getparent().remove(tag)
    candidates = []
    for container in tree.xpath('//article | //div[contains(@class, "content")] | //div[contains(@class, "article")] | //div[contains(@class, "post")] | //main | //div[@id="content"]'):
        text = container.text_content().strip()
        if len(text) > 100:
            candidates.append((len(text), text))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    return tree.text_content().strip()

def _detect_is_search_engine(url: str, html: str) -> bool:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    search_domains = ['google.com', 'bing.com', 'baidu.com', 'sogou.com', '360.cn', 'so.com', 'duckduckgo.com', 'yandex.com', 'yahoo.com']
    return any(d in parsed.netloc for d in search_domains)

def _extract_search_results(html: str, url: str) -> str:
    from lxml import html as lxml_html
    from urllib.parse import urlparse
    tree = lxml_html.fromstring(html)
    domain = urlparse(url).netloc
    results = []
    if 'baidu.com' in domain:
        items = tree.xpath('//div[contains(@class, "result") and contains(@class, "c-container")]')
        for i, item in enumerate(items[:10]):
            title_el = item.xpath('.//h3/a//text()')
            link_el = item.xpath('.//h3/a/@href')
            snippet_el = item.xpath('.//span[contains(@class, "content-right_8Zs40")]
                                      | .//div[contains(@class, "c-abstract")]
                                      | .//span[contains(@class, "c-color-text")]')
            if title_el:
                title = "".join(title_el).strip()
                link = link_el[0] if link_el else ""
                snippet = snippet_el[0].text_content().strip() if snippet_el else ""
                results.append(f"{i+1}. {title}\n   {snippet}\n   链接: {link}")
    elif 'bing.com' in domain:
        items = tree.xpath('//li[@class="b_algo"]')
        for i, item in enumerate(items[:10]):
            title_el = item.xpath('.//h2/a')
            snippet_el = item.xpath('.//div[@class="b_caption"]//p')
            if title_el:
                title = title_el[0].text_content().strip()
                link = title_el[0].get("href", "")
                snippet = snippet_el[0].text_content().strip() if snippet_el else ""
                results.append(f"{i+1}. {title}\n   {snippet}\n   链接: {link}")
    elif 'google.com' in domain:
        items = tree.xpath('//div[contains(@class, "g")]')
        for i, item in enumerate(items[:10]):
            title_el = item.xpath('.//h3')
            snippet_el = item.xpath('.//div[contains(@class, "VwiC3b")] | .//span[contains(@class, "st")]')
            if title_el:
                title = title_el[0].text_content().strip()
                link_el = item.xpath('.//a/@href')
                link = link_el[0] if link_el else ""
                snippet = snippet_el[0].text_content().strip() if snippet_el else ""
                results.append(f"{i+1}. {title}\n   {snippet}\n   链接: {link}")
    else:
        return ""
    if results:
        return f"搜索引擎结果 (共{len(results)}条):\n" + "\n".join(results)
    return ""

def _extract_links(html: str, base_url: str) -> list:
    from lxml import html as lxml_html
    from urllib.parse import urljoin
    tree = lxml_html.fromstring(html)
    links = []
    for a in tree.xpath('//a[@href]'):
        href = a.get('href', '')
        text = a.text_content().strip()
        if href and text and len(text) > 2:
            full_url = urljoin(base_url, href)
            if full_url.startswith('http'):
                links.append({"text": text[:80], "url": full_url})
    seen = set()
    unique = []
    for link in links:
        if link['url'] not in seen:
            seen.add(link['url'])
            unique.append(link)
    return unique[:20]

@register_tool(
    name="web_browse",
    description="浏览网页并提取主要内容。自动过滤广告、导航栏等噪音，提取正文文本。支持任意网页URL。",
    schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要浏览的网页URL"}
        },
        "required": ["url"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=30,
)
def web_browse(url: str) -> ToolResult:
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        result = _browse_httpx(url)
        html = result["text"]
        status_code = result["status_code"]
        final_url = result["url"]
        if not html or not html.strip():
            return ToolResult.fail(f"页面内容为空 (状态码: {status_code})")
        title = _extract_title(html)
        meta = _extract_meta(html)
        page_type = _detect_page_type(html)
        if _detect_is_search_engine(url, html):
            search_content = _extract_search_results(html, url)
            if search_content:
                content = f"标题: {title}\n类型: 搜索引擎结果页\n\n{search_content}"
                return ToolResult.ok(content[:5000])
        if page_type == "article":
            content = _extract_article(html)
        else:
            content = _extract_text_from_html(html)
        links = _extract_links(html, final_url)
        output_parts = [f"标题: {title}"]
        if meta.get('description'):
            output_parts.append(f"描述: {meta['description']}")
        if final_url != url:
            output_parts.append(f"最终URL: {final_url}")
        output_parts.append(f"\n{content[:4000]}")
        if links:
            output_parts.append("\n相关链接:")
            for link in links[:10]:
                output_parts.append(f"  {link['text']}: {link['url']}")
        return ToolResult.ok("\n".join(output_parts)[:5000])
    except Exception as e:
        return ToolResult.fail(f"网页浏览失败: {str(e)}")