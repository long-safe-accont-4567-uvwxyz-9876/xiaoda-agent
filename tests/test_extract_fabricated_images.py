"""_extract_fabricated_images_from_reply 测试：兜底提取 LLM 伪造的图片 URL。

场景：LLM 不调 agnes_image_generate，而在回复里写 markdown 图 ![](url)
或裸 pollinations URL。本函数下载 URL→image_paths，并从文本剥离。
httpx 下载需 mock，避免真实网络。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from agent_core.tool_executor import ToolExecutorMixin


class _DummyMixin(ToolExecutorMixin):
    """最小桩：ToolExecutorMixin 是 mixin，无 __init__ 依赖即可调用方法。"""
    pass


def _make_reply_with_md_image():
    return (
        "爸爸别催嘛～终于准备好送给主人了 !! "
        "![Self Portrait](https://image.pollinations.ai/prompt/cute+cat?width=560&height=792) "
        'Width Height: 560x792 | Seed: 93847 | Model: Default | Prompt: "cute cat"'
    )


def _fake_httpx_get(content: bytes):
    """返回一个模拟 httpx.AsyncClient 上下文管理器。"""
    client = MagicMock()
    resp = MagicMock()
    resp.content = content
    resp.raise_for_status = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get = AsyncMock(return_value=resp)
    return client


def test_extracts_markdown_image_and_strips(tmp_path):
    m = _DummyMixin()
    reply = _make_reply_with_md_image()
    with patch("agent_core.tool_executor.FILE_DIR", tmp_path), \
         patch("httpx.AsyncClient", return_value=_fake_httpx_get(b"PNGDATA")):
        paths, cleaned = asyncio.run(m._extract_fabricated_images_from_reply(reply))
    assert len(paths) == 1
    assert paths[0].exists()
    assert paths[0].read_bytes() == b"PNGDATA"
    # markdown 图与伪造元数据已剥离
    assert "image.pollinations.ai" not in cleaned
    assert "Width Height" not in cleaned
    assert "终于准备好送给主人了" in cleaned


def test_extracts_bare_pollinations_url(tmp_path):
    m = _DummyMixin()
    reply = "图在这里 https://image.pollinations.ai/prompt/dog 给你"
    with patch("agent_core.tool_executor.FILE_DIR", tmp_path), \
         patch("httpx.AsyncClient", return_value=_fake_httpx_get(b"IMG")):
        paths, cleaned = asyncio.run(m._extract_fabricated_images_from_reply(reply))
    assert len(paths) == 1
    assert "image.pollinations.ai" not in cleaned
    assert "图在这里" in cleaned and "给你" in cleaned


def test_multiple_images(tmp_path):
    m = _DummyMixin()
    reply = (
        "![a](https://image.pollinations.ai/prompt/cat) "
        "![b](https://image.pollinations.ai/prompt/dog)"
    )
    with patch("agent_core.tool_executor.FILE_DIR", tmp_path), \
         patch("httpx.AsyncClient", return_value=_fake_httpx_get(b"IMG")):
        paths, cleaned = asyncio.run(m._extract_fabricated_images_from_reply(reply))
    assert len(paths) == 2
    assert "![" not in cleaned


def test_download_failure_does_not_raise(tmp_path):
    m = _DummyMixin()
    reply = "![a](https://image.pollinations.ai/prompt/cat)"
    client = _fake_httpx_get(b"")
    client.get = AsyncMock(side_effect=Exception("network down"))
    with patch("agent_core.tool_executor.FILE_DIR", tmp_path), \
         patch("httpx.AsyncClient", return_value=client):
        paths, cleaned = asyncio.run(m._extract_fabricated_images_from_reply(reply))
    # 下载失败：image_paths 不追加，但 markdown 仍剥离，不抛异常
    assert paths == []
    assert "![" not in cleaned
    assert "image.pollinations.ai" not in cleaned


def test_normal_link_not_extracted(tmp_path):
    m = _DummyMixin()
    reply = "看这个 https://example.com/info 挺好的"
    with patch("agent_core.tool_executor.FILE_DIR", tmp_path), \
         patch("httpx.AsyncClient", return_value=_fake_httpx_get(b"")):
        paths, cleaned = asyncio.run(m._extract_fabricated_images_from_reply(reply))
    assert paths == []
    assert "https://example.com/info" in cleaned  # 普通链接保留


def test_empty_reply():
    m = _DummyMixin()
    paths, cleaned = asyncio.run(m._extract_fabricated_images_from_reply(""))
    assert paths == []
    assert cleaned == ""
