"""T4：/wechat/start 僵尸 adapter 回归测试（后端可靠性小任务 B）。

原缺陷：
- adapter.start() 吞掉初始化错误并回滚到 disconnected，却正常返回——
  router 无从得知失败；
- router start 后无条件把 adapter 挂上 app.state 并返回 success=True——
  产生"看似已挂载、实际未运行"的僵尸实例（/wechat/stop 对其无效）。

修复契约：
1. WeChatBotAdapter.start() 返回结构化 readiness dict：
   {ok: bool, connected: bool, polling: bool, error: str}；
2. router /wechat/start 仅在 readiness.ok 时挂载 app.state.wechat_bot，
   失败时如实返回失败详情（Envelope ok=False + START_FAILED + 详情）；
3. server._ensure_wechat_bot_task 自动恢复路径复用同一 readiness 判定。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import wechat_bot_adapter as wba
from wechat_bot_adapter import WeChatBotAdapter

# ---------------------------------------------------------------------------
# 公共替身
# ---------------------------------------------------------------------------

# 测试不建真实 AgentCore：start() 在 core=None 时会自建 AgentCore 并走
# 真实初始化（依赖真实配置目录）。这里注入最小假核心（_initialized=True
# 使 start() 跳过 init 阶段），仅支撑 readiness/生命周期断言语义。
_CORE_STUB = SimpleNamespace(_initialized=True)


def _make_adapter(**over):
    kwargs = dict(db=object(), router=object(), api=None, user_openid="u",
                  core=_CORE_STUB)
    kwargs.update(over)
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        WeChatBotAdapter, "_load_cursor", return_value=""
    ):
        return WeChatBotAdapter(**kwargs)


class _FakeState:
    def __init__(self):
        self.wechat_bot = None
        self.core = None


class _FakeApp:
    def __init__(self):
        self.state = _FakeState()


class _FakeRequest:
    def __init__(self, app=None):
        self.app = app or _FakeApp()


# ---------------------------------------------------------------------------
# 1. start() 返回结构化 readiness
# ---------------------------------------------------------------------------

def test_start_returns_readiness_ok_with_credentials(monkeypatch, tmp_path):
    """有凭证且 client 初始化成功 → ok=True, connected=True, polling=True。"""
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")
    (tmp_path / "wechat_credentials.json").write_text(
        json.dumps({"bot_token": "T1"}), encoding="utf-8")

    bot = _make_adapter()

    async def _drive():
        try:
            readiness = await bot.start()
            assert isinstance(readiness, dict), "start 应返回结构化 readiness"
            assert readiness.get("ok") is True
            assert readiness.get("connected") is True
            assert readiness.get("polling") is True
        finally:
            await bot.stop()

    asyncio.run(_drive())


def test_start_returns_readiness_error_without_credentials(monkeypatch, tmp_path):
    """无凭证 → ok=False 且带 error 详情（不再静默回滚后正常返回）。"""
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")

    bot = _make_adapter()

    readiness = asyncio.run(bot.start())
    assert readiness["ok"] is False
    assert readiness["error"], "失败时必须给出 error 详情"
    assert bot.is_closed()


def test_start_returns_readiness_error_on_client_failure(monkeypatch, tmp_path):
    """ILinkClient 初始化失败 → ok=False 且带详情（不再吞错回滚）。"""
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")
    (tmp_path / "wechat_credentials.json").write_text(
        json.dumps({"bot_token": "T1"}), encoding="utf-8")

    bot = _make_adapter()

    class _BoomClient:
        def __init__(self, **kwargs):
            raise RuntimeError("tls handshake exploded")

    monkeypatch.setattr(wba, "ILinkClient", _BoomClient)

    readiness = asyncio.run(bot.start())
    assert readiness["ok"] is False
    assert "tls handshake exploded" in readiness["error"]
    assert bot._ACTIVE_BOT is not bot if hasattr(bot, "_ACTIVE_BOT") else True


# ---------------------------------------------------------------------------
# 2. router：失败不挂载 + 如实返回
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_router_start_failure_does_not_mount(monkeypatch, tmp_path):
    """/wechat/start：adapter 未就绪时不挂载 app.state.wechat_bot，返回失败详情。"""
    import web.routers.wechat as wx

    monkeypatch.setattr(wx, "load_credentials",
                        lambda: {"bot_token": "T1", "ilink_user_id": "u"})
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", tmp_path / "wechat_credentials.json")

    class _FailingAdapter:
        is_connected = False
        is_polling = False

        async def start(self):
            return {"ok": False, "connected": False, "polling": False,
                    "error": "ilink init failed"}

        async def stop(self):
            return None

    # router._build_adapter 经 wechat_bot_adapter.WeChatBotAdapter 构造（函数内 import）
    monkeypatch.setattr(wba, "WeChatBotAdapter",
                        lambda **kwargs: _FailingAdapter())

    request = _FakeRequest()
    request.app.state.core = SimpleNamespace(db=object(), router=object())
    envelope = await wx.start_bot(request)

    assert envelope.ok is False
    assert envelope.error.code == "START_FAILED"
    assert "ilink init failed" in str(envelope.error.message)
    assert request.app.state.wechat_bot is None, "未就绪的 adapter 不得挂载"


@pytest.mark.asyncio
async def test_router_start_success_mounts(monkeypatch, tmp_path):
    """/wechat/start：readiness.ok 时挂载并返回 success=True。"""
    import web.routers.wechat as wx

    monkeypatch.setattr(wx, "load_credentials",
                        lambda: {"bot_token": "T1", "ilink_user_id": "u"})

    class _ReadyAdapter:
        is_connected = True
        is_polling = True

        async def start(self):
            return {"ok": True, "connected": True, "polling": True, "error": ""}

        async def stop(self):
            return None

    adapter = _ReadyAdapter()
    monkeypatch.setattr(wba, "WeChatBotAdapter", lambda **kwargs: adapter)

    request = _FakeRequest()
    request.app.state.core = SimpleNamespace(db=object(), router=object())
    envelope = await wx.start_bot(request)

    assert envelope.ok is True
    assert envelope.data["success"] is True
    assert request.app.state.wechat_bot is adapter


# ---------------------------------------------------------------------------
# 3. server 自动恢复路径复用同一 readiness 判定
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_server_auto_recover_uses_readiness(monkeypatch, tmp_path):
    """_ensure_wechat_bot_task 用 readiness.ok 判定是否挂载（与 router 同一口径）。

    旧实现以 `adapter.is_connected and adapter.is_polling` 判定且 start() 吞错
    返回 None；修复后 start() 返回 readiness dict，server 端仅按 readiness['ok']
    挂载。未就绪时 app.state.wechat_bot 保持 None（不产生僵尸实例）。
    """
    from web import server as server_module

    real_path = wba.CREDENTIALS_PATH

    class _FakeExistingPath(real_path.__class__):
        def exists(self):
            return True

    fake_path = _FakeExistingPath(real_path)
    monkeypatch.setattr(wba, "CREDENTIALS_PATH", fake_path)

    started: list[str] = []

    class _NotReadyAdapter:
        def __init__(self, **kwargs):
            self.is_connected = False
            self.is_polling = False

        async def start(self):
            started.append("start")
            return {"ok": False, "connected": False, "polling": False,
                    "error": "no route to ilink"}

        async def stop(self):
            return None

    # server._ensure_wechat_bot_task 经 `from wechat_bot_adapter import
    # WeChatBotAdapter`（函数内 import）——patch 源头模块属性。
    monkeypatch.setattr(wba, "WeChatBotAdapter", _NotReadyAdapter)

    app = _FakeApp()
    app.state.core = SimpleNamespace(db=object(), router=object())

    await server_module._ensure_wechat_bot_task(app)

    assert started == ["start"]
    assert app.state.wechat_bot is None, "readiness 失败时不得挂载僵尸 adapter"


@pytest.mark.asyncio
async def test_server_auto_recover_mounts_when_ready(monkeypatch, tmp_path):
    """自动恢复：readiness.ok=True 时挂载 adapter。"""
    from web import server as server_module

    real_path = wba.CREDENTIALS_PATH

    class _FakeExistingPath(real_path.__class__):
        def exists(self):
            return True

    monkeypatch.setattr(wba, "CREDENTIALS_PATH", _FakeExistingPath(real_path))

    mounted: list = []

    class _ReadyAdapter:
        def __init__(self, **kwargs):
            self.is_connected = True
            self.is_polling = True

        async def start(self):
            return {"ok": True, "connected": True, "polling": True, "error": ""}

        async def stop(self):
            return None

    def _factory(**kwargs):
        a = _ReadyAdapter(**kwargs)
        mounted.append(a)
        return a

    monkeypatch.setattr(wba, "WeChatBotAdapter", _factory)

    app = _FakeApp()
    app.state.core = SimpleNamespace(db=object(), router=object())

    await server_module._ensure_wechat_bot_task(app)

    assert len(mounted) == 1
    assert app.state.wechat_bot is mounted[0]
