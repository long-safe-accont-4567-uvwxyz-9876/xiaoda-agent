"""邮件轮询器 —— 自动检测收件箱新邮件，注入 Agent 处理，再通过邮件回复。

仿 GreetingScheduler / MemoryRecallScheduler 的后台循环模式：
  start() / stop() / _loop() 三件套，挂在 web/server.py 的 _start_services。

工作流：
  1. 每 60s 轮询 agently-cli message +list --dir inbox --is-unread
  2. 按收件模式（off/allowlist/all）过滤发件人
  3. 读取邮件全文 → core.process() 生成回复
  4. 两阶段 agently-cli message +reply 回复发件人

配置存在 ConfigService 的 mail 段（config/webui_overrides.json），前端可热改。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, UTC
from typing import Any

from loguru import logger


class MailPoller:
    """邮件机器人后台轮询器。"""

    TICK_SECONDS = 60  # 轮询间隔
    INITIAL_DELAY = 30  # 启动后延迟首次轮询，避免与启动资源争抢

    def __init__(self, core: Any, config_service: Any) -> None:
        self.core = core
        self.cfg = config_service
        self._task: asyncio.Task | None = None
        self._processed_ids: set[str] = set()
        # 水位线：回溯1小时，避免重启前刚收到的邮件被丢弃
        # 配合 _processed_ids 内存去重避免重复处理
        from datetime import timedelta
        self._last_poll_time = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # 日配额
        self._daily_count = 0
        self._daily_reset_date = datetime.now().date()

    # ── 生命周期 ──────────────────────────────────────────────
    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._loop())
            logger.info("mail.poller.started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
            logger.info("mail.poller.stopped")

    async def _loop(self) -> None:
        await asyncio.sleep(self.INITIAL_DELAY)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("mail.poller.tick_error error={}", str(e)[:200])
            await asyncio.sleep(self.TICK_SECONDS)

    # ── 核心逻辑 ──────────────────────────────────────────────
    async def _tick(self) -> None:
        # 读取配置（每次 tick 都读，支持前端热改）
        enabled = self.cfg.get("mail.enabled", False)
        if not enabled:
            return
        mode = self.cfg.get("mail.mode", "off")
        if mode == "off":
            return

        # 免打扰
        if self._is_dnd():
            logger.debug("mail.poller.dnd_skip")
            return

        # 日配额重置
        today = datetime.now().date()
        if today != self._daily_reset_date:
            self._daily_count = 0
            self._daily_reset_date = today

        max_per_day = int(self.cfg.get("mail.max_per_day", 50))
        if self._daily_count >= max_per_day:
            logger.debug("mail.poller.daily_limit_reached count={}", self._daily_count)
            return

        await self._poll_inbox(mode)

    async def _poll_inbox(self, mode: str) -> None:
        from tools.mail_tools import _run_agently

        # 查未读邮件
        args = [
            "message", "+list", "--dir", "inbox",
            "--is-unread", "--limit", "10",
            "--after", self._last_poll_time,
        ]
        rc, out, err = await _run_agently(args, timeout=30)
        if rc != 0:
            logger.warning("mail.poller.list_failed rc={} err={}", rc, err[:200])
            return

        messages = _extract_messages(out)
        if not messages:
            return

        logger.info("mail.poller.found_unread count={}", len(messages))

        allowed_senders = self.cfg.get("mail.allowed_senders", [])
        reply_channel = self.cfg.get("mail.reply_channel", "mail")

        for msg in messages:
            msg_id = msg.get("message_id", "")
            if not msg_id or msg_id in self._processed_ids:
                continue

            from_info = msg.get("from", {})
            from_email = from_info.get("email", "") if isinstance(from_info, dict) else ""
            from_name = from_info.get("name", "") if isinstance(from_info, dict) else ""

            # 白名单过滤
            if mode == "allowlist" and from_email not in allowed_senders:
                logger.debug("mail.poller.skip_not_allowed from={}", from_email)
                self._processed_ids.add(msg_id)  # 标记已处理避免重复检查
                continue

            # 处理这封邮件
            try:
                await self._process_one_email(msg_id, from_email, from_name,
                                              msg.get("subject", ""), msg.get("snippet", ""),
                                              reply_channel)
            except TimeoutError:
                logger.warning("mail.poller.process_timeout id={}", msg_id)
                # 超时的邮件不标记为已处理，下次轮询会重试
                continue
            except Exception as e:
                logger.warning("mail.poller.process_failed id={} error={}", msg_id, str(e)[:200])

            self._processed_ids.add(msg_id)
            self._daily_count += 1

            # 裁剪去重集合：保留最近 2000 条，避免全量清空导致重复处理
            if len(self._processed_ids) > 5000:
                # 转为列表保留最新的 2000 条
                recent = list(self._processed_ids)[-2000:]
                self._processed_ids = set(recent)

            if self._daily_count >= int(self.cfg.get("mail.max_per_day", 50)):
                break

        # 更新水位线
        self._last_poll_time = _utc_now_iso()

    async def _process_one_email(self, msg_id: str, from_email: str, from_name: str,
                                  subject: str, snippet: str, reply_channel: str) -> None:
        """处理单封邮件：读全文 → 注入 core → 回复。"""
        from tools.mail_tools import _run_agently

        # 1. 读取邮件全文
        rc, out, _err = await _run_agently(["message", "+read", "--id", msg_id], timeout=30)
        body = ""
        if rc == 0:
            try:
                envelope = json.loads(out.strip())
                data = envelope.get("data", {})
                if isinstance(data, dict):
                    body = data.get("body") or data.get("content") or data.get("text") or snippet
                elif isinstance(data, str):
                    body = data
            except json.JSONDecodeError:
                body = snippet
        else:
            body = snippet  # 降级用摘要

        logger.info("mail.poller.processing id={} from={} subject={}", msg_id, from_email, subject[:50])

        # 2. 构造用户输入，注入 Agent（用主人身份，让小妲以完整人设回复）
        user_input = _format_email_as_input(from_name, from_email, subject, body)
        session_id = f"mail_{from_email}"
        user_id = f"mail_{from_email}"

        try:
            result = await asyncio.wait_for(
                self.core.process(
                    user_input,
                    user_id=user_id,
                    source="mail",
                    user_openid=from_email,
                    session_id=session_id,
                    is_master=True,
                ),
                timeout=180,
            )
            reply_text = result.reply if result and result.reply else "已收到你的邮件，但暂时无法生成回复。"
            reply_text = _clean_reply_text(reply_text)
        except TimeoutError:
            logger.warning("mail.poller.process_timeout id={}", msg_id)
            reply_text = "抱歉，处理你的邮件超时了，请稍后重试。"
        except Exception as e:
            logger.warning("mail.poller.process_error id={} error={}", msg_id, str(e)[:200])
            reply_text = f"处理邮件时遇到问题：{str(e)[:100]}"

        # 3. 通过邮件回复（用 +send 新建邮件，避免 +reply 自动附加原文引用）
        if reply_channel in ("mail", "mail_and_qq"):
            reply_subject = f"Re: {subject}" if not subject.lower().startswith("re:") else subject
            ok = await _send_reply(from_email, reply_subject, reply_text)
            if ok:
                logger.info("mail.poller.replied id={} to={}", msg_id, from_email)
            else:
                logger.warning("mail.poller.reply_failed id={}", msg_id)

        # 4. 可选：QQ 通知
        if reply_channel == "mail_and_qq":
            try:
                from qq_bot_adapter import send_proactive_message
                await send_proactive_message(f"收到来自 {from_name}({from_email}) 的邮件「{subject}」，已通过邮件回复。")
            except Exception:
                logger.debug("mail.qq_notify_error", exc_info=True)

    # ── 辅助 ──────────────────────────────────────────────────
    def _is_dnd(self) -> bool:
        """免打扰时段检查（从配置读取，默认 0:00-0:00 即不启用 DND）。"""
        start = int(self.cfg.get("mail.dnd_start", 0))
        end = int(self.cfg.get("mail.dnd_end", 0))
        if start == end:
            return False  # 起止相同 = 不启用 DND
        hour = datetime.now().hour
        if start < end:
            return start <= hour < end
        # 跨天时段（如 22:00-7:00）
        return hour >= start or hour < end

    def get_stats(self) -> dict:
        """返回当前状态统计（供 API 查询）。"""
        return {
            "enabled": self.cfg.get("mail.enabled", False),
            "mode": self.cfg.get("mail.mode", "off"),
            "daily_count": self._daily_count,
            "max_per_day": int(self.cfg.get("mail.max_per_day", 50)),
            "processed_total": len(self._processed_ids),
            "last_poll_time": self._last_poll_time,
        }


# ── 模块级辅助函数 ──────────────────────────────────────────────
def _utc_now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串（Z 后缀）。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_messages(stdout: str) -> list[dict]:
    """从 agently-cli 的 JSON 输出中提取邮件列表。

    输出格式：{"ok": true, "data": {"data": [...], "pagination": {...}}}
    后面可能跟一行 "tip: ..." 提示文本。
    """
    text = stdout.strip()
    if not text:
        return []
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        # 可能后面有非 JSON 行，尝试截取第一个完整 JSON
        brace_end = text.rfind("}")
        if brace_end > 0:
            try:
                envelope = json.loads(text[:brace_end + 1])
            except json.JSONDecodeError:
                return []
        else:
            return []

    data = envelope.get("data", {})
    if isinstance(data, dict):
        return data.get("data", []) or data.get("messages", []) or []
    if isinstance(data, list):
        return data
    return []


def _format_email_as_input(from_name: str, from_email: str, subject: str, body: str) -> str:
    """把邮件正文直接作为用户输入传给 Agent，不加任何提示词。

    小妲的人设、语气由 AgentCore 的系统提示统一控制，
    邮件层只负责把内容传进去，不做任何引导。
    """
    return body.strip()[:2000] if body else "(空邮件)"


def _clean_reply_text(reply: str) -> str:
    """清洗 Agent 回复，保证发给邮件的是干净的自然语言正文。

    处理：
    1. JSON/工具调用格式 → 提取 text/content 字段，或降级提示
    2. 元指令前缀（"爸爸，可以这样回复："等）
    3. text/```等格式标记
    4. ---------- Original ---------- 之后的原文引用
    """
    if not reply:
        return reply
    import re
    import json
    text = reply.strip()

    # 1. 如果回复是 JSON，提取其中的文本字段
    if text.startswith(('{', '[')):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                # 尝试常见字段名
                for key in ('reply', 'text', 'content', 'message', 'body', 'response'):
                    if key in obj and isinstance(obj[key], str):
                        text = obj[key].strip()
                        break
                else:
                    # 没有 text 字段，降级提示
                    text = "（回复格式异常，请稍后重试）"
            else:
                text = "（回复格式异常，请稍后重试）"
        except json.JSONDecodeError:
            pass  # 不是合法 JSON，继续后续清洗

    # 2. 去掉开头的元指令前缀（精确匹配）
    text = re.sub(r'^爸爸[，,][^。\n]*?回复[：:]\s*', '', text).strip()
    text = re.sub(r'^可以这样回复[：:]\s*', '', text).strip()
    text = re.sub(r'^建议回复[：:]\s*', '', text).strip()

    # 3. 去掉开头的 text 标记和代码块标记
    text = re.sub(r'^text\s*\n?', '', text).strip()
    text = re.sub(r'^```[a-zA-Z]*\s*\n?', '', text).strip()
    if text.endswith('```'):
        text = text[:-3].strip()

    # 4. 去掉 ---------- Original ---------- 之后的原文引用
    text = re.split(r'-{5,}\s*Original\s*-{5,}', text, flags=re.IGNORECASE)[0].strip()
    return text.rstrip()


async def _send_reply(to_email: str, subject: str, body: str) -> bool:
    """用 message +send 新建邮件回复（避免 +reply 自动附加原文引用）。

    两阶段确认：
    1. 首次调用（无 token）→ 返回 confirmation_token + 摘要
    2. 带 token 重新调用 → 真正完成发送
    """
    from tools.mail_tools import _run_agently

    # 阶段 1：调用 send（无 token），获取 confirmation_token
    args1 = ["message", "+send", "--to", to_email, "--subject", subject, "--body", body]
    rc1, out1, err1 = await _run_agently(args1, timeout=60)

    # 先尝试提取 confirmation_token（无论 rc 是 0 还是 8，data 里都可能带 token）
    ctk = _extract_confirmation_token(out1)

    if ctk:
        # 阶段 2：带 token 真正发送
        args2 = ["message", "+send", "--to", to_email, "--subject", subject,
                 "--body", body, "--confirmation-token", ctk]
        rc2, out2, err2 = await _run_agently(args2, timeout=60)
        if rc2 == 0:
            try:
                envelope2 = json.loads(out2.strip())
                if envelope2.get("ok", False):
                    return True
                logger.warning("mail.poller.send_stage2_ok_false to={} out={}",
                               to_email, out2[:200])
                return False
            except json.JSONDecodeError:
                return True
        logger.warning("mail.poller.send_stage2_failed to={} rc={} err={}",
                       to_email, rc2, err2[:200])
        return False

    # 无 confirmation_token：检查是否直接成功
    if rc1 == 0:
        try:
            envelope1 = json.loads(out1.strip())
            if envelope1.get("ok"):
                return True
        except json.JSONDecodeError:
            pass

    logger.warning("mail.poller.send_failed to={} rc={} out={} err={}",
                   to_email, rc1, out1[:200], err1[:200])
    return False


def _extract_confirmation_token(stdout: str) -> str:
    """从 agently-cli 输出中提取 confirmation_token。"""
    text = stdout.strip()
    if not text:
        return ""
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        brace_end = text.rfind("}")
        if brace_end > 0:
            try:
                envelope = json.loads(text[:brace_end + 1])
            except json.JSONDecodeError:
                return ""
        else:
            return ""

    data = envelope.get("data", {})
    if isinstance(data, dict):
        return data.get("confirmation_token") or data.get("ctk") or ""
    return ""
