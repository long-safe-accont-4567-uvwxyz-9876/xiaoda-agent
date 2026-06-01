import os
import sys
import ssl
import asyncio
import base64
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

_original_create_default_context = ssl.create_default_context

def _patched_create_default_context(*args, **kwargs):
    ctx = _original_create_default_context(*args, **kwargs)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

ssl.create_default_context = _patched_create_default_context

from logging_config import setup_logging
setup_logging()

from loguru import logger

import botpy
from botpy.gateway import BotWebSocket
from botpy.message import C2CMessage, GroupMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_core import AgentCore, ProcessResult
from config import AGENT_CONFIG
from nudge_engine import NudgeEngine

_original_is_system_event = BotWebSocket._is_system_event

async def _patched_is_system_event(self, message_event, ws):
    event_op = message_event.get("op")
    if event_op == BotWebSocket.WS_HEARTBEAT_ACK:
        self._last_heartbeat_ack = asyncio.get_event_loop().time()
    return await _original_is_system_event(self, message_event, ws)

BotWebSocket._is_system_event = _patched_is_system_event

_original_send_heart = BotWebSocket._send_heart

async def _patched_send_heart(self, interval):
    _log = __import__("botpy.logging", fromlist=["get_logger"]).get_logger()
    _log.info("[botpy] 心跳维持启动（带超时检测）...")
    self._last_heartbeat_ack = asyncio.get_event_loop().time()
    missed_acks = 0
    while True:
        if self._conn is None:
            _log.debug("[botpy] 连接已关闭!")
            return
        if self._conn.closed:
            _log.debug("[botpy] ws连接已关闭, 心跳检测停止")
            return

        now = asyncio.get_event_loop().time()
        if now - self._last_heartbeat_ack > interval * 2.5:
            missed_acks += 1
            _log.warning(f"[botpy] 心跳ACK超时 ({missed_acks}次), 上次ACK: {int(now - self._last_heartbeat_ack)}秒前")
            if missed_acks >= 2:
                _log.warning("[botpy] 心跳ACK连续超时，强制断开重连!")
                await self._conn.close()
                return
        else:
            missed_acks = 0

        payload = {
            "op": self.WS_HEARTBEAT,
            "d": self._session["last_seq"],
        }
        await self.send_msg(__import__("json").dumps(payload))
        await asyncio.sleep(interval)

BotWebSocket._send_heart = _patched_send_heart

from botpy.client import Client as _BotpyClient

_original_pool_init = _BotpyClient._pool_init


async def _patched_pool_init(self, token, session_interval):
    _botpy_log = __import__("botpy.logging", fromlist=["get_logger"]).get_logger()
    for i in range(self._ws_ap["shards"]):
        session = {
            "session_id": "",
            "last_seq": 0,
            "intent": self.intents,
            "token": token,
            "url": self._ws_ap["url"],
            "shards": {"shard_id": i, "shard_count": self._ws_ap["shards"]},
        }
        self._connection.add(session)

    loop = self._connection.loop

    def _loop_exception_handler(_loop, context):
        _loop.default_exception_handler(context)
        exception = context.get("exception")
        if isinstance(exception, ZeroDivisionError):
            _loop.stop()

    loop.set_exception_handler(_loop_exception_handler)

    recon_attempts = 0
    max_recon_delay = 60

    while not self._closed:
        _botpy_log.debug("[botpy] 会话循环检查...")
        try:
            coroutine = self._connection.multi_run(session_interval)
            if self.ret_coro:
                return coroutine
            elif coroutine:
                await coroutine
                recon_attempts = 0
            else:
                recon_attempts += 1
                delay = min(5 * (2 ** min(recon_attempts - 1, 4)), max_recon_delay)
                _botpy_log.warning(f"[botpy] session丢失，{delay}秒后重新登录 (第{recon_attempts}次)")

                if recon_attempts > 10:
                    _botpy_log.error("[botpy] 重连次数过多，放弃重连")
                    await self.close()
                    return

                await asyncio.sleep(delay)

                try:
                    await self._bot_login(token)
                    for i in range(self._ws_ap["shards"]):
                        session = {
                            "session_id": "",
                            "last_seq": 0,
                            "intent": self.intents,
                            "token": token,
                            "url": self._ws_ap["url"],
                            "shards": {"shard_id": i, "shard_count": self._ws_ap["shards"]},
                        }
                        self._connection.add(session)
                    _botpy_log.info("[botpy] 重新登录成功，恢复会话")
                except Exception as login_err:
                    _botpy_log.error(f"[botpy] 重新登录失败: {login_err}")
        except KeyboardInterrupt:
            _botpy_log.info("[botpy] 服务强行停止!")
            return
        except Exception as e:
            recon_attempts += 1
            delay = min(5 * (2 ** min(recon_attempts - 1, 4)), max_recon_delay)
            _botpy_log.error(f"[botpy] 会话异常: {e}, {delay}秒后重试 (第{recon_attempts}次)")
            await asyncio.sleep(delay)
            try:
                await self._bot_login(token)
                for i in range(self._ws_ap["shards"]):
                    session = {
                        "session_id": "",
                        "last_seq": 0,
                        "intent": self.intents,
                        "token": token,
                        "url": self._ws_ap["url"],
                        "shards": {"shard_id": i, "shard_count": self._ws_ap["shards"]},
                    }
                    self._connection.add(session)
            except Exception as login_err:
                _botpy_log.error(f"[botpy] 异常后重新登录失败: {login_err}")


_BotpyClient._pool_init = _patched_pool_init

APP_ID = os.getenv("QQBOT_APP_ID")
APP_SECRET = os.getenv("QQBOT_APP_SECRET")

_qq_cfg = AGENT_CONFIG.get("qq_bot", {})
MAX_REPLY_LEN = _qq_cfg.get("max_reply_length", 8000)

_msg_seq_counter = int(time.time() * 1000) % (10 ** 8)

def _next_msg_seq() -> int:
    global _msg_seq_counter
    _msg_seq_counter += 1
    return _msg_seq_counter


class AIQQBot(botpy.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.agent = AgentCore()
        self.nudge_engine = None
        self._processed_msg_ids = set()
        self._MAX_MSG_IDS = 100
        self._agent_initialized = False

    def _is_duplicate_msg(self, msg_id: str) -> bool:
        if msg_id in self._processed_msg_ids:
            return True
        self._processed_msg_ids.add(msg_id)
        if len(self._processed_msg_ids) > self._MAX_MSG_IDS:
            excess = len(self._processed_msg_ids) - self._MAX_MSG_IDS
            for _ in range(excess):
                self._processed_msg_ids.pop()
        return False

    async def on_ready(self):
        logger.info("qq_bot.connected", app_id=APP_ID)

        if not self._agent_initialized:
            await self.agent.init()
            self._agent_initialized = True
            logger.info("qq_bot.agent_initialized")
        else:
            logger.info("qq_bot.reconnected_agent_reused")

        nudge_enabled = os.getenv("NUDGE_ENABLED", "false").lower() == "true"
        if nudge_enabled and self.nudge_engine is None:
            user_openid = os.getenv("NUDGE_USER_OPENID", "")
            if user_openid:
                try:
                    self.nudge_engine = NudgeEngine(
                        db=self.agent.db,
                        analytics=self.agent.db.analytics,
                        router=self.agent.router,
                        api=self.api,
                        user_openid=user_openid,
                        greeting_threshold=int(os.getenv("NUDGE_GREETING_THRESHOLD", "3600")),
                        dnd_start=int(os.getenv("NUDGE_DND_START", "23")),
                        dnd_end=int(os.getenv("NUDGE_DND_END", "8")),
                        portrait_manager=self.agent.portrait_manager,
                    )
                    await self.nudge_engine.start()
                except Exception as e:
                    logger.warning("nudge.init_failed", error=str(e))

        if self.nudge_engine:
            self.nudge_engine.poke()

    async def on_error(self, error):
        logger.error("qq_bot.ws_error", error=str(error)[:200])

    async def on_close(self, close_status_code, close_msg):
        logger.warning("qq_bot.ws_closed", code=close_status_code, msg=str(close_msg)[:100])

    async def on_c2c_message_create(self, message: C2CMessage):
        content = message.content.strip()

        attachment_info = ""
        if hasattr(message, 'attachments') and message.attachments:
            parts = []
            for att in message.attachments:
                ct = getattr(att, 'content_type', '') or ''
                fn = getattr(att, 'filename', '') or ''
                result = await self.agent.receive_file(att)
                if result["status"] == "ok":
                    if result.get("text_preview"):
                        parts.append(f"[文件: {fn}]\n内容预览:\n{result['text_preview'][:500]}")
                    else:
                        parts.append(f"[文件: {fn}，已保存到 {result['save_path']}]")
                else:
                    if ct.startswith("image/"):
                        parts.append(f"[图片: {fn or 'image'}]")
                    elif ct.startswith("video/"):
                        parts.append(f"[视频: {fn or 'video'}]")
                    else:
                        parts.append(f"[附件: {fn or 'unknown'}]")
            attachment_info = " ".join(str(p) for p in parts)

        if not content and not attachment_info:
            return

        user_input = f"{content} {attachment_info}".strip() if content else attachment_info

        user_openid = getattr(message.author, 'user_openid', '') if hasattr(message, 'author') else ''
        user_id = f"qq_{user_openid}" if user_openid else "qq_unknown"
        logger.info("qq_bot.c2c_message", user_id=user_id, openid=user_openid, content=user_input[:80])

        if self.nudge_engine:
            self.nudge_engine.poke()

        session_id = ""
        try:
            session = await self.agent.get_session(user_openid)
            if session:
                session_id = session["id"]
            else:
                session_id = await self.agent.create_session(user_openid)
        except Exception:
            pass

        msg_id = getattr(message, 'id', '') or getattr(message, 'message_id', '')
        if msg_id and self._is_duplicate_msg(msg_id):
            return

        try:
            await message.reply(content="纳西妲收到啦，正在想～🌿", msg_seq=_next_msg_seq())

            async def status_notify(msg: str):
                await message.reply(content=msg, msg_seq=_next_msg_seq())

            result = await self.agent.process(user_input, user_id=user_id, source="qq_c2c",
                                              user_openid=user_openid, session_id=session_id,
                                              status_callback=status_notify)
            if result.reply:
                await self._send_reply_with_sticker(message, result)
        except Exception as e:
            logger.error(f"qq_bot.c2c_error: {e}")
            try:
                await message.reply(content="嗯……出了点小问题，等会儿再聊好不好？", msg_seq=_next_msg_seq())
            except Exception:
                pass

    async def on_group_at_message_create(self, message: GroupMessage):
        content = message.content.strip()

        attachment_info = ""
        if hasattr(message, 'attachments') and message.attachments:
            parts = []
            for att in message.attachments:
                ct = getattr(att, 'content_type', '') or ''
                fn = getattr(att, 'filename', '') or ''
                result = await self.agent.receive_file(att)
                if result["status"] == "ok":
                    if result.get("text_preview"):
                        parts.append(f"[文件: {fn}]\n内容预览:\n{result['text_preview'][:500]}")
                    else:
                        parts.append(f"[文件: {fn}，已保存到 {result['save_path']}]")
                else:
                    if ct.startswith("image/"):
                        parts.append(f"[图片: {fn or 'image'}]")
                    elif ct.startswith("video/"):
                        parts.append(f"[视频: {fn or 'video'}]")
                    else:
                        parts.append(f"[附件: {fn or 'unknown'}]")
            attachment_info = " ".join(str(p) for p in parts)

        if not content and not attachment_info:
            return

        user_input = f"{content} {attachment_info}".strip() if content else attachment_info

        member_openid = getattr(message.author, 'member_openid', '') if hasattr(message, 'author') else ''
        user_id = f"qq_{member_openid}" if member_openid else "qq_unknown"
        logger.info("qq_bot.group_message", user_id=user_id, openid=member_openid, content=user_input[:80])

        if self.nudge_engine:
            self.nudge_engine.poke()

        msg_id = getattr(message, 'id', '') or getattr(message, 'message_id', '')
        if msg_id and self._is_duplicate_msg(msg_id):
            return

        try:
            await message.reply(content="纳西妲收到啦，正在想～🌿", msg_seq=_next_msg_seq())

            async def status_notify(msg: str):
                await message.reply(content=msg, msg_seq=_next_msg_seq())

            result = await self.agent.process(user_input, user_id=user_id, source="qq_group",
                                              user_openid=member_openid,
                                              status_callback=status_notify)
            if result.reply:
                await self._send_reply_with_sticker(message, result)
        except Exception as e:
            logger.error(f"qq_bot.group_error: {e}")
            try:
                await message.reply(content="嗯……出了点小问题，等会儿再聊好不好？", msg_seq=_next_msg_seq())
            except Exception:
                pass

    async def _send_reply_with_media(self, message, reply: str,
                                      image_path: Path | None = None,
                                      image_url: str | None = None):
        if not image_path and not image_url:
            await message.reply(content=reply, msg_seq=_next_msg_seq())
            return

        try:
            if isinstance(message, C2CMessage):
                openid = message.author.user_openid
                if image_path:
                    file_info = await self._upload_c2c_base64(openid, image_path)
                else:
                    media = await self.api.post_c2c_file(
                        openid=openid, file_type=1, url=image_url
                    )
                    file_info = media.file_info
                await self.api.post_c2c_message(
                    openid=openid, msg_id=message.id,
                    msg_type=7, content=reply,
                    media={"file_info": file_info}, msg_seq=_next_msg_seq()
                )
            elif isinstance(message, GroupMessage):
                group_openid = message.group_openid
                if image_path:
                    file_info = await self._upload_group_base64(group_openid, image_path)
                else:
                    media = await self.api.post_group_file(
                        group_openid=group_openid, file_type=1, url=image_url
                    )
                    file_info = media.file_info
                await self.api.post_group_message(
                    group_openid=group_openid, msg_id=message.id,
                    msg_type=7, content=reply,
                    media={"file_info": file_info}, msg_seq=_next_msg_seq()
                )
            else:
                await message.reply(content=reply, msg_seq=_next_msg_seq())
        except Exception as e:
            logger.warning("qq_bot.media_send_failed", error=str(e))
            await message.reply(content=reply, msg_seq=_next_msg_seq())

    async def _upload_c2c_base64(self, openid: str, image_path: Path, file_type: int = 1) -> str:
        from botpy.http import Route

        def _read():
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode()

        file_data = await asyncio.to_thread(_read)
        payload = {
            "openid": openid,
            "file_type": file_type,
            "file_data": file_data,
            "srv_send_msg": False,
        }
        route = Route("POST", "/v2/users/{openid}/files", openid=openid)
        result = await self.api._http.request(route, json=payload)
        if isinstance(result, dict):
            return result.get("file_info", "")
        return result.file_info

    async def _upload_group_base64(self, group_openid: str, image_path: Path, file_type: int = 1) -> str:
        from botpy.http import Route

        def _read():
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode()

        file_data = await asyncio.to_thread(_read)
        payload = {
            "group_openid": group_openid,
            "file_type": file_type,
            "file_data": file_data,
            "srv_send_msg": False,
        }
        route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=group_openid)
        result = await self.api._http.request(route, json=payload)
        if isinstance(result, dict):
            return result.get("file_info", "")
        return result.file_info

    async def _send_reply_with_sticker(self, message, result: ProcessResult):
        from text_utils import smart_truncate, split_long_reply

        reply = result.reply
        clean_reply = self.agent.strip_emotion_tag(reply)

        parts = split_long_reply(clean_reply, MAX_REPLY_LEN)

        if len(parts) == 1:
            final_text = parts[0]
        else:
            for part in parts[:-1]:
                try:
                    await message.reply(content=part, msg_seq=_next_msg_seq())
                except Exception:
                    pass
            final_text = parts[-1]

        if result.sticker_path:
            try:
                await self._send_reply_with_media(message, final_text, image_path=result.sticker_path)
            except Exception as e:
                logger.warning("qq_bot.sticker_send_failed", error=str(e))
                await message.reply(content=final_text, msg_seq=_next_msg_seq())
        else:
            await message.reply(content=final_text, msg_seq=_next_msg_seq())

        if result.audio_path and result.audio_path.exists():
            try:
                await self._send_audio(message, result.audio_path)
            except Exception as e:
                logger.warning("qq_bot.audio_send_failed", error=str(e))

    async def _send_audio(self, message, audio_path: Path):
        if isinstance(message, C2CMessage):
            openid = message.author.user_openid
            file_info = await self._upload_c2c_base64(openid, audio_path, file_type=3)
            await self.api.post_c2c_message(
                openid=openid, msg_id=message.id,
                msg_type=7, content="",
                media={"file_info": file_info}, msg_seq=_next_msg_seq()
            )
        elif isinstance(message, GroupMessage):
            group_openid = message.group_openid
            file_info = await self._upload_group_base64(group_openid, audio_path, file_type=3)
            await self.api.post_group_message(
                group_openid=group_openid, msg_id=message.id,
                msg_type=7, content="",
                media={"file_info": file_info}, msg_seq=_next_msg_seq()
            )


if __name__ == "__main__":
    if not APP_ID or APP_ID == "your_app_id_here":
        print("=" * 55)
        print("  请先配置 QQ Bot AppID 和 AppSecret")
        print("")
        print("  步骤:")
        print("  1. 浏览器打开: https://q.qq.com")
        print("  2. 用手机 QQ 扫码登录")
        print("  3. 点击「创建机器人」")
        print("  4. 复制 AppID 和 AppSecret")
        print("  5. 填入 .env 文件")
        print("=" * 55)
        sys.exit(1)

    print("=" * 50)
    print("纳西妲的 QQ Bot 启动中...")
    print("  私聊: 全自动回复")
    print("  群聊: @机器人 触发")
    print("=" * 50)

    intents = botpy.Intents(public_messages=True)
    is_sandbox = _qq_cfg.get("is_sandbox", False)

    MAX_RETRIES = 100
    BASE_DELAY = 5
    MAX_DELAY = 120

    retry_count = 0

    while retry_count < MAX_RETRIES:
        try:
            client = AIQQBot(intents=intents, is_sandbox=is_sandbox)
            client.run(appid=APP_ID, secret=APP_SECRET)
            retry_count = 0
            logger.warning("qq_bot.exited_normally_restarting")
        except KeyboardInterrupt:
            logger.info("qq_bot.keyboard_interrupt")
            break
        except Exception as e:
            retry_count += 1
            delay = min(BASE_DELAY * (2 ** min(retry_count - 1, 6)), MAX_DELAY)
            logger.error(
                "qq_bot.crashed_retrying",
                error=str(e)[:200],
                retry=retry_count,
                delay=delay,
            )
            print(f"\n  ⚠ QQ Bot 异常退出: {str(e)[:100]}")
            print(f"  🔄 {delay:.0f} 秒后重连 (第 {retry_count} 次)...\n")
            time.sleep(delay)

    if retry_count >= MAX_RETRIES:
        logger.error("qq_bot.max_retries_exceeded")
        print("  ❌ QQ Bot 重连次数已达上限，退出")
        sys.exit(1)
