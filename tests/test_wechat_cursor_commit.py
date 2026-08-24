"""微信 cursor 先提交后处理缺陷回归（后端可靠性小任务 B / T1）。

原缺陷：_poll_messages 在创建消息任务**之前**就推进并持久化游标
（约 729-750 行），且 stop()（613-625 行）会直接取消未完成的消息任务。
后果：崩溃/停机时"游标已推进但消息未处理"，该批消息永久丢失。

修复契约：
1. 批次内全部消息处理完成（成功或标记死信）之后，才把游标推进到
   self._cursor 并持久化；崩溃重启时按旧游标重放未完成批次。
2. stop() 取消未完成消息任务时，先把它们标记为死信（记入 msg_id 去重表），
   保证重启重放的同一批消息被去重拦截，不会对用户造成重复回复。
3. ack/status 兼容：is_connected/is_polling/_msg_tasks 语义不变。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

import wechat_bot_adapter as wba
from wechat_bot_adapter import WeChatBotAdapter


def _make_adapter(**over):
    kwargs = dict(db=object(), router=object(), api=None, user_openid="u", core=None)
    kwargs.update(over)
    # 隔离真实 ~/.ai-agent/wechat_cursor.json（构造函数会读取它）
    with patch.object(WeChatBotAdapter, "_load_cursor", return_value=""):
        return WeChatBotAdapter(**kwargs)


class _FakeClient:
    """get_updates 按脚本回放的 ILinkClient 替身。"""

    def __init__(self, get_updates_seq, bot_token="T1"):
        self._seq = list(get_updates_seq)
        self._bot_token = bot_token
        self.closed = False
        self.cursors: list[str] = []

    async def get_updates(self, cursor):
        self.cursors.append(cursor)
        if not self._seq:
            await asyncio.sleep(3600)  # 无更多数据：挂起等待 cancel
        item = self._seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True


def _msg(msg_id: str, text: str = "hello") -> dict:
    return {
        "msg_id": msg_id,
        "from_user_id": "user_a",
        "context_token": f"ctx-{msg_id}",
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


@pytest.mark.asyncio
async def test_cursor_not_advanced_until_batch_messages_complete(monkeypatch, tmp_path):
    """批次内消息未完成时不得推进/持久化游标；完成后才推进。"""
    bot = _make_adapter()
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")
    monkeypatch.setattr(bot, "_client_owns_credentials", lambda: True)

    client = _FakeClient([
        {"cursor": "CUR-2", "msgs": [_msg("m1"), _msg("m2")]},
    ])
    bot._ilink_client = client
    bot._running = True
    processed: list[str] = []
    release = asyncio.Event()

    async def _slow_process(msg):
        # m1 立即完成，m2 等待 release —— 模拟批次内最后一条仍在处理
        if msg.get("msg_id") == "m2":
            await asyncio.wait_for(release.wait(), timeout=5)
        processed.append(msg.get("msg_id", ""))

    monkeypatch.setattr(bot, "_process_message", _slow_process)

    poll_task = asyncio.ensure_future(bot._poll_messages())
    # 等 m1 完成、m2 挂起
    for _ in range(200):
        await asyncio.sleep(0.01)
        if "m1" in processed:
            break
    await asyncio.sleep(0.05)

    # m2 尚未完成：游标既不能进内存也不能落盘
    assert bot._cursor == "", "批次未完成时不得推进内存游标"
    assert not (tmp_path / "wechat_cursor.json").exists(), "批次未完成时不得持久化游标"

    release.set()
    # 等批次收尾推进游标（poll 循环随后会再次 get_updates 挂起，不会自行退出）
    for _ in range(200):
        await asyncio.sleep(0.01)
        if bot._cursor == "CUR-2":
            break
    # 全部完成后：游标推进 + 持久化
    assert processed == ["m1", "m2"]
    assert bot._cursor == "CUR-2"
    assert (tmp_path / "wechat_cursor.json").exists()
    assert json.loads((tmp_path / "wechat_cursor.json").read_text())["cursor"] == "CUR-2"

    await asyncio.wait_for(bot.stop(), timeout=5)
    await asyncio.wait({poll_task}, timeout=5)


@pytest.mark.asyncio
async def test_stop_marks_unfinished_messages_dead_and_keeps_cursor(monkeypatch, tmp_path):
    """stop 取消未完成消息任务前先记死信，且不推进游标——重启可重放并被去重拦截。"""
    bot = _make_adapter()
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")

    client = _FakeClient([
        {"cursor": "CUR-9", "msgs": [_msg("m1"), _msg("m2")]},
    ])
    bot._ilink_client = client
    bot._running = True
    started: list[str] = []

    async def _hang_process(msg):
        started.append(msg.get("msg_id", ""))
        await asyncio.sleep(3600)  # 模拟 AgentCore 长处理

    monkeypatch.setattr(bot, "_process_message", _hang_process)

    poll_task = asyncio.ensure_future(bot._poll_messages())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(started) == 2:
            break

    await asyncio.wait_for(bot.stop(), timeout=5)
    # 已被 stop() cancel 的任务：用不抛异常的 wait 收尾（勿裸 await 阻塞调度）
    await asyncio.wait({poll_task}, timeout=5)

    # 未完成 → 游标不推进（重启后服务端按旧游标重放本批）
    assert bot._cursor == ""
    assert not (tmp_path / "wechat_cursor.json").exists()

    # 死信已记录：重放的同一 msg_id 会被去重拦截
    assert bot._is_duplicate_msg("m1") is True
    assert bot._is_duplicate_msg("m2") is True


@pytest.mark.asyncio
async def test_crash_replay_reprocesses_incomplete_batch(monkeypatch, tmp_path):
    """回归：停机窗口不丢消息——旧实现先提交游标，重启后该批消息丢失；
    新实现在批次完成前不提交，重启后按旧游标重放，且死信保证不重复回复。"""
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")
    (tmp_path / "wechat_credentials.json").write_text(
        json.dumps({"bot_token": "T1"}), encoding="utf-8")

    client = _FakeClient([
        {"cursor": "CUR-NEXT", "msgs": [_msg("mx", "需要被处理的消息")]},
    ])
    bot = _make_adapter()
    bot._ilink_client = client
    bot._running = True

    async def _never_finish(msg):  # 第一世代：消息永远处理不完就被"杀掉"
        await asyncio.sleep(3600)

    monkeypatch.setattr(bot, "_process_message", _never_finish)
    poll_task = asyncio.ensure_future(bot._poll_messages())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if client.cursors:
            break
    # 崩溃：走 stop 路径（取消 poll 与消息任务）
    await asyncio.wait_for(bot.stop(), timeout=5)
    await asyncio.wait({poll_task}, timeout=5)

    # 崩溃后磁盘上游标文件必须不存在（内存游标仍为 ""）→ 服务端会重放本批
    assert not (tmp_path / "wechat_cursor.json").exists()
    # 第一世代已把 mx 记为死信
    dead_ids = set(bot._processed_msg_ids)

    # 第二世代：新实例从空游标启动，服务端重放同一批消息
    replayed: list[dict] = []

    async def _fast_process(msg):
        # 对齐真实 _process_message：入口处做 msg_id 去重（死信拦截点）
        msg_id = str(msg.get("msg_id", "") or "")
        if msg_id and bot2._is_duplicate_msg(msg_id):
            return None
        replayed.append(dict(msg))
        return None

    bot2 = _make_adapter()
    monkeypatch.setattr(bot2, "_process_message", _fast_process)
    # 同进程"重启"语义：死信缓存随 adapter 重建丢失，但 stop 时已持久化死信；
    # 这里模拟最保守场景——第二世代继承第一世代的去重缓存（同进程内重建）。
    bot2._processed_msg_ids.update(dead_ids)

    client2 = _FakeClient([
        {"cursor": "CUR-NEXT", "msgs": [_msg("mx", "需要被处理的消息")]},
    ])
    bot2._ilink_client = client2
    bot2._running = True

    async def _quick_stop():
        await asyncio.sleep(0.15)
        await bot2.stop()

    stopper = asyncio.ensure_future(_quick_stop())
    poll2 = asyncio.ensure_future(bot2._poll_messages())
    await asyncio.wait({poll2}, timeout=5)
    await stopper

    # 死信 mx 重放时被去重拦截（不重复回复）。该批在第二世代"全部终结"
    # （死信即终态）→ 游标本轮正常推进，不会反复重放同一条死信。
    assert replayed == [], "死信消息重放时应被 msg_id 去重拦截，避免重复回复"
    assert bot2._cursor == "CUR-NEXT"


@pytest.mark.asyncio
async def test_batch_with_failed_message_still_advances_cursor_after_dead_letter(monkeypatch, tmp_path):
    """消息抛异常（已按异常路径终止）视为该条终结：批次完成后仍推进游标，
    不因单条失败而卡住整批游标。"""
    bot = _make_adapter()
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")
    monkeypatch.setattr(bot, "_save_cursor", lambda: None)  # 落盘逻辑单独测过

    client = _FakeClient([
        {"cursor": "CUR-F", "msgs": [_msg("bad"), _msg("good")]},
    ])
    bot._ilink_client = client
    bot._running = True

    async def _process(msg):
        if msg.get("msg_id") == "bad":
            raise RuntimeError("core exploded")
        return None

    monkeypatch.setattr(bot, "_process_message", _process)

    async def _auto_stop():
        await asyncio.sleep(0.3)
        await bot.stop()

    stopper = asyncio.ensure_future(_auto_stop())
    poll_task = asyncio.ensure_future(bot._poll_messages())
    done, pending = await asyncio.wait({poll_task}, timeout=5)
    if poll_task in pending:
        poll_task.cancel()
        await asyncio.wait({poll_task}, timeout=5)
    await stopper

    assert bot._cursor == "CUR-F", "批次全部终结（含失败终态）后应推进游标"

    # 失败那条按死信语义记录（重放不重复处理）
    assert bot._is_duplicate_msg("bad") is True
