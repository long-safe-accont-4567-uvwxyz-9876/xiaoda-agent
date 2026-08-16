"""A2：微信分段流式发送 / session_id / status_callback 与分片工具下沉测试。

覆盖：
1. 分片工具下沉：qq_bot_adapter.AIQQBot 与 channel_adapter_base.ChannelAdapterBase
   共享同一实现（继承同对象），300 字符切分 / 4 段 cap / 7800 字节重切行为一致。
2. 微信 _handle_text_message：
   - 长回复（>400 字符）分段流式发送：顺序/数量正确、段间合并逻辑正确
   - 短回复仍走单条 send_message（行为不变）
   - process 收到 session_id（get_session/create_session 缓存命中）
   - DB 异常兜底 wechat_tmp_{from_user_id[:16]}
   - process 收到可调用的 status_callback，最近状态写入 _last_status_by_user
"""
import asyncio
import sqlite3
import time
from unittest.mock import AsyncMock, patch

from channel_adapter_base import STREAM_C2C_MAX_SEGMENTS, ChannelAdapterBase
from qq_bot_adapter import AIQQBot
from wechat_bot_adapter import WeChatBotAdapter

FROM_USER = "user@im.wechat"


class FakeResult:
    def __init__(self, reply, sticker_path=None):
        self.reply = reply
        self.sticker_path = sticker_path


class FakeCore:
    """伪造 AgentCore：提供 get_session/create_session 并记录 process 调用参数。"""

    def __init__(self, result):
        self._result = result
        self.process_calls = []
        self.get_session_calls = 0
        self.create_session_calls = 0
        self.sessions = {}  # user_openid -> {"id": sid}

    async def process(self, text, user_id="", source="", user_openid="",
                      session_id=None, status_callback=None, **kwargs):
        self.process_calls.append({
            "text": text,
            "user_id": user_id,
            "source": source,
            "user_openid": user_openid,
            "session_id": session_id,
            "status_callback": status_callback,
        })
        return self._result

    async def get_session(self, user_openid):
        self.get_session_calls += 1
        return self.sessions.get(user_openid)

    async def create_session(self, user_openid=""):
        self.create_session_calls += 1
        sid = f"wx_sess_{user_openid[:8]}_{self.create_session_calls}"
        self.sessions[user_openid] = {"id": sid}
        return sid


def _make_adapter(core):
    """构造不调用 __init__ 的 WeChatBotAdapter，补齐 A2 新增状态。"""
    adapter = WeChatBotAdapter.__new__(WeChatBotAdapter)
    adapter._core = core
    adapter._ilink_client = None  # 测试中一律 monkeypatch send_message，不经过 ILinkClient
    adapter._last_from_user_id = FROM_USER
    adapter._ctx_by_user = {FROM_USER: "ctx_tok"}
    adapter._expired = False
    # A2 新增状态（正常实例由 __init__ 初始化，__new__ 路径手动补齐）
    adapter._user_session_cache = {}
    adapter._user_session_cache_ts = {}
    adapter._USER_SESSION_CACHE_TTL = 3600
    adapter._USER_SESSION_CACHE_MAX_SIZE = 1000
    adapter._last_status_by_user = {}
    adapter._last_status_by_user_ts = {}
    adapter._USER_STATUS_TTL = 3600
    return adapter


def _run_handle(adapter, text="你好", monkeypatch=None, send_impl=None):
    """运行 _handle_text_message；send_impl(sent, to_user_id, content) -> bool 可选编排。"""
    sent = []

    async def _record(content, msg_type="text", to_user_id="", context_token=""):
        if send_impl is None:
            sent.append((to_user_id, content))
            return True
        return await send_impl(sent, to_user_id, content)

    monkeypatch.setattr(adapter, "send_message", _record)
    with patch("wechat_bot_adapter.asyncio.sleep", AsyncMock()):
        asyncio.run(adapter._handle_text_message(text, FROM_USER, "ctx_tok"))
    return sent


# ---------------------------------------------------------------------------
# 1. 分片工具下沉：QQ 与基类共享同一实现
# ---------------------------------------------------------------------------

def test_split_tools_shared_same_objects():
    """QQ 侧三个分片工具应为基类方法的同一函数对象（继承，未再定义）。"""
    assert AIQQBot._split_text_by_bytes is ChannelAdapterBase._split_text_by_bytes
    assert AIQQBot._split_text_for_streaming is ChannelAdapterBase._split_text_for_streaming
    assert AIQQBot._cap_stream_segments is ChannelAdapterBase._cap_stream_segments
    assert STREAM_C2C_MAX_SEGMENTS == 4


def test_split_text_for_streaming_300_chars():
    """300 字符切分：600 字符文本切为 2 片各 300 字符，可无损还原。"""
    bot = AIQQBot.__new__(AIQQBot)
    text = "今天天气真好" * 100  # 600 字符
    segments = bot._split_text_for_streaming(text, 300)
    assert len(segments) == 2
    assert len(segments[0]) == 300
    assert len(segments[1]) == 300
    assert "".join(segments) == text


def test_cap_stream_segments_c2c_4_segments():
    """4 段 cap：超出部分合并到最后一片并按 7800 字节重切，前缀保持不变。"""
    bot = AIQQBot.__new__(AIQQBot)
    text = "甲乙丙丁" * 400  # 1600 字符 → 约 6 片
    segments = bot._split_text_for_streaming(text, 300)
    assert len(segments) > 4
    capped = bot._cap_stream_segments(segments, False, "t.resplit", "t.capped")
    assert capped[:3] == segments[:3]
    assert "".join(capped) == text
    assert all(len(s.encode("utf-8")) <= 7800 for s in capped)


def test_split_text_by_bytes_7800_resplit():
    """7800 字节重切：每片 ≤ 7800 字节，代码块被截断时闭合（成对 ```），不破坏后续片。

    注意：_split_text_by_bytes 在代码块被截断时会主动闭合/重开代码围栏（对齐 QQ
    原行为），因此含围栏文本的 join 不等于原文（会多出一对 ```）；无围栏文本才
    严格无损。这里分别断言两种约束。
    """
    bot = AIQQBot.__new__(AIQQBot)
    text = "```python\n" + ("打印内容😀\n" * 2000) + "```"
    pieces = bot._split_text_by_bytes(text, 7800)
    assert len(pieces) > 1
    assert all(len(p.encode("utf-8")) <= 7800 for p in pieces)
    for piece in pieces:
        assert piece.count("```") % 2 == 0
    # 无代码围栏的纯文本重切严格无损还原
    plain = "打印内容😀\n" * 2000
    plain_pieces = bot._split_text_by_bytes(plain, 7800)
    assert "".join(plain_pieces) == plain
    assert all(len(p.encode("utf-8")) <= 7800 for p in plain_pieces)


# ---------------------------------------------------------------------------
# 2. 微信分段流式发送
# ---------------------------------------------------------------------------

def test_short_reply_still_single_send(monkeypatch):
    """短回复（<400 字符）仍走单条 send_message：ACK + 回复共 2 条（行为不变）。"""
    core = FakeCore(FakeResult("短回复"))
    adapter = _make_adapter(core)
    sent = _run_handle(adapter, monkeypatch=monkeypatch)
    assert len(sent) == 2
    assert sent[1][1] == "短回复"


def test_long_reply_streams_segments_in_order(monkeypatch):
    """长回复（>400 字符）分段发送：非 ACK 部分按序拼接等于原文。"""
    long_reply = "今天天气真好呀" * 60  # 420 字符 → 2 片
    core = FakeCore(FakeResult(long_reply))
    adapter = _make_adapter(core)
    sent = _run_handle(adapter, monkeypatch=monkeypatch)
    replies = [content for _to, content in sent[1:]]
    assert len(replies) == 2
    assert "".join(replies) == long_reply
    # 每片均为 send_message 独立调用，目标用户正确
    assert all(to == FROM_USER for to, _ in sent)


def test_segment_failure_merges_remaining_and_resends_once(monkeypatch):
    """某段失败：合并剩余段为单条重发一次，随后停止。"""
    long_reply = "甲" * 420  # 2 片：300 + 120
    core = FakeCore(FakeResult(long_reply))
    adapter = _make_adapter(core)

    async def send_impl(sent, to_user_id, content):
        sent.append((to_user_id, content))
        # 第 1 次=ACK 成功，第 2 次=seg0 成功，第 3 次=seg1 失败，第 4 次=合并重发成功
        return len(sent) != 3

    sent = _run_handle(adapter, monkeypatch=monkeypatch, send_impl=send_impl)
    assert len(sent) == 4
    assert sent[2][1] == "甲" * 120            # 第 2 片失败
    assert sent[3][1] == "甲" * 120            # 合并剩余（segments[1:]）单条重发


def test_segment_failure_merge_fails_stops(monkeypatch):
    """合并重发也失败：警告后停止，不再发送任何内容。"""
    long_reply = "乙" * 420
    core = FakeCore(FakeResult(long_reply))
    adapter = _make_adapter(core)

    async def send_impl(sent, to_user_id, content):
        sent.append((to_user_id, content))
        return len(sent) <= 2  # ACK、seg0 成功；seg1 与合并重发均失败

    sent = _run_handle(adapter, monkeypatch=monkeypatch, send_impl=send_impl)
    assert len(sent) == 4
    assert sent[3][1] == "乙" * 120


# ---------------------------------------------------------------------------
# 3. session_id
# ---------------------------------------------------------------------------

def test_process_receives_session_id_with_cache_hit(monkeypatch):
    """process 收到 session_id；第二次消息命中内存缓存，不再查/建 DB 会话。"""
    core = FakeCore(FakeResult("ok"))
    adapter = _make_adapter(core)
    _run_handle(adapter, text="第一条", monkeypatch=monkeypatch)
    first = core.process_calls[-1]
    assert first["session_id"] == "wx_sess_user@im._1"
    assert core.get_session_calls == 1
    assert core.create_session_calls == 1
    # 第二条：缓存命中，get_session/create_session 均不再调用
    _run_handle(adapter, text="第二条", monkeypatch=monkeypatch)
    second = core.process_calls[-1]
    assert second["session_id"] == first["session_id"]
    assert core.get_session_calls == 1
    assert core.create_session_calls == 1


def test_session_db_error_falls_back_to_tmp_id(monkeypatch):
    """DB 异常/超时兜底 wechat_tmp_{from_user_id[:16]}，且不丢消息。"""

    class FlakyCore(FakeCore):
        async def get_session(self, user_openid):
            raise sqlite3.OperationalError("database is locked")

        async def create_session(self, user_openid=""):
            raise sqlite3.OperationalError("database is locked")

    core = FlakyCore(FakeResult("ok"))
    adapter = _make_adapter(core)
    sent = _run_handle(adapter, monkeypatch=monkeypatch)
    pcall = core.process_calls[-1]
    assert pcall["session_id"] == f"wechat_tmp_{FROM_USER[:16]}"
    # 消息仍正常回复
    assert any(content == "ok" for _to, content in sent)


def test_session_cache_skips_tmp_id_on_next_lookup(monkeypatch):
    """缓存的 wechat_tmp_ 兜底 ID 视为失效：下次消息重新查 DB 恢复真实 session。"""
    core = FakeCore(FakeResult("ok"))
    adapter = _make_adapter(core)
    # 预置 tmp 兜底缓存
    adapter._user_session_cache[FROM_USER] = f"wechat_tmp_{FROM_USER[:16]}"
    adapter._user_session_cache_ts[FROM_USER] = time.time()
    _run_handle(adapter, monkeypatch=monkeypatch)
    pcall = core.process_calls[-1]
    assert pcall["session_id"] == "wx_sess_user@im._1"


# ---------------------------------------------------------------------------
# 4. status_callback
# ---------------------------------------------------------------------------

def test_process_receives_callable_status_callback_and_records(monkeypatch):
    """process 收到可调用的 status_callback；调用后写入 _last_status_by_user。"""
    core = FakeCore(FakeResult("ok"))
    adapter = _make_adapter(core)

    async def fake_process(text, user_id="", source="", user_openid="",
                           session_id=None, status_callback=None, **kwargs):
        assert status_callback is not None
        assert asyncio.iscoroutinefunction(status_callback)
        await status_callback("正在查询工具…")
        return FakeResult("ok")

    monkeypatch.setattr(core, "process", fake_process)
    sent = _run_handle(adapter, monkeypatch=monkeypatch)
    assert adapter._last_status_by_user.get(FROM_USER) == "正在查询工具…"
    assert adapter._last_status_by_user_ts.get(FROM_USER) is not None
    assert sent[-1][1] == "ok"


def test_status_cache_prunes_expired_entries():
    """_remember_last_status 写入时清理过期状态条目（TTL 1 小时）。"""
    adapter = _make_adapter(FakeCore(FakeResult("ok")))
    adapter._last_status_by_user = {"old_user": "stale"}
    adapter._last_status_by_user_ts = {"old_user": time.time() - 7200}
    adapter._remember_last_status(FROM_USER, "new_status")
    assert "old_user" not in adapter._last_status_by_user
    assert adapter._last_status_by_user.get(FROM_USER) == "new_status"
