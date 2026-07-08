"""增强版网页浏览工具 —— 三级降级策略

优先级 1：平台专有提取器（知乎/B站/微信/微博/36kr/CSDN/抖音）
优先级 2：Jina Reader 通用 Markdown 提取
优先级 3：原有 primp + html2text（tools.web_browse_tools.web_browse）
"""
import re
import asyncio
import httpx
from loguru import logger
from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult
from config import JINA_API_KEY
from security.ssrf_guard import validate_url as _ssrf_validate_url
from core.degradation_strategy import get_degradation_strategy

_PLATFORM_EXTRACTORS = {
    "zhihu.com": "_extract_zhihu",
    "zhuanlan.zhihu.com": "_extract_zhihu",
    "weibo.com": "_extract_weibo",
    "m.weibo.cn": "_extract_weibo",
    "bilibili.com": "_extract_bilibili",
    "mp.weixin.qq.com": "_extract_wechat",
    "36kr.com": "_extract_36kr",
    "csdn.net": "_extract_csdn",
    "douyin.com": "_extract_douyin",
}


def _route_platform(url: str) -> str | None:
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""
    for domain, extractor_name in _PLATFORM_EXTRACTORS.items():
        if domain in hostname:
            return extractor_name
    return None


async def _extract_via_jina(url: str) -> tuple[str, str]:
    jina_url = f"https://r.jina.ai/{url}"
    headers = {"Accept": "text/markdown", "X-Return-Format": "markdown"}
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(jina_url, headers=headers)
    if resp.status_code == 200:
        content = resp.text
        title = _extract_title_from_markdown(content)
        return title, content
    else:
        raise RuntimeError(f"Jina Reader HTTP {resp.status_code}")


def _extract_title_from_markdown(md: str) -> str:
    for line in md.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return ""


async def _extract_zhihu(url: str) -> tuple[str, str]:
    try:
        return await _extract_via_jina(url)
    except Exception:
        logger.debug("zhihu.jina_extract_failed", exc_info=True)
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path = parsed.path
    # 问题页：www.zhihu.com/question/123 → www.zhihu.com/api/v4/questions/123
    if "/question/" in path:
        qid = path.split("/question/")[1].split("/")[0]
        mobile_url = f"https://www.zhihu.com/api/v4/questions/{qid}"
    else:
        # 专栏文章等其他页面，保留原 URL
        mobile_url = url
    headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X)", "Accept-Language": "zh-CN,zh;q=0.9"}
    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers) as client:
        resp = await client.get(mobile_url)
    if resp.status_code != 200:
        raise RuntimeError(f"知乎提取失败: HTTP {resp.status_code}")
    from lxml import html as lxml_html
    tree = lxml_html.fromstring(resp.text)
    content_els = tree.xpath('//div[contains(@class,"RichText")]')
    if content_els:
        text = content_els[0].text_content().strip()
        title_els = tree.xpath('//h1/text()')
        title = title_els[0].strip() if title_els else "知乎文章"
        return title, text
    raise RuntimeError("知乎正文提取失败")


async def _extract_bilibili(url: str) -> tuple[str, str]:
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN,zh;q=0.9"}
    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"B站提取失败: HTTP {resp.status_code}")
    html_text = resp.text
    match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});', html_text, re.DOTALL)
    if match:
        import json
        try:
            data = json.loads(match.group(1))
            desc = data.get("videoData", {}).get("desc", "")
            title = data.get("videoData", {}).get("title", "")
            if title and desc:
                return title, f"# {title}\n\n{desc}"
        except json.JSONDecodeError:
            pass
    return await _extract_via_jina(url)


async def _extract_wechat(url: str) -> tuple[str, str]:
    return await _extract_via_jina(url)


async def _extract_weibo(url: str) -> tuple[str, str]:
    return await _extract_via_jina(url)


async def _extract_36kr(url: str) -> tuple[str, str]:
    return await _extract_via_jina(url)


async def _extract_csdn(url: str) -> tuple[str, str]:
    return await _extract_via_jina(url)


async def _extract_douyin(url: str) -> tuple[str, str]:
    return await _extract_via_jina(url)


async def _is_private_ip_async(hostname: str) -> bool:
    """[deprecated] 旧 IP 检查, 保留以兼容现有测试; 新流程改用 security.ssrf_guard.validate_url"""
    from tools.web_browse_tools import _is_private_ip
    return await asyncio.to_thread(_is_private_ip, hostname)


async def _ssrf_check_async(url: str) -> tuple[bool, str]:
    """SSRF v2 5步法校验 (在线程中执行, 避免阻塞事件循环)"""
    return await asyncio.to_thread(_ssrf_validate_url, url)


@register_tool(
    name="web_browse",
    description=(
        "打开网页 URL 读取正文全文。这是 web_search 的配套工具："
        "搜索结果只有摘要，挑最相关的链接用本工具读全文后再回答，信息才准确完整。"
        "自动识别国内平台（知乎/B站/微信等）使用专有提取器，"
        "通用网页使用 Jina Reader 高质量提取，最后降级到传统 HTML 解析。"
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
async def web_browse_enhanced(url: str) -> ToolResult:
    try:
        # 降级检查: L2+ 关闭网页浏览, 返回降级提示
        if not get_degradation_strategy().is_feature_available("web_browse"):
            return ToolResult.fail("网页浏览功能当前不可用（系统降级中）")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # 沙箱域名检查
        from tools.web_browse_tools import check_domain_allowed
        allowed, reason = check_domain_allowed(url)
        if not allowed:
            return ToolResult.fail(f"沙箱安全限制: {reason}")

        # SSRF v2 防护：5步法 (协议白名单 + 主机名黑名单 + DNS解析 + IP分类 + DNS Pinning)
        ok, reason = await _ssrf_check_async(url)
        if not ok:
            return ToolResult.fail(f"安全限制：{reason}")

        # 优先级 1：平台专有提取器
        extractor_name = _route_platform(url)
        if extractor_name:
            try:
                extractor = globals().get(extractor_name)
                if extractor:
                    title, content = await extractor(url)
                    logger.info("web_browse.platform_extracted platform={} len={}", extractor_name, len(content))
                    return ToolResult.ok(f"网页: {title}\nURL: {url}\n{'='*40}\n{content}")
            except Exception as e:
                logger.warning("web_browse.platform_failed platform={} error={}", extractor_name, str(e)[:100])

        # 优先级 2：Jina Reader
        try:
            title, content = await _extract_via_jina(url)
            logger.info("web_browse.jina_extracted len={}", len(content))
            if len(content) > 200:
                return ToolResult.ok(f"网页: {title}\nURL: {url}\n{'='*40}\n{content}")
        except Exception as e:
            logger.warning("web_browse.jina_failed error={}", str(e)[:100])

        # 优先级 3：原有 primp + html2text
        from tools.web_browse_tools import web_browse as _original_browse
        return await _original_browse(url)
    except Exception as e:
        return ToolResult.fail(f"浏览网页失败: {e!s}")