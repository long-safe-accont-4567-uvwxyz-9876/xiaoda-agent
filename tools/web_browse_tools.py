from urllib.parse import urlparse
import ssl
from tool_registry import register_tool, ToolPermission, ToolResult

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


@register_tool(
    name="web_browse",
    description="浏览网页并提取文本内容。输入URL地址。",
    schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要浏览的网页URL"}
        },
        "required": ["url"],
    },
    permission=ToolPermission.READ_ONLY,
    category="web",
    max_frequency=5,
)
def web_browse(url: str) -> ToolResult:
    try:
        import urllib.request

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

        with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx) as response:
            if response.status >= 400:
                return ToolResult.fail(f"HTTP 错误: {response.status} {response.reason}")
            html = response.read().decode("utf-8", errors="ignore")

        try:
            import html2text
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.ignore_images = True
            h.body_width = 0
            h.unicode_snob = True
            text = h.handle(html)
        except ImportError:
            text = _simple_html_to_text(html)

        title = _extract_title(html)
        header = f"网页: {title}\nURL: {url}\n{'='*40}\n"

        if len(text) > 5000:
            text = text[:5000] + "\n...(内容过长已截断)"

        return ToolResult.ok(header + text)
    except Exception as e:
        return ToolResult.fail(f"浏览网页失败: {str(e)}")


def _simple_html_to_text(html: str) -> str:
    import re
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</p>', '\n', text)
    text = re.sub(r'</div>', '\n', text)
    text = re.sub(r'</li>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()[:5000]


def _extract_title(html: str) -> str:
    import re
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        return title[:100] if title else "无标题"
    return "无标题"
