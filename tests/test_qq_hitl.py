"""QQ 人工审批 (HITL) 两段式确认单元测试.

覆盖场景:
- 主人白名单自动通过
- 用户确认 / 取消
- 超时自动取消
- 无关回复不匹配
- 无 pending 请求时返回 False
- 关键词变体（确定/yes/y）
- 审计日志正确记录
- send_callback 被调用
- 取消后 pending 被清理
"""
import asyncio
import sys
from pathlib import Path

import pytest

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from security.human_approval import (
    ApprovalRequest, ApprovalStatus, RiskLevel, IMApprovalChannel,
)


def _make_req(user_id: str = "u1", operation: str = "delete_file",
              risk_level: RiskLevel = RiskLevel.CRITICAL) -> ApprovalRequest:
    """构造测试用 ApprovalRequest。"""
    return ApprovalRequest(
        id="req-" + user_id,
        user_id=user_id,
        operation=operation,
        args={},
        risk_level=risk_level,
        reason="unit test high-risk op",
    )


# ── 主人白名单 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_approve_for_owner():
    """主人白名单跳过确认：is_owner=True 时直接 AUTO_APPROVED，不发送确认消息。"""
    sent: list[str] = []

    async def send_cb(text: str) -> None:
        sent.append(text)

    ch = IMApprovalChannel(send_callback=send_cb, timeout=5.0)
    req = _make_req()
    status = await ch.request_approval(req, is_owner=True)

    assert status == ApprovalStatus.AUTO_APPROVED
    # 主人不应触发确认消息
    assert sent == []
    # 审计日志应记录 auto_approved
    assert len(ch._audit_log) == 1
    assert ch._audit_log[0]["status"] == "auto_approved"


# ── 用户确认 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_confirm():
    """用户回复"确认"后通过。"""
    sent: list[str] = []

    async def send_cb(text: str) -> None:
        sent.append(text)

    ch = IMApprovalChannel(send_callback=send_cb, timeout=5.0)
    req = _make_req(user_id="confirm-user")

    task = asyncio.create_task(ch.request_approval(req, is_owner=False))
    # 等待 send_callback 执行 + pending 注册
    await asyncio.sleep(0.05)

    # 确认消息已发送
    assert len(sent) == 1
    assert "高危操作" in sent[0]
    assert "delete_file" in sent[0]

    matched = await ch.handle_user_reply("confirm-user", "确认")
    assert matched is True

    status = await task
    assert status == ApprovalStatus.APPROVED


# ── 用户取消 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_reject():
    """用户回复"取消"后拒绝。"""
    async def send_cb(text: str) -> None:
        pass

    ch = IMApprovalChannel(send_callback=send_cb, timeout=5.0)
    req = _make_req(user_id="reject-user")

    task = asyncio.create_task(ch.request_approval(req, is_owner=False))
    await asyncio.sleep(0.05)

    matched = await ch.handle_user_reply("reject-user", "取消")
    assert matched is True

    status = await task
    assert status == ApprovalStatus.REJECTED


# ── 超时 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_timeout():
    """超时自动取消：60s（测试用 0.1s）超时返回 TIMEOUT。"""
    async def send_cb(text: str) -> None:
        pass

    ch = IMApprovalChannel(send_callback=send_cb, timeout=0.1)
    req = _make_req(user_id="timeout-user")

    status = await ch.request_approval(req, is_owner=False)
    assert status == ApprovalStatus.TIMEOUT
    # 超时后 pending 应被清理
    assert "timeout-user" not in ch._pending
    # 审计日志记录 timeout
    assert any(e["status"] == "timeout" for e in ch._audit_log)


# ── 无关回复 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_reply_ignored():
    """无关回复不匹配：返回 False，请求仍在 pending。"""
    async def send_cb(text: str) -> None:
        pass

    ch = IMApprovalChannel(send_callback=send_cb, timeout=5.0)
    req = _make_req(user_id="invalid-user")

    task = asyncio.create_task(ch.request_approval(req, is_owner=False))
    await asyncio.sleep(0.05)

    matched = await ch.handle_user_reply("invalid-user", "今天天气怎么样")
    assert matched is False
    # 请求仍在 pending
    assert "invalid-user" in ch._pending

    # 清理：用取消结束
    await ch.handle_user_reply("invalid-user", "取消")
    await task


# ── 无 pending 请求 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_pending_request():
    """无 pending 请求时 handle_user_reply 返回 False。"""
    async def send_cb(text: str) -> None:
        pass

    ch = IMApprovalChannel(send_callback=send_cb, timeout=5.0)
    matched = await ch.handle_user_reply("nobody", "确认")
    assert matched is False


# ── 关键词变体 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_keyword_variants():
    """关键词变体："确定"/"yes"/"y" 视为同意；"no"/"n" 视为拒绝。"""
    confirm_words = ("确定", "yes", "y", "YES", "Y")
    reject_words = ("no", "n", "NO", "拒绝")

    for kw in confirm_words:
        async def send_cb(text: str) -> None:
            pass
        ch = IMApprovalChannel(send_callback=send_cb, timeout=5.0)
        uid = f"kw-{kw}"
        req = _make_req(user_id=uid)
        task = asyncio.create_task(ch.request_approval(req, is_owner=False))
        await asyncio.sleep(0.05)
        matched = await ch.handle_user_reply(uid, kw)
        assert matched is True, f"确认关键词 {kw!r} 应匹配"
        status = await task
        assert status == ApprovalStatus.APPROVED, f"关键词 {kw!r} 应视为同意"

    for kw in reject_words:
        async def send_cb(text: str) -> None:
            pass
        ch = IMApprovalChannel(send_callback=send_cb, timeout=5.0)
        uid = f"kw-{kw}"
        req = _make_req(user_id=uid)
        task = asyncio.create_task(ch.request_approval(req, is_owner=False))
        await asyncio.sleep(0.05)
        matched = await ch.handle_user_reply(uid, kw)
        assert matched is True, f"拒绝关键词 {kw!r} 应匹配"
        status = await task
        assert status == ApprovalStatus.REJECTED, f"关键词 {kw!r} 应视为拒绝"


# ── 审计日志 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_log():
    """审计日志正确记录：request_id / user_id / operation / status / decided_by / reason。"""
    sent: list[str] = []

    async def send_cb(text: str) -> None:
        sent.append(text)

    ch = IMApprovalChannel(send_callback=send_cb, timeout=5.0)
    req = _make_req(user_id="audit-user", operation="install_package")

    task = asyncio.create_task(ch.request_approval(req, is_owner=False))
    await asyncio.sleep(0.05)
    await ch.handle_user_reply("audit-user", "确认")
    status = await task

    assert status == ApprovalStatus.APPROVED
    assert len(ch._audit_log) == 1
    entry = ch._audit_log[0]
    assert entry["request_id"] == req.id
    assert entry["user_id"] == "audit-user"
    assert entry["operation"] == "install_package"
    assert entry["status"] == "approved"
    assert entry["decided_by"] == "audit-user"
    assert "confirmed" in entry["reason"].lower()


# ── send_callback 被调用 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_send_callback_invoked():
    """非主人审批时 send_callback 被调用，消息包含操作名和超时提示。"""
    sent: list[str] = []

    async def send_cb(text: str) -> None:
        sent.append(text)

    ch = IMApprovalChannel(send_callback=send_cb, timeout=0.3)
    req = _make_req(user_id="cb-user", operation="restart_service")

    task = asyncio.create_task(ch.request_approval(req, is_owner=False))
    await asyncio.sleep(0.05)

    assert len(sent) == 1
    assert "restart_service" in sent[0]
    assert "高危操作" in sent[0]
    # 超时提示
    assert "0" in sent[0]  # timeout 秒数

    await task  # 等待超时清理


# ── pending 清理 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pending_cleared_after_decision():
    """用户做出决定后 pending 和 waiters 被清理。"""
    async def send_cb(text: str) -> None:
        pass

    ch = IMApprovalChannel(send_callback=send_cb, timeout=5.0)
    req = _make_req(user_id="clear-user")

    task = asyncio.create_task(ch.request_approval(req, is_owner=False))
    await asyncio.sleep(0.05)
    assert "clear-user" in ch._pending

    await ch.handle_user_reply("clear-user", "取消")
    await task

    # 决定后 pending 和 waiters 应为空
    assert "clear-user" not in ch._pending
    assert "clear-user" not in ch._waiters
