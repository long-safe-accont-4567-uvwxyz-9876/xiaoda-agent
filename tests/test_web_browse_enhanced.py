"""测试 tools/web_browse_enhanced.py — 平台路由、Markdown 标题提取、
SSRF 安全检查、Jina Reader 调用（mock httpx）。

风格参考 tests/test_instinct_manager.py：unittest + asyncio.run + mock。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tools.web_browse_enhanced import (
    _extract_title_from_markdown,
    _extract_via_jina,
    _is_private_ip_async,
    _route_platform,
    web_browse_enhanced,
)


class TestRoutePlatform(unittest.TestCase):
    """_route_platform 对各平台 URL 返回正确的提取器名"""

    def test_route_platform(self):
        cases = {
            "https://www.zhihu.com/question/123": "_extract_zhihu",
            "https://zhuanlan.zhihu.com/p/456": "_extract_zhihu",
            "https://www.bilibili.com/video/BV1xx": "_extract_bilibili",
            "https://mp.weixin.qq.com/s/abc": "_extract_wechat",
            "https://weibo.com/123/abc": "_extract_weibo",
            "https://m.weibo.cn/123/abc": "_extract_weibo",
            "https://36kr.com/p/123": "_extract_36kr",
            "https://blog.csdn.net/x/article": "_extract_csdn",
            "https://www.douyin.com/video/1": "_extract_douyin",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(_route_platform(url), expected)

    def test_route_platform_unknown(self):
        """未知平台返回 None"""
        self.assertIsNone(_route_platform("https://example.com/article"))
        self.assertIsNone(_route_platform("https://news.ycombinator.com/"))


class TestExtractTitleFromMarkdown(unittest.TestCase):
    """从 Markdown 提取首个 '# ' 标题"""

    def test_extract_title_from_markdown(self):
        self.assertEqual(_extract_title_from_markdown("# 标题\n正文"), "标题")

    def test_extract_title_from_markdown_empty(self):
        """无标题行返回空串"""
        self.assertEqual(_extract_title_from_markdown("正文没有标题\n第二行"), "")
        self.assertEqual(_extract_title_from_markdown(""), "")
        # '## ' 二级标题不应被识别为 '# ' 标题
        self.assertEqual(_extract_title_from_markdown("## 二级\n正文"), "")


class TestIsPrivateIpAsync(unittest.TestCase):
    """_is_private_ip_async 通过 to_thread 委托给 _is_private_ip"""

    def test_is_private_ip_async(self):
        """mock _is_private_ip：127.0.0.1→True，8.8.8.8→False"""
        async def _run():
            with patch("tools.web_browse_tools._is_private_ip") as mock_p:
                mock_p.side_effect = lambda h: h == "127.0.0.1"
                self.assertTrue(await _is_private_ip_async("127.0.0.1"))
                self.assertFalse(await _is_private_ip_async("8.8.8.8"))
                # 验证底层同步函数被以正确参数调用
                mock_p.assert_any_call("127.0.0.1")
                mock_p.assert_any_call("8.8.8.8")

        asyncio.run(_run())


class TestWebBrowseEnhancedSecurityBlock(unittest.TestCase):
    """沙箱安全校验：check_domain_allowed 返回 (False, ...) 时 ToolResult.fail"""

    def test_web_browse_enhanced_security_block(self):
        async def _run():
            with patch("tools.web_browse_tools.check_domain_allowed",
                       return_value=(False, "blocked")):
                result = await web_browse_enhanced("https://example.com/")
            self.assertFalse(result.success)
            self.assertIn("blocked", result.error)

        asyncio.run(_run())


class TestExtractViaJinaMock(unittest.TestCase):
    """mock httpx.AsyncClient 验证 Jina 调用与 Markdown 标题提取"""

    def test_extract_via_jina_mock(self):
        async def _run():
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = "# Test\nContent"

            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.get.return_value = mock_resp

            with patch("tools.web_browse_enhanced.httpx.AsyncClient",
                       return_value=mock_client):
                title, content = await _extract_via_jina("https://example.com/page")

            self.assertEqual(title, "Test")
            self.assertIn("Content", content)
            # 验证 get 被调用，URL 拼接到 r.jina.ai
            mock_client.get.assert_awaited_once()
            called_url = mock_client.get.await_args.args[0]
            self.assertIn("r.jina.ai", called_url)
            self.assertIn("example.com", called_url)

        asyncio.run(_run())


if __name__ == '__main__':
    unittest.main()
