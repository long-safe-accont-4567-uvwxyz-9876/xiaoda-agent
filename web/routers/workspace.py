"""工作目录授权 API 路由

提供工作目录选择、授权确认、撤销、目录浏览、命令白名单管理、
命令动态确认、操作审计查询等端点。

授权持久化到 config_service 的 workspace 配置段，启动时由 bootstrap 恢复。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

from security.permission_manager import get_permission_manager, AuditEntry
from web.schemas import Envelope

router = APIRouter(prefix="/workspace", tags=["workspace"])


# ── 请求模型 ───────────────────────────────────────────────
class ConfirmBody(BaseModel):
    path: str


class WhitelistBody(BaseModel):
    command: str


class ConfirmCmdBody(BaseModel):
    request_id: str
    decision: str  # "allow" / "allow_once" / "deny"
    add_to_whitelist: bool = False
    command: str = ""
    session_id: str = ""  # 发起确认请求的会话（前端从 WS 消息回显），用于身份校验


# ── 命令确认决策暂存（request_id → 决策记录） ───────────────
# 简化实现：内存映射，工具层通过 request_id 查询决策。
# 记录中携带发起会话 session_id：confirm_cmd 必须由同一会话回传，
# 避免无关客户端（其它标签页/LAN 连接）替他人决定命令放行/拒绝。
_pending_cmd_decisions: dict[str, dict] = {}


def register_cmd_decision_scope(request_id: str, session_id: str) -> None:
    """登记命令确认请求的发起会话（供 confirm_cmd 身份校验）。"""
    _pending_cmd_decisions[request_id] = {"session_id": session_id or "", "decision": ""}


def discard_cmd_decision_scope(request_id: str) -> None:
    """丢弃未决的确认请求（超时未决策时清理，防止内存泄漏与迟到决策）。"""
    _pending_cmd_decisions.pop(request_id, None)


def _persist_workspace(cwd: str, authorized_at: str = "") -> None:
    """持久化工作目录授权状态到 config（同步，best-effort）"""
    try:
        from web.config_service import get_config_service
        cs = get_config_service()
        cs.set_many({
            "workspace.cwd": cwd,
            "workspace.authorized_at": authorized_at,
        })
    except Exception as e:
        logger.warning("workspace.persist_failed", error=str(e))


def _persist_whitelist(whitelist: list[str]) -> None:
    """持久化命令白名单到 config（同步，best-effort）"""
    try:
        from web.config_service import get_config_service
        cs = get_config_service()
        cs.set_many({"workspace.cmd_whitelist": whitelist})
    except Exception as e:
        logger.warning("workspace.whitelist_persist_failed", error=str(e))


# ── 工作目录授权端点 ────────────────────────────────────────
@router.get("")
async def get_workspace():
    """获取当前工作目录授权状态"""
    pm = get_permission_manager()
    return Envelope(data={
        "authorized": pm.is_cwd_authorized(),
        "path": pm.cwd,
        "permissions": ["read", "write", "exec_restricted"] if pm.is_cwd_authorized() else [],
    })


@router.post("/confirm")
async def confirm_workspace(body: ConfirmBody):
    """确认授权工作目录

    用户在 ConfirmDialog 点击"授权"后调用。
    持久化授权状态 + 更新 PermissionManager.cwd。
    """
    if not os.path.isdir(body.path):
        raise HTTPException(status_code=400, detail=f"路径不存在或不是目录：{body.path}")
    pm = get_permission_manager()
    pm.set_cwd(body.path)
    authorized_at = datetime.now().isoformat()
    _persist_workspace(body.path, authorized_at)
    logger.info("workspace.authorized", path=body.path)
    return Envelope(data={
        "authorized": True,
        "path": body.path,
        "authorized_at": authorized_at,
    })


@router.delete("")
async def revoke_workspace():
    """撤销工作目录授权"""
    pm = get_permission_manager()
    pm.clear_cwd()
    _persist_workspace("", "")
    logger.info("workspace.revoked")
    return Envelope(data={"authorized": False})


@router.get("/browse")
async def browse_directory(path: str = ""):
    """列出目录下的子目录（用于 DirectoryPickerDialog 浏览）"""
    target = path or os.path.expanduser("~")
    if not os.path.isdir(target):
        raise HTTPException(status_code=400, detail=f"不是目录：{target}")
    try:
        entries = os.listdir(target)
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"无权限访问：{target}")
    dirs = sorted([e for e in entries if os.path.isdir(os.path.join(target, e))])
    parent = os.path.dirname(target)
    return Envelope(data={
        "current": target,
        "parent": parent if parent and parent != target else None,
        "dirs": dirs,
    })


# ── 命令白名单管理端点 ─────────────────────────────────────
@router.get("/whitelist")
async def get_whitelist():
    """获取命令白名单"""
    pm = get_permission_manager()
    return Envelope(data={"whitelist": pm.get_whitelist()})


@router.post("/whitelist")
async def add_to_whitelist(body: WhitelistBody):
    """添加命令到白名单"""
    pm = get_permission_manager()
    pm.add_to_whitelist(body.command)
    _persist_whitelist(pm.get_whitelist())
    return Envelope(data={"whitelist": pm.get_whitelist()})


@router.delete("/whitelist/{command}")
async def remove_from_whitelist(command: str):
    """从白名单删除命令"""
    pm = get_permission_manager()
    pm.remove_from_whitelist(command)
    _persist_whitelist(pm.get_whitelist())
    return Envelope(data={"whitelist": pm.get_whitelist()})


# ── 命令动态确认端点 ───────────────────────────────────────
@router.post("/confirm_cmd")
async def confirm_cmd(body: ConfirmCmdBody):
    """回传命令确认决策

    前端 CmdConfirmDialog 用户选择后调用。
    决策存入 _pending_cmd_decisions，工具层通过 request_id 查询。
    身份校验：请求若带发起会话，则必须与发起会话一致，否则拒绝
    （防止无关客户端替他人决定命令放行/拒绝）。
    """
    rec = _pending_cmd_decisions.get(body.request_id)
    if rec is None:
        logger.warning("workspace.cmd_confirm_unknown_request", request_id=body.request_id)
        return Envelope(data={"status": "unknown_request"})
    initiator = rec.get("session_id", "")
    if initiator and body.session_id != initiator:
        logger.warning("workspace.cmd_confirm_session_mismatch",
                       request_id=body.request_id,
                       initiator=initiator, caller=body.session_id)
        raise HTTPException(status_code=403, detail="确认请求仅能由发起会话回传")
    if body.decision in ("allow", "allow_once") and body.add_to_whitelist and body.command:
        pm = get_permission_manager()
        pm.add_to_whitelist(body.command)
        _persist_whitelist(pm.get_whitelist())
    rec["decision"] = body.decision
    logger.info("workspace.cmd_confirmed",
                request_id=body.request_id, decision=body.decision,
                add_to_whitelist=body.add_to_whitelist)
    return Envelope(data={"status": "ok", "decision": body.decision})


def get_pending_cmd_decision(request_id: str) -> str | None:
    """工具层查询命令确认决策（None 表示尚未决策）"""
    rec = _pending_cmd_decisions.get(request_id)
    if rec is None:
        return None
    decision = rec.get("decision") or None
    if decision is not None:
        _pending_cmd_decisions.pop(request_id, None)
    return decision


# ── 审计日志端点 ───────────────────────────────────────────
@router.get("/audit")
async def get_audit(limit: int = 100):
    """获取工作目录操作审计日志"""
    pm = get_permission_manager()
    return Envelope(data={"entries": pm.get_audit_log(limit=limit)})
