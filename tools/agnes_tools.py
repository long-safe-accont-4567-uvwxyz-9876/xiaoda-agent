"""Agnes AI 图像/视频生成工具"""
from typing import Any
import os
import asyncio
import base64
import time
from collections import defaultdict
from pathlib import Path
from loguru import logger
from tool_engine.tool_registry import register_tool, ToolResult, ToolPermission
from config import AGNES_IMAGE_MODEL, AGNES_VIDEO_MODEL, FILE_DIR

# 速率限制：滑动窗口
_RATE_LIMITS = {
    "image": {"max": 10, "window": 3600},  # 10次/小时
    "video": {"max": 3, "window": 3600},    # 3次/小时
}
_rate_timestamps: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(category: str) -> str | None:
    """检查速率限制，返回错误消息或 None（允许通过）"""
    cfg = _RATE_LIMITS.get(category)
    if not cfg:
        return None
    now = time.time()
    cutoff = now - cfg["window"]
    # 清理过期时间戳
    _rate_timestamps[category] = [t for t in _rate_timestamps[category] if t > cutoff]
    if len(_rate_timestamps[category]) >= cfg["max"]:
        return f"{category}生成频率超限（{cfg['max']}次/小时），请稍后再试"
    _rate_timestamps[category].append(now)
    return None

# 模块级懒加载单例客户端
_agnes_openai_client: "AsyncOpenAI | None" = None
_agnes_http_client: "httpx.AsyncClient | None" = None


def _get_agnes_openai_client() -> Any:
    """获取或创建 AsyncOpenAI 单例客户端"""
    global _agnes_openai_client
    if _agnes_openai_client is None:
        from openai import AsyncOpenAI
        _key = os.getenv("AGNES_API_KEY", "")
        _url = os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")
        _agnes_openai_client = AsyncOpenAI(api_key=_key, base_url=_url)
    return _agnes_openai_client


def _get_agnes_http_client() -> Any:
    """获取或创建 httpx.AsyncClient 单例客户端"""
    global _agnes_http_client
    if _agnes_http_client is None:
        import httpx
        _agnes_http_client = httpx.AsyncClient(timeout=30)
    return _agnes_http_client


async def close_agnes_clients() -> None:
    """关闭全局 Agnes 单例客户端, 释放 TCP 连接."""
    global _agnes_openai_client, _agnes_http_client
    if _agnes_openai_client is not None:
        try:
            await _agnes_openai_client.close()
        except Exception:
            pass
        _agnes_openai_client = None
    if _agnes_http_client is not None:
        try:
            await _agnes_http_client.aclose()
        except Exception:
            pass
        _agnes_http_client = None


@register_tool(
    name="agnes_image_generate",
    description="使用 AI 生成图片。支持文生图和图生图。",
    schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "图片描述（英文效果更好）"},
            "image_url": {"type": "string", "description": "参考图片URL（可选，用于图生图）"},
            "size": {"type": "string", "enum": ["1024x1024", "512x512", "1792x1024", "1024x1792"], "default": "1024x1024"},
            "n": {"type": "integer", "default": 1, "description": "生成图片数量"},
        },
        "required": ["prompt"],
    },
    permission=ToolPermission.READ_ONLY,
)
async def agnes_image_generate(prompt: str, image_url: str = "",
                                size: str = "1024x1024", n: int = 1) -> ToolResult:
    """生成图片"""
    if not os.getenv("AGNES_API_KEY", ""):
        return ToolResult.fail("Agnes API Key 未配置")

    rate_err = _check_rate_limit("image")
    if rate_err:
        return ToolResult.fail(rate_err)

    try:
        client = _get_agnes_openai_client()

        kwargs = {
            "model": AGNES_IMAGE_MODEL,
            "prompt": prompt,
            "size": size,
            "n": n,
        }
        if image_url:
            # 图生图模式：使用 extra_body.image 传递参考图 URL
            # prompt 仍需保留，用于描述需要改变/保持的内容
            kwargs["extra_body"] = {"image": [image_url]}

        response = await client.images.generate(**kwargs)

        results = []
        ts = int(time.time())
        for idx, img in enumerate(response.data):
            if img.url:
                results.append(f"图片URL: {img.url}")
            elif img.b64_json:
                # 保存 base64 图片到文件
                img_data = base64.b64decode(img.b64_json)
                img_path = FILE_DIR / f"agnes_img_{ts}_{idx}.png"
                img_path.write_bytes(img_data)
                results.append(f"图片已保存到: {img_path}")

        return ToolResult.ok("\n".join(results) if results else "图片生成完成但无结果")
    except Exception as e:
        logger.error("agnes.image_generate_failed", error=str(e))
        return ToolResult.fail(f"图片生成失败: {e}")


@register_tool(
    name="agnes_video_generate",
    description="使用 AI 生成视频。支持文生视频，异步任务模式。",
    schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "视频描述"},
            "seconds": {"type": "number", "default": 5, "description": "视频时长（秒）"},
            "fps": {"type": "integer", "default": 24, "description": "帧率"},
        },
        "required": ["prompt"],
    },
    permission=ToolPermission.READ_ONLY,
)
async def agnes_video_generate(prompt: str, seconds: float = 5, fps: int = 24) -> ToolResult:
    """生成视频（异步任务模式）"""
    if not os.getenv("AGNES_API_KEY", ""):
        return ToolResult.fail("Agnes API Key 未配置")

    rate_err = _check_rate_limit("video")
    if rate_err:
        return ToolResult.fail(rate_err)

    try:
        from utils.lazy_deps import ensure
        if not ensure("httpx"):
            return ToolResult.fail("httpx 未安装，无法生成视频")

        client = _get_agnes_http_client()
        _agnes_key = os.getenv("AGNES_API_KEY", "")
        _agnes_url = os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")

        video_id, data = await _agnes_create_video_task(
            client, _agnes_url, _agnes_key, prompt, seconds, fps
        )
        if not video_id:
            return ToolResult.fail(f"视频任务创建失败: {data}")

        return await _agnes_poll_and_download_video(
            client, _agnes_url, _agnes_key, video_id
        )
    except Exception as e:
        logger.error("agnes.video_generate_failed", error=str(e))
        return ToolResult.fail(f"视频生成失败: {e}")


async def _agnes_create_video_task(
    client: Any, url: str, key: str, prompt: str, seconds: float, fps: int
) -> tuple[str, dict]:
    """创建视频生成任务，返回 (video_id, data)。"""
    import math
    # 计算帧数：num_frames = 8n + 1, 且 <= 441
    raw_frames = int(seconds * fps)
    n = max(1, (raw_frames - 1) // 8)
    num_frames = min(8 * n + 1, 441)

    # 创建视频生成任务
    resp = await client.post(
        f"{url}/video/generations",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": AGNES_VIDEO_MODEL,
            "prompt": prompt,
            "num_frames": num_frames,
            "frame_rate": fps,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    # 任务 ID 可能在 id、video_id 或 task_id 字段
    video_id = data.get("id") or data.get("video_id", "") or data.get("task_id", "")
    return video_id, data


async def _agnes_poll_and_download_video(
    client: Any, url: str, key: str, video_id: str
) -> ToolResult:
    """轮询视频任务状态并下载结果。"""
    # 轮询等待结果（自适应间隔：前3次5s，之后10s，总等待180s）
    poll_count = 0
    max_polls = 24  # 3*5 + 21*10 = 225s，但实际 3*5 + 15*10 = 165s 足够
    while poll_count < max_polls:
        interval = 5 if poll_count < 3 else 10
        await asyncio.sleep(interval)
        poll_count += 1
        status_resp = await client.get(
            f"{url}/video/generations/{video_id}",
            headers={"Authorization": f"Bearer {key}"},
        )
        status_resp.raise_for_status()
        status_data = status_resp.json()
        # 状态可能在顶层 status 或 data.status
        status = status_data.get("status", "").lower()
        inner_data = status_data.get("data", {})
        if inner_data and isinstance(inner_data, dict):
            inner_status = inner_data.get("status", "").lower()
            if inner_status in ("completed", "succeeded", "success"):
                status = inner_status

        if status in ("completed", "succeeded", "success"):
            # 提取视频URL
            video_url = status_data.get("output", {}).get("url", "")
            if video_url:
                logger.info("agnes.video_url_extracted", path="output.url")
            else:
                video_url = status_data.get("url", "")
                if video_url:
                    logger.info("agnes.video_url_extracted", path="top_level_url")

            if video_url:
                # 下载视频到本地
                try:
                    tts_cache_dir = FILE_DIR.parent / "tts_cache"
                    tts_cache_dir.mkdir(parents=True, exist_ok=True)
                    video_ts = int(time.time())
                    local_path = tts_cache_dir / f"video_{video_ts}.mp4"
                    download_resp = await client.get(video_url)
                    download_resp.raise_for_status()
                    local_path.write_bytes(download_resp.content)
                    return ToolResult.ok(f"视频生成完成！本地路径: {local_path}")
                except Exception as dl_err:
                    logger.error("agnes.video_download_failed", error=str(dl_err))
                    # 降级：返回 URL 供用户手动查看
                    return ToolResult.ok(f"视频生成完成！下载失败，请直接访问：{video_url}")
            return ToolResult.fail(f"视频生成完成，但未获取到URL: {status_data}")
        elif status in ("failed", "error"):
            error_msg = status_data.get("fail_reason", "") or status_data.get("error", "未知错误")
            return ToolResult.fail(f"视频生成失败: {error_msg}")

    return ToolResult.fail(f"视频生成超时，任务ID: {video_id}，请稍后查询")
