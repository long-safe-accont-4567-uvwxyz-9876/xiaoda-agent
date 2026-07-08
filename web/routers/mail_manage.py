"""邮箱管理路由 —— 前端「邮箱管理」页面的后端 API。

提供收件处理设置读写、轮询状态统计、收件箱预览三个接口。
设置存储在 ConfigService 的 mail 段（config/webui_overrides.json）。
"""
from __future__ import annotations

import json
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel

from web.routers.auth import get_current_user
from web.schemas import Envelope
import contextlib

router = APIRouter(tags=["mail"], dependencies=[Depends(get_current_user)])

_auth_status_cache: dict = {}
_AUTH_CACHE_TTL = 300
_auth_status_lock = threading.Lock()


def _clear_auth_status_cache() -> None:
    """清除 auth-status 缓存（供后台任务调用）。"""
    with _auth_status_lock:
        _auth_status_cache.clear()


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
def _extract_first_json_object(text: str, log_tag: str = "") -> dict:
    """从文本中提取第一个完整 JSON 对象（跳过 "tip:" 行等非 JSON 后缀）。

    解析失败时返回空 dict；若提供 log_tag 则记录 debug 日志。
    """
    text = text.strip()
    brace_count = 0
    end_pos = 0
    for i, ch in enumerate(text):
        if ch == '{':
            brace_count += 1
        elif ch == '}':
            brace_count -= 1
            if brace_count == 0:
                end_pos = i + 1
                break
    if not end_pos:
        return {}
    try:
        return json.loads(text[:end_pos])
    except Exception as exc:
        if log_tag:
            logger.debug("mail.{}_parse_failed: {}", log_tag, exc, exc_info=True)
        return {}


def _extract_agent_email_from_inbox(out: str) -> str:
    """从 inbox 列表输出中提取 Agent 自己的邮箱（收件人 to 字段）。"""
    try:
        envelope = _extract_first_json_object(out, log_tag="inbox")
        data = envelope.get("data", {})
        if isinstance(data, list) and data:
            first = data[0] if isinstance(data[0], dict) else {}
            # 从 to（收件人）提取 Agent 自己的邮箱，而非 from（发件人）
            to_field = first.get("to", "")
            if isinstance(to_field, list) and to_field:
                return to_field[0].get("email", "") if isinstance(to_field[0], dict) else str(to_field[0])
            if isinstance(to_field, str) and "@" in to_field:
                return to_field
            if isinstance(to_field, dict):
                return to_field.get("email", "")
    except Exception as exc:
        logger.debug("mail.inbox_parse_failed: {}", exc, exc_info=True)
    return ""


def _match_email_regex(text: str) -> str:
    """从文本中正则匹配第一个邮箱地址（兜底）。"""
    try:
        import re
        m = re.search(r'[\w.+-]+@[\w.-]+\.\w+', text)
        if m:
            return m.group(0)
    except Exception as exc:
        logger.debug("mail.email_regex_match_failed: {}", exc, exc_info=True)
    return ""


async def _fetch_agent_email(out_auth: str, cfg: Any) -> tuple[str, str]:
    """获取 Agent 注册的邮箱地址。

    优先读缓存，否则用 message +list 查询，最后正则兜底。
    返回 (email, error)，error 非空表示需提前返回（如 OAuth 失效）。
    """
    from tools.mail_tools import _run_agently

    # 先从缓存中读取（之前成功获取时保存的）
    cached_email = cfg.get("mail.agent_email", "")
    if cached_email:
        return cached_email, ""

    # 尝试用 message +list 获取，从收件人（to）字段提取 Agent 自己的邮箱
    rc, out, err = await _run_agently(
        ["message", "+list", "--dir", "inbox", "--limit", "1"],
        timeout=15,
    )
    if rc == 3:
        # invalid_grant: OAuth 授权已失效，需要重新授权
        return "", "邮箱 OAuth 授权已失效，请重新授权"

    email = ""
    if rc == 0:
        email = _extract_agent_email_from_inbox(out)

    # 如果仍未获取到，尝试正则匹配（兜底）
    if not email:
        email = _match_email_regex(out_auth)

    # 缓存到配置中，下次不再查询
    if email:
        cfg.set("mail.agent_email", email)

    return email, ""


@router.get("/mail/auth-status", response_model=Envelope[dict])
async def get_mail_auth_status(request: Request) -> Any:
    """检查 agently-cli 安装状态和邮箱授权状态。

    返回:
      - installed: agently-cli 是否已安装
      - cli_path: agently-cli 路径（未安装时为 null）
      - authorized: 邮箱是否已授权
      - email: Agent 注册的邮箱地址（未授权时为空）
      - error: 错误信息
    """
    from tools.mail_tools import _resolve_agently_cli, _run_agently

    cli_path = _resolve_agently_cli()
    if not cli_path:
        return Envelope(data={
            "installed": False,
            "cli_path": None,
            "authorized": False,
            "email": "",
            "error": "agently-cli 未安装，请先安装",
        })

    # 用 auth status 检查登录状态（快速，不触发 API 调用）
    rc_auth, out_auth, err_auth = await _run_agently(
        ["auth", "status"],
        timeout=10,
    )

    if rc_auth != 0:
        return Envelope(data={
            "installed": True,
            "cli_path": cli_path,
            "authorized": False,
            "email": "",
            "error": "邮箱未授权或授权已失效",
        })

    # 解析 auth status JSON（agently-cli 输出多行格式化 JSON，需整体解析）
    auth_envelope = _extract_first_json_object(out_auth, log_tag="auth_status")
    auth_data = auth_envelope.get("data", {})

    logged_in = auth_data.get("logged_in", False)
    if not logged_in:
        return Envelope(data={
            "installed": True,
            "cli_path": cli_path,
            "authorized": False,
            "email": "",
            "error": "邮箱未授权，请先完成 OAuth 登录",
        })

    # 检查 token 状态
    # token_status 为 auto_refresh 时表示 CLI 会自动刷新 token，即使 expires_at 已过期也是正常的
    token_status = auth_data.get("token_status", "")
    if token_status not in ("auto_refresh", "valid", ""):
        return Envelope(data={
            "installed": True,
            "cli_path": cli_path,
            "authorized": False,
            "email": "",
            "error": f"邮箱授权状态异常: {token_status}",
        })

    # logged_in=true 且 token_status 正常，尝试获取 Agent 注册的邮箱地址
    cfg = _get_cfg_service(request)
    email, error = await _fetch_agent_email(out_auth, cfg)
    if error:
        return Envelope(data={
            "installed": True,
            "cli_path": cli_path,
            "authorized": False,
            "email": "",
            "error": error,
        })

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

    cli_path = _resolve_agently_cli()
    if not cli_path:
        return Envelope(data={
            "started": False,
            "message": "agently-cli 未安装，请先安装 agently-cli",
            "cli_path": None,
        })

    # 构造子进程环境（与 _run_agently 保持一致）
    import os
    env = os.environ.copy()
    cred_home = os.environ.get("AGENTLY_CLI_HOME", "").strip()
    if cred_home:
        env["HOME"] = cred_home
    # 确保 node 在 PATH 中
    from tools.mail_tools import _ensure_node_in_path
    _ensure_node_in_path(env)

    try:
        # 启动 auth login（非阻塞，CLI 会输出授权 URL 或自动打开浏览器）
        proc = await asyncio.create_subprocess_exec(
            cli_path, "auth", "login", "--verbose",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # 逐行读取 CLI 输出，在 15 秒内捕获授权 URL
        import re
        collected = ""
        deadline = asyncio.get_running_loop().time() + 15
        auth_url = ""

        try:
            while asyncio.get_running_loop().time() < deadline:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                line_bytes = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=remaining
                )
                if not line_bytes:
                    break  # EOF
                line = line_bytes.decode("utf-8", errors="replace")
                collected += line
                # 提取授权 URL（CLI 输出格式: "[info] 授权链接：https://..."）
                url_match = re.search(r'(https?://\S+)', line)
                if url_match:
                    auth_url = url_match.group(1).rstrip(")")
                    break
        except asyncio.TimeoutError:
            pass

        if auth_url:
            # 不杀进程，让它继续等待用户完成 OAuth 回调
            return Envelope(data={
                "started": True,
                "message": "请在浏览器中打开以下链接完成授权",
                "auth_url": auth_url,
                "cli_path": cli_path,
            })

        # 没找到 URL，检查进程是否已结束
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2)

        rc = proc.returncode
        if rc == 0:
            with _auth_status_lock:
                _auth_status_cache.clear()
            return Envelope(data={
                "started": True,
                "message": "授权成功！邮箱已连接",
                "auth_url": "",
                "cli_path": cli_path,
            })

        # 超时或失败，返回已收集的输出供调试
        return Envelope(data={
            "started": bool(collected.strip()),
            "message": collected.strip()[-200:] if collected.strip() else "授权流程已启动，请查看浏览器或终端输出",
            "auth_url": "",
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