"""后台委托任务：注册表 + 主代理口吻转述投递 + 进度/终止支持。

用户模型（2026-08-25 review 迭代定稿）：
    不需要受理回执与专用推送帧。主代理当轮流式输出正常回复的同时任务在
    后台跑；子代理完成后把结果交回**主代理本人**，由主代理用自己的口吻
    转述要点，经普通消息通道主动发给用户——对用户而言就是主 Agent 又
    说了一句话。

通道出口：
- qq/wechat → 各适配器 send_proactive_message（普通消息语义）
- ws/web   → send_to_session 发送标准 final 帧（前端按普通回复渲染）
- cli/未知 → 日志留痕

插话：job.interjections 列表由控制工具写入、被 SubAgent._chat_loop 每轮
消费；未消费完的剩余插话会并入转述提示，保证用户的话一定被回应。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class BackgroundDelegation:
    """一条后台委托任务的登记项。"""

    task_id: str
    agent: str
    display_name: str
    mode: str = "single"
    verifier: str = ""
    task_text: str = ""
    channel: str = ""          # qq / wechat / web(ws) / cli
    session_id: str = ""       # ws 通道投递目标
    user_openid: str = ""      # qq 通道投递目标（空则适配器回退最近私聊用户）
    user_id: str = ""
    address_term: str = ""     # 称谓（转述措辞用）
    started_at: float = field(default_factory=time.time)
    status: str = "running"    # running/completed/failed/cancelled/delivered/deliver_failed
    finished_at: float | None = None
    result_preview: str = ""
    last_progress: str = ""    # 最近一次执行阶段描述
    interjections: list = field(default_factory=list)  # 共享引用：控制工具写、_chat_loop 消费
    asyncio_task: Any = None   # 执行体句柄（终止用；不进 snapshot）

    def to_snapshot(self) -> dict:
        elapsed = round((self.finished_at or time.time()) - self.started_at, 1)
        return {
            "task_id": self.task_id,
            "agent": self.agent,
            "display_name": self.display_name,
            "mode": self.mode,
            "channel": self.channel,
            "session_id": self.session_id,
            "status": self.status,
            "elapsed_s": elapsed,
            "started_at": self.started_at,
            "last_progress": self.last_progress,
            "pending_interjections": len(self.interjections),
            "result_preview": self.result_preview,
        }


_JOBS: dict[str, BackgroundDelegation] = {}

# 终态任务保留一段时间供 status 查询，过期即从登记表移除；
# 条目持有 asyncio_task 引用，不淘汰会导致已完成协程的帧无法回收。
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled",
                                "delivered", "deliver_failed"})
_RETENTION_SECONDS = 3600
_MAX_JOBS = 200


def _evict_expired(now: float | None = None) -> int:
    """移除终态且超过保留期的登记项，返回淘汰数。"""
    now = time.time() if now is None else now
    expired = [tid for tid, j in _JOBS.items()
               if j.status in _TERMINAL_STATUSES
               and j.finished_at is not None
               and now - j.finished_at > _RETENTION_SECONDS]
    for tid in expired:
        del _JOBS[tid]
    return len(expired)


def register(job: BackgroundDelegation) -> str:
    _evict_expired()
    overflow = len(_JOBS) - _MAX_JOBS + 1
    if overflow > 0:
        terminal = sorted((j for j in _JOBS.values()
                           if j.status in _TERMINAL_STATUSES),
                          key=lambda j: j.finished_at or 0.0)
        for stale in terminal[:overflow]:
            del _JOBS[stale.task_id]
    _JOBS[job.task_id] = job
    return job.task_id


def get(task_id: str) -> BackgroundDelegation | None:
    return _JOBS.get(task_id)


def mark_done(task_id: str, ok: bool, preview: str = "") -> None:
    job = _JOBS.get(task_id)
    if job is None:
        return
    job.status = "completed" if ok else "failed"
    job.finished_at = time.time()
    job.result_preview = preview


def mark_cancelled(task_id: str) -> None:
    job = _JOBS.get(task_id)
    if job is None:
        return
    job.status = "cancelled"
    job.finished_at = time.time()


def note_progress(job: BackgroundDelegation, text: str) -> None:
    job.last_progress = (text or "")[:80]


def find_running(agent: str | None = None,
                 task_id_prefix: str | None = None,
                 display_name: str | None = None) -> BackgroundDelegation | None:
    """取最近一条运行中的任务（可按 agent 名/编号前缀/显示名过滤）。"""
    candidates = [
        j for j in _JOBS.values()
        if j.status == "running"
        and (agent is None or j.agent == agent.lower())
        and (task_id_prefix is None or j.task_id.startswith(task_id_prefix))
        and (display_name is None or j.display_name.lower() == display_name.lower())
    ]
    return max(candidates, key=lambda j: j.started_at, default=None)


def snapshot(status: str | None = None) -> list[dict]:
    jobs = [
        j.to_snapshot() for j in _JOBS.values()
        if status is None or j.status == status
    ]
    return sorted(jobs, key=lambda x: x["started_at"], reverse=True)


# ── 投递 ─────────────────────────────────────────────────────


async def _send_channel(job: BackgroundDelegation, text: str) -> bool:
    channel = (job.channel or "").lower()
    if channel == "web":
        channel = "ws"  # RequestContext.source 的 web 与 WS 同一投递出口
    try:
        if channel == "qq":
            from qq_bot_adapter import send_proactive_message
            return await send_proactive_message(text, openid=job.user_openid or None)
        if channel == "wechat":
            from wechat_bot_adapter import send_proactive_message
            return await send_proactive_message(text)
        if channel == "ws":
            from web.ws_hub import manager
            if not job.session_id:
                raise RuntimeError("ws 通道缺少 session_id")
            await manager.send_to_session(job.session_id, {
                "type": "final",           # 标准 final 帧：前端按普通回复渲染
                "msg_id": job.task_id,
                "reply": text,
                "emotion": "",
                "sticker_url": "",
                "audio_url": None,
                "image_urls": [],
                "video_url": None,
                "agent": "xiaoda",
                "elapsed_ms": int((time.time() - job.started_at) * 1000),
            })
            return True
        logger.info("async_delegation.cli_deliver channel={} text={}",
                    channel or "?", text[:200])
        return True
    except Exception as e:  # noqa: BLE001 —— 投递失败不炸后台任务
        logger.warning("async_delegation.send_failed channel={} error={}",
                       channel or "?", str(e)[:150])
        return False


async def deliver_text(job: BackgroundDelegation, text: str,
                       *, failed: bool = False) -> bool:
    """不经转述直接以主代理口吻发送（终止通知等短消息用）。"""
    ok = await _send_channel(job, text)
    job.status = ("delivered" if ok and not failed
                  else "failed" if failed
                  else "deliver_failed")
    if job.finished_at is None:
        job.finished_at = time.time()
    logger.info("async_delegation.delivered task_id={} agent={} ok={}",
                job.task_id, job.agent, ok)
    return ok


async def compose_and_deliver(core: Any, job: BackgroundDelegation,
                              result: str, *, failed: bool = False) -> bool:
    """结果先经主代理 LLM 转述成自然口吻，再走普通通道发出。

    LLM 转述失败/超时（20s）时降级为模板拼接——保证结果必达。
    未消费的用户插话会并入转述要求，确保一定被回应。
    """
    address = job.address_term or "爸爸"
    interject_block = ""
    if job.interjections:
        notes = "; ".join(str(n)[:200] for n in job.interjections)
        interject_block = (f"\n\n用户在任务执行期间补充了以下指示，请务必一并回应：\n{notes}")

    composed = ""
    if not failed:
        try:
            sys_prompt = (
                f"你是小妲。你之前把一项任务交给助手{job.display_name}处理，现在她做完了，"
                f"你要用自己自然的口吻向{address}汇报结果要点。"
                "要求：简洁（不超过 6 句）；保留关键结论与数据；"
                "可以自然说明是谁协助完成的；不要出现'后台''委托''任务编号'这类机制词汇。"
            )
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content":
                    f"原任务：{job.task_text}\n\n执行输出：\n{result[:3000]}{interject_block}"},
            ]
            composed = await core.router.route("chat", messages,
                                               max_tokens=700, timeout=20)
        except Exception as e:  # noqa: BLE001 —— 转述失败降级模板
            logger.warning("async_delegation.compose_failed error={}", str(e)[:120])
            composed = ""

    if not (composed and composed.strip()):
        verb = "出错了" if failed else "有结果了"
        composed = (f"{address}，{job.display_name}{verb}：\n{result[:1500]}"
                    + (f"\n\n另外，你之前补充的指示我记着呢：{interject_block.strip()}"
                       if interject_block else ""))
    if failed:
        composed = f"{address}，{job.display_name}那边没能完成：\n{result[:1500]}"

    ok = await _send_channel(job, composed)
    job.status = ("delivered" if ok and not failed
                  else "failed" if failed and not ok
                  else "delivered" if ok
                  else "deliver_failed")
    if job.finished_at is None:
        job.finished_at = time.time()
    logger.info("async_delegation.delivered task_id={} agent={} ok={}",
                job.task_id, job.agent, ok)
    return ok
