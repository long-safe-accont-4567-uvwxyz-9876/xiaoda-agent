"""P0 安全回归：on_group_add_robot 不应自动绑定拉群者为主人。

背景（v0.5.45）：
``on_group_add_robot`` 旧实现会调用 ``_save_master_openid(op_openid)``，
导致任何把机器人拉进群的 QQ 用户立即获得主人权限（is_owner=True 触发
IMApprovalChannel 的 AUTO_APPROVED 分支，绕过所有高危操作的人工确认），
并被持久化到 ``.env``。这是 v0.4.25 修复的"首个私聊者自动绑主"问题的
同源但更严重的回归（群添加是任何用户都能触发的低门槛动作）。

本测试验证：handler 被调用后 ``MASTER_QQ_OPENID`` 不变、``.env`` 文件
不被创建或写入。
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _FakeEvent:
    """模拟 botpy 的 group_add_robot 事件。"""
    def __init__(self, op_openid: str, group_openid: str) -> None:
        self.op_member_openid = op_openid
        self.group_openid = group_openid


class _StubBot:
    """只挂载我们关心的方法，避开 AIQQBot 完整 __init__ 的重依赖。"""
    on_group_add_robot = None  # 由测试注入


def _make_handler_callable():
    """从 qq_bot_adapter.AIQQBot 取一份 on_group_add_robot 作为可调用。"""
    from qq_bot_adapter import AIQQBot
    # 直接拿 unbound method 即可，避免构造 AIQQBot
    return AIQQBot.on_group_add_robot


async def test_on_group_add_robot_does_not_modify_env(tmp_path, monkeypatch):
    """核心断言：拉群事件后，MASTER_QQ_OPENID 与 .env 文件内容都不变。"""
    handler = _make_handler_callable()

    # 1) 配置临时 .env 路径，确保 _save_master_openid 即使被误调也无法污染真实文件
    env_file = tmp_path / ".env"
    env_file.write_text("MASTER_QQ_OPENID=ORIGINAL_MASTER_123\n", encoding="utf-8-sig")
    monkeypatch.setattr("config.ENV_PATH", str(env_file), raising=False)
    monkeypatch.setenv("MASTER_QQ_OPENID", "ORIGINAL_MASTER_123")

    # 2) 构造一个 stub 实例，绑定 handler
    stub = _StubBot()
    await handler(stub, _FakeEvent(
        op_openid="ATTACKER_OPENID_999",
        group_openid="GROUP_OPENID_555",
    ))

    # 3) 核心断言：MASTER_QQ_OPENID 仍是原来的主人
    assert os.environ.get("MASTER_QQ_OPENID") == "ORIGINAL_MASTER_123", (
        "on_group_add_robot 不应修改 MASTER_QQ_OPENID，"
        f"实际={os.environ.get('MASTER_QQ_OPENID')!r}"
    )

    # 4) 核心断言：.env 文件未被改写，攻击者 openid 不应出现
    after = env_file.read_text(encoding="utf-8-sig")
    assert "ATTACKER_OPENID_999" not in after, (
        f".env 被污染，包含攻击者 openid：\n{after}"
    )
    assert "ORIGINAL_MASTER_123" in after


async def test_on_group_add_robot_creates_no_env_when_missing(tmp_path, monkeypatch):
    """即使 .env 不存在，handler 也不应创建它。"""
    handler = _make_handler_callable()

    env_file = tmp_path / ".env"
    assert not env_file.exists()
    monkeypatch.setattr("config.ENV_PATH", str(env_file), raising=False)
    monkeypatch.delenv("MASTER_QQ_OPENID", raising=False)

    stub = _StubBot()
    await handler(stub, _FakeEvent(
        op_openid="ATTACKER_OPENID_888",
        group_openid="GROUP_OPENID_777",
    ))

    # handler 不应主动创建 .env
    assert not env_file.exists(), (
        f"handler 不应创建 .env，但文件已生成：\n"
        f"{env_file.read_text(encoding='utf-8-sig') if env_file.exists() else ''}"
    )
    # MASTER_QQ_OPENID 仍为空（未配置任何主人）
    assert os.environ.get("MASTER_QQ_OPENID", "") == ""
