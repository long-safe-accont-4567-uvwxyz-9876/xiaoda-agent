"""微信死信持久化回归（2026-08-29 丢消息修复 2）。

原缺陷：stop()/异常路径把未完成消息的 msg_id 记入内存去重表（死信），
无任何持久化——进程重启后新 adapter 加载旧游标却无死信，同消息重复处理。

修复契约（游标文件扩展为 {"cursor": str, "dead": {msg_id: ts}}）：
1. 死信落盘 → 同目录新建 adapter → 同 msg_id 被去重拦截。
2. 落盘死信超过 TTL（1h）→ 新实例不拦截；损坏条目忽略。
3. 与 R3-Major#1 token 归属校验共存：凭证已被重新扫码更新（token 不一致）
   时，旧会话死信不得写进新会话的游标文件。
4. 推进游标落盘时保留死信表（两表同源，互不抹除）。
5. 旧格式（仅 {"cursor"}）文件向后兼容加载。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

import wechat_bot_adapter as wba
from wechat_bot_adapter import WeChatBotAdapter


def _make_adapter(**over):
    """构造 adapter（CREDENTIALS_PATH 须先指向 tmp，_load_cursor 走真实读取）。"""
    kwargs = dict(db=object(), router=object(), api=None, user_openid="u", core=None)
    kwargs.update(over)
    return WeChatBotAdapter(**kwargs)


def _write_creds(tmp_path, token: str) -> None:
    (tmp_path / "wechat_credentials.json").write_text(
        json.dumps({"bot_token": token}), encoding="utf-8")


async def _mark_dead_and_flush(bot, msg_id: str) -> None:
    """挂起一条"处理中"消息并走真实 _mark_msg_dead_by_task 记死信，排空落盘。"""

    async def _hang():
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_hang())
    await asyncio.sleep(0)  # 让任务启动
    bot._task_msg_ids[task] = msg_id
    bot._mark_msg_dead_by_task(task)
    if bot._dead_save_task is not None:
        await asyncio.wait({bot._dead_save_task}, timeout=5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_dead_letter_persisted_and_reloaded_across_instances(monkeypatch, tmp_path):
    """死信落盘 → 同目录新建 adapter → 同 msg_id 被拦截（跨实例恢复）。"""
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")
    _write_creds(tmp_path, "T1")

    bot = _make_adapter()
    bot._ilink_client = SimpleNamespace(_bot_token="T1")
    await _mark_dead_and_flush(bot, "mx")
    assert bot._is_duplicate_msg("mx") is True, "本世代内存死信即拦"

    cursor_file = tmp_path / "wechat_cursor.json"
    on_disk = json.loads(cursor_file.read_text(encoding="utf-8"))
    assert on_disk["dead"].get("mx"), "死信应已随游标文件落盘"

    # 第二世代：同目录新建实例（构造函数真实读取游标文件并合并死信表）
    bot2 = _make_adapter()
    assert bot2._is_duplicate_msg("mx") is True, "重启后重放的同一条死信应被拦截"


@pytest.mark.asyncio
async def test_dead_save_repeats_when_new_dead_arrives_during_write(monkeypatch, tmp_path):
    """写盘在途时新增死信，revision 循环必须再写一轮覆盖最新状态。"""
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")
    _write_creds(tmp_path, "T1")
    bot = _make_adapter()
    bot._ilink_client = SimpleNamespace(_bot_token="T1")
    first_started = threading.Event()
    release_first = threading.Event()
    writes: list[str] = []
    import utils.wechat_cursor_state as cursor_state
    real_atomic_write = cursor_state.atomic_write

    def blocking_atomic_write(path, content, **kwargs):
        writes.append(content)
        if len(writes) == 1:
            first_started.set()
            assert release_first.wait(timeout=5)
        real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(cursor_state, "atomic_write", blocking_atomic_write)
    task1 = asyncio.create_task(asyncio.sleep(3600))
    task2 = asyncio.create_task(asyncio.sleep(3600))
    bot._task_msg_ids[task1] = "m1"
    bot._mark_msg_dead_by_task(task1)
    assert await asyncio.to_thread(first_started.wait, 5)
    bot._task_msg_ids[task2] = "m2"
    bot._mark_msg_dead_by_task(task2)
    release_first.set()
    await bot._dead_save_task
    task1.cancel()
    task2.cancel()
    await asyncio.gather(task1, task2, return_exceptions=True)

    data = json.loads((tmp_path / "wechat_cursor.json").read_text(encoding="utf-8"))
    assert len(writes) == 2
    assert set(data["dead"]) == {"m1", "m2"}


@pytest.mark.asyncio
async def test_stop_waits_for_dead_save_before_closing_client(monkeypatch, tmp_path):
    """stop 必须先排空死信落盘，再关闭提供 token 归属的 iLinkClient。"""
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")
    _write_creds(tmp_path, "T1")
    bot = _make_adapter()
    order: list[str] = []

    class Client(SimpleNamespace):
        async def close(self):
            order.append("close")

    bot._ilink_client = Client(_bot_token="T1")

    async def save_dead():
        await asyncio.sleep(0)
        order.append("save")

    bot._dead_save_task = asyncio.create_task(save_dead())
    await bot.stop()
    assert order == ["save", "close"]


@pytest.mark.asyncio
async def test_expired_or_corrupt_dead_entries_not_loaded(monkeypatch, tmp_path):
    """超 TTL 的死信条目加载时丢弃（不拦截）；损坏条目忽略。"""
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")
    _write_creds(tmp_path, "T1")
    now = time.time()
    (tmp_path / "wechat_cursor.json").write_text(json.dumps({
        "cursor": "CUR",
        "dead": {"fresh": now - 60, "stale": now - 7200, "garbage": "not-a-number"},
    }), encoding="utf-8")

    bot = _make_adapter()
    assert bot._cursor == "CUR"
    assert bot._is_duplicate_msg("fresh") is True
    assert bot._is_duplicate_msg("stale") is False, "超 TTL（1h）死信不得拦截"
    assert bot._is_duplicate_msg("garbage") is False, "损坏时间戳条目应忽略"


@pytest.mark.asyncio
async def test_dead_save_skipped_when_token_changed(monkeypatch, tmp_path):
    """token 归属校验共存：凭证已更新为新 token（重新扫码）时，
    旧会话死信不得写进新会话的游标文件（R3-Major#1 同款语义）。"""
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")
    _write_creds(tmp_path, "T2")  # 凭证文件已是新会话 token

    bot = _make_adapter()
    bot._ilink_client = SimpleNamespace(_bot_token="T1")  # 本实例仍是旧 token
    await _mark_dead_and_flush(bot, "mx-old")

    # 内存死信仍有效（本进程内拦截），但不得落盘污染新会话文件
    assert bot._is_duplicate_msg("mx-old") is True
    assert not (tmp_path / "wechat_cursor.json").exists(), "token 变更后不得写旧死信"


def test_cursor_save_keeps_dead_table(monkeypatch, tmp_path):
    """推进游标落盘时保留当前死信表——cursor 与 dead 两表同源、互不抹除。"""
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")
    _write_creds(tmp_path, "T1")
    bot = _make_adapter()
    bot._ilink_client = SimpleNamespace(_bot_token="T1")
    bot._processed_msg_ids["mk"] = 111.0
    bot._cursor = "CUR-2"
    bot._save_cursor_sync()

    data = json.loads(
        (tmp_path / "wechat_cursor.json").read_text(encoding="utf-8"))
    assert data == {"cursor": "CUR-2", "dead": {"mk": 111.0}}


def test_legacy_cursor_file_without_dead_key(monkeypatch, tmp_path):
    """旧格式（仅 {"cursor"}）文件向后兼容：正常加载游标，dead 缺省为空。"""
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")
    _write_creds(tmp_path, "T1")
    (tmp_path / "wechat_cursor.json").write_text(
        json.dumps({"cursor": "OLD"}), encoding="utf-8")

    bot = _make_adapter()
    assert bot._cursor == "OLD"
    assert bot._processed_msg_ids == {}
