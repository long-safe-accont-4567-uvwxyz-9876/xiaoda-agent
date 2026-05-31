import os
import sys
import asyncio
import json
import base64
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from io import BytesIO

import botpy
from botpy import logging as qq_logging
from botpy.message import Message, GroupMessage, C2CMessage
from botpy.types.message import Media

from dotenv import load_dotenv

load_dotenv()

from logging_config import setup_logging
setup_logging()

from loguru import logger as loguru_logger
from config import load_config
from agent_core import AgentCore
from agent_dispatcher import SubAgentConfig
from task_orchestrator import build_task_graph, run_task_graph
from text_utils import split_long_reply, smart_truncate
from emoji_config import get_status_msg
from slash_commands import SlashCommandHandler
from knowledge_graph import KnowledgeGraph
from sticker_manager import StickerManager
from nudge_engine import NudgeEngine
from portrait_manager import PortraitManager
from file_receiver import FileReceiver
from db_memory import MemoryDB


class QQBotAdapter(botpy.Client):

    def __init__(self):
        intents = botpy.Intents.none()
        intents.public_messages = True
        intents.direct_message = True
        super().__init__(intents=intents)

        self._config = load_config()
        self._agent_core: Optional[AgentCore] = None
        self._slash_handler: Optional[SlashCommandHandler] = None
        self._task_graph = None
        self._knowledge_graph: Optional[KnowledgeGraph] = None
        self._sticker_manager: Optional[StickerManager] = None
        self._nudge_engine: Optional[NudgeEngine] = None
        self._portrait_manager: Optional[PortraitManager] = None
        self._file_receiver: Optional[FileReceiver] = None
        self._ready = False

    async def on_ready(self):
        loguru_logger.info("qq_bot.ready", bot_name=self.robot.name)
        await self._init_components()
        self._ready = True

    async def _init_components(self):
        self._agent_core = AgentCore(self._config)
        await self._agent_core.init()

        self._slash_handler = SlashCommandHandler(self._agent_core)

        from agent_dispatcher import AgentDispatcher, SubAgentConfig
        dispatcher = self._agent_core._dispatcher

        client = self._agent_core._model_router.get_client()
        model = self._agent_core._model_router.model
        self._task_graph = build_task_graph(
            dispatcher,
            dispatcher._agents,
            client,
            model,
            nahida_chat_callback=self._agent_core.chat,
        )

        self._knowledge_graph = KnowledgeGraph(self._config)
        await self._knowledge_graph.init()

        self._sticker_manager = StickerManager()
        self._portrait_manager = PortraitManager(self._config)
        self._file_receiver = FileReceiver()

        owner_qq = self._config.get("owner_qq")
        if owner_qq:
            self._nudge_engine = NudgeEngine(self, self._agent_core, self._config)
            asyncio.create_task(self._nudge_engine.start())

        loguru_logger.info("qq_bot.components_initialized")

    async def on_c2c_message_create(self, message: C2CMessage):
        if not self._ready:
            return

        user_openid = message.author.user_openid
        content = message.content.strip()

        if not content:
            return

        if content.startswith("/"):
            await self._handle_slash_command(message, content, user_openid, "c2c")
            return

        if message.attachments:
            await self._handle_media_message(message, content, user_openid, "c2c")
            return

        try:
            async def status_callback(msg: str):
                try:
                    await message._api.post_c2c_message(
                        openid=user_openid,
                        msg_type=0,
                        msg_id=message.id,
                        content=msg,
                    )
                except Exception as e:
                    loguru_logger.debug("c2c.status_send_failed", error=str(e))

            state = await run_task_graph(
                self._task_graph,
                content,
                user_openid,
                session_id=f"c2c_{user_openid}",
                status_callback=status_callback,
                agent_configs=getattr(self._task_graph, '_agent_configs', {}),
                dispatcher=getattr(self._task_graph, '_dispatcher', None),
            )
            reply = state.final_output or state.sub_agent_reply or "旅行者，人家没听懂呢……"

            await self._send_reply(message, reply, user_openid, "c2c")

            asyncio.create_task(self._try_send_sticker(message, content, reply, user_openid, "c2c"))

        except Exception as e:
            loguru_logger.error("c2c.process_error", error=str(e))
            try:
                await message._api.post_c2c_message(
                    openid=user_openid,
                    msg_type=0,
                    msg_id=message.id,
                    content="旅行者，人家出了点小问题……稍后再试试吧",
                )
            except Exception:
                pass

    async def on_group_at_message_create(self, message: GroupMessage):
        if not self._ready:
            return

        group_openid = message.group_openid
        member_openid = message.author.member_openid
        content = message.content.strip()

        if not content:
            return

        if content.startswith("/"):
            await self._handle_slash_command(message, content, member_openid, "group", group_openid=group_openid)
            return

        if message.attachments:
            await self._handle_media_message(message, content, member_openid, "group", group_openid=group_openid)
            return

        try:
            async def status_callback(msg: str):
                pass

            state = await run_task_graph(
                self._task_graph,
                content,
                member_openid,
                session_id=f"group_{group_openid}",
                status_callback=status_callback,
                agent_configs=getattr(self._task_graph, '_agent_configs', {}),
                dispatcher=getattr(self._task_graph, '_dispatcher', None),
            )
            reply = state.final_output or state.sub_agent_reply or "旅行者，人家没听懂呢……"

            segments = split_long_reply(reply)
            for seg in segments:
                seg = smart_truncate(seg)
                try:
                    await message._api.post_group_message(
                        group_openid=group_openid,
                        msg_type=0,
                        msg_id=message.id,
                        content=seg,
                    )
                except Exception as e:
                    loguru_logger.error("group.send_failed", error=str(e))

            asyncio.create_task(self._try_send_sticker(message, content, reply, member_openid, "group", group_openid=group_openid))

        except Exception as e:
            loguru_logger.error("group.process_error", error=str(e))

    async def _handle_slash_command(self, message, content: str, user_openid: str, msg_type: str, group_openid: str = ""):
        try:
            reply = await self._slash_handler.handle(content)

            if msg_type == "c2c":
                segments = split_long_reply(reply)
                for seg in segments:
                    seg = smart_truncate(seg)
                    await message._api.post_c2c_message(
                        openid=user_openid,
                        msg_type=0,
                        msg_id=message.id,
                        content=seg,
                    )
            else:
                segments = split_long_reply(reply)
                for seg in segments:
                    seg = smart_truncate(seg)
                    await message._api.post_group_message(
                        group_openid=group_openid,
                        msg_type=0,
                        msg_id=message.id,
                        content=seg,
                    )
        except Exception as e:
            loguru_logger.error("slash_command.error", error=str(e))

    async def _handle_media_message(self, message, text_content: str, user_openid: str, msg_type: str, group_openid: str = ""):
        try:
            attachment = message.attachments[0] if message.attachments else None
            if not attachment:
                return

            media_data = None
            if hasattr(attachment, 'url') and attachment.url:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(attachment.url) as resp:
                        if resp.status == 200:
                            media_data = await resp.read()

            if not media_data:
                await self._send_text_reply(message, "旅行者，人家没能下载到这个文件呢……", user_openid, msg_type, group_openid)
                return

            filename = getattr(attachment, 'filename', 'file')
            file_path = await self._file_receiver.save_temp(media_data, filename)

            context = ""
            if text_content:
                context = f"用户附带的文字说明: {text_content}"

            reply = await self._agent_core.chat(
                f"我收到了一个文件: {filename}，路径: {file_path}。{context} 请帮我处理这个文件。",
                user_id=user_openid,
            )

            await self._send_reply(message, reply, user_openid, msg_type, group_openid=group_openid)

        except Exception as e:
            loguru_logger.error("media.handle_error", error=str(e))
            await self._send_text_reply(message, "旅行者，处理文件时出了点问题……", user_openid, msg_type, group_openid)

    async def _send_reply(self, message, reply: str, user_openid: str, msg_type: str, group_openid: str = ""):
        emoji_path = None
        if self._sticker_manager and self._agent_core:
            emoji_path = self._sticker_manager.get_sticker(reply)

        if emoji_path and os.path.exists(emoji_path):
            try:
                with open(emoji_path, "rb") as f:
                    media_data = f.read()

                if msg_type == "c2c":
                    upload_result = await message._api.post_c2c_file(
                        openid=user_openid,
                        file_type=1,
                        file_data=media_data,
                        srv_send_msg=False,
                    )
                    if upload_result:
                        await message._api.post_c2c_message(
                            openid=user_openid,
                            msg_type=7,
                            msg_id=message.id,
                            media=upload_result,
                        )
                else:
                    upload_result = await message._api.post_group_file(
                        group_openid=group_openid,
                        file_type=1,
                        file_data=media_data,
                        srv_send_msg=False,
                    )
                    if upload_result:
                        await message._api.post_group_message(
                            group_openid=group_openid,
                            msg_type=7,
                            msg_id=message.id,
                            media=upload_result,
                        )
            except Exception as e:
                loguru_logger.debug("sticker.send_failed", error=str(e))

        segments = split_long_reply(reply)
        for seg in segments:
            seg = smart_truncate(seg)
            try:
                if msg_type == "c2c":
                    await message._api.post_c2c_message(
                        openid=user_openid,
                        msg_type=0,
                        msg_id=message.id,
                        content=seg,
                    )
                else:
                    await message._api.post_group_message(
                        group_openid=group_openid,
                        msg_type=0,
                        msg_id=message.id,
                        content=seg,
                    )
            except Exception as e:
                loguru_logger.error("reply.send_failed", error=str(e), msg_type=msg_type)

    async def _send_text_reply(self, message, text: str, user_openid: str, msg_type: str, group_openid: str = ""):
        try:
            if msg_type == "c2c":
                await message._api.post_c2c_message(
                    openid=user_openid,
                    msg_type=0,
                    msg_id=message.id,
                    content=text,
                )
            else:
                await message._api.post_group_message(
                    group_openid=group_openid,
                    msg_type=0,
                    msg_id=message.id,
                    content=text,
                )
        except Exception as e:
            loguru_logger.error("text_reply.send_failed", error=str(e))

    async def _try_send_sticker(self, message, user_input: str, reply: str, user_openid: str, msg_type: str, group_openid: str = ""):
        try:
            if not self._sticker_manager:
                return

            sticker_path = self._sticker_manager.get_sticker(reply)
            if not sticker_path or not os.path.exists(sticker_path):
                return

            with open(sticker_path, "rb") as f:
                media_data = f.read()

            if msg_type == "c2c":
                upload_result = await message._api.post_c2c_file(
                    openid=user_openid,
                    file_type=1,
                    file_data=media_data,
                    srv_send_msg=False,
                )
                if upload_result:
                    await message._api.post_c2c_message(
                        openid=user_openid,
                        msg_type=7,
                        msg_id=message.id,
                        media=upload_result,
                    )
            else:
                upload_result = await message._api.post_group_file(
                    group_openid=group_openid,
                    file_type=1,
                    file_data=media_data,
                    srv_send_msg=False,
                )
                if upload_result:
                    await message._api.post_group_message(
                        group_openid=group_openid,
                        msg_type=7,
                        msg_id=message.id,
                        media=upload_result,
                    )

        except Exception as e:
            loguru_logger.debug("sticker.async_send_failed", error=str(e))

    async def send_audio(self, user_openid: str, audio_path: str, msg_type: str = "c2c", group_openid: str = ""):
        try:
            if not os.path.exists(audio_path):
                return

            with open(audio_path, "rb") as f:
                audio_data = f.read()

            if msg_type == "c2c":
                upload_result = await self.api.post_c2c_file(
                    openid=user_openid,
                    file_type=3,
                    file_data=audio_data,
                    srv_send_msg=False,
                )
                if upload_result:
                    await self.api.post_c2c_message(
                        openid=user_openid,
                        msg_type=7,
                        media=upload_result,
                    )
            else:
                upload_result = await self.api.post_group_file(
                    group_openid=group_openid,
                    file_type=3,
                    file_data=audio_data,
                    srv_send_msg=False,
                )
                if upload_result:
                    await self.api.post_group_message(
                        group_openid=group_openid,
                        msg_type=7,
                        media=upload_result,
                    )

            loguru_logger.info("audio.sent", msg_type=msg_type)

        except Exception as e:
            loguru_logger.error("audio.send_failed", error=str(e))

    async def send_image(self, user_openid: str, image_path: str, msg_type: str = "c2c", group_openid: str = ""):
        try:
            if not os.path.exists(image_path):
                return

            with open(image_path, "rb") as f:
                image_data = f.read()

            if msg_type == "c2c":
                upload_result = await self.api.post_c2c_file(
                    openid=user_openid,
                    file_type=1,
                    file_data=image_data,
                    srv_send_msg=False,
                )
                if upload_result:
                    await self.api.post_c2c_message(
                        openid=user_openid,
                        msg_type=7,
                        media=upload_result,
                    )
            else:
                upload_result = await self.api.post_group_file(
                    group_openid=group_openid,
                    file_type=1,
                    file_data=image_data,
                    srv_send_msg=False,
                )
                if upload_result:
                    await self.api.post_group_message(
                        group_openid=group_openid,
                        msg_type=7,
                        media=upload_result,
                    )

            loguru_logger.info("image.sent", msg_type=msg_type)

        except Exception as e:
            loguru_logger.error("image.send_failed", error=str(e))

    async def send_to_owner(self, content: str):
        owner_qq = self._config.get("owner_qq")
        if not owner_qq:
            return
        try:
            await self.api.post_c2c_message(
                openid=owner_qq,
                msg_type=0,
                content=content,
            )
        except Exception as e:
            loguru_logger.error("owner.notify_failed", error=str(e))

    @property
    def agent_core(self) -> Optional[AgentCore]:
        return self._agent_core


def main():
    app_id = os.environ.get("QQ_APP_ID", "")
    app_secret = os.environ.get("QQ_APP_SECRET", "")

    if not app_id or not app_secret:
        print("请设置环境变量 QQ_APP_ID 和 QQ_APP_SECRET")
        print("export QQ_APP_ID=your_app_id")
        print("export QQ_APP_SECRET=your_app_secret")
        sys.exit(1)

    bot = QQBotAdapter()
    bot.run(appid=app_id, secret=app_secret)


if __name__ == "__main__":
    main()
