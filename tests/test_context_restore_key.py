"""Bug 1 (P0-1): QQ 会话恢复键与写库键不一致 → 突然失忆

根因：message_processor._init_and_restore_context 中
  _restore_id = user_openid or user_id   ← 优先用裸 openid
而写库键是 qq_{openid}（qq_bot_adapter.py:628），查询用裸 openid → DB 返回 0 行 → 失忆。

修复：_restore_id = user_id or user_openid（优先用带 qq_ 前缀的 user_id）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.message_processor import MessageProcessorMixin


def _make_processor(**overrides):
    """构造最小化 mixin 宿主对象，mock 掉所有外部依赖。"""
    proc = MagicMock()
    proc._tool_call_handler = MagicMock()
    proc._tool_call_handler._tool_repair = MagicMock()
    proc.security = MagicMock()
    proc.security.is_allowed = MagicMock(return_value=(True, ""))
    proc.context = MagicMock()
    proc.context.switch_user_context = AsyncMock()
    proc.context.restore_from_db = AsyncMock()
    proc.context.current_address_term = "爸爸"
    proc.db = MagicMock()  # truthy，触发 restore_from_db 分支
    for k, v in overrides.items():
        setattr(proc, k, v)
    return proc


@pytest.mark.asyncio
async def test_restore_id_prefers_user_id_with_qq_prefix():
    """_restore_id 应优先用带 qq_ 前缀的 user_id，与写库键一致。

    写库键：qq_bot_adapter.py:628  user_id = f"qq_{user_openid}"
    恢复键：应使用同一个 user_id，而非裸 openid。
    """
    proc = _make_processor()
    ctx = MagicMock()

    user_openid = "ABCDEF123456"
    user_id = f"qq_{user_openid}"  # 与写库键一致

    await MessageProcessorMixin._init_and_restore_context(
        proc, ctx, "你好", user_id, "qq", None, user_openid, "session-1",
    )

    # switch_user_context 必须用带 qq_ 前缀的 user_id
    assert proc.context.switch_user_context.await_count == 1
    actual_restore_id = proc.context.switch_user_context.await_args.args[0]
    assert actual_restore_id == user_id, (
        f"恢复键应使用 user_id('{user_id}') 与写库键一致，"
        f"但实际用了 '{actual_restore_id}'（裸 openid）→ 会导致 DB 查询 0 行 → 失忆"
    )

    # restore_from_db 也必须用同一个 user_id
    assert proc.context.restore_from_db.await_count == 1
    actual_db_user_id = proc.context.restore_from_db.await_args.kwargs.get("user_id")
    assert actual_db_user_id == user_id, (
        f"restore_from_db 的 user_id 应为 '{user_id}'，实际为 '{actual_db_user_id}'"
    )


@pytest.mark.asyncio
async def test_restore_id_falls_back_to_openid_when_no_user_id():
    """当 user_id 为空时，回退到 user_openid（兼容无前缀场景）。"""
    proc = _make_processor()
    ctx = MagicMock()

    user_openid = "ABCDEF123456"

    await MessageProcessorMixin._init_and_restore_context(
        proc, ctx, "你好", "", "qq", None, user_openid, "session-1",
    )

    assert proc.context.switch_user_context.await_count == 1
    actual_restore_id = proc.context.switch_user_context.await_args.args[0]
    assert actual_restore_id == user_openid
