"""高危操作人工审批 (S10) — 高危操作需用户确认

参考:
- Human-in-the-loop (HITL) patterns
- AWS IAM approval workflow
- Slack Approval Flow

特性:
- 高危操作自动分类 (RiskClassifier L4)
- 多种审批通道: CLI / WebUI / IM
- 超时自动拒绝 (默认 5 分钟)
- 审批结果回调
- 操作审计日志
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from loguru import logger


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    AUTO_APPROVED = "auto_approved"   # 白名单用户


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# 默认高危操作清单
HIGH_RISK_OPERATIONS = {
    "shell_command": RiskLevel.HIGH,
    "execute_code": RiskLevel.HIGH,
    "delete_file": RiskLevel.CRITICAL,
    "write_file": RiskLevel.MEDIUM,
    "install_package": RiskLevel.HIGH,
    "send_email": RiskLevel.MEDIUM,
    "make_payment": RiskLevel.CRITICAL,
    "share_data": RiskLevel.HIGH,
    "modify_config": RiskLevel.MEDIUM,
    "restart_service": RiskLevel.HIGH,
}


@dataclass
class ApprovalRequest:
    """审批请求"""
    id: str
    user_id: str
    operation: str
    args: dict
    risk_level: RiskLevel
    reason: str                       # 为什么是高危
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0            # 0 = 使用默认超时
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: Optional[str] = None
    decided_at: float = 0
    decision_reason: str = ""

    @property
    def is_expired(self) -> bool:
        """返回审批请求是否已过期 (未设过期时间视为未过期)."""
        if self.expires_at == 0:
            return False
        return time.time() > self.expires_at


@dataclass
class ApprovalResult:
    """审批结果"""
    request_id: str
    status: ApprovalStatus
    decided_by: Optional[str] = None
    reason: str = ""


class HumanApprovalGate:
    """人工审批门控

    用法:
        gate = HumanApprovalGate()
        # 高危操作前先请求审批
        req = await gate.request(
            user_id="u1",
            operation="delete_file",
            args={"path": "/important"},
            risk_level=RiskLevel.CRITICAL,
            reason="File deletion is irreversible",
        )
        # 等待用户决定
        result = await gate.wait_for_decision(req.id, timeout=300)
        if result.status == ApprovalStatus.APPROVED:
            await execute_delete(...)
    """

    def __init__(self, default_timeout: float = 300.0) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._waiters: dict[str, asyncio.Future] = {}
        self._callbacks: list[Callable] = []
        self._default_timeout = default_timeout
        self._auto_approve_users: set[str] = set()  # owner
        self._audit_log: list[dict] = []

    def register_auto_approve_user(self, user_id: str) -> None:
        """注册白名单用户 (高危操作自动通过)"""
        self._auto_approve_users.add(user_id)

    def is_high_risk(self, operation: str) -> bool:
        """判断操作是否高危"""
        level = HIGH_RISK_OPERATIONS.get(operation, RiskLevel.LOW)
        return level in (RiskLevel.HIGH, RiskLevel.CRITICAL)

    def get_risk_level(self, operation: str) -> RiskLevel:
        """返回操作的风险级别, 未知操作返回 LOW."""
        return HIGH_RISK_OPERATIONS.get(operation, RiskLevel.LOW)

    async def request(self, user_id: str, operation: str,
                       args: Optional[dict] = None,
                       risk_level: Optional[RiskLevel] = None,
                       reason: str = "",
                       timeout: Optional[float] = None) -> ApprovalRequest:
        """发起审批请求"""
        risk = risk_level or self.get_risk_level(operation)

        # 白名单用户自动通过
        if user_id in self._auto_approve_users:
            req = ApprovalRequest(
                id=str(uuid.uuid4()),
                user_id=user_id, operation=operation,
                args=args or {}, risk_level=risk, reason=reason,
                status=ApprovalStatus.AUTO_APPROVED,
                decided_by="auto(owner)", decided_at=time.time(),
                decision_reason="User is in auto-approve whitelist",
            )
            self._audit_log.append({
                "request_id": req.id, "user_id": user_id,
                "operation": operation, "status": req.status.value,
                "ts": time.time(),
            })
            return req

        req = ApprovalRequest(
            id=str(uuid.uuid4()),
            user_id=user_id, operation=operation,
            args=args or {}, risk_level=risk, reason=reason,
            expires_at=time.time() + (timeout or self._default_timeout),
        )
        self._requests[req.id] = req

        # 通知回调 (用于推送到 WebUI / IM)
        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(req)
                else:
                    cb(req)
            except Exception as e:
                logger.warning(f"ApprovalGate.callback_failed: {e}")

        logger.info(f"ApprovalGate.request id={req.id} op={operation} "
                     f"risk={risk.value} user={user_id}")
        return req

    async def wait_for_decision(self, request_id: str,
                                  timeout: Optional[float] = None
                                  ) -> ApprovalResult:
        """等待审批决定"""
        req = self._requests.get(request_id)
        if not req:
            return ApprovalResult(request_id=request_id,
                                    status=ApprovalStatus.REJECTED,
                                    reason="Request not found")

        if req.status != ApprovalStatus.PENDING:
            return ApprovalResult(request_id=request_id, status=req.status,
                                    decided_by=req.decided_by,
                                    reason=req.decision_reason)

        # 注册 future
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._waiters[request_id] = future

        # 等待
        effective_timeout = timeout or (req.expires_at - time.time())
        try:
            result = await asyncio.wait_for(future, timeout=max(0.1, effective_timeout))
            return result
        except asyncio.TimeoutError:
            req.status = ApprovalStatus.TIMEOUT
            req.decided_at = time.time()
            req.decision_reason = "Approval timed out"
            self._audit_log.append({
                "request_id": req.id, "user_id": req.user_id,
                "operation": req.operation, "status": "timeout", "ts": time.time(),
            })
            return ApprovalResult(request_id=request_id,
                                    status=ApprovalStatus.TIMEOUT,
                                    reason="Approval timed out")

    def decide(self, request_id: str, decision: ApprovalStatus,
                decided_by: str, reason: str = "") -> bool:
        """作出决定 (由审批 UI 调用)"""
        req = self._requests.get(request_id)
        if not req or req.status != ApprovalStatus.PENDING:
            return False
        req.status = decision
        req.decided_by = decided_by
        req.decided_at = time.time()
        req.decision_reason = reason

        # 唤醒等待者
        future = self._waiters.pop(request_id, None)
        if future and not future.done():
            future.set_result(ApprovalResult(
                request_id=request_id, status=decision,
                decided_by=decided_by, reason=reason,
            ))

        self._audit_log.append({
            "request_id": req.id, "user_id": req.user_id,
            "operation": req.operation, "status": decision.value,
            "decided_by": decided_by, "ts": time.time(),
        })
        logger.info(f"ApprovalGate.decided id={request_id} "
                     f"status={decision.value} by={decided_by}")
        return True

    def on_request(self, callback: Callable) -> None:
        """注册请求回调"""
        self._callbacks.append(callback)

    def get_pending_requests(self, user_id: Optional[str] = None) -> list[ApprovalRequest]:
        """获取待审批请求"""
        return [r for r in self._requests.values()
                 if r.status == ApprovalStatus.PENDING
                 and (user_id is None or r.user_id == user_id)
                 and not r.is_expired]

    def cleanup_expired(self) -> int:
        """清理过期请求"""
        n = 0
        for r in list(self._requests.values()):
            if r.is_expired and r.status == ApprovalStatus.PENDING:
                r.status = ApprovalStatus.TIMEOUT
                r.decision_reason = "Auto-expired"
                n += 1
                future = self._waiters.pop(r.id, None)
                if future and not future.done():
                    future.set_result(ApprovalResult(
                        request_id=r.id, status=ApprovalStatus.TIMEOUT,
                        reason="Auto-expired",
                    ))
        return n

    def stats(self) -> dict:
        """返回审批门控统计 (各状态请求数与审计日志大小)."""
        return {
            "total_requests": len(self._requests),
            "pending": sum(1 for r in self._requests.values()
                              if r.status == ApprovalStatus.PENDING),
            "approved": sum(1 for r in self._requests.values()
                              if r.status == ApprovalStatus.APPROVED),
            "rejected": sum(1 for r in self._requests.values()
                              if r.status == ApprovalStatus.REJECTED),
            "timeout": sum(1 for r in self._requests.values()
                              if r.status == ApprovalStatus.TIMEOUT),
            "audit_log_size": len(self._audit_log),
        }


# 全局单例
_gate: Optional[HumanApprovalGate] = None


def get_approval_gate() -> HumanApprovalGate:
    """获取全局 HumanApprovalGate 单例, 不存在时创建."""
    global _gate
    if _gate is None:
        _gate = HumanApprovalGate()
    return _gate


class IMApprovalChannel:
    """IM 平台（QQ/微信）两段式确认通道

    流程：
    1. 高危操作触发时，Bot 发送"⚠️ 即将执行 X，回复 确认/取消（60s 超时）"
    2. 等待用户回复 "确认" / "取消"
    3. 60s 超时自动取消
    4. 主人（is_owner=true）白名单跳过确认
    """

    # 关键词匹配（小写包含即视为该决定；confirm 先于 reject 检查）
    _CONFIRM_KEYWORDS: tuple[str, ...] = ("确认", "确定", "yes", "y")
    _REJECT_KEYWORDS: tuple[str, ...] = ("取消", "拒绝", "no", "n")

    def __init__(self, send_callback: Callable[[str], Any],
                 timeout: float = 60.0) -> None:
        """
        :param send_callback: async callable，用于向用户发送消息
        :param timeout: 超时秒数（默认 60）
        """
        self._send_callback = send_callback
        self._timeout = timeout
        self._pending: dict[str, ApprovalRequest] = {}
        self._waiters: dict[str, asyncio.Future] = {}
        self._audit_log: list[dict] = []

    def _classify_reply(self, text: str) -> Optional[ApprovalStatus]:
        """将用户回复文本分类为 APPROVED / REJECTED / None。"""
        if not text:
            return None
        lower = text.strip().lower()
        if not lower:
            return None
        for kw in self._CONFIRM_KEYWORDS:
            if kw in lower:
                return ApprovalStatus.APPROVED
        for kw in self._REJECT_KEYWORDS:
            if kw in lower:
                return ApprovalStatus.REJECTED
        return None

    def _audit(self, req: ApprovalRequest, status: ApprovalStatus,
               decided_by: str, reason: str) -> None:
        """记录审计日志。"""
        self._audit_log.append({
            "request_id": req.id, "user_id": req.user_id,
            "operation": req.operation, "status": status.value,
            "decided_by": decided_by, "reason": reason,
            "ts": time.time(),
        })
        logger.info("approval.im_channel",
                    request_id=req.id, user_id=req.user_id,
                    operation=req.operation, status=status.value,
                    decided_by=decided_by, reason=reason)

    async def request_approval(self, req: ApprovalRequest,
                                 is_owner: bool = False) -> ApprovalStatus:
        """请求用户审批"""
        # 主人白名单跳过
        if is_owner:
            req.status = ApprovalStatus.AUTO_APPROVED
            req.decided_by = "auto(owner)"
            req.decision_reason = "Owner whitelist auto-approved"
            req.decided_at = time.time()
            self._audit(req, ApprovalStatus.AUTO_APPROVED,
                        "auto(owner)", req.decision_reason)
            return ApprovalStatus.AUTO_APPROVED

        # 发送确认请求消息
        prompt = (f"⚠️ 即将执行高危操作：{req.operation}\n"
                  f"原因：{req.reason or '未提供'}\n"
                  f"回复「确认」继续，或「取消」放弃"
                  f"（{int(self._timeout)}s 超时自动取消）")
        try:
            await self._send_callback(prompt)
        except Exception as e:
            logger.warning("approval.im_channel.send_failed error={}", str(e)[:200])

        # 注册 pending 请求与 future
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[req.user_id] = req
        self._waiters[req.user_id] = future

        try:
            status = await asyncio.wait_for(future, timeout=self._timeout)
            # handle_user_reply 已设置 req.status，返回 future 结果
            if isinstance(status, ApprovalStatus):
                return status
            return req.status
        except asyncio.TimeoutError:
            req.status = ApprovalStatus.TIMEOUT
            req.decision_reason = f"User did not reply within {self._timeout}s"
            req.decided_at = time.time()
            self._audit(req, ApprovalStatus.TIMEOUT,
                        "system", req.decision_reason)
            return ApprovalStatus.TIMEOUT
        finally:
            self._pending.pop(req.user_id, None)
            self._waiters.pop(req.user_id, None)

    async def handle_user_reply(self, user_id: str, text: str) -> bool:
        """处理用户回复（"确认"/"取消"），返回是否匹配待审批请求"""
        req = self._pending.get(user_id)
        if req is None:
            return False
        decision = self._classify_reply(text)
        if decision is None:
            return False
        reason = ("User confirmed" if decision == ApprovalStatus.APPROVED
                  else "User rejected")
        req.status = decision
        req.decided_by = user_id
        req.decided_at = time.time()
        req.decision_reason = reason
        future = self._waiters.pop(user_id, None)
        if future is not None and not future.done():
            future.set_result(decision)
        self._audit(req, decision, user_id, reason)
        self._pending.pop(user_id, None)
        return True
