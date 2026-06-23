import os
import sys
import ssl
import asyncio
import base64
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

# 安全加固：不再全局 monkey patch ssl.create_default_context
# botpy 内部已使用 SSLContext() 处理 WebSocket SSL，无需全局禁用证书验证

from utils.logging_config import setup_logging
setup_logging()

from loguru import logger

import botpy
from botpy.gateway import BotWebSocket
from botpy.message import C2CMessage, GroupMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_core import AgentCore, ProcessResult
from config import AGENT_CONFIG
from emotion.nudge_engine import NudgeEngine
from utils.text_utils import encode_image_to_base64

_original_is_system_event = BotWebSocket._is_system_event

async def _patched_is_system_event(self, message_event, ws):
    event_op = message_event.get("op")
    if event_op == BotWebSocket.WS_HEARTBEAT_ACK:
        self._last_heartbeat_ack = asyncio.get_running_loop().time()
    return await _original_is_system_event(self, message_event, ws)

BotWebSocket._is_system_event = _patched_is_system_event

_original_send_heart = BotWebSocket._send_heart

async def _patched_send_heart(self, interval):
    _log = __import__("botpy.logging", fromlist=["get_logger"]).get_logger()
    _log.info("[botpy] 心跳维持启动（带超时检测）...")
    self._last_heartbeat_ack = asyncio.get_running_loop().time()
    missed_acks = 0
    while True:
        if self._conn is None:
            _log.debug("[botpy] 连接已关闭!")
            return
        if self._conn.closed:
            _log.debug("[botpy] ws连接已关闭, 心跳检测停止")
            return

        # 先发送心跳
        payload = {
            "op": self.WS_HEARTBEAT,
            "d": self._session["last_seq"],
        }
        await self.send_msg(__import__("json").dumps(payload))
        await asyncio.sleep(interval)

        # 再检查 ACK 是否超时
        now = asyncio.get_running_loop().time()
        if now - self._last_heartbeat_ack > interval * 4:
            missed_acks += 1
            _log.warning(f"[botpy] 心跳ACK超时 ({missed_acks}次), 上次ACK: {int(now - self._last_heartbeat_ack)}秒前")
            if missed_acks >= 3:
                _log.warning("[botpy] 心跳ACK连续超时，强制断开重连!")
                await self._conn.close()
                return
        else:
            missed_acks = 0

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
_msg_seq_lock = threading.Lock()

def _next_msg_seq() -> int:
    global _msg_seq_counter
    with _msg_seq_lock:
        _msg_seq_counter += 1
        return _msg_seq_counter


def _save_master_openid(openid: str) -> None:
    """将 openid 追加到 MASTER_QQ_OPENID（逗号分隔），并更新运行时环境变量。"""
    existing = os.getenv("MASTER_QQ_OPENID", "").strip()
    ids = [x.strip() for x in existing.split(",") if x.strip()]
    if openid in ids:
        return
    ids.append(openid)
    value = ",".join(ids)

    from pathlib import Path
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        env_path.write_text(f"MASTER_QQ_OPENID={value}\n", encoding="utf-8")
    else:
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith("MASTER_QQ_OPENID="):
                lines[i] = f"MASTER_QQ_OPENID={value}\n"
                found = True
                break
        if not found:
            lines.append(f"\nMASTER_QQ_OPENID={value}\n")
        env_path.write_text("".join(lines), encoding="utf-8")
    os.environ["MASTER_QQ_OPENID"] = value
    logger.info("qq_bot.master_openid_saved", openid=openid, total=len(ids))


# 当前活跃的 bot 实例（同进程内 GreetingScheduler 等主动消息入口使用）
_ACTIVE_BOT: "AIQQBot | None" = None


def _save_master_openid(openid: str) -> bool:
    """将 MASTER_QQ_OPENID 写入 .env 文件，永久生效。"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith("MASTER_QQ_OPENID="):
                lines[i] = f"MASTER_QQ_OPENID={openid}\n"
                found = True
                break
        if not found:
            if lines and not lines[-1].endswith("\n"):
                lines.append("\n")
            lines.append(f"MASTER_QQ_OPENID={openid}\n")

        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        os.environ["MASTER_QQ_OPENID"] = openid
        logger.info("qq_bot.master_openid_saved", openid=openid)
        return True
    except Exception as e:
        logger.error("qq_bot.master_openid_save_failed", error=str(e))
        return False


async def send_proactive_message(text: str, openid: str = "") -> bool:
    """向最近私聊用户（或指定 openid）主动发一条 QQ 消息。

    供 web/greeting_scheduler 等同进程模块调用；QQ client 未连接时返回 False。
    """
    bot = _ACTIVE_BOT
    if bot is None or bot.is_closed():
        raise RuntimeError("QQ client 未连接")
    target = openid or bot._last_c2c_openid
    if not target:
        raise RuntimeError("没有可用的 QQ 用户 openid（等用户先发一条私聊，或设置 NUDGE_USER_OPENID）")
    await bot.api.post_c2c_message(
        openid=target, content=text, msg_type=0, msg_seq=_next_msg_seq())
    logger.info("qq_bot.proactive_sent openid={} text={}", target[:8], text[:40])
    return True


async def run_qq_bot(agent: "AgentCore", *, sandbox: bool = False) -> None:
    """在现有事件循环中运行 QQ client（与 WebUI 同进程模式）。

    内部带指数退避重连；任务被取消时干净退出。
    """
    if not APP_ID or APP_ID == "your_app_id_here":
        logger.warning("qq_bot.disabled_no_appid")
        return
    intents = botpy.Intents(public_messages=True)
    delay = 5
    while True:
        client = AIQQBot(intents=intents, is_sandbox=sandbox, timeout=30, agent=agent)
        try:
            await client.start(appid=APP_ID, secret=APP_SECRET)
            logger.warning("qq_bot.exited_reconnecting")
            delay = 5
        except asyncio.CancelledError:
            try:
                await client.close()
            except Exception as e:
                logger.warning(f"qq_bot.close_on_cancel_failed: {e}")
            raise
        except Exception as e:
            logger.error("qq_bot.crashed_retrying error={} delay={}", str(e)[:200], delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


class AIQQBot(botpy.Client):
    def __init__(self, *args, agent: "AgentCore | None" = None, **kwargs):
        super().__init__(*args, **kwargs)
        # 支持注入共享的 AgentCore（与 WebUI 同进程同实例），未注入则自建（独立运行模式）
        self.agent = agent or AgentCore()
        self._agent_shared = agent is not None
        self.nudge_engine = None
        # 消息去重缓存：msg_id → 时间戳，保留最近 1 小时
        self._processed_msg_ids: dict[str, float] = {}
        self._MSG_ID_TTL = 3600  # 1 小时
        self._agent_initialized = agent is not None and getattr(agent, "_initialized", False)
        # 最近一个私聊用户 openid，主动消息（问候同步）发给该用户
        self._last_c2c_openid: str = os.getenv("NUDGE_USER_OPENID", "")
        global _ACTIVE_BOT
        _ACTIVE_BOT = self

    def _is_duplicate_msg(self, msg_id: str) -> bool:
        now = time.time()
        # 清理过期项
        expired = [k for k, ts in self._processed_msg_ids.items() if now - ts > self._MSG_ID_TTL]
        for k in expired:
            del self._processed_msg_ids[k]
        # 检查重复
        if msg_id in self._processed_msg_ids:
            return True
        self._processed_msg_ids[msg_id] = now
        return False

    @staticmethod
    def _get_config_service():
        try:
            from web.config_service import get_config_service
            return get_config_service()
        except Exception:
            return None

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
                        config_service=self._get_config_service(),
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
        # 注意：不在 on_close 中调用 agent.shutdown()
        # 因为 on_close 在临时断开时也会触发，而外层重连循环会复用同一实例
        # shutdown 会释放数据库等资源，导致重连后 Agent 不可用
        # shutdown 应在程序真正退出时调用

    async def _process_message_attachments(self, message) -> tuple[list[dict], str]:
        """处理消息中的附件，返回图片数据和附件描述文本。

        遍历消息附件，接收文件、编码图片为 base64，生成附件描述文本。
        C2C 消息和群消息的附件处理逻辑完全一致，提取此方法消除重复。

        Args:
            message: QQ Bot 消息对象（C2CMessage 或 GroupMessage）

        Returns:
            tuple[list[dict], str]: (image_data, attachment_info)
                image_data: 图片的 mimeType+base64 列表，供视觉识别使用
                attachment_info: 附件描述文本，拼接到用户输入中
        """
        image_data = []
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
                        if ct.startswith("image/"):
                            save_path = result.get('save_path', '')
                            parts.append(f"[图片: {fn}，已保存到 {save_path}]")
                            try:
                                mime, img_b64 = encode_image_to_base64(save_path)
                                image_data.append({"mimeType": mime, "data": img_b64})
                            except (FileNotFoundError, ValueError):
                                pass
                            except Exception as e:
                                logger.warning("qq_bot.image_encode_failed", error=str(e))
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
        return image_data, attachment_info

    async def on_group_add_robot(self, event):
        """机器人被拉入群时，自动将拉入者绑定为主人。"""
        op_openid = getattr(event, "op_member_openid", "")
        group_openid = getattr(event, "group_openid", "")
        if not op_openid:
            logger.warning("qq_bot.group_add_robot.no_openid", group=group_openid)
            return
        logger.info("qq_bot.group_add_robot", group=group_openid, op_openid=op_openid)
        _save_master_openid(op_openid)

    async def on_c2c_message_create(self, message: C2CMessage):
        content = (getattr(message, 'content', None) or "").strip()

        image_data, attachment_info = await self._process_message_attachments(message)

        if not content and not attachment_info:
            return

        user_input = f"{content} {attachment_info}".strip() if content else attachment_info

        user_openid = getattr(message.author, 'user_openid', '') if hasattr(message, 'author') else ''
        user_id = f"qq_{user_openid}" if user_openid else "qq_unknown"
        if user_openid:
            self._last_c2c_openid = user_openid
        logger.info("qq_bot.c2c_message", user_id=user_id, openid=user_openid, content=user_input[:80])

        # 主人识别：对比 openid 与 MASTER_QQ_OPENID（逗号分隔多值）
        master_raw = os.getenv("MASTER_QQ_OPENID", "").strip()
        master_ids = [x.strip() for x in master_raw.split(",") if x.strip()]
        is_master = bool(master_ids) and user_openid in master_ids

        # 私聊自动绑定：首次私聊自动将发送者绑定为主人
        if not is_master and user_openid and not master_ids:
            _save_master_openid(user_openid)
            is_master = True
            logger.info("qq_bot.c2c_auto_bind", openid=user_openid)

        if not is_master:
            logger.info("qq_bot.non_master_message", user_id=user_id, openid=user_openid, content=user_input[:80])

        if self.nudge_engine:
            self.nudge_engine.poke()

        session_id = ""
        try:
            session = await self.agent.get_session(user_openid)
            if session:
                session_id = session["id"]
            else:
                session_id = await self.agent.create_session(user_openid)
        except Exception as e:
            logger.error(f"qq_bot.c2c_session_failed: {e}")

        msg_id = getattr(message, 'id', '') or getattr(message, 'message_id', '')
        if msg_id and self._is_duplicate_msg(msg_id):
            return

        # /whoami 指令：回复发送者的 openid（用于主人在 Setup 中填写）
        if content.strip() in ("/whoami", "/whoami "):
            await message.reply(content=f"你的 OpenID 是：\n{user_openid}\n\n在 Setup 配置页面的「主人 QQ OpenID」填入此值即可绑定主人身份。", msg_seq=_next_msg_seq())
            return

        try:
            await message.reply(content="纳西妲收到啦，正在想～🌿", msg_seq=_next_msg_seq())

            async def status_notify(msg: str):
                await message.reply(content=msg, msg_seq=_next_msg_seq())

            result = await self.agent.process(user_input, user_id=user_id, source="qq_c2c",
                                              user_openid=user_openid, session_id=session_id,
                                              status_callback=status_notify,
                                              image_data=image_data if image_data else None,
                                              is_master=is_master)
            if result.reply:
                await self._send_reply_with_sticker(message, result)
        except Exception as e:
            logger.error(f"qq_bot.c2c_error: {e}")
            try:
                await message.reply(content="嗯……出了点小问题，等会儿再聊好不好？", msg_seq=_next_msg_seq())
            except Exception as e:
                logger.error(f"qq_bot.c2c_fallback_reply_failed: {e}")

    async def on_group_at_message_create(self, message: GroupMessage):
        try:
            content = (getattr(message, 'content', None) or "").strip()

            image_data, attachment_info = await self._process_message_attachments(message)

            if not content and not attachment_info:
                return

            user_input = f"{content} {attachment_info}".strip() if content else attachment_info

            member_openid = getattr(message.author, 'member_openid', '') if hasattr(message, 'author') else ''
            user_id = f"qq_{member_openid}" if member_openid else "qq_unknown"
            logger.info("qq_bot.group_message", user_id=user_id, openid=member_openid, content=user_input[:80])

            # 主人识别：对比 member_openid 与 MASTER_QQ_OPENID（逗号分隔多值）
            # on_group_add_robot 已自动绑定拉群者的 member_openid
            master_raw = os.getenv("MASTER_QQ_OPENID", "").strip()
            master_ids = [x.strip() for x in master_raw.split(",") if x.strip()]
            is_master = bool(master_ids) and member_openid in master_ids
            # 群聊自动绑定：首次群聊@机器人时自动绑定为主人（仅当未配置任何主人时）
            if not is_master and member_openid and not master_ids:
                _save_master_openid(member_openid)
                is_master = True
                logger.info("qq_bot.group_auto_bind", openid=member_openid)
            if is_master:
                logger.info("qq_bot.master_identified", member_openid=member_openid)
            else:
                logger.info("qq_bot.non_master_message", user_id=user_id, openid=member_openid, content=user_input[:80])

            if self.nudge_engine:
                self.nudge_engine.poke()

            msg_id = getattr(message, 'id', '') or getattr(message, 'message_id', '')
            if msg_id and self._is_duplicate_msg(msg_id):
                return

            # /whoami 指令：回复发送者的 openid（用于主人在 Setup 中填写）
            if content.strip() in ("/whoami", "/whoami "):
                await message.reply(content=f"你的 OpenID 是：\n{member_openid}\n\n在 Setup 配置页面的「主人 QQ OpenID」填入此值即可绑定主人身份。", msg_seq=_next_msg_seq())
                return

            # 群聊被动回复有次数限制（5分钟内最多2次），不要浪费在"收到啦"上
            # 只在处理时间较长时通过 status_notify 通知

            async def status_notify(msg: str):
                # 群聊中使用主动消息发送状态通知，避免消耗被动回复配额
                try:
                    await self.api.post_group_message(
                        group_openid=message.group_openid,
                        msg_type=0, content=msg,
                        msg_seq=_next_msg_seq(),
                    )
                except Exception as _e:
                    logger.debug("qq_bot.status_notify_failed", error=str(_e))

            result = await self.agent.process(user_input, user_id=user_id, source="qq_group",
                                              user_openid=member_openid,
                                              status_callback=status_notify,
                                              image_data=image_data if image_data else None,
                                              is_master=is_master)
            if result.reply:
                await self._send_reply_with_sticker(message, result)
        except Exception as e:
            logger.error(f"qq_bot.group_error: {e}", exc_info=True)
            try:
                await message.reply(content="嗯……出了点小问题，等会儿再聊好不好？", msg_seq=_next_msg_seq())
            except Exception as e2:
                logger.error(f"qq_bot.group_fallback_reply_failed: {e2}")

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
                try:
                    # 先尝试被动回复（需要 msg_id）
                    await self.api.post_group_message(
                        group_openid=group_openid, msg_id=message.id,
                        msg_type=7, content=reply,
                        media={"file_info": file_info}, msg_seq=_next_msg_seq()
                    )
                except Exception as e:
                    if "被动回复" in str(e) or "超过限制" in str(e):
                        # 被动回复超限，改用主动消息（不需要 msg_id）
                        logger.info("qq_bot.passive_reply_limited_switching_to_proactive")
                        await self.api.post_group_message(
                            group_openid=group_openid,
                            msg_type=7, content=reply,
                            media={"file_info": file_info}, msg_seq=_next_msg_seq()
                        )
                    else:
                        raise
            else:
                await message.reply(content=reply, msg_seq=_next_msg_seq())
        except Exception as e:
            logger.warning("qq_bot.media_send_failed", error=str(e))
            # 最终兜底：尝试纯文本回复
            try:
                await message.reply(content=reply, msg_seq=_next_msg_seq())
            except Exception as _e:
                logger.debug("qq_bot.fallback_reply_failed", error=str(_e))

    async def _upload_c2c_base64(self, openid: str, image_path: Path, file_type: int = 1) -> str:
        from botpy.http import Route

        compressed_path: Path | None = None
        try:
            def _read():
                nonlocal compressed_path
                # 图片类型且文件过大时压缩
                path_to_upload = image_path
                if file_type == 1 and image_path.stat().st_size > 800_000:
                    compressed_path = self._compress_image(image_path)
                    path_to_upload = compressed_path
                with open(path_to_upload, "rb") as f:
                    return base64.b64encode(f.read()).decode()

            file_data = await asyncio.to_thread(_read)
            payload = {
                "openid": openid,
                "file_type": file_type,
                "file_data": file_data,
                "srv_send_msg": False,
            }
            route = Route("POST", "/v2/users/{openid}/files", openid=openid)
            # 重试最多3次，每次间隔递增
            last_err = None
            for attempt in range(3):
                try:
                    result = await self.api._http.request(route, json=payload)
                    if isinstance(result, dict):
                        file_info = result.get("file_info", "")
                    else:
                        file_info = result.file_info
                    if not file_info:
                        raise RuntimeError(f"C2C文件上传返回空file_info (openid={openid})")
                    return file_info
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        wait = (attempt + 1) * 3
                        logger.warning("qq_bot.upload_retry", attempt=attempt + 1, wait=wait, error=str(e))
                        await asyncio.sleep(wait)
            raise last_err
        finally:
            if compressed_path is not None:
                try:
                    compressed_path.unlink()
                    logger.info("qq_bot.temp_file_cleaned", path=str(compressed_path))
                except Exception as e:
                    logger.warning(f"qq_bot.temp_file_cleanup_failed: {e}")

    async def _upload_group_base64(self, group_openid: str, image_path: Path, file_type: int = 1) -> str:
        from botpy.http import Route

        compressed_path: Path | None = None
        try:
            def _read():
                nonlocal compressed_path
                # 图片类型且文件过大时压缩
                path_to_upload = image_path
                if file_type == 1 and image_path.stat().st_size > 800_000:
                    compressed_path = self._compress_image(image_path)
                    path_to_upload = compressed_path
                with open(path_to_upload, "rb") as f:
                    return base64.b64encode(f.read()).decode()

            file_data = await asyncio.to_thread(_read)
            payload = {
                "group_openid": group_openid,
                "file_type": file_type,
                "file_data": file_data,
                "srv_send_msg": False,
            }
            route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=group_openid)
            # 重试最多3次，每次间隔递增
            last_err = None
            for attempt in range(3):
                try:
                    result = await self.api._http.request(route, json=payload)
                    if isinstance(result, dict):
                        file_info = result.get("file_info", "")
                    else:
                        file_info = result.file_info
                    if not file_info:
                        raise RuntimeError(f"群文件上传返回空file_info (group_openid={group_openid})")
                    return file_info
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        wait = (attempt + 1) * 3
                        logger.warning("qq_bot.upload_retry", attempt=attempt + 1, wait=wait, error=str(e))
                        await asyncio.sleep(wait)
            raise last_err
        finally:
            if compressed_path is not None:
                try:
                    compressed_path.unlink()
                    logger.info("qq_bot.temp_file_cleaned", path=str(compressed_path))
                except Exception as e:
                    logger.warning(f"qq_bot.temp_file_cleanup_failed: {e}")

    @staticmethod
    def _compress_image(image_path: Path, max_size: int = 800_000, quality: int = 75) -> Path:
        """压缩图片到指定大小以下，返回压缩后的临时文件路径。

        所有中间临时文件会在方法内部清理，只保留最终成功的文件。
        调用者负责在不再需要时删除返回的临时文件。
        """
        from PIL import Image
        import tempfile

        tmp_path: Path | None = None

        with Image.open(image_path) as img:
            # 如果是 RGBA/P 模式，转为 RGB
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            # 逐步降低质量直到满足大小要求
            for q in range(quality, 20, -10):
                prev_tmp = tmp_path
                with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
                    tmp_path = Path(f.name)
                img.save(tmp_path, "JPEG", quality=q)
                # 清理上一次的临时文件
                if prev_tmp is not None:
                    try:
                        prev_tmp.unlink()
                    except Exception as e:
                        logger.warning(f"qq_bot.compress_temp_cleanup_failed: {e}")
                if tmp_path.stat().st_size <= max_size:
                    logger.info("qq_bot.image_compressed", original=str(image_path),
                                original_size=image_path.stat().st_size,
                                compressed_size=tmp_path.stat().st_size, quality=q)
                    return tmp_path

            # 如果质量降到 20 还是太大，缩小尺寸
            scale = 0.75
            while scale >= 0.25:
                new_w = int(img.width * scale)
                new_h = int(img.height * scale)
                resized = img.resize((new_w, new_h), Image.LANCZOS)
                prev_tmp = tmp_path
                with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
                    tmp_path = Path(f.name)
                resized.save(tmp_path, "JPEG", quality=60)
                # 清理上一次的临时文件
                if prev_tmp is not None:
                    try:
                        prev_tmp.unlink()
                    except Exception as e:
                        logger.warning(f"qq_bot.resize_temp_cleanup_failed: {e}")
                if tmp_path.stat().st_size <= max_size:
                    logger.info("qq_bot.image_resized", original=f"{img.width}x{img.height}",
                                resized=f"{new_w}x{new_h}", size=tmp_path.stat().st_size)
                    return tmp_path
                scale -= 0.1

        # 最终兜底：返回最小版本
        return tmp_path

    async def _send_reply_with_sticker(self, message, result: ProcessResult):
        from utils.text_utils import smart_truncate, split_long_reply

        reply = result.reply
        clean_reply = self.agent.strip_emotion_tag(reply)

        parts = split_long_reply(clean_reply, MAX_REPLY_LEN)

        if len(parts) == 1:
            final_text = parts[0]
        else:
            failed = False
            for part in parts[:-1]:
                try:
                    await message.reply(content=part, msg_seq=_next_msg_seq())
                except Exception as e:
                    logger.warning(f"qq_bot.long_reply_part_failed: {e}")
                    failed = True
                    break
            if failed:
                final_text = parts[-1] + "\n（内容过长部分发送失败）"
            else:
                final_text = parts[-1]

        # 1. 文字+表情包立刻发送（用户最快看到回复）
        if result.sticker_path:
            try:
                await self._send_reply_with_media(message, final_text, image_path=result.sticker_path)
            except Exception as e:
                logger.warning("qq_bot.sticker_send_failed", error=str(e))
                await message.reply(content=final_text, msg_seq=_next_msg_seq())
        else:
            await message.reply(content=final_text, msg_seq=_next_msg_seq())

        # 2. 语音和图片并行发送
        send_tasks = []

        # TTS 语音发送（同步模式：audio_path 已有缓存文件）
        if result.audio_path and result.audio_path.exists():
            async def _send_cached_audio():
                try:
                    await self._send_audio(message, result.audio_path)
                except Exception as e:
                    logger.warning("qq_bot.audio_send_failed", error=str(e))
            send_tasks.append(_send_cached_audio())

        # TTS 语音发送（异步模式：tts_pending=True 时现场合成）
        elif getattr(result, "tts_pending", False) and result.tts_text:
            async def _send_async_tts():
                try:
                    audio_path = await self.agent.tts.synthesize_nahida(
                        result.tts_text, emotion=result.emotion or ""
                    )
                    if audio_path and audio_path.exists():
                        await self._send_audio(message, audio_path)
                    else:
                        logger.warning("qq_bot.async_tts_no_audio")
                except Exception as e:
                    logger.warning("qq_bot.async_tts_failed", error=str(e))
            send_tasks.append(_send_async_tts())

        # 视频发送
        if result.video_path and result.video_path.exists():
            async def _send_vid():
                try:
                    await self._send_video(message, result.video_path)
                except Exception as e:
                    logger.warning("qq_bot.video_send_failed", error=str(e))
            send_tasks.append(_send_vid())

        # 图片发送
        if result.image_paths:
            async def _send_images():
                for img_path in result.image_paths:
                    try:
                        await self._send_reply_with_media(message, "", image_path=img_path)
                    except Exception as e:
                        logger.error("qq_bot.image_send_error", error=str(e), path=str(img_path))
                        try:
                            await message.reply(content="图片生成成功，但发送失败", msg_seq=_next_msg_seq())
                        except Exception as e2:
                            logger.error(f"qq_bot.image_fallback_reply_failed: {e2}")
            send_tasks.append(_send_images())

        # 并行等待所有媒体发送完成
        if send_tasks:
            await asyncio.gather(*send_tasks, return_exceptions=True)

    async def _send_video(self, message, video_path: Path):
        """发送视频消息"""
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
                except Exception as e:
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
        except Exception as e:
            logger.error("qq_bot.video_send_error", error=str(e), video_path=str(video_path))
            # 降级为文本消息
            try:
                await message.reply(content=f"视频生成成功，但发送失败: {e}", msg_seq=_next_msg_seq())
            except Exception as e:
                logger.error(f"qq_bot.video_fallback_reply_failed: {e}")

    async def _send_audio(self, message, audio_path: Path):
        silk_path = None
        try:
            silk_path = await self._convert_to_silk(audio_path)
            if silk_path is None:
                logger.warning("qq_bot.silk_convert_failed", path=str(audio_path))
                await message.reply(content="语音消息发送失败：缺少 SILK 编码库，请联系管理员安装 pilk", msg_seq=_next_msg_seq())
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
                except Exception as e:
                    if "被动回复" in str(e) or "超过限制" in str(e):
                        logger.info("qq_bot.audio_passive_limited_switching_to_proactive")
                        await self.api.post_group_message(
                            group_openid=group_openid,
                            msg_type=7, content="",
                            media={"file_info": file_info}, msg_seq=_next_msg_seq()
                        )
                    else:
                        raise
        except Exception as e:
            logger.warning("qq_bot.audio_send_error", error=str(e))
        finally:
            # 只清理中间文件（silk），不删除输入文件（audio_path）
            if silk_path is not None:
                try:
                    p = Path(silk_path)
                    if p.exists():
                        p.unlink()
                        logger.info("qq_bot.temp_file_cleaned", path=str(p))
                except Exception as e:
                    logger.warning(f"qq_bot.audio_temp_cleanup_failed: {e}")

    async def _convert_to_silk(self, audio_path: Path) -> Path | None:
        pcm_path = None
        try:
            import pilk
            import subprocess

            pcm_path = audio_path.with_suffix('.pcm')
            silk_path = audio_path.with_suffix('.silk')

            def _do_convert():
                result = subprocess.run(
                    ['ffmpeg', '-y', '-i', str(audio_path), '-ar', '16000', '-ac', '1', '-f', 's16le', str(pcm_path)],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode != 0:
                    logger.warning("qq_bot.ffmpeg_failed", stderr=result.stderr[:200])
                    return False
                pilk.encode(str(pcm_path), str(silk_path), pcm_rate=16000, tencent=True)
                return True

            ok = await asyncio.to_thread(_do_convert)

            # 无论成功失败，都清理 pcm 中间文件
            try:
                pcm_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"qq_bot.pcm_cleanup_failed: {e}")

            if ok and silk_path.exists() and silk_path.stat().st_size > 0:
                logger.info("qq_bot.silk_convert_ok", input=str(audio_path), output=str(silk_path),
                            size_kb=silk_path.stat().st_size // 1024)
                return silk_path
            # 转换失败时清理可能残留的 silk 文件
            try:
                silk_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"qq_bot.silk_cleanup_failed: {e}")
            return None
        except ImportError:
            logger.warning("qq_bot.pilk_not_installed")
            return None
        except Exception as e:
            logger.warning("qq_bot.silk_convert_failed", error=str(e))
            return None


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
            client = AIQQBot(intents=intents, is_sandbox=is_sandbox, timeout=30)
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
