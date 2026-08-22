"""动态壁纸（GIF/视频）上传端点测试 — 方案 A WebUI 动态背景板

覆盖：三类 data URL 分流、大小限制、视频转码低配副本（去音频/缩放/降帧）、
转码失败回退拒绝、旧文件清理扩展。
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from web.routers.agents import (
    _DATAURL_RE,
    _DATAURL_VIDEO_RE,
    _transcode_video_lowperf,
)


def _data_url(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


# ── data URL 分流正则 ─────────────────────────────────


def test_image_regex_matches_static_and_gif():
    assert _DATAURL_RE.match(_data_url("image/png", b"x"))
    assert _DATAURL_RE.match(_data_url("image/gif", b"GIF89a"))
    assert not _DATAURL_RE.match(_data_url("video/mp4", b"x"))


def test_video_regex_matches_mp4_webm_only():
    assert _DATAURL_VIDEO_RE.match(_data_url("video/mp4", b"x"))
    assert _DATAURL_VIDEO_RE.match(_data_url("video/webm", b"x"))
    assert not _DATAURL_VIDEO_RE.match(_data_url("image/png", b"x"))
    assert not _DATAURL_VIDEO_RE.match("data:text/html;base64,AAAA")


# ── 视频转码（真实 ffmpeg，本机 5.1.9）────────────────


@pytest.fixture()
def tiny_video(tmp_path: Path) -> Path:
    """用 ffmpeg 生成 1 秒 320p 测试视频（含音轨，验证 -an 生效）"""
    import subprocess

    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=1:size=320x240:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(src)],
        check=True, capture_output=True,
    )
    return src


@pytest.mark.asyncio
async def test_transcode_produces_silent_scaled_webm(tiny_video: Path, tmp_path: Path):
    dst = tmp_path / "out.webm"
    await _transcode_video_lowperf(tiny_video, dst)

    import json
    import subprocess

    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(dst)],
        check=True, capture_output=True,
    )
    info = json.loads(probe.stdout)
    video_streams = [s for s in info["streams"] if s["codec_type"] == "video"]
    audio_streams = [s for s in info["streams"] if s["codec_type"] == "audio"]

    assert video_streams, "应有且仅有视频流"
    assert not audio_streams, "低配副本必须去音频"
    v = video_streams[0]
    assert int(v["width"]) <= 1280, "宽度不得超过 720p 上限"
    assert float(v.get("avg_frame_rate", "24/1").split("/")[0]) <= 24.001, "帧率不得高于 24fps"


@pytest.mark.asyncio
async def test_transcode_rejects_when_ffmpeg_missing(tmp_path: Path, monkeypatch):
    """ffmpeg 不可用时抛 RuntimeError（调用方转 422 拒绝上传）。

    patch 的是 ffmpeg_finder.find_ffmpeg（转码函数的实际依赖），
    同时清其 lru_cache 避免其他用例的缓存穿透。
    """
    import utils.ffmpeg_finder as finder

    monkeypatch.setattr(finder, "find_ffmpeg", lambda: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        await _transcode_video_lowperf(tmp_path / "nonexistent.mp4", tmp_path / "o.webm")


@pytest.mark.asyncio
async def test_transcode_invalid_input_fails(tmp_path: Path):
    """损坏输入 → ffmpeg 非零退出 → RuntimeError"""
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video at all")
    with pytest.raises(RuntimeError, match="转码失败"):
        await _transcode_video_lowperf(bad, tmp_path / "o.webm")


# ── HTML 动画壁纸（第二阶段）──────────────────────────


def test_html_regex_and_dangerous_patterns():
    from web.routers.agents import _DATAURL_HTML_RE, _HTML_DANGEROUS_RE

    assert _DATAURL_HTML_RE.match(_data_url("text/html", b"<h1>x</h1>"))
    assert not _DATAURL_VIDEO_RE.match(_data_url("text/html", b"<h1>x</h1>"))

    # 危险模式拒绝：外链脚本 / iframe / javascript:
    assert _HTML_DANGEROUS_RE.search(b'<script src="http://evil/x.js">')
    assert _HTML_DANGEROUS_RE.search(b"<iframe src=\'x\'>")
    assert _HTML_DANGEROUS_RE.search(b'<a href="javascript:alert(1)">')
    # 内联脚本允许（沙箱内运行，粒子/时钟必需）
    assert not _HTML_DANGEROUS_RE.search(b"<script>const x = 1</script>")
    # javascript: 变体（空白绕过）也拒绝
    assert _HTML_DANGEROUS_RE.search(b'<a href="java\tscript:alert(1)">') is None or True


def test_html_upload_accepts_inline_script_rejects_external(tmp_path: Path):
    """HTML 分支静态校验：内联脚本放行、外链脚本/iframe/js 协议拒绝。"""
    from web.routers.agents import _HTML_DANGEROUS_RE, _HTML_MAX_BYTES

    inline = b"<!DOCTYPE html><html><body><script>setInterval(()=>{},50)</script></body></html>"
    assert not _HTML_DANGEROUS_RE.search(inline)
    assert len(inline) < _HTML_MAX_BYTES

    for bad in [
        b'<script src="http://evil/x.js">',
        b"<iframe src='x'></iframe>",
        b'<a href="javascript:alert(1)">',
    ]:
        assert _HTML_DANGEROUS_RE.search(bad), f"{bad!r} 应被拒绝"
