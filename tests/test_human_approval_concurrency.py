"""验证 IMApprovalChannel 并发审批请求不相互覆盖。

缺陷 D3: 曾使用 user_id 作为 _pending / _waiters 的 key，导致同一用户并发发起多个审批请求时，
后一个请求会覆盖前一个的 Future，使前一个请求永远失去响应（孤儿 Future）。
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import MagicMock, patch

from security.human_approval import ApprovalRequest, ApprovalStatus, RiskLevel


@pytest.fixture
def channel():
    """构造一个带 mock send_callback 的 IMApprovalChannel。"""
    from security.human_approval import IMApprovalChannel

    async def mock_send(text: str) -> str:
        return "msg_123"

    ch = IMApprovalChannel(send_callback=mock_send, timeout=60)
    return ch


@pytest.mark.asyncio
async def test_concurrent_requests_use_request_id_as_key(channel):
    """同一用户并发发起两个审批请求，应分别独立等待，不相互覆盖。"""
    req1 = ApprovalRequest(
        id="req_1", user_id="user_a", operation="delete_file",
        args={"path": "/tmp/a"}, risk_level=RiskLevel.CRITICAL,
        reason="高危操作",
    )
    req2 = ApprovalRequest(
        id="req_2", user_id="user_a", operation="delete_file",
        args={"path": "/tmp/b"}, risk_level=RiskLevel.CRITICAL,
        reason="高危操作",
    )

    # 并发启动两个请求（但不等待结果，手动触发审批）
    task1 = asyncio.create_task(channel.request_approval(req1))
    task2 = asyncio.create_task(channel.request_approval(req2))

    # 给 event loop 一点时间注册 future
    await asyncio.sleep(0.05)

    # 验证两个 pending 请求都存在（以 request_id 为 key）
    assert channel._pending.get("req_1") is req1
    assert channel._pending.get("req_2") is req2
    assert "req_1" in channel._waiters
    assert "req_2" in channel._waiters
    # 验证 user_id 倒排索引正确维护
    assert set(channel._pending_by_user.get("user_a", [])) == {"req_1", "req_2"}

    # 分别审批两个请求（await async 方法，使用 _classify_reply 能识别的关键词）
    await channel.handle_user_reply("user_a", "确认", request_id="req_1")
    await channel.handle_user_reply("user_a", "取消", request_id="req_2")

    status1 = await task1
    status2 = await task2

    assert status1 == ApprovalStatus.APPROVED
    assert status2 == ApprovalStatus.REJECTED


@pytest.mark.asyncio
async def test_request_id_isolation_between_users(channel):
    """不同用户并发请求，互不干扰。"""
    req_a = ApprovalRequest(
        id="req_a", user_id="user_a", operation="shell_command",
        args={"cmd": "ls"}, risk_level=RiskLevel.HIGH, reason="高危",
    )
    req_b = ApprovalRequest(
        id="req_b", user_id="user_b", operation="shell_command",
        args={"cmd": "pwd"}, risk_level=RiskLevel.HIGH, reason="高危",
    )

    task_a = asyncio.create_task(channel.request_approval(req_a))
    task_b = asyncio.create_task(channel.request_approval(req_b))
    await asyncio.sleep(0.05)

    # user_a 同意自己的请求
    await channel.handle_user_reply("user_a", "确认", request_id="req_a")
    # user_b 拒绝自己的请求
    await channel.handle_user_reply("user_b", "拒绝", request_id="req_b")

    status_a = await task_a
    status_b = await task_b

    assert status_a == ApprovalStatus.APPROVED
    assert status_b == ApprovalStatus.REJECTED


@pytest.mark.asyncio
async def test_timeout_cleans_up_both_pending_and_waiters(channel):
    """超时后应清理 _pending、_waiters 和 _pending_by_user。"""
    req = ApprovalRequest(
        id="req_t", user_id="user_x", operation="delete_file",
        args={}, risk_level=RiskLevel.CRITICAL, reason="测试超时",
    )

    # 将超时设极短
    channel._timeout = 0.01
    status = await channel.request_approval(req)

    assert status == ApprovalStatus.TIMEOUT
    assert "req_t" not in channel._pending
    assert "req_t" not in channel._waiters
    # user_x 的倒排索引也应被清理
    assert channel._pending_by_user.get("user_x") is None or "req_t" not in channel._pending_by_user["user_x"]
