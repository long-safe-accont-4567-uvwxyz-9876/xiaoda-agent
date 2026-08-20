"""QQ Bot 媒体上传/发送 Mixin。

从 qq_bot_adapter.py 拆分而来，负责：
- base64 文件上传（C2C / 群聊共用）
- 图片压缩
- 媒体回复发送（图文混排）
- 视频/语音消息发送
- SILK 音频转码
- 媒体发送任务编排
"""
from __future__ import annotations

import asyncio
import base64
import subprocess
import time
from pathlib import Path
from typing import Any

from loguru import logger

from botpy.message import C2CMessage, GroupMessage


class QQMediaMixin:
    """媒体上传/发送方法组。

    要求宿主类提供以下属性/方法：
    - self.api: botpy API 客户端
    - _next_msg_seq(): 模块级函数（通过闭包或直接 import）
    """

    async def _send_reply_with_media(self, message: Any, reply: str,
                                      image_path: Path | None = None,
                                      image_url: str | None = None) -> None:
        from qq_bot_adapter import _next_msg_seq

        if not image_path and not image_url:
            await message.reply(content=reply, msg_seq=_next_msg_seq())
            return

        try:
            if isinstance(message, C2CMessage):
                await self._send_c2c_media(message, reply, image_path, image_url)
            elif isinstance(message, GroupMessage):
                await self._send_group_media(message, reply, image_path, image_url)
            else:
                await message.reply(content=reply, msg_seq=_next_msg_seq())
        except (OSError, RuntimeError, ConnectionError, ValueError) as e:
            logger.warning("qq_bot.media_send_failed", error=str(e))
            try:
                await message.reply(content=reply, msg_seq=_next_msg_seq())
            except (OSError, RuntimeError, ConnectionError) as _e:
                logger.debug("qq_bot.fallback_reply_failed", error=str(_e))

    async def _send_c2c_media(self, message: Any, reply: str,
                               image_path: Path | None, image_url: str | None) -> None:
        from qq_bot_adapter import _next_msg_seq

        openid = message.author.user_openid
        if image_path:
            file_info = await self._upload_c2c_base64(openid, image_path)
        else:
            media = await self.api.post_c2c_file(
                openid=openid, file_type=1, url=image_url
            )
            file_info = getattr(media, "file_info", "")
        if not file_info:
            raise RuntimeError("C2C媒体接口返回空file_info")
        response = await self.api.post_c2c_message(
            openid=openid, msg_id=message.id,
            msg_type=7, content=reply,
            media={"file_info": file_info}, msg_seq=_next_msg_seq()
        )
        if response is None:
            raise RuntimeError("C2C消息接口返回None")

    async def _send_group_media(self, message: Any, reply: str,
                                 image_path: Path | None, image_url: str | None) -> None:
        from qq_bot_adapter import _next_msg_seq

        group_openid = message.group_openid
        if image_path:
            file_info = await self._upload_group_base64(group_openid, image_path)
        else:
            media = await self.api.post_group_file(
                group_openid=group_openid, file_type=1, url=image_url
            )
            file_info = getattr(media, "file_info", "")
        if not file_info:
            raise RuntimeError("群媒体接口返回空file_info")
        try:
            await self.api.post_group_message(
                group_openid=group_openid, msg_id=message.id,
                msg_type=7, content=reply,
                media={"file_info": file_info}, msg_seq=_next_msg_seq()
            )
        except (OSError, RuntimeError, ConnectionError) as e:
            if "被动回复" in str(e) or "超过限制" in str(e):
                logger.warning("qq_bot.group_media_passive_limited_no_proactive",
                               error=str(e))
            else:
                raise

    async def _upload_c2c_base64(self, openid: str, image_path: Path, file_type: int = 1) -> str:
        return await self._upload_base64(openid, image_path, file_type, group=False)

    async def _upload_group_base64(self, group_openid: str, image_path: Path, file_type: int = 1) -> str:
        return await self._upload_base64(group_openid, image_path, file_type, group=True)

    async def _upload_base64(self, target: str, image_path: Path, file_type: int = 1,
                             *, group: bool = False) -> str:
        """上传 base64 文件到 QQ 文件接口（C2C/群聊共用）。

        图片类型（file_type=1）且文件 >800KB 时自动压缩；临时文件由 finally 清理。
        """
        from botpy.http import Route

        compressed_path: Path | None = None
        try:
            def _read() -> Any:
                nonlocal compressed_path
                path_to_upload = image_path
                if file_type == 1 and image_path.stat().st_size > 800_000:
                    compressed_path = self._compress_image(image_path)
                    path_to_upload = compressed_path
                with open(path_to_upload, "rb") as f:
                    return base64.b64encode(f.read()).decode()

            file_data = await asyncio.to_thread(_read)
            if group:
                payload = {
                    "group_openid": target,
                    "file_type": file_type,
                    "file_data": file_data,
                    "srv_send_msg": False,
                }
                route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=target)
                desc = "群文件上传"
            else:
                payload = {
                    "openid": target,
                    "file_type": file_type,
                    "file_data": file_data,
                    "srv_send_msg": False,
                }
                route = Route("POST", "/v2/users/{openid}/files", openid=target)
                desc = "C2C文件上传"
            last_err: BaseException | None = None
            for attempt in range(3):
                try:
                    result = await self.api._http.request(route, json=payload)
                    file_info = result.get("file_info", "") if isinstance(result, dict) else getattr(result, "file_info", "")
                    if not file_info:
                        raise RuntimeError(f"{desc}返回空file_info (target={target})")
                    return file_info
                except (OSError, RuntimeError, ConnectionError, TimeoutError) as e:
                    last_err = e
                    if attempt < 2:
                        wait = (attempt + 1) * 3
                        logger.warning("qq_bot.upload_retry", attempt=attempt + 1, wait=wait, error=str(e))
                        await asyncio.sleep(wait)
            raise RuntimeError(f"{desc}失败（已重试3次）") from last_err
        finally:
            if compressed_path is not None:
                try:
                    compressed_path.unlink()
                    logger.info("qq_bot.temp_file_cleaned", path=str(compressed_path))
                except OSError as e:
                    logger.warning("qq_bot.temp_file_cleanup_failed: {}", e)

    @staticmethod
    def _compress_image(image_path: Path, max_size: int = 800_000, quality: int = 75) -> Path:
        """压缩图片到指定大小以下，返回压缩后的临时文件路径。"""
        from PIL import Image
        import tempfile

        tmp_path: Path | None = None

        try:
            with Image.open(image_path) as img:
                save_img = img.convert("RGB") if img.mode in ("RGBA", "P") else img.copy()

                for q in range(quality, 20, -10):
                    prev_tmp = tmp_path
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
                        tmp_path = Path(f.name)
                    save_img.save(tmp_path, "JPEG", quality=q)
                    if prev_tmp is not None:
                        prev_tmp.unlink(missing_ok=True)
                    if tmp_path.stat().st_size <= max_size:
                        logger.info("qq_bot.image_compressed", original=str(image_path),
                                    original_size=image_path.stat().st_size,
                                    compressed_size=tmp_path.stat().st_size, quality=q)
                        return tmp_path

                scale = 0.75
                while scale >= 0.25:
                    new_w = int(save_img.width * scale)
                    new_h = int(save_img.height * scale)
                    resized = save_img.resize((new_w, new_h), Image.LANCZOS)
                    prev_tmp = tmp_path
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
                        tmp_path = Path(f.name)
                    resized.save(tmp_path, "JPEG", quality=60)
                    if prev_tmp is not None:
                        prev_tmp.unlink(missing_ok=True)
                    if tmp_path.stat().st_size <= max_size:
                        logger.info("qq_bot.image_resized", original=f"{save_img.width}x{save_img.height}",
                                    resized=f"{new_w}x{new_h}", size=tmp_path.stat().st_size)
                        return tmp_path
                    scale -= 0.1
        except (ImportError, OSError, RuntimeError, ValueError):
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            raise

        except Exception:
            logger.exception(".qq_bot_media.unexpected")
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            raise

        return tmp_path

    def _gather_media_send_tasks(self, message: Any, result: Any) -> list:
        """构建媒体发送任务列表（TTS 语音/视频/图片），用于并行发送。"""
        from qq_bot_adapter import _next_msg_seq, QQ_GROUP_MEDIA_BUDGET

        send_tasks = []

        if result.audio_path and result.audio_path.exists():
            async def _send_cached_audio() -> None:
                try:
                    await self._send_audio(message, result.audio_path)
                except (OSError, RuntimeError, ConnectionError) as e:
                    logger.warning("qq_bot.audio_send_failed", error=str(e))
            send_tasks.append(_send_cached_audio())

        elif getattr(result, "tts_pending", False) and result.tts_text:
            async def _send_async_tts() -> None:
                try:
                    audio_path = await self.agent.tts.synthesize_xiaoda(
                        result.tts_text, emotion=result.emotion or ""
                    )
                    if audio_path and audio_path.exists():
                        await self._send_audio(message, audio_path)
                    else:
                        logger.warning("qq_bot.async_tts_no_audio")
                except (OSError, RuntimeError, ValueError) as e:
                    logger.warning("qq_bot.async_tts_failed", error=str(e))
            send_tasks.append(_send_async_tts())

        if result.video_path and result.video_path.exists():
            async def _send_vid() -> None:
                try:
                    await self._send_video(message, result.video_path)
                except (OSError, RuntimeError, ConnectionError) as e:
                    logger.warning("qq_bot.video_send_failed", error=str(e))
            send_tasks.append(_send_vid())

        if result.image_paths:
            async def _send_images() -> None:
                image_paths = result.image_paths[:QQ_GROUP_MEDIA_BUDGET] if isinstance(message, GroupMessage) else result.image_paths
                for img_path in image_paths:
                    try:
                        await self._send_reply_with_media(message, "", image_path=img_path)
                    except (OSError, RuntimeError, ConnectionError) as e:
                        logger.error("qq_bot.image_send_error", error=str(e), path=str(img_path))
                        try:
                            await message.reply(content="图片生成成功，但发送失败", msg_seq=_next_msg_seq())
                        except (OSError, RuntimeError, ConnectionError) as e2:
                            logger.error("qq_bot.image_fallback_reply_failed: {}", e2)
            send_tasks.append(_send_images())

        return send_tasks

    async def _send_video(self, message: Any, video_path: Path) -> None:
        from qq_bot_adapter import _next_msg_seq

        try:
            if isinstance(message, C2CMessage):
                file_info = await self._upload_c2c_base64(message.author.user_openid, video_path, file_type=2)
                await self.api.post_c2c_message(
                    openid=message.author.user_openid,
                    msg_type=7,
                    content="",
                    media={"file_info": file_info},
                    msg_seq=_next_msg_seq(),
                    msg_id=message.id
                )
            elif isinstance(message, GroupMessage):
                file_info = await self._upload_group_base64(message.group_openid, video_path, file_type=2)
                try:
                    await self.api.post_group_message(
                        group_openid=message.group_openid,
                        msg_type=7,
                        content="",
                        media={"file_info": file_info},
                        msg_seq=_next_msg_seq(),
                        msg_id=message.id
                    )
                except (OSError, RuntimeError, ConnectionError, ValueError) as e:
                    if "被动回复" in str(e) or "超过限制" in str(e):
                        logger.info("qq_bot.video_passive_limited_switching_to_proactive")
                        await self.api.post_group_message(
                            group_openid=message.group_openid,
                            msg_type=7,
                            content="",
                            media={"file_info": file_info},
                            msg_seq=_next_msg_seq(),
                        )
                    else:
                        raise
            logger.info("qq_bot.video_sent", video_path=str(video_path))
        except (OSError, RuntimeError, ConnectionError) as e:
            logger.error("qq_bot.video_send_error", error=str(e), video_path=str(video_path))
            try:
                await message.reply(content=f"视频生成成功，但发送失败: {e}", msg_seq=_next_msg_seq())
            except (OSError, RuntimeError, ConnectionError) as e:
                logger.error("qq_bot.video_fallback_reply_failed: {}", e, exc_info=True)

    async def _send_audio(self, message: Any, audio_path: Path) -> None:
        from qq_bot_adapter import _next_msg_seq

        silk_path = None
        try:
            silk_path = await self._convert_to_silk(audio_path)
            if silk_path is None:
                logger.warning("qq_bot.silk_convert_failed", path=str(audio_path))
                await message.reply(
                    content="语音消息发送失败：缺少 SILK 编码库，请联系管理员安装 pilk",
                    msg_seq=_next_msg_seq(),
                )
                return

            if isinstance(message, C2CMessage):
                openid = message.author.user_openid
                file_info = await self._upload_c2c_base64(openid, silk_path, file_type=3)
                await self.api.post_c2c_message(
                    openid=openid, msg_id=message.id,
                    msg_type=7, content="",
                    media={"file_info": file_info}, msg_seq=_next_msg_seq()
                )
            elif isinstance(message, GroupMessage):
                group_openid = message.group_openid
                file_info = await self._upload_group_base64(group_openid, silk_path, file_type=3)
                try:
                    await self.api.post_group_message(
                        group_openid=group_openid, msg_id=message.id,
                        msg_type=7, content="",
                        media={"file_info": file_info}, msg_seq=_next_msg_seq()
                    )
                except (OSError, RuntimeError, ConnectionError, ValueError) as e:
                    if "被动回复" in str(e) or "超过限制" in str(e):
                        logger.info("qq_bot.audio_passive_limited_switching_to_proactive")
                        await self.api.post_group_message(
                            group_openid=group_openid,
                            msg_type=7, content="",
                            media={"file_info": file_info}, msg_seq=_next_msg_seq()
                        )
                    else:
                        raise
        except (OSError, RuntimeError, ConnectionError) as e:
            logger.warning("qq_bot.audio_send_error", error=str(e))
        finally:
            if silk_path is not None:
                try:
                    p = Path(silk_path)
                    if p.exists():
                        p.unlink()
                        logger.info("qq_bot.temp_file_cleaned", path=str(p))
                except (OSError, RuntimeError) as e:
                    logger.warning("qq_bot.audio_temp_cleanup_failed: {}", e)

    async def _convert_to_silk(self, audio_path: Path) -> Path | None:
        pcm_path = None
        silk_path = None
        converted = False
        try:
            import pilk

            pcm_path = audio_path.with_suffix('.pcm')
            silk_path = audio_path.with_suffix('.silk')

            def _do_convert() -> bool:
                result = subprocess.run(
                    ['ffmpeg', '-y', '-i', str(audio_path), '-ar', '16000', '-ac', '1',
                     '-f', 's16le', str(pcm_path)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=30, check=False,
                )
                if result.returncode != 0:
                    logger.warning("qq_bot.ffmpeg_failed", stderr=result.stderr[:200])
                    return False
                pilk.encode(str(pcm_path), str(silk_path), pcm_rate=16000, tencent=True)
                return True

            ok = await asyncio.to_thread(_do_convert)

            if ok and silk_path.exists() and silk_path.stat().st_size > 0:
                converted = True
                logger.info("qq_bot.silk_convert_ok", input=str(audio_path), output=str(silk_path),
                            size_kb=silk_path.stat().st_size // 1024)
                return silk_path
            return None
        except ImportError:
            logger.warning("qq_bot.pilk_not_installed")
            return None
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            logger.warning("qq_bot.silk_convert_failed", error=str(e))
            return None
        finally:
            if pcm_path is not None:
                pcm_path.unlink(missing_ok=True)
            if silk_path is not None and not converted:
                silk_path.unlink(missing_ok=True)