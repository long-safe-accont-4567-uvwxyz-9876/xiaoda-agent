"""后台委托任务：注册表 + 结果主动投递路由。

用户模型（2026-08-25 review 提出）：
    主代理分配任务 → 子代理后台执行 → 完成后把结果主动推送给用户。
本模块承接"后台"与"主动推送"两环：

- delegate_task(background=true) 时，主代理当轮立即返回受理回执，
  子代理脱离当轮在后台执行（run_background_delegation）；
- 完成后按委托时刻的来源通道（qq/wechat/ws/cli）选择出口，
  经各通道既有的 send_proactive_message / ws 会话帧主动投递。

平台约束备忘：
- QQ/微信主动消息依赖 bot 活跃实例（微信还需 context_token），
  未就绪时投递失败仅记日志——结果保留在注册表（snapshot() 可查），
  不重试不丢弃告警。
- ws 通道若用户已断开连接，send_to_session 找不到连接同样静默；
  前端当前对未知帧类型忽略，delegate_result 帧的消费属后续前端工作。

通道判定：RequestContext.channel 由 AgentCore.process(source=...) 打标
（qq/wechat/ws/cli），随 contextvar 被 create_task 捕获进后台任务。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from loguru import logger

_TERMINAL_STATUSES = ("delivered", "deliver_failed")


@dataclass
class BackgroundDelegation:
    """一条后台委托任务的登记项。"""

    task_id: str
    agent: str
    display_name: str
    mode: str = "single"
    verifier: str = ""
    task_text: str = ""
    channel: str = ""          # qq / wechat / ws / cli
    session_id: str = ""       # ws 通道投递目标
    user_openid: str = ""      # qq 通道投递目标（空则适配器回退最近私聊用户）
    user_id: str = ""
    started_at: float = field(default_factory=time.time)
    status: str = "running"    # running / completed / failed / delivered / deliver_failed
    finished_at: float | None = None
    result_preview: str = ""

    def to_snapshot(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent": self.agent,
            "display_name": self.display_name,
            "mode": self.mode,
            "channel": self.channel,
            "session_id": self.session_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result_preview": self.result_preview,
        }


_JOBS: dict[str, BackgroundDelegation] = {}


def register(job: BackgroundDelegation) -> str:
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


def snapshot(status: str | None = None) -> list[dict]:
    jobs = [
        j.to_snapshot() for j in _JOBS.values()
        if status is None or j.status == status
    ]
    return sorted(jobs, key=lambda x: x["started_at"], reverse=True)


async def deliver(job: BackgroundDelegation, content: str,
                  failed: bool = False) -> bool:
    """按 job.channel 路由主动投递。content 应为可直接阅读的结果文本。"""
    header = (f"📌 你委托的{job.display_name}后台任务"
              f"{'失败了' if failed else '已完成'}：")
    text = f"{header}\n{content}"
    channel = (job.channel or "").lower()
    if channel == "web":  # RequestContext.source 的 web 与 WS 同一投递出口
        channel = "ws"
    ok = False
    try:
        if channel == "qq":
            from qq_bot_adapter import send_proactive_message
            ok = await send_proactive_message(text, openid=job.user_openid or None)
        elif channel == "wechat":
            from wechat_bot_adapter import send_proactive_message
            ok = await send_proactive_message(text)
        elif channel == "ws":
            from web.ws_hub import manager
            if not job.session_id:
                raise RuntimeError("ws 通道缺少 session_id，无法定位投递目标")
            await manager.send_to_session(job.session_id, {
                "type": "delegate_result",
                "task_id": job.task_id,
                "agent": job.agent,
                "ok": not failed,
                "header": header,
                "reply": content,
            })
            ok = True
        else:
            # cli / 未识别通道：落日志留痕（结果仍在 snapshot 中可查）
            logger.info("async_delegation.cli_deliver channel={} text={}",
                        channel or "?", text[:200])
            ok = True
    except Exception as e:  # noqa: BLE001 —— 投递失败绝不向上炸后台任务
        logger.warning("async_delegation.deliver_failed channel={} error={}",
                       channel or "?", str(e)[:150])
        ok = False

    job.status = ("delivered" if ok and not failed
                  else "failed" if failed and not ok
                  else "delivered" if ok
                  else "deliver_failed")
    if job.finished_at is None:
        job.finished_at = time.time()
    logger.info("async_delegation.delivered task_id={} agent={} channel={} "
                "ok={}", job.task_id, job.agent, channel or "?", ok)
    return ok
