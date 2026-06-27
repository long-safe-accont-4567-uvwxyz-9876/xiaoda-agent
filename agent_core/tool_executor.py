"""工具执行 Mixin —— 拆分自原 agent_core.py 的 AgentCore 类。

包含带钩子的工具执行、工具调用处理、notebook 上下文加载、
媒体路径提取、回复清洗与表情包信息获取等工具执行相关方法。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from config import FILE_DIR
from emotion.emotion_enum import CN_TO_EN
from utils.text_utils import strip_dsml, strip_reasoning, humanize

from agent_core.core import _current_request_ctx

if TYPE_CHECKING:
    from agent_core.core import RequestContext
    from tool_engine.tool_registry import ToolResult


class ToolExecutorMixin:
    """工具执行相关方法的 Mixin，由 AgentCore 组合使用。"""

    async def _execute_tool_with_hooks(self, tool_name: str, arguments: dict,
                                        user_id: str = "", safe_mode: bool = False,
                                        user_input: str = "") -> 'ToolResult':
        """带钩子的工具执行"""
        from tool_engine.tool_registry import ToolResult

        # PreToolUse 钩子
        hook_result = await self._hook_engine.fire_pre_tool_use(
            tool_name=tool_name, arguments=arguments,
            user_input=user_input, safe_mode=safe_mode
        )
        if not hook_result.allowed:
            return ToolResult.fail(hook_result.reason or "工具执行被安全策略阻止")

        # 使用修改后的参数（如果有）
        actual_args = hook_result.modified_args or arguments

        # WebUI 工具过程可视化（无 WebUI 时为 no-op）
        import time as _time
        _tool_t0 = _time.time()
        try:
            from web.tool_events import emit_tool_event
            await emit_tool_event("start", tool_name, actual_args)
        except Exception as e:
            logger.debug(f"WebUI工具事件(start)发送失败，非关键: {e}")

        # 工具护栏检查
        from tool_engine.tool_guardrails import get_tool_guardrails
        guardrails = get_tool_guardrails()

        # L1/L2/L3 参数验证（在循环检测之前）
        valid, reason = guardrails.validate_args(tool_name, arguments)
        if not valid:
            logger.warning("tool.validation_failed", tool=tool_name, reason=reason)
            return ToolResult.fail(f"参数验证失败: {reason}")

        action, guard_msg = await guardrails.check(tool_name, arguments)
        if action == "halt":
            return ToolResult.fail(guard_msg)

        # 执行工具
        # Task 7: 流式状态推送 —— 工具执行前通知
        await self._notify_status(f"正在使用工具: {tool_name}")
        result = await self.tool_executor.execute(tool_name, actual_args, user_id, safe_mode)

        # 工具调用后更新认知状态（is_tool=True）
        if result.success:
            self._circuit_breaker.on_success(self._cognitive_state, is_tool=True)
        else:
            self._circuit_breaker.on_failure(self._cognitive_state, is_tool=True)

        try:
            from web.tool_events import emit_tool_event
            await emit_tool_event("end", tool_name, ok=result.success,
                                  elapsed_ms=int((_time.time() - _tool_t0) * 1000))
        except Exception as e:
            logger.debug(f"WebUI工具事件(end)发送失败，非关键: {e}")

        # 记录工具调用到护栏
        await guardrails.record_call(tool_name, arguments, result.success,
                               str(result.data)[:100] if result.data else "")

        # PostToolUse 钩子
        post_result = await self._hook_engine.fire_post_tool_use(
            tool_name=tool_name, arguments=actual_args,
            output=str(result.data) if result.data else result.error or "",
            user_input=user_input
        )

        # 如果钩子修改了输出
        if post_result.modified_output is not None:
            if result.success:
                return ToolResult.ok(post_result.modified_output)
            else:
                return ToolResult.fail(post_result.modified_output)

        # 护栏警告注入
        if action == "warn" and guard_msg:
            # 在输出中注入警告
            if result.success:
                result = ToolResult.ok(f"[护栏警告: {guard_msg}]\n{result.data}")

        return result

    async def _handle_tool_calls(self, tool_calls: list[dict], messages: list[dict],
                                  trace, *,
                                  assistant_content: str = "",
                                  reasoning_content: str | None = None,
                                  user_openid: str = "",
                                  session_id: str = "",
                                  safe_mode: bool = False,
                                  ctx: RequestContext | None = None,
                                  skip_summarize: bool = False) -> tuple[str, list]:
        _ctx = ctx or _current_request_ctx.get()
        self._tool_call_handler.set_status_callback(_ctx.status_callback if _ctx else None)
        return await self._tool_call_handler.handle(tool_calls, messages, trace, assistant_content=assistant_content, reasoning_content=reasoning_content, user_openid=user_openid, session_id=session_id, safe_mode=safe_mode, current_user_input=_ctx.user_input if _ctx else "", user_id=_ctx.user_id if _ctx else "", skip_summarize=skip_summarize)

    async def _load_notebook_context(self) -> None:
        try:
            focus = await self.notebook_manager.get_current_focus()
            if focus:
                self.context.notebook_focus = focus

            tasks = await self.notebook_manager.get_pending_tasks_summary()
            if tasks:
                self.context.pending_tasks = tasks
        except Exception as e:
            logger.warning("notebook.context_load_failed", error=str(e))

    async def _extract_media_from_tool_results(self, tool_results: list, reply: str) -> tuple[list[Path], Path | None, str]:
        """从工具结果中提取图片/视频路径，并清理回复文本中的冗余路径描述。"""
        image_paths: list[Path] = []
        video_path: Path | None = None
        extracted_paths: list[str] = []  # 用于清理回复文本

        for result in tool_results:
            if not result.success or not result.data:
                continue
            data_str = result.data if isinstance(result.data, str) else json.dumps(result.data, ensure_ascii=False)

            # 提取图片路径：匹配 "图片已保存到: /path" 或 "图片URL: https://..."
            for m in re.finditer(r'图片已保存到:\s*(\S+)', data_str):
                try:
                    p = Path(m.group(1))
                    if p.exists():
                        image_paths.append(p)
                        extracted_paths.append(m.group(0))
                        logger.info("media.extracted_image", path=str(p))
                except Exception as e:
                    logger.warning("media.image_path_parse_failed", raw=m.group(1), error=str(e))

            for m in re.finditer(r'图片URL:\s*(\S+)', data_str):
                try:
                    url = m.group(1).rstrip('`')
                    # 下载 URL 图片到本地，以便通过 QQ 富媒体消息发送
                    try:
                        import httpx
                        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as dl_client:
                            resp = await dl_client.get(url)
                            resp.raise_for_status()
                            img_dir = FILE_DIR if FILE_DIR.exists() else Path("tts_cache")
                            img_dir.mkdir(parents=True, exist_ok=True)
                            local_path = img_dir / f"agnes_dl_{int(time.time())}_{len(image_paths)}.png"
                            local_path.write_bytes(resp.content)
                            image_paths.append(local_path)
                            logger.info("media.downloaded_image_url", url=url, local=str(local_path))
                    except Exception as dl_err:
                        logger.warning("media.image_url_download_failed", url=url, error=str(dl_err))
                    extracted_paths.append(m.group(0))
                except Exception as e:
                    logger.warning("media.image_url_parse_failed", raw=m.group(1), error=str(e))

            # 提取视频路径：匹配 "视频生成完成！本地路径: /path" 或 "视频已保存到: /path"
            for m in re.finditer(r'(?:视频生成完成！本地路径|视频已保存到):\s*(\S+)', data_str):
                try:
                    p = Path(m.group(1))
                    if p.exists() and video_path is None:
                        video_path = p
                        extracted_paths.append(m.group(0))
                        logger.info("media.extracted_video", path=str(p))
                except Exception as e:
                    logger.warning("media.video_path_parse_failed", raw=m.group(1), error=str(e))

        # 收集已提取的图片 URL（用于清理回复文本中的裸 URL 引用）
        extracted_urls: list[str] = []
        for m in re.finditer(r'图片URL:\s*(\S+)', '\n'.join(
            r.data if isinstance(r.data, str) else json.dumps(r.data, ensure_ascii=False)
            for r in tool_results if r.success and r.data
        )):
            extracted_urls.append(m.group(1).rstrip('`'))

        # 清理回复文本中包含已提取路径或裸 URL 的行
        clean_reply = reply
        if extracted_paths or extracted_urls:
            lines = clean_reply.split('\n')
            filtered_lines = []
            for line in lines:
                should_remove = False
                for ep in extracted_paths:
                    if ep in line:
                        should_remove = True
                        break
                if not should_remove:
                    for url in extracted_urls:
                        if url in line:
                            should_remove = True
                            break
                if not should_remove:
                    filtered_lines.append(line)
            clean_reply = '\n'.join(filtered_lines).strip()
            # 清理可能残留的空行
            clean_reply = re.sub(r'\n{3,}', '\n\n', clean_reply)

        return image_paths, video_path, clean_reply

    def _clean_reply(self, text: str) -> str:
        text = text.strip()
        prefixes = ["昔涟：", "纳西妲：", "助手：", "AI："]
        for p in prefixes:
            if text.startswith(p):
                text = text[len(p):].strip()
        text = strip_dsml(text)
        text = strip_reasoning(text)
        text = humanize(text, style="nahida")
        return text

    def _finalize_reply(self, reply: str, strip_emotion: bool = True, style: str = "nahida") -> str:
        """统一的回复文本处理：strip_reasoning + strip_emotion_tag + humanize。

        所有回复路径（主 nahida、单子 Agent、并行子 Agent、TaskGraph）统一调用此方法，
        确保回复清洗流程一致。
        """
        text = reply.strip() if reply else ""
        text = strip_reasoning(text)
        if strip_emotion:
            text = self.klee_sticker_manager.strip_emotion_tag(text)
        text = humanize(text, style=style)
        return text

    def get_sticker_info(self, reply: str, user_emotion: str = "", force_sticker: bool = False) -> tuple[str, Path | None]:
        clean_reply = self.sticker_manager.strip_emotion_tag(reply)
        sticker_path = None
        if self.sticker_manager.available:
            if force_sticker:
                detected = self.sticker_manager.detect_emotion(clean_reply) or "happy"
                sticker_path = self.sticker_manager.pick(detected)
            else:
                detected = self.sticker_manager.detect_emotion(clean_reply)
                if not detected and user_emotion:
                    detected = CN_TO_EN.get(user_emotion, "")
                if self.sticker_manager.should_send(clean_reply, detected_emotion=detected):
                    sticker_path = self.sticker_manager.pick(detected)
        return clean_reply, sticker_path
