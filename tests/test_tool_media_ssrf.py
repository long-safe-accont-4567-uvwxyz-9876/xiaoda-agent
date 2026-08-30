"""工具结果二次图片抓取的 SSRF 防护测试（审计修复 2026-08-29）。

覆盖：
- 私网/回环 URL（http://127.0.0.1:9/x）不发任何 HTTP 请求（guard 在传输层之前拒绝）；
- 正常公网 URL 必须经 ssrf_guard.resolve_and_pin 解析+钉定后才请求（Host 头为原始主机）；
- 3xx 重定向不跟随直接放弃；响应体超 10 MiB 拒绝；非图片内容拒绝；
- Content-Type image/* 或 PNG/JPEG/GIF/WebP 文件头魔数至少一种命中才落盘。

全部在 httpx/ssrf_guard 边界 mock，不发真实网络请求。
"""
from types import SimpleNamespace

import httpx
import pytest

import agent_core.tool_executor_mixin as mixin_mod
from agent_core.tool_executor_mixin import (
    ToolExecutorMixin,
    _bytes_look_like_image,
    _download_image_url_guarded,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _fake_result(data: str) -> SimpleNamespace:
    """构造带 success/data 的假 ToolResult（避免拖入真实注册表）。"""
    return SimpleNamespace(success=True, data=data, error="")


def _make_mixin() -> ToolExecutorMixin:
    """构造不走 AgentCore.__init__ 的裸 Mixin 实例（被测方法不依赖实例状态）。"""
    return ToolExecutorMixin.__new__(ToolExecutorMixin)


@pytest.fixture
def no_real_transport(monkeypatch):
    """任何请求到达真实 httpx 传输层即失败——证明防护在传输层之前生效。"""

    async def _boom(self, request):
        raise AssertionError("不应有任何请求到达真实 httpx 传输层")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _boom)


@pytest.fixture
def fake_pinned_url(monkeypatch):
    """钉定成功的假 resolve_and_pin：返回公网钉定 URL + 原始 Host 头。"""
    calls: list[str] = []

    def _fake(base_url: str) -> tuple[str, str]:
        calls.append(base_url)
        return "http://93.184.216.34/pinned", "example.com"

    monkeypatch.setattr("security.ssrf_guard.resolve_and_pin", _fake)
    return calls


@pytest.mark.asyncio
async def test_loopback_url_never_fetched(tmp_path, monkeypatch, no_real_transport):
    """图片URL 指向回环地址：guard 拒绝且不发任何请求、不落盘。"""
    monkeypatch.setattr(mixin_mod, "FILE_DIR", tmp_path)
    result = _fake_result("图片URL: http://127.0.0.1:9/x.png")
    image_paths, _, _ = await _make_mixin()._extract_media_from_tool_results([result], "回复")
    assert image_paths == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_public_url_goes_through_guard_and_saves(tmp_path, monkeypatch, fake_pinned_url):
    """正常公网 URL：经 resolve_and_pin 钉定后下载，图片落入 image_paths。"""
    body = PNG_MAGIC + b"fake-png-bytes"

    async def fake_handle(self, request):
        # 钉定生效：请求被改写到锁定 IP，Host 头保留原始主机名
        assert request.headers["Host"] == "example.com"
        return httpx.Response(200, headers={"content-type": "image/png"},
                              content=body, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_handle)
    monkeypatch.setattr(mixin_mod, "FILE_DIR", tmp_path)
    result = _fake_result("图片URL: http://example.com/img.png")
    image_paths, _, _ = await _make_mixin()._extract_media_from_tool_results([result], "回复")
    assert fake_pinned_url == ["http://example.com/img.png"]
    assert len(image_paths) == 1
    assert image_paths[0].read_bytes() == body


@pytest.mark.asyncio
async def test_oversized_body_rejected(tmp_path, monkeypatch, fake_pinned_url):
    """响应体超过 10 MiB：流式读取中途失败，不落盘。"""
    oversized = PNG_MAGIC + b"\x00" * (10 * 1024 * 1024)  # 头 8 字节 + 10 MiB > 上限

    async def fake_handle(self, request):
        return httpx.Response(200, headers={"content-type": "image/png"},
                              content=oversized, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_handle)
    local = await _download_image_url_guarded("http://example.com/big.png", tmp_path, 0)
    assert local is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_non_image_content_rejected(tmp_path, monkeypatch, fake_pinned_url):
    """text/html 且无图片魔数：内容校验失败，丢弃不落盘。"""

    async def fake_handle(self, request):
        return httpx.Response(200, headers={"content-type": "text/html"},
                              content=b"<html>definitely not an image</html>", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_handle)
    local = await _download_image_url_guarded("http://example.com/page", tmp_path, 0)
    assert local is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_redirect_not_followed(tmp_path, monkeypatch, fake_pinned_url, no_real_transport):
    """3xx 重定向：follow_redirects=False 直接放弃，不请求跳转目标。"""

    async def fake_handle(self, request):
        return httpx.Response(302, headers={"location": "http://10.0.0.1/evil.png"},
                              request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_handle)
    local = await _download_image_url_guarded("http://example.com/redirect", tmp_path, 0)
    assert local is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_magic_bytes_fallback_saves_without_content_type(tmp_path, monkeypatch, fake_pinned_url):
    """无 image/* Content-Type 时按文件头魔数放行（WebP）。"""

    async def fake_handle(self, request):
        return httpx.Response(200, headers={"content-type": "application/octet-stream"},
                              content=b"RIFF\x24\x00\x00\x00WEBPVP8 PAYLOAD", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_handle)
    local = await _download_image_url_guarded("http://example.com/pic.webp", tmp_path, 0)
    assert local is not None
    assert local.read_bytes().startswith(b"RIFF")


@pytest.mark.asyncio
async def test_content_type_image_passes_without_magic(tmp_path, monkeypatch, fake_pinned_url):
    """Content-Type image/* 直接放行（无需魔数命中）。"""

    async def fake_handle(self, request):
        return httpx.Response(200, headers={"content-type": "image/jpeg"},
                              content=b"payload-not-a-real-jpeg", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_handle)
    local = await _download_image_url_guarded("http://example.com/pic.jpeg", tmp_path, 0)
    assert local is not None


@pytest.mark.asyncio
async def test_transport_error_returns_none(tmp_path, monkeypatch, fake_pinned_url):
    """传输层异常（连接失败等）返回 None，不向主流程抛异常。"""

    async def fake_handle(self, request):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_handle)
    local = await _download_image_url_guarded("http://example.com/dead.png", tmp_path, 0)
    assert local is None


def test_bytes_look_like_image_formats():
    """魔数识别覆盖 PNG/JPEG/GIF87a/GIF89a/WebP 命中与杂乱字节不命中。"""
    assert _bytes_look_like_image(PNG_MAGIC + b"x")
    assert _bytes_look_like_image(b"\xff\xd8\xff\xe0rest")
    assert _bytes_look_like_image(b"GIF87a....")
    assert _bytes_look_like_image(b"GIF89a....")
    assert _bytes_look_like_image(b"RIFF\x24\x00\x00\x00WEBPVP8 ")
    assert not _bytes_look_like_image(b"<html></html>")
    assert not _bytes_look_like_image(b"RIFF short")
    assert not _bytes_look_like_image(b"")
