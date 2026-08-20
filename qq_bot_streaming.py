"""QQ Bot 流式回复 Mixin。

从 qq_bot_adapter.py 拆分而来，负责：
- 流式分片发送（模拟打字效果）
- 文本切片（群聊字节上限 / C2C 字符切片）
- 表情包合并发送
- 异常恢复（配额耗尽合并 / 超时跳段防重复）
- 短回复兜底发送
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any

from loguru import logger

from botpy.message import GroupMessage

from channel_adapter_base import (
    STREAM_C2C_MAX_SEGMENTS,
    STREAM_GROUP_MAX_SEGMENTS,
    _split_text_by_bytes,
    _split_text_for_streaming,
    _cap_stream_segments,
)


class QQStreamingMixin:
    """流式回复方法组。

    要求宿主类提供以下属性/方法：
    - self.api: botpy API 客户端
    - self.agent: AgentCore 实例
    - _send_reply_with_media(): 来自 QQMediaMixin
    """

    def _split_group_text(self, text: str) -> list[str]:
        from utils.text_utils import split_for_group_passive

        segments = split_for_group_passive(text)
        if "".join(segments).replace("\n```\n```\n", "\n") == text:
            return segments
        marker = "\n（内容已截断）"
        last = segments[-1].rstrip()
        while len((last + marker).encode("utf-8")) > 4000:
            last = last[:-1]
        segments[-1] = last + marker
        return segments

    @staticmethod
    def _remaining_segments_after_error(segments: list[str], index: int,
                                        exc: BaseException) -> str:
        """发送失败后计算需要合并的剩余文本。

        TimeoutError 时请求可能已发出但响应超时，QQ 服务端可能已接收当前段，
        重发会重复 → 跳过当前段；其他异常当前段可能没发出 → 重发含当前段。
        """
        if isinstance(exc, TimeoutError):
            return "".join(segments[index + 1:])
        return "".join(segments[index:])

    async def _send_stream_segment(self, message: Any, text: str, *,
                                   passive: bool, is_group: bool, log_key: str) -> bool:
        """发送单个流式分片。True=发送成功，False=群聊被动配额耗尽被静默拒绝。"""
        from qq_bot_adapter import _next_msg_seq

        group_no_proactive = ("被动回复", "超过限制", "无权限", "40034105")
        try:
            if is_group or passive or not getattr(self, "api", None):
                await message.reply(content=text, msg_seq=_next_msg_seq())
            else:
                response = await self.api.post_c2c_message(
                    openid=message.author.user_openid,
                    content=text,
                    msg_type=0,
                    msg_seq=_next_msg_seq(),
                )
                if response is None:
                    raise RuntimeError("C2C主动消息接口返回None")
            return True
        except (TimeoutError, OSError, RuntimeError, ValueError) as e:
            err_str = str(e)
            if is_group and any(k in err_str for k in group_no_proactive):
                logger.info("{}_passive_limited_no_proactive",
                            log_key, error=err_str, remaining_to_merge=True)
                return False
            raise

    async def _recover_remaining_segments(
        self, message: Any, segments: list[str], from_index: int,
        is_group: bool, sent_count: int, stream_start: float,
    ) -> int:
        """配额耗尽或异常时，合并剩余段重发，返回总发送数。"""
        remaining = "".join(segments[from_index:])
        recovery_pieces = _split_text_by_bytes(remaining, 7800)
        recovery_sent = 0
        for piece in recovery_pieces:
            try:
                ok = await self._send_stream_segment(
                    message, piece, passive=False, is_group=is_group, log_key="qq_bot.stream")
                if ok:
                    recovery_sent += 1
                else:
                    logger.error("qq_bot.stream_quota_merge_failed_too", remaining_len=len(piece))
                    break
            except (TimeoutError, OSError, RuntimeError) as e2:
                logger.error("qq_bot.stream_quota_merge_exception", error=str(e2), remaining_len=len(piece))
                break
        total_sent = sent_count + recovery_sent
        if recovery_sent > 0:
            recovery_ms = (time.monotonic() - stream_start) * 1000
            logger.info("qq_bot.stream_recovery_done", sent=total_sent, ms=round(recovery_ms, 1))
        return total_sent

    async def _send_streaming_reply(self, message: Any, full_text: str) -> None:
        """流式分片发送回复，模拟打字效果。"""
        from qq_bot_adapter import _next_msg_seq, QQ_GROUP_MAX_SEGMENTS
        from config import get_agent_display_name

        if not full_text:
            return

        stream_start = time.monotonic()
        total_len = len(full_text)
        is_group = isinstance(message, GroupMessage)

        if is_group:
            segments = self._split_group_text(full_text)
        else:
            segments = _split_text_for_streaming(full_text, chunk_size=300)

        segments = _cap_stream_segments(
            segments, is_group,
            "qq_bot.stream_capped_resplit", "qq_bot.stream_capped")

        if len(segments) <= 1:
            try:
                single = segments[0] if segments else full_text
                t0 = time.monotonic()
                ok = await self._send_stream_segment(message, single, passive=True, is_group=is_group, log_key="qq_bot.stream")
                elapsed = (time.monotonic() - t0) * 1000
                if ok:
                    logger.info("qq_bot.stream_single", total_len=total_len, ms=round(elapsed, 1))
                else:
                    logger.warning("qq_bot.stream_single_quota_exhausted", total_len=total_len, ms=round(elapsed, 1))
            except (TimeoutError, OSError, RuntimeError) as e:
                logger.error("qq_bot.stream_final_failed", error=str(e))
            return

        num_segments = len(segments)
        logger.info("qq_bot.stream_start", total_len=total_len,
                     segments=num_segments, is_group=is_group)

        if not is_group:
            try:
                await message.reply(
                    content=f"{get_agent_display_name('xiaoda')}正在打字...",
                    msg_seq=_next_msg_seq(),
                )
            except (OSError, RuntimeError) as e:
                logger.debug("qq_bot.typing_indicator_failed", error=str(e))

        sent_count = 0
        for i, seg in enumerate(segments):
            try:
                if i > 0:
                    await asyncio.sleep(random.uniform(0.8, 1.2))
                t0 = time.monotonic()
                ok = await self._send_stream_segment(message, seg, passive=i == 0, is_group=is_group, log_key="qq_bot.stream")
                seg_ms = (time.monotonic() - t0) * 1000
                if ok:
                    sent_count += 1
                    logger.debug("qq_bot.stream_segment", index=i, size=len(seg),
                                 ms=round(seg_ms, 1), sent=sent_count)
                else:
                    logger.warning("qq_bot.stream_segment_quota_exhausted",
                                   at_segment=i, sent_segments=sent_count,
                                   total_segments=num_segments)
                    await self._recover_remaining_segments(
                        message, segments, i, is_group, sent_count, stream_start)
                    return
            except (TimeoutError, OSError, RuntimeError) as e:
                logger.warning("qq_bot.stream_segment_failed",
                               error=str(e), sent_segments=sent_count)
                await self._recover_remaining_segments(
                    message, segments, i, is_group, sent_count, stream_start)
                return

        total_ms = (time.monotonic() - stream_start) * 1000
        logger.info("qq_bot.stream_done", total_len=total_len,
                     segments=num_segments, sent=sent_count,
                     ms=round(total_ms, 1))

    async def _send_reply_with_sticker(self, message: Any, result: Any) -> None:
        from qq_bot_adapter import _next_msg_seq

        reply = result.reply
        clean_reply = self.agent.strip_emotion_tag(reply)

        stream_enabled = os.getenv("QQ_STREAM_REPLY", "true").lower() in ("true", "1", "yes")
        if stream_enabled and len(clean_reply) > 400:
            await self._send_streaming_reply_with_sticker(message, clean_reply, result)
        else:
            await self._send_fallback_reply_with_sticker(message, clean_reply, result)

        send_tasks = self._gather_media_send_tasks(message, result)
        if send_tasks:
            await asyncio.gather(*send_tasks, return_exceptions=True)

    async def _send_streaming_reply_with_sticker(self, message: Any, clean_reply: str,
                                                   result: Any) -> None:
        """流式发送长回复，最后一片与表情包合并发送。"""
        from qq_bot_adapter import _next_msg_seq

        if not result.sticker_path:
            await self._send_streaming_reply(message, clean_reply)
            return

        is_group = isinstance(message, GroupMessage)
        if is_group:
            segments = self._split_group_text(clean_reply)
        else:
            segments = _split_text_for_streaming(clean_reply, chunk_size=300)

        segments = _cap_stream_segments(
            segments, is_group,
            "qq_bot.stream_sticker_capped_resplit", "qq_bot.stream_sticker_capped")

        if len(segments) <= 1:
            try:
                await self._send_reply_with_media(message, clean_reply, image_path=result.sticker_path)
            except (OSError, RuntimeError, ConnectionError) as e:
                logger.warning("qq_bot.sticker_send_failed", error=str(e))
                await message.reply(content=clean_reply, msg_seq=_next_msg_seq())
            return

        for i, seg in enumerate(segments[:-1]):
            try:
                if i > 0:
                    await asyncio.sleep(random.uniform(0.8, 1.2))
                t0 = time.monotonic()
                ok = await self._send_stream_segment(message, seg, passive=True, is_group=is_group, log_key="qq_bot.stream_sticker")
                seg_ms = (time.monotonic() - t0) * 1000
                if ok:
                    logger.debug("qq_bot.stream_sticker_segment", index=i, size=len(seg), ms=round(seg_ms, 1))
                else:
                    logger.warning("qq_bot.stream_sticker_segment_quota_exhausted",
                                   at_segment=i, total_segments=len(segments))
                    remaining = "".join(segments[i:])
                    try:
                        await self._send_reply_with_media(message, remaining, image_path=result.sticker_path)
                        logger.info("qq_bot.stream_sticker_quota_recovered_with_merge",
                                    merged_from=len(segments) - i, ms=round(seg_ms, 1))
                    except (OSError, RuntimeError, ConnectionError) as e2:
                        logger.error("qq_bot.stream_sticker_quota_merge_failed",
                                     error=str(e2), remaining_len=len(remaining))
                        try:
                            await message.reply(content=remaining, msg_seq=_next_msg_seq())
                        except (TimeoutError, OSError, RuntimeError) as e3:
                            logger.error("qq_bot.stream_sticker_fallback_failed", error=str(e3))
                    return
            except (TimeoutError, OSError, RuntimeError) as e:
                logger.warning("qq_bot.stream_sticker_segment_failed", error=str(e))
                remaining = self._remaining_segments_after_error(segments, i, e)
                if not remaining:
                    return
                try:
                    pieces = _split_text_by_bytes(remaining, 7800)
                    for piece in pieces[:-1]:
                        await self._send_stream_segment(message, piece, passive=True, is_group=is_group, log_key="qq_bot.stream_sticker")
                    await self._send_reply_with_media(message, pieces[-1], image_path=result.sticker_path)
                    logger.info("qq_bot.stream_sticker_recovery_done_with_merge")
                except (OSError, RuntimeError, ConnectionError) as e2:
                    logger.error("qq_bot.stream_sticker_recovery_failed", error=str(e2))
                    try:
                        for piece in _split_text_by_bytes(remaining, 7800):
                            await self._send_stream_segment(message, piece, passive=True, is_group=is_group, log_key="qq_bot.stream_sticker")
                    except (TimeoutError, OSError, RuntimeError) as e3:
                        logger.error("qq_bot.stream_sticker_recovery_final_failed", error=str(e3))
                return

        last_seg = segments[-1]
        try:
            await self._send_reply_with_media(message, last_seg, image_path=result.sticker_path)
        except (OSError, RuntimeError, ConnectionError) as e:
            logger.warning("qq_bot.sticker_with_last_segment_failed", error=str(e))
            try:
                ok = await self._send_stream_segment(message, last_seg, passive=True, is_group=is_group, log_key="qq_bot.stream_sticker")
                if not ok:
                    logger.error("qq_bot.sticker_last_segment_quota_exhausted_no_recovery")
            except (OSError, RuntimeError) as e2:
                logger.debug("qq_bot.fallback_segment_also_failed", error=str(e2))

    async def _send_fallback_reply_with_sticker(self, message: Any, clean_reply: str,
                                                  result: Any) -> None:
        """短回复或流式禁用时，单条发送回复+表情包。"""
        from qq_bot_adapter import _next_msg_seq, MAX_REPLY_LEN, QQ_GROUP_MAX_SEGMENTS
        from utils.text_utils import split_long_reply, split_for_group_passive

        is_group = isinstance(message, GroupMessage)

        if is_group:
            original_len = len(clean_reply.encode('utf-8'))
            segments = split_for_group_passive(clean_reply)
            group_quota = QQ_GROUP_MAX_SEGMENTS
            if len(segments) > group_quota:
                segments = segments[:group_quota]
                segments[-1] = segments[-1].rstrip() + "\n（…）"
                logger.info("qq_bot.group_reply_quota_truncated_with_marker",
                            original_bytes=original_len,
                            sent_segments=group_quota,
                            marker="（…）")
            sent_count = 0
            for i, seg in enumerate(segments[:-1]):
                try:
                    await message.reply(content=seg, msg_seq=_next_msg_seq())
                    sent_count += 1
                except (OSError, RuntimeError, ConnectionError) as e:
                    logger.warning("qq_bot.group_reply_part_failed",
                                   part_index=i, total_parts=len(segments), error=str(e))
                    remaining = self._remaining_segments_after_error(segments, i, e)
                    try:
                        await message.reply(content=remaining, msg_seq=_next_msg_seq())
                        sent_count += 1
                        logger.info("qq_bot.group_reply_merge_recovered", merged_from=len(segments) - i)
                        final_text = ""
                    except (OSError, RuntimeError, ConnectionError) as e2:
                        logger.error("qq_bot.group_reply_merge_failed",
                                     error=str(e2), remaining_len=len(remaining))
                        final_text = segments[-1] + "\n（内容过长部分发送失败）"
                    break
            else:
                final_text = segments[-1]
            truncated_len = sum(len(s.encode('utf-8')) for s in segments)
            if truncated_len < original_len and not (len(segments) == group_quota):
                logger.info("qq_bot.group_reply_truncated_no_marker",
                            original_bytes=original_len, truncated_bytes=truncated_len,
                            dropped_segments=0)
        else:
            parts = split_long_reply(clean_reply, MAX_REPLY_LEN)
            if len(parts) == 1:
                final_text = parts[0]
            else:
                final_text = parts[-1]
                merge_done = False
                for i, part in enumerate(parts[:-1]):
                    try:
                        await message.reply(content=part, msg_seq=_next_msg_seq())
                    except (OSError, RuntimeError, ConnectionError) as e:
                        logger.warning("qq_bot.long_reply_part_failed_merging",
                                       part_index=i, total_parts=len(parts), error=str(e))
                        remaining = self._remaining_segments_after_error(parts, i, e)
                        try:
                            for piece in _split_text_by_bytes(remaining, 7800):
                                await message.reply(content=piece, msg_seq=_next_msg_seq())
                            logger.info("qq_bot.long_reply_merge_recovered", merged_from=len(parts) - i)
                            merge_done = True
                            final_text = ""
                        except (OSError, RuntimeError, ConnectionError) as e2:
                            logger.error("qq_bot.long_reply_merge_failed",
                                         error=str(e2), remaining_len=len(remaining))
                            final_text = parts[-1] + "\n（内容过长部分发送失败）"
                        break
                if not merge_done and final_text == parts[-1]:
                    final_text = parts[-1]

        if result.sticker_path:
            try:
                await self._send_reply_with_media(message, final_text, image_path=result.sticker_path)
            except (OSError, RuntimeError, ConnectionError) as e:
                logger.warning("qq_bot.sticker_send_failed", error=str(e))
                try:
                    await message.reply(content=final_text, msg_seq=_next_msg_seq())
                except (OSError, RuntimeError, ConnectionError) as e2:
                    logger.error("qq_bot.sticker_fallback_reply_failed", error=str(e2))
        elif final_text:
            try:
                await message.reply(content=final_text, msg_seq=_next_msg_seq())
            except (OSError, RuntimeError, ConnectionError) as e:
                logger.error("qq_bot.final_text_reply_failed", error=str(e))