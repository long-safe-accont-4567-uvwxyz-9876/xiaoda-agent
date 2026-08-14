"""测试工具结果长度上限截断 —— 防止无上限返回内容打爆 LLM 上下文。

覆盖三个无截断返回点：
- tools/web_browse_enhanced.py：Jina / 平台提取结果 content 无上限
- tools/mail_tools.py：mail_read / mail_search 正文无上限
- tools/system_tools.py：dev_assist(logs) 日志无上限

对齐 tools/web_browse_tools.py 的 8000 字符上限惯例。
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from tools.web_browse_enhanced import _truncate as _truncate_web
from tools.mail_tools import _truncate as _truncate_mail, _truncate_mail_data
from tools.system_tools import _truncate as _truncate_sys

_TRUNCATION_MARKER = "\n...(内容过长已截断)"


@pytest.mark.parametrize("truncate_fn", [_truncate_web, _truncate_mail, _truncate_sys])
def test_truncate_short_content_unchanged(truncate_fn):
    """短内容不应被截断或附加标记。"""
    text = "这是一段很短的普通内容。"
    assert truncate_fn(text) == text


@pytest.mark.parametrize("truncate_fn", [_truncate_web, _truncate_mail, _truncate_sys])
def test_truncate_overlong_content_capped_with_marker(truncate_fn):
    """超长内容应被截断到 8000 字符以内（不含省略标记），并带省略提示。"""
    text = "x" * 20000
    result = truncate_fn(text, limit=8000)
    assert _TRUNCATION_MARKER in result
    assert result.startswith("x" * 8000)
    assert len(result) <= 8000 + len(_TRUNCATION_MARKER)


def test_truncate_mail_data_caps_body_strings():
    """mail 返回数据中的超长字符串字段（正文）应被截断，短字段保持不变。"""
    data = {
        "id": "msg_123",
        "body": "y" * 20000,
        "attachments": [{"name": "report.pdf"}],
    }
    result = _truncate_mail_data(data)
    assert result["id"] == "msg_123"
    assert _TRUNCATION_MARKER in result["body"]
    assert len(result["body"]) <= 8000 + len(_TRUNCATION_MARKER)
    assert result["attachments"] == [{"name": "report.pdf"}]


def test_truncate_mail_data_preserves_structural_fields():
    """结构化字段（id/download_url/next_cursor/token）不应被截断，正文仍截断。"""
    long_url = "https://example.com/download/" + "a" * 20000
    long_cursor = "c" * 20000
    long_token = "t" * 20000
    data = {
        "id": "msg_123",
        "body": "y" * 20000,
        "download_url": long_url,
        "next_cursor": long_cursor,
        "confirmation_token": long_token,
    }
    result = _truncate_mail_data(data)

    assert result["id"] == "msg_123"
    assert result["download_url"] == long_url
    assert result["next_cursor"] == long_cursor
    assert result["confirmation_token"] == long_token
    assert _TRUNCATION_MARKER in result["body"]


def test_web_browse_enhanced_jina_content_truncated():
    """Jina 路径返回的超长 content 应被截断。"""
    async def _run():
        from tools.web_browse_enhanced import web_browse_enhanced
        long_content = "# Title\n" + "x" * 20000
        with patch("tools.web_browse_tools.check_domain_allowed", return_value=(True, "")), \
             patch("tools.web_browse_enhanced._ssrf_check_async", new=AsyncMock(return_value=(True, ""))), \
             patch("tools.web_browse_enhanced._extract_via_jina", new=AsyncMock(return_value=("Title", long_content))):
            result = await web_browse_enhanced("https://example.com/page")
        assert result.success
        assert _TRUNCATION_MARKER in result.data
        assert len(result.data) <= 8000 + len(_TRUNCATION_MARKER) + 200

    asyncio.run(_run())


def test_web_browse_enhanced_platform_content_truncated():
    """平台专有提取器返回的超长 content 应被截断。"""
    async def _run():
        from tools.web_browse_enhanced import web_browse_enhanced
        long_content = "z" * 20000
        with patch("tools.web_browse_tools.check_domain_allowed", return_value=(True, "")), \
             patch("tools.web_browse_enhanced._ssrf_check_async", new=AsyncMock(return_value=(True, ""))), \
             patch("tools.web_browse_enhanced._route_platform", return_value="_extract_zhihu"), \
             patch("tools.web_browse_enhanced._extract_zhihu", new=AsyncMock(return_value=("知乎文章", long_content))):
            result = await web_browse_enhanced("https://www.zhihu.com/question/123")
        assert result.success
        assert _TRUNCATION_MARKER in result.data
        assert len(result.data) <= 8000 + len(_TRUNCATION_MARKER) + 200

    asyncio.run(_run())


def test_dev_assist_logs_truncated():
    """dev_assist(logs) 返回的超长日志应被截断。"""
    async def _run():
        from tools.system_tools import _dev_assist_logs
        with patch("tools.system_tools.os.path.isdir", return_value=False), \
             patch("tools.system_tools._run_cmd", new=AsyncMock(return_value=(0, "x" * 20000, ""))):
            result = await _dev_assist_logs("/tmp", 50, "xiaoda-web")
        assert result.success
        assert _TRUNCATION_MARKER in result.data

    asyncio.run(_run())
