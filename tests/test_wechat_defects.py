"""微信 Bot 适配器缺陷回归测试（M1-M4、Major#1/#2 及部分 Minor）。

每个用例先复现 bug（RED），再验证修复（GREEN）。
"""
import asyncio
import json

import pytest

from ilink_client import SessionExpiredError
from wechat_bot_adapter import WeChatBotAdapter


# ---------------------------------------------------------------------------
# 公共 fake
# ---------------------------------------------------------------------------

def _make_adapter(**over):
    kwargs = dict(db=object(), router=object(), api=None, user_openid="u", core=None)
    kwargs.update(over)
    return WeChatBotAdapter(**kwargs)


class _FakeILinkClient:
    """可注入指定异常/返回的 ILinkClient 替身。"""

    def __init__(self, *, send_media_exc=None, get_updates_seq=None, bot_token=""):
        self._send_media_exc = send_media_exc
        self._get_updates_seq = list(get_updates_seq or [])
        self.closed = False
        # 对齐真实 ILinkClient 的私有属性名（_client_owns_credentials 读取它）
        self._bot_token = bot_token

    async def send_media_message(self, to_user_id, context_token, content, image_path):
        if self._send_media_exc is not None:
            raise self._send_media_exc
        return {"ret": 0}

    async def get_updates(self, cursor):
        if not self._get_updates_seq:
            raise SessionExpiredError("expired")
        item = self._get_updates_seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# M1: send_media_message 遇 SessionExpiredError 不得清凭证
# ---------------------------------------------------------------------------

def test_send_media_session_expired_does_not_clear_credentials(monkeypatch):
    """M1: send_media_message 遇会话过期时只标记未连接，不清凭证、不置 _expired。"""
    bot = _make_adapter()
    bot._ilink_client = _FakeILinkClient(send_media_exc=SessionExpiredError("expired"))
    bot._last_from_user_id = "user_a"
    bot._connected = True

    cleared = {"called": False}

    def _fake_clear():
        cleared["called"] = True

    monkeypatch.setattr(bot, "_clear_credentials", _fake_clear)

    ok = asyncio.run(
        bot.send_media_message("", "/tmp/x.png", to_user_id="user_a", context_token="tok")
    )
    assert ok is False
    assert cleared["called"] is False, "send_media 不应清除凭证（应交给 poll W5 恢复）"
    assert bot._expired is False, "send_media 不应置 _expired"
    assert bot._connected is False


# ---------------------------------------------------------------------------
# M3: start lock 必须绑定当前运行 loop，跨 asyncio.run 不得抛 cross-loop 错误
# ---------------------------------------------------------------------------

def test_start_lock_is_loop_bound_across_runs():
    """M3: 在两个不同 asyncio.run() 中获取 start lock 不应抛跨 loop 错误。"""
    import wechat_bot_adapter

    async def _acquire():
        lock = wechat_bot_adapter._get_start_lock()
        async with lock:
            pass

    asyncio.run(_acquire())
    # 第二次独立事件循环——若 lock 绑定到首个 loop，此处会抛
    # "bound to a different event loop"。
    asyncio.run(_acquire())


# ---------------------------------------------------------------------------
# M4: 确认过期后 poll 循环收敛到干净终态
# ---------------------------------------------------------------------------

def test_poll_converges_to_terminal_state_on_confirmed_expiry(monkeypatch):
    """M4: 连续过期超过 MAX 后，_running/_connected=False、_expired=True、
    poll_task/ilink_client 引用被释放、_closed 置位。"""
    bot = _make_adapter()
    client = _FakeILinkClient(get_updates_seq=[])  # get_updates 恒抛 SessionExpiredError
    bot._ilink_client = client
    bot._running = True
    bot._expire_retries = 0
    bot._MAX_EXPIRE_RETRIES = 2

    # 避免退避真实等待
    async def _no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    # 避免真的删除凭证文件
    monkeypatch.setattr(bot, "_clear_credentials", lambda: None)

    async def _drive():
        bot._poll_task = asyncio.ensure_future(bot._poll_messages())
        await bot._poll_task

    asyncio.run(_drive())

    assert bot._expired is True
    assert bot._connected is False
    assert bot._running is False, "确认过期后应停止运行"
    assert bot._closed is True, "确认过期后应收敛到 closed 终态"
    assert bot._ilink_client is None, "确认过期后应释放 client 引用"
    assert bot._poll_task is None, "确认过期后应释放 poll_task 引用"


# ---------------------------------------------------------------------------
# M2: /wechat/test 存在活跃实例时不得新建 client / 走 getupdates / 改游标
# ---------------------------------------------------------------------------

class _FakeState:
    def __init__(self):
        self.wechat_bot = None


class _FakeApp:
    def __init__(self):
        self.state = _FakeState()


class _FakeRequest:
    def __init__(self):
        self.app = _FakeApp()


class _ActiveBotStub:
    """模拟活跃、已连接的 adapter 实例。"""

    def __init__(self, connected=True, expired=False, running=True):
        self._connected = connected
        self._expired = expired
        self._running = running
        self._closed = False

    def is_closed(self):
        return self._closed


def test_wechat_test_uses_live_state_when_active_instance(monkeypatch):
    """M2: 有活跃实例时，/wechat/test 读实时状态，不新建 ILinkClient、
    不调用 getupdates、不改写游标文件。"""
    import wechat_bot_adapter
    import web.routers.wechat as wx

    # 若路由误建 client 则立刻炸出来（证明没走 getupdates 路径）
    def _boom(*a, **k):
        raise AssertionError("有活跃实例时不应创建 ILinkClient / 走 getupdates")

    monkeypatch.setattr(wx, "ILinkClient", _boom)
    # 若误动游标持久化也炸出来
    if hasattr(wechat_bot_adapter.ILinkClient, "_persist_verify_cursor"):
        monkeypatch.setattr(
            wechat_bot_adapter.ILinkClient,
            "_persist_verify_cursor",
            staticmethod(lambda c: (_ for _ in ()).throw(AssertionError("不应写游标"))),
        )

    stub = _ActiveBotStub(connected=True)
    monkeypatch.setattr(wechat_bot_adapter, "_ACTIVE_BOT", stub, raising=False)

    result = asyncio.run(wx.test_connection(_FakeRequest()))
    assert result.data["success"] is True


def test_wechat_test_reports_expired_when_active_instance_expired(monkeypatch):
    """M2: 活跃实例已过期时，/wechat/test 报告失败且不走 getupdates。"""
    import wechat_bot_adapter
    import web.routers.wechat as wx

    monkeypatch.setattr(
        wx, "ILinkClient",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应创建 client")),
    )
    stub = _ActiveBotStub(connected=False, expired=True)
    monkeypatch.setattr(wechat_bot_adapter, "_ACTIVE_BOT", stub, raising=False)

    result = asyncio.run(wx.test_connection(_FakeRequest()))
    assert result.data["success"] is False


# ---------------------------------------------------------------------------
# m1: _init_failed 在后续成功 init/start 时应被重置为 False
# m9: 成功 start 时应重置 _expire_retries=0
# ---------------------------------------------------------------------------

class _OkCore:
    def __init__(self):
        self._initialized = False
        self.init_calls = 0

    async def init(self):
        self.init_calls += 1
        self._initialized = True


def test_init_failed_reset_and_expire_retries_reset_on_success(monkeypatch):
    """m1+m9: AgentCore 成功初始化后 _init_failed 复位、_expire_retries 归零。"""
    core = _OkCore()
    bot = _make_adapter(core=core)
    bot._init_failed = True   # 模拟上一次失败残留
    bot._expire_retries = 2   # 模拟上一次过期重试残留

    # 无凭证：start 不会拉起 poller，验证到 init 段即可
    monkeypatch.setattr(bot, "_load_credentials", lambda: None)

    asyncio.run(bot.start())

    assert core.init_calls == 1
    assert bot._init_failed is False, "成功 init 后应复位 _init_failed"
    assert bot._expire_retries == 0, "成功 start 后应重置 _expire_retries"

    asyncio.run(bot.stop())


# ---------------------------------------------------------------------------
# m2: _ctx_by_user 需有 TTL/大小上限，避免无界增长
# ---------------------------------------------------------------------------

def test_ctx_by_user_has_ttl_and_size_cap():
    """m2: _ctx_by_user 超过上限后按 TTL 清理过期项，避免无界增长。"""
    bot = _make_adapter()
    import time as _t
    # 灌入大量陈旧上下文
    old = _t.time() - 10 * 3600
    for i in range(300):
        bot._ctx_by_user[f"u{i}"] = f"tok{i}"
        bot._ctx_by_user_ts[f"u{i}"] = old
    # 触发清理（记录一个新用户上下文）
    bot._remember_ctx("fresh_user", "fresh_tok")
    assert "fresh_user" in bot._ctx_by_user
    assert bot._ctx_by_user["fresh_user"] == "fresh_tok"
    # 陈旧项应被清理
    assert len(bot._ctx_by_user) < 300


# ---------------------------------------------------------------------------
# m4: 保存新凭证时应清理陈旧游标，避免重放旧积压
# ---------------------------------------------------------------------------

def test_save_credentials_clears_stale_cursor(tmp_path, monkeypatch):
    """m4: save_credentials 落盘新凭证时应清除旧游标文件。"""
    import wechat_bot_adapter as wba

    cred = tmp_path / "wechat_credentials.json"
    cursor = tmp_path / "wechat_cursor.json"
    cursor.write_text('{"cursor": "STALE"}', encoding="utf-8")
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", cred)

    wba.save_credentials("tok", "bid", "uid", "https://x")

    assert cred.exists()
    assert not cursor.exists(), "保存新凭证后陈旧游标应被清除"


# ---------------------------------------------------------------------------
# R3 Major#1: 换 token 后旧 poller 不得删新凭证 / 回写旧游标
# ---------------------------------------------------------------------------

def test_clear_credentials_skips_when_token_changed(tmp_path, monkeypatch):
    """R3-Major#1: 凭证文件 token(T2) 与 client token(T1) 不一致时，_clear_credentials 不得删除凭证。"""
    import wechat_bot_adapter as wba

    cred = tmp_path / "wechat_credentials.json"
    cred.write_text(
        json.dumps({"bot_token": "T2", "ilink_bot_id": "b", "ilink_user_id": "u"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", cred)

    bot = _make_adapter()
    bot._ilink_client = _FakeILinkClient(bot_token="T1")

    bot._clear_credentials()
    assert cred.exists(), "token 已被重新扫码更新时，旧 poller 不得删除新凭证"


def test_clear_credentials_deletes_when_token_matches(tmp_path, monkeypatch):
    """R3-Major#1: 凭证 token 与 client token 一致时（真正过期），_clear_credentials 删除凭证。"""
    import wechat_bot_adapter as wba

    cred = tmp_path / "wechat_credentials.json"
    cred.write_text(
        json.dumps({"bot_token": "T1", "ilink_bot_id": "b", "ilink_user_id": "u"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", cred)

    bot = _make_adapter()
    bot._ilink_client = _FakeILinkClient(bot_token="T1")

    bot._clear_credentials()
    assert not cred.exists(), "token 归属一致（真正过期）时应删除凭证"


def test_save_cursor_skips_when_token_changed(tmp_path, monkeypatch):
    """R3-Major#1: 凭证 token(T2) 与 client token(T1) 不一致时，旧 poller 不得把旧游标写回文件。"""
    import wechat_bot_adapter as wba

    cred = tmp_path / "wechat_credentials.json"
    cred.write_text(
        json.dumps({"bot_token": "T2", "ilink_bot_id": "b", "ilink_user_id": "u"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", cred)
    cursor_path = tmp_path / "wechat_cursor.json"
    assert not cursor_path.exists()

    bot = _make_adapter()
    bot._ilink_client = _FakeILinkClient(bot_token="T1")
    bot._cursor = "OLD-CURSOR-FROM-T1-SESSION"

    bot._save_cursor()
    assert not cursor_path.exists(), "token 已被更新时旧 poller 不得回写旧游标"


def test_save_cursor_writes_when_token_matches(tmp_path, monkeypatch):
    """R3-Major#1: 凭证 token 与 client token 一致时，正常写回游标。"""
    import wechat_bot_adapter as wba

    cred = tmp_path / "wechat_credentials.json"
    cred.write_text(
        json.dumps({"bot_token": "T1", "ilink_bot_id": "b", "ilink_user_id": "u"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", cred)
    cursor_path = tmp_path / "wechat_cursor.json"

    bot = _make_adapter()
    bot._ilink_client = _FakeILinkClient(bot_token="T1")
    bot._cursor = "CURSOR-T1"

    bot._save_cursor()
    assert cursor_path.exists(), "token 归属一致时应正常持久化游标"


# ---------------------------------------------------------------------------
# R3 Major#2: start() 无凭证/初始化失败时不得留下僵尸 _ACTIVE_BOT
# ---------------------------------------------------------------------------

def test_start_no_credentials_does_not_leave_zombie_active_bot(monkeypatch):
    """R3-Major#2: 无凭证时 start() 后 _ACTIVE_BOT 不应指向未运行的实例。"""
    import wechat_bot_adapter as wba

    bot = _make_adapter()
    monkeypatch.setattr(bot, "_load_credentials", lambda: None)
    # 避免 AgentCore 自建
    monkeypatch.setattr(wba.WeChatBotAdapter, "_core", None, raising=False)

    asyncio.run(bot.start())

    active = wba._ACTIVE_BOT
    assert active is not bot, "无凭证启动失败后不应残留僵尸 _ACTIVE_BOT"
    assert bot._running is False, "无凭证启动失败后不应保持 running"
    asyncio.run(bot.stop())
