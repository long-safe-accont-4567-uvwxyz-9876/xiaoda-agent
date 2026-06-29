"""邮箱管理路由 —— 前端「邮箱管理」页面的后端 API。

提供收件处理设置读写、轮询状态统计、收件箱预览三个接口。
设置存储在 ConfigService 的 mail 段（config/webui_overrides.json）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel

from web.routers.auth import get_current_user
from web.schemas import Envelope

router = APIRouter(tags=["mail"], dependencies=[Depends(get_current_user)])


# ── 请求/响应模型 ──────────────────────────────────────────────
class MailConfig(BaseModel):
    enabled: bool = False
    mode: str = "off"  # off / allowlist / all
    allowed_senders: list[str] = []
    reply_channel: str = "mail"  # mail / mail_and_qq
    max_per_day: int = 50
    dnd_start: int = 0  # 免打扰开始小时（0-23），0+0=不启用
    dnd_end: int = 0    # 免打扰结束小时（0-23）


# ── 配置读写 ──────────────────────────────────────────────────
@router.get("/mail/config", response_model=Envelope[dict])
async def get_mail_config(request: Request) -> Any:
    """读取邮件机器人配置。"""
    cfg = _get_cfg_service(request)
    data = {
        "enabled": cfg.get("mail.enabled", False),
        "mode": cfg.get("mail.mode", "off"),
        "allowed_senders": cfg.get("mail.allowed_senders", []),
        "reply_channel": cfg.get("mail.reply_channel", "mail"),
        "max_per_day": int(cfg.get("mail.max_per_day", 50)),
        "dnd_start": int(cfg.get("mail.dnd_start", 0)),
        "dnd_end": int(cfg.get("mail.dnd_end", 0)),
    }
    return Envelope(data=data)


@router.put("/mail/config", response_model=Envelope[dict])
async def put_mail_config(request: Request, body: MailConfig) -> Any:
    """更新邮件机器人配置。"""
    cfg = _get_cfg_service(request)

    # 校验
    if body.mode not in ("off", "allowlist", "all"):
        raise HTTPException(status_code=400, detail="mode 必须是 off/allowlist/all")
    if body.reply_channel not in ("mail", "mail_and_qq"):
        raise HTTPException(status_code=400, detail="reply_channel 必须是 mail/mail_and_qq")
    if body.max_per_day < 1 or body.max_per_day > 200:
        raise HTTPException(status_code=400, detail="max_per_day 须在 1-200 之间")
    if not (0 <= body.dnd_start <= 23) or not (0 <= body.dnd_end <= 23):
        raise HTTPException(status_code=400, detail="dnd_start/dnd_end 须在 0-23 之间")

    cfg.set("mail.enabled", body.enabled)
    cfg.set("mail.mode", body.mode)
    cfg.set("mail.allowed_senders", [s.strip() for s in body.allowed_senders if s.strip()])
    cfg.set("mail.reply_channel", body.reply_channel)
    cfg.set("mail.max_per_day", body.max_per_day)
    cfg.set("mail.dnd_start", body.dnd_start)
    cfg.set("mail.dnd_end", body.dnd_end)

    logger.info("mail.config_updated enabled={} mode={} senders={} dnd={}~{}",
                body.enabled, body.mode, len(body.allowed_senders),
                body.dnd_start, body.dnd_end)

    return Envelope(data={
        "enabled": body.enabled,
        "mode": body.mode,
        "allowed_senders": body.allowed_senders,
        "reply_channel": body.reply_channel,
        "max_per_day": body.max_per_day,
        "dnd_start": body.dnd_start,
        "dnd_end": body.dnd_end,
    })


# ── 状态统计 ──────────────────────────────────────────────────
@router.get("/mail/stats", response_model=Envelope[dict])
async def get_mail_stats(request: Request) -> Any:
    """获取邮件轮询器运行状态。"""
    poller = getattr(request.app.state, "mail_poller", None)
    if poller is None:
        return Envelope(data={"enabled": False, "mode": "off", "error": "轮询器未启动"})
    return Envelope(data=poller.get_stats())


# ── 收件箱预览 ────────────────────────────────────────────────
@router.get("/mail/inbox", response_model=Envelope[dict])
async def get_mail_inbox(request: Request, limit: int = Query(10, ge=1, le=50)) -> Any:
    """预览收件箱（代理 agently-cli message +list）。"""
    from tools.mail_tools import _run_agently

    args = ["message", "+list", "--dir", "inbox", "--limit", str(limit)]
    rc, out, err = await _run_agently(args, timeout=30)
    if rc != 0:
        raise HTTPException(status_code=502, detail=f"agently-cli 失败: {err[:200]}")

    # 解析 JSON（可能后面有 tip: 行）
    import json
    text = out.strip()
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        brace_end = text.rfind("}")
        if brace_end > 0:
            envelope = json.loads(text[:brace_end + 1])
        else:
            raise HTTPException(status_code=502, detail="无法解析 agently-cli 输出")

    data = envelope.get("data", {})
    return Envelope(data=data if isinstance(data, dict) else {"data": data})


# ── 邮箱授权状态检查 ──────────────────────────────────────────
@router.get("/mail/auth-status", response_model=Envelope[dict])
async def get_mail_auth_status(request: Request) -> Any:
    """检查 agently-cli 安装状态和邮箱授权状态。

    返回:
      - installed: agently-cli 是否已安装
      - cli_path: agently-cli 路径（未安装时为 null）
      - authorized: 邮箱是否已授权
      - email: 已授权的邮箱地址（未授权时为空）
      - error: 错误信息
    """
    from tools.mail_tools import _resolve_agently_cli, _run_agently
    import json as _json

    cli_path = _resolve_agently_cli()
    if not cli_path:
        return Envelope(data={
            "installed": False,
            "cli_path": None,
            "authorized": False,
            "email": "",
            "error": "agently-cli 未安装，请先安装",
        })

    # 用 message +list --limit 1 探测授权状态
    rc, out, err = await _run_agently(
        ["message", "+list", "--dir", "inbox", "--limit", "1"],
        timeout=15,
    )

    if rc == 3:
        return Envelope(data={
            "installed": True,
            "cli_path": cli_path,
            "authorized": False,
            "email": "",
            "error": "邮箱未授权或授权已失效",
        })

    if rc == 99 or rc == 97:
        return Envelope(data={
            "installed": False,
            "cli_path": None,
            "authorized": False,
            "email": "",
            "error": "agently-cli 执行失败",
        })

    if rc != 0:
        return Envelope(data={
            "installed": True,
            "cli_path": cli_path,
            "authorized": False,
            "email": "",
            "error": f"检查失败 (exit={rc}): {err[:200]}",
        })

    # 尝试从输出中提取邮箱地址
    email = ""
    try:
        text = out.strip()
        envelope = _json.loads(text)
        data = envelope.get("data", {})
        if isinstance(data, dict):
            # 尝试从 account/user 字段提取邮箱
            email = data.get("account", "") or data.get("user", "") or data.get("email", "")
    except Exception:
        pass

    return Envelope(data={
        "installed": True,
        "cli_path": cli_path,
        "authorized": True,
        "email": email,
        "error": "",
    })


# ── 触发邮箱授权登录 ──────────────────────────────────────────
@router.post("/mail/auth-login", response_model=Envelope[dict])
async def trigger_mail_auth_login(request: Request) -> Any:
    """触发 agently-cli auth login 进行 OAuth 授权。

    此端点启动一个后台子进程执行 agently-cli auth login，
    该命令会自动打开系统默认浏览器让用户完成 QQ 邮箱 OAuth 授权。

    返回:
      - started: 是否成功启动
      - message: 提示信息
      - cli_path: agently-cli 路径
    """
    from tools.mail_tools import _resolve_agently_cli
    import asyncio
    import os

    cli_path = _resolve_agently_cli()
    if not cli_path:
        return Envelope(data={
            "started": False,
            "message": "agently-cli 未安装，请先安装 agently-cli",
            "cli_path": None,
        })

    # 构造子进程环境
    env = os.environ.copy()
    cred_home = os.environ.get("AGENTLY_CLI_HOME", "").strip()
    if cred_home:
        env["HOME"] = cred_home

    try:
        # 启动 auth login（非阻塞，让浏览器自动弹出）
        proc = await asyncio.create_subprocess_exec(
            cli_path, "auth", "login",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # 给它 3 秒启动时间，如果还在运行说明浏览器已弹出
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
            # 3 秒内就结束了，检查结果
            rc = proc.returncode or 0
            if rc == 0:
                return Envelope(data={
                    "started": True,
                    "message": "授权成功！邮箱已连接",
                    "cli_path": cli_path,
                })
            else:
                stdout = (await proc.stdout.read()).decode("utf-8", errors="replace") if proc.stdout else ""
                stderr = (await proc.stderr.read()).decode("utf-8", errors="replace") if proc.stderr else ""
                return Envelope(data={
                    "started": False,
                    "message": f"授权失败 (exit={rc}): {stderr[:200]}",
                    "cli_path": cli_path,
                })
        except asyncio.TimeoutError:
            # 还在运行 = 浏览器已弹出，等待用户操作
            return Envelope(data={
                "started": True,
                "message": "浏览器已打开，请在浏览器中完成 QQ 邮箱授权",
                "cli_path": cli_path,
            })

    except Exception as e:
        logger.error("mail.auth_login.start_failed", error=str(e))
        return Envelope(data={
            "started": False,
            "message": f"启动授权失败: {e}",
            "cli_path": cli_path,
        })


# ── 辅助 ──────────────────────────────────────────────────────
def _get_cfg_service(request: Request):
    """从 app.state 获取 ConfigService。"""
    from web.config_service import get_config_service
    return get_config_service()
