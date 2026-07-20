import asyncio
import ipaddress
import socket
import time
from collections import OrderedDict
from typing import Any
from loguru import logger
from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult
from security.sandbox_config import check_domain_allowed
from security.ssrf_guard import validate_url as _ssrf_validate_url

_CONTENT_LIMIT = 8000

# 网页浏览缓存：5分钟TTL + LRU 上限
_browse_cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
_BROWSE_CACHE_TTL = 300.0  # 5分钟
_BROWSE_CACHE_MAX_SIZE = 256

# 模块级 primp.Client 单例
_primp_client = None


def _get_primp_client() -> Any:
    global _primp_client
    if _primp_client is None:
        import primp
        _primp_client = primp.Client(impersonate="chrome")
    return _primp_client


_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
]

_PRIVATE_NETWORKS_V6 = [
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_ip(hostname: str) -> bool:
    """检查 hostname 解析后的 IP 是否为内网/保留地址，防止 SSRF"""
    try:
        resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for _family, _type_, _proto, _canonname, sockaddr in resolved:
            ip = ipaddress.ip_address(sockaddr[0])
            if isinstance(ip, ipaddress.IPv4Address):
                for net in _PRIVATE_NETWORKS:
                    if ip in net:
                        logger.warning("web_browse.blocked_private_ip", hostname=hostname, ip=str(ip))
                        return True
            elif isinstance(ip, ipaddress.IPv6Address):
                for net in _PRIVATE_NETWORKS_V6:
                    if ip in net:
                        logger.warning("web_browse.blocked_private_ip", hostname=hostname, ip=str(ip))
                        return True
    except socket.gaierror:
        return True  # 无法解析的域名也拒绝
    return False


def _verify_dns_pin(url: str) -> str | None:
    """DNS Pin 一致性校验: 调用 validate_url 后实际请求前, 再次解析 hostname,
    确认解析 IP 与 ssrf_guard 缓存的 pinned_ip 一致, 防 TOCTOU/DNS rebinding.

    Returns:
        None  — 校验通过 (或 pinned_ip 不可用, 跳过)
        str   — 失败原因
    """
    try:
        from security.ssrf_guard import get_pinned_ip
    except ImportError:
        return None  # ssrf_guard 不可用, 跳过
    try:
        pinned_ip = get_pinned_ip(url)
    except Exception:
        return None  # 校验异常, 跳过 (不阻塞, 已在入口处校验)
    if not pinned_ip:
        return None  # 无 pinned IP (例如白名单主机), 跳过
    try:
        from urllib.parse import urlparse
        hostname = urlparse(url).hostname
        if not hostname:
            return None
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        return f"DNS 解析失败: {e}"
    current_ips: list[str] = []
    for info in infos:
        ip_str = info[4][0]
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        if ip_str not in current_ips:
            current_ips.append(ip_str)
    if pinned_ip not in current_ips:
        logger.warning(
            "ssrf.dns_pin_mismatch host={} pinned={} current={}",
            hostname, pinned_ip, current_ips,
        )
        return f"DNS Pin 不一致 (pinned={pinned_ip}, current={current_ips})"
    return None


def _fetch_html(url: str, timeout: int = 15) -> tuple[int, str, str]:
    # DNS Pin 一致性校验 (TOCTOU 防护): 实际请求前再次解析 hostname,
    # 确认解析 IP 仍为 validate_url 锁定的 pinned_ip, 不一致则拒绝
    pin_err = _verify_dns_pin(url)
    if pin_err:
        return 0, "", f"SSRF DNS Pin 校验失败: {pin_err}"

    try:
        client = _get_primp_client()
        resp = client.get(url, headers={
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        if resp.status_code >= 400:
            return resp.status_code, "", f"HTTP 错误: {resp.status_code}"
        return resp.status_code, resp.text, ""
    except ImportError:
        pass
    except Exception as e:
        logger.debug("web_browse.primp_failed", error=str(e))

    import urllib.request
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if response.status >= 400:
            return response.status, "", f"HTTP 错误: {response.status} {response.reason}"
        html = response.read().decode("utf-8", errors="ignore")
    return response.status, html, ""


@register_tool(
    name="web_browse",
    description=(
        "打开网页 URL 读取正文全文。这是 web_search 的配套工具："
        "搜索结果只有摘要，挑最相关的链接用本工具读全文后再回答，信息才准确完整。"
        "适合读新闻、文章、文档、百科页面。"
    ),
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
async def web_browse(url: str) -> ToolResult:
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # 沙箱域名检查
        allowed, reason = check_domain_allowed(url)
        if not allowed:
            return ToolResult.fail(f"沙箱安全限制: {reason}")

        # SSRF v2 防护：5步法 (协议白名单 + 主机名黑名单 + DNS解析 + IP分类 + DNS Pinning)
        ok, reason = await asyncio.to_thread(_ssrf_validate_url, url)
        if not ok:
            return ToolResult.fail(f"安全限制：{reason}")

        # 检查浏览缓存
        now = time.monotonic()
        cached = _browse_cache.get(url)
        if cached is not None:
            if (now - cached[0]) < _BROWSE_CACHE_TTL:
                _browse_cache.move_to_end(url)
                return cached[1]
            # 已过期，移除
            _browse_cache.pop(url, None)

        _status, html, error = await asyncio.to_thread(_fetch_html, url)
        if error:
            await asyncio.sleep(2)
            _status2, html, error = await asyncio.to_thread(_fetch_html, url)
            if error:
                return ToolResult.fail(f"浏览网页失败: {error}")

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

        if len(text) > _CONTENT_LIMIT:
            text = text[:_CONTENT_LIMIT] + "\n...(内容过长已截断)"

        result = ToolResult.ok(header + text)

        # 更新浏览缓存
        _browse_cache[url] = (now, result)
        _browse_cache.move_to_end(url)
        while len(_browse_cache) > _BROWSE_CACHE_MAX_SIZE:
            _browse_cache.popitem(last=False)

        return result
    except Exception as e:
        return ToolResult.fail(f"浏览网页失败: {e!s}")


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
    return text.strip()[:_CONTENT_LIMIT]


def _extract_title(html: str) -> str:
    import re
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        return title[:100] if title else "无标题"
    return "无标题"
